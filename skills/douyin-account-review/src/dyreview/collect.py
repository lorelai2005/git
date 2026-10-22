"""定时采集编排：账号 -> token（必要时自动刷新）-> 作品列表 -> 作品指标 -> 落库。

设计取舍
--------
* **账号级隔离**：单个账号失败不影响整批；失败原因写进 dy.collect_run，可回查。
* **限次翻页/限次批量**：`max_video_pages`、`video_data_rounds` 是硬闸门，
  防止某个大号作品上万条时把定时任务拖死。
* **写库幂等**：同一天重跑只会覆盖当天快照，不会产生重复行。
* **token 失效自愈**：任意一次调用被判 token 失效，`call_with_token` 会刷新后重试一次。
"""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from .config import Config
from .douyin_client import DouyinClient
from .errors import DouyinApiError, DyReviewError, ReauthRequired, TokenInvalid
from .logging_util import local_today, to_utc, utcnow
from .metrics import (normalize_account_metric, normalize_fan_profile,
                      normalize_video_metric, parse_epoch, parse_video_item)
from .models import Account, CollectReport, VideoMeta
from .repo import Repo
from .token_manager import TokenManager

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, cfg: Config, repo: Repo, tokens: TokenManager, client: DouyinClient, *,
                 clock: Callable[[], datetime] = utcnow,
                 today: Callable[[], date] | None = None) -> None:
        self.cfg = cfg
        self.repo = repo
        self.tokens = tokens
        self.client = client
        self._clock = clock
        self._today = today or (lambda: local_today(cfg.timezone))
        self.lookback_days = cfg.get_int("collect.lookback_days", 30)
        self.list_count = cfg.get_int("douyin.batch.video_list_count", 20)
        self.data_batch = max(1, cfg.get_int("douyin.batch.video_data_batch", 20))
        self.max_pages = max(1, cfg.get_int("douyin.batch.max_video_pages", 25))
        self.data_rounds = max(1, cfg.get_int("douyin.batch.video_data_rounds", 3))
        self.fan_weekday = cfg.get_int("collect.fan_profile_weekday", 0)

    # ==================================================================
    # 对外入口
    # ==================================================================
    def run(self, accounts: Iterable[Account], kind: str = "incremental",
            *, dry_run: bool = False) -> list[CollectReport]:
        reports: list[CollectReport] = []
        for account in accounts:
            if account.status in ("paused", "revoked"):
                reports.append(CollectReport(account.id, account.account_name,
                                             status="skipped",
                                             error_msg=f"账号状态为 {account.status}，跳过采集"))
                continue
            reports.append(self.collect_account(account, kind=kind, dry_run=dry_run))
        return reports

    # ==================================================================
    # 单账号
    # ==================================================================
    def collect_account(self, account: Account, *, kind: str = "incremental",
                        dry_run: bool = False) -> CollectReport:
        rep = CollectReport(account.id, account.account_name)
        run_id: int | None = None
        calls_before = self.client.call_count
        stat_date = self._today()

        if dry_run:
            st = self.tokens.status(account)
            rep.status = "skipped"
            rep.error_msg = f"dry-run：token 状态 {st.get('state')}"
            rep.warnings.append("dry-run 不调用接口、不写库")
            return rep

        run_id = self.repo.start_run(account.id, kind)
        token_version_before = None
        try:
            # ---------- 1) token：缓存命中直接用，过期自动刷新 ----------
            rec = self.repo.get_token(account.id)
            token_version_before = rec.version if rec else None
            access = self.tokens.get_access_token(account)
            rep.token_refreshed = access.refreshed
            token = access.token

            # ---------- 2) 账号级指标 ----------
            account_data: dict = {}
            try:
                account_data = self.tokens.call_with_token(
                    account, lambda t: self.client.user_data(account.open_id, t))
            except (TokenInvalid, ReauthRequired):
                raise
            except DyReviewError as exc:
                rep.warnings.append(f"账号指标拉取失败：{exc.message}")

            if account_data:
                stats = normalize_account_metric(account_data)
                if any(v is not None for v in stats.values()):
                    self.repo.upsert_account_metric(account.id, stat_date, stats, account_data)
                    rep.account_metric_written = True
                    self._update_profile(account, account_data)
                else:
                    rep.warnings.append("账号指标字段全部缺失，已跳过写库（不写 0 冒充数据）")

            # ---------- 3) 作品列表 ----------
            metas, pages = self._fetch_video_list(account)
            rep.video_pages = pages
            rep.videos_seen = len(metas)
            for meta in metas:
                self.repo.upsert_video(account.id, meta)
            rep.videos_upserted = len(metas)

            # ---------- 4) 作品指标 ----------
            targets = self._pick_metric_targets(metas, kind=kind)
            rep.metrics_written = self._fetch_video_metrics(account, targets, stat_date)

            # ---------- 5) 粉丝画像（低频） ----------
            if kind in ("full", "fan_profile") or self._fan_due(account):
                self._fetch_fan_profile(account, stat_date, rep)

            self.repo.touch_last_collect(account.id)
            rep.status = "success" if not rep.warnings else "partial"

        except ReauthRequired as exc:
            rep.status = "failed"
            rep.error_code = "REAUTH_REQUIRED"
            rep.error_msg = exc.message
            log.error("账号 %s 采集失败：需要重新授权", account.account_name)
        except TokenInvalid as exc:
            rep.status = "failed"
            rep.error_code = exc.error_code
            rep.error_msg = exc.description
        except DouyinApiError as exc:
            rep.status = "partial" if rep.videos_upserted else "failed"
            rep.error_code = exc.error_code
            rep.error_msg = exc.message
            log.error("账号 %s 采集失败：%s", account.account_name, exc.message)
        except Exception as exc:  # 兜底：不允许一个账号把整批任务带崩
            rep.status = "partial" if rep.videos_upserted else "failed"
            rep.error_code = "UNEXPECTED"
            rep.error_msg = f"{type(exc).__name__}: {exc}"
            log.error("账号 %s 采集出现未预期异常：\n%s", account.account_name,
                      traceback.format_exc())
        finally:
            # 采集过程中（接口报 token 失效）触发的刷新也要计入，便于运维判断
            try:
                final = self.repo.get_token(account.id)
                if final is not None and token_version_before is not None \
                        and final.version > token_version_before:
                    rep.token_refreshed = True
            except Exception:  # pragma: no cover
                pass
            rep.api_calls = self.client.call_count - calls_before
            if run_id is not None:
                try:
                    self.repo.finish_run(
                        run_id, rep.status, api_calls=rep.api_calls,
                        video_pages=rep.video_pages, videos_seen=rep.videos_seen,
                        videos_upserted=rep.videos_upserted,
                        token_refreshed=rep.token_refreshed,
                        error_code=rep.error_code, error_msg=rep.error_msg,
                        detail={"warnings": rep.warnings,
                                "metrics_written": rep.metrics_written})
                except Exception:  # pragma: no cover
                    log.warning("采集批次状态回写失败 run_id=%s", run_id)
        return rep

    # ==================================================================
    # 内部步骤
    # ==================================================================
    def _fetch_video_list(self, account: Account) -> tuple[list[VideoMeta], int]:
        pages = 0
        cursor = 0
        metas: dict[str, VideoMeta] = {}
        exhausted = False
        for _ in range(self.max_pages):
            payload = self.tokens.call_with_token(
                account,
                lambda t, c=cursor: self.client.video_list(
                    account.open_id, t, cursor=c, count=self.list_count))
            items = DouyinClient.list_items(payload)
            pages += 1
            for item in items:
                meta = parse_video_item(item)
                if meta is not None:
                    metas[meta.item_id] = meta
            if not items or not DouyinClient.has_more(payload):
                exhausted = True
                break
            next_cursor = DouyinClient.next_cursor(payload, cursor + len(items))
            if next_cursor <= cursor:
                log.warning("账号 %s 作品列表游标未推进（%s），停止翻页", account.account_name, cursor)
                exhausted = True
                break
            cursor = next_cursor
        if not exhausted:
            log.warning("账号 %s 作品列表达到翻页上限 %d 页，按已获取数据处理",
                        account.account_name, self.max_pages)
        return list(metas.values()), pages

    def _pick_metric_targets(self, metas: list[VideoMeta], *, kind: str) -> list[VideoMeta]:
        """挑要拉指标的作品：窗口内 + 最新若干条（爆款可能很久以前发的）。"""
        cap = self.data_rounds * self.data_batch
        if kind == "full":
            if len(metas) > cap:
                log.info("全量模式下作品数 %d 超过单次上限 %d，按最新优先截断",
                         len(metas), cap)
                ordered = sorted(metas, key=lambda m: to_utc(m.create_time)
                                 or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                return ordered[:cap]
            return metas
        cutoff = to_utc(self._clock()) - timedelta(days=self.lookback_days)  # type: ignore[operator]
        in_window = [m for m in metas if m.create_time is None or to_utc(m.create_time) >= cutoff]
        newest = sorted(metas, key=lambda m: to_utc(m.create_time) or datetime.min.replace(tzinfo=cutoff.tzinfo),
                        reverse=True)[:self.data_batch]
        picked: dict[str, VideoMeta] = {m.item_id: m for m in in_window}
        for m in newest:
            picked.setdefault(m.item_id, m)
        targets = list(picked.values())
        if len(targets) > cap:
            targets = targets[:cap]
            log.info("作品指标拉取按上限截断到 %d 条（video_data_rounds × batch）", cap)
        return targets

    def _fetch_video_metrics(self, account: Account, targets: list[VideoMeta],
                             stat_date: date) -> int:
        written = 0
        rounds = 0
        for start in range(0, len(targets), self.data_batch):
            if rounds >= self.data_rounds:
                break
            chunk = targets[start:start + self.data_batch]
            item_ids = [m.item_id for m in chunk]
            payload = self.tokens.call_with_token(
                account, lambda t, ids=item_ids: self.client.video_data(account.open_id, t, ids))
            rounds += 1
            rows = DouyinClient.list_items(payload)
            got = set()
            for row in rows:
                item_id = str(row.get("item_id") or row.get("itemId") or row.get("aweme_id") or "")
                if not item_id:
                    continue
                stats = normalize_video_metric(row)
                if all(stats.get(k) is None for k in
                       ("play_count", "like_count", "comment_count", "share_count")):
                    continue
                self.repo.upsert_video_metric(account.id, item_id, stat_date, stats, row)
                got.add(item_id)
                written += 1
            missing = [i for i in item_ids if i not in got]
            if missing:
                log.info("账号 %s 有 %d 条作品本次未返回指标（可能数据未生成）",
                         account.account_name, len(missing))
        return written

    def _fetch_fan_profile(self, account: Account, stat_date: date, rep: CollectReport) -> None:
        try:
            payload = self.tokens.call_with_token(
                account, lambda t: self.client.fan_data(account.open_id, t))
        except DyReviewError as exc:
            rep.warnings.append(f"粉丝画像拉取失败：{exc.message}")
            return
        dims = normalize_fan_profile(payload)
        if not dims:
            rep.warnings.append("粉丝画像返回为空（可能未授权数据权限）")
            return
        for dim, buckets in dims.items():
            self.repo.upsert_fan_profile(account.id, stat_date, dim, buckets)
        log.info("账号 %s 粉丝画像已入库：%s", account.account_name, list(dims))

    def _fan_due(self, account: Account) -> bool:
        if self._today().weekday() != self.fan_weekday:
            return False
        profiles = self.repo.latest_fan_profile(account.id)
        if not profiles:
            return True
        latest = max((p["stat_date"] for p in profiles), default=None)
        return latest is None or (self._today() - latest).days >= 6

    def _update_profile(self, account: Account, account_data: dict) -> None:
        nickname = (account_data.get("nickname") or account_data.get("name")
                    or account_data.get("nick_name"))
        avatar = account_data.get("avatar") or account_data.get("avatar_url")
        if isinstance(avatar, dict):
            urls = avatar.get("url_list") or []
            avatar = urls[0] if urls else None
        douyin_id = account_data.get("douyin_id") or account_data.get("unique_id")
        if not any((nickname, avatar, douyin_id)):
            return
        try:
            self.repo.update_account_profile(account.id, douyin_id=douyin_id,
                                             nickname=nickname, avatar_url=avatar)
        except Exception:  # pragma: no cover
            log.debug("账号资料更新失败（忽略）", exc_info=True)

    # ==================================================================
    # 新鲜度
    # ==================================================================
    def is_stale(self, account: Account, max_age_hours: float) -> bool:
        """复盘前判断：数据是否足够新，不新就先补采。"""
        fresh = self.repo.data_freshness(account.id)
        last = fresh.get("last_success_run_at") or fresh.get("last_collect_at")
        if not last:
            return True
        last_dt = to_utc(last)
        assert last_dt is not None
        return (self._clock() - last_dt) > timedelta(hours=max_age_hours)
