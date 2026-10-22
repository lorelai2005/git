"""离线内存实现（MemoryRepo）+ 示例数据。

用途有三个，都很实在：
1. `dyreview demo`：不连 193 数据库、不碰抖音密钥，就能把
   “事实包 -> 诊断 -> 报告渲染 -> 数字校验” 全链路跑一遍，给客户/领导演示；
2. 193 服务器部署后的**冒烟测试**：先 demo 通过，再接真库；
3. 单元测试的确定性数据源（固定 seed，结果可复现）。

注意：这里的数据是**生成出来的样例**，不是真实账号数据，报告里必须标明。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .models import Account, ReviewWindow, TokenRecord

SAMPLE_ACCOUNT = Account(
    id=1,
    open_id="sample_open_id_0001",
    account_name="环宇家居（示例）",
    client_key="default",
    douyin_id="huanyu_home",
    nickname="环宇家居｜整装定制",
    org_id="C0001",
    status="active",
    feishu_chat_id="oc_sample_chat",
)

SAMPLE_TITLES = [
    "装修最容易后悔的3个地方，第2个90%的人踩过",
    "10万装出30万效果，我把预算表放这了",
    "师傅不会告诉你的瓷砖铺贴细节",
    "小户型显大秘诀：这面墙千万别砌",
    "验收时一定要带这3样东西",
    "定制柜和成品柜到底怎么选",
    "水电改造的5个隐藏增项",
    "厨房动线这样改，做饭省一半时间",
    "装修前必看的报价单陷阱",
    "全屋定制避坑清单",
]


class MemoryRepo:
    """只实现离线链路需要的方法；没实现的会明确报错，不静默出错。"""

    def __init__(self, *, seed: int = 7, today: date | None = None,
                 account: Account | None = None, days: int = 60) -> None:
        self.account = account or SAMPLE_ACCOUNT
        self.rng = random.Random(seed)
        self.today = today or datetime.now(timezone.utc).date()
        self.tokens: dict[int, TokenRecord] = {}
        self.accounts: dict[int, Account] = {self.account.id: self.account}
        self.events: list[dict] = []
        self.reviews: dict[int, dict] = {}
        self._review_seq = 0
        self.kv: dict[str, tuple[Any, datetime | None]] = {}
        self.task_locks: dict[str, datetime] = {}
        self.llm_usage: list[dict] = []
        self.status_updates: list[tuple[int, str]] = []
        self.runs: list[dict] = []
        self.metric_upserts: list[dict] = []
        self.video_upserts: list[dict] = []
        self._run_seq = 0
        self._build_sample(days)

    # =================================================================
    # 样例数据
    # =================================================================
    def _build_sample(self, days: int) -> None:
        rng = self.rng
        self.videos: dict[str, dict] = {}
        self.video_series: dict[str, list[dict]] = {}
        self.account_series: list[dict] = []

        followers = 41230
        base_day = self.today - timedelta(days=days)
        video_count = 12
        for idx in range(video_count):
            item_id = f"750000000000000{idx:03d}"
            create = datetime.combine(base_day + timedelta(days=idx * 5 + 1),
                                      datetime.min.time(), tzinfo=timezone.utc) + \
                timedelta(hours=rng.choice([9, 12, 18, 20, 21]))
            base_play = int(rng.gauss(28000, 9000))
            base_play = max(base_play, 3000)
            duration = rng.choice([13, 18, 22, 28, 35, 47, 62])
            self.videos[item_id] = {
                "item_id": item_id,
                "title": SAMPLE_TITLES[idx % len(SAMPLE_TITLES)],
                "create_time": create,
                "duration_sec": duration,
                "topics": ["装修避坑", "全屋定制"] if idx % 3 == 0 else ["装修"],
                "is_top": False,
                "cover_url": None,
                "share_url": None,
                "media_type": "video",
            }
            series = []
            cumulative_play = 0
            like_total = 0
            comment_total = 0
            share_total = 0
            collect_total = 0
            finish = min(max(rng.gauss(0.21, 0.06), 0.05), 0.45)
            for offset in range(0, days + 1):
                stat_day = base_day + timedelta(days=offset)
                if stat_day < create.date():
                    continue
                age = (stat_day - create.date()).days
                decay = max(0.02, 0.75 ** age)
                daily = int(base_play * 0.55 * decay * rng.uniform(0.7, 1.3))
                cumulative_play += daily
                like_total += int(daily * rng.uniform(0.02, 0.06))
                comment_total += int(daily * rng.uniform(0.001, 0.006))
                share_total += int(daily * rng.uniform(0.0005, 0.004))
                collect_total += int(daily * rng.uniform(0.004, 0.012))
                series.append({
                    "stat_date": stat_day,
                    "play_count": cumulative_play,
                    "like_count": like_total,
                    "comment_count": comment_total,
                    "share_count": share_total,
                    "collect_count": collect_total,
                    "finish_rate": round(finish * rng.uniform(0.92, 1.08), 4),
                    "bounce_rate": round(min(max(rng.gauss(0.42, 0.08), 0.1), 0.9), 4),
                    "full_play_rate": round(finish * rng.uniform(0.6, 0.9), 4),
                    "cover_click_rate": round(rng.gauss(0.085, 0.02), 4),
                })
            self.video_series[item_id] = series

        new_fans_pool = [120, 86, 45, 210, 32, 0, 8, 65, 15, 40, 22, 5]
        for offset in range(days + 1):
            stat_day = base_day + timedelta(days=offset)
            new_fans = max(0, int(rng.gauss(55, 40))) + new_fans_pool[offset % len(new_fans_pool)]
            unfollow = max(0, int(rng.gauss(8, 6)))
            followers += new_fans - unfollow
            self.account_series.append({
                "stat_date": stat_day,
                "follower_count": followers,
                "new_follower_count": new_fans,
                "unfollower_count": unfollow,
                "total_favorited": 180000 + offset * 900,
                "video_count": video_count,
                "profile_visit_count": int(rng.gauss(1200, 300)),
            })

        self.fan_profile = [
            {"dimension": "gender", "stat_date": self.today, "payload": [
                {"bucket": "女", "pct": 0.63}, {"bucket": "男", "pct": 0.37}]},
            {"dimension": "age", "stat_date": self.today, "payload": [
                {"bucket": "25-34", "pct": 0.46}, {"bucket": "35-44", "pct": 0.29},
                {"bucket": "18-24", "pct": 0.15}]},
            {"dimension": "region", "stat_date": self.today, "payload": [
                {"bucket": "广州", "pct": 0.18}, {"bucket": "深圳", "pct": 0.14},
                {"bucket": "佛山", "pct": 0.09}]},
        ]

    # =================================================================
    # 账号
    # =================================================================
    def list_accounts(self, status: str | None = "active") -> list[Account]:
        rows = list(self.accounts.values())
        return [a for a in rows if status is None or a.status == status]

    def get_account(self, account_id: int) -> Account | None:
        return self.accounts.get(int(account_id))

    def get_account_by_open_id(self, open_id: str, client_key: str = "default") -> Account | None:
        for acc in self.accounts.values():
            if acc.open_id == open_id:
                return acc
        return None

    def search_accounts(self, term: str, limit: int = 10) -> list[Account]:
        term_l = (term or "").lower()
        hits = [a for a in self.accounts.values()
                if term_l in a.open_id.lower()
                or term_l in a.account_name.lower()
                or term_l in (a.douyin_id or "").lower()
                or term_l in (a.nickname or "").lower()]
        return hits[:limit]

    def upsert_account(self, **kwargs: Any) -> Account:
        acc = Account(id=len(self.accounts) + 1, open_id=kwargs.get("open_id", ""),
                      account_name=kwargs.get("account_name", "未命名"),
                      douyin_id=kwargs.get("douyin_id"))
        self.accounts[acc.id] = acc
        return acc

    def update_account_profile(self, account_id: int, **kwargs: Any) -> None:
        return None

    def set_account_status(self, account_id: int, status: str,
                           remark: str | None = None) -> None:
        self.status_updates.append((account_id, status))
        acc = self.accounts.get(int(account_id))
        if acc:
            acc.status = status

    def touch_last_collect(self, account_id: int, when: datetime | None = None) -> None:
        acc = self.accounts.get(int(account_id))
        if acc:
            acc.last_collect_at = when or datetime.now(timezone.utc)

    # =================================================================
    # Token
    # =================================================================
    def get_token(self, account_id: int) -> TokenRecord | None:
        return self.tokens.get(int(account_id))

    def insert_token(self, rec: TokenRecord) -> None:
        self.tokens[rec.account_id] = rec

    def save_refreshed_token(self, rec: TokenRecord, expected_version: int) -> int | None:
        current = self.tokens.get(rec.account_id)
        if current is None:
            return None
        if current.version != expected_version:
            return None
        rec.version = expected_version + 1
        rec.refresh_count = current.refresh_count + 1
        self.tokens[rec.account_id] = rec
        return rec.version

    def set_token_error(self, account_id: int, error: str) -> None:
        rec = self.tokens.get(int(account_id))
        if rec:
            rec.last_refresh_error = error

    def log_oauth_event(self, account_id: int, event: str, **kwargs: Any) -> None:
        self.events.append({"account_id": account_id, "event": event, **kwargs})

    def token_health(self) -> list[dict]:
        return [{"account_id": a.id, "account_name": a.account_name} for a in self.accounts.values()]

    # =================================================================
    # 指标
    # =================================================================
    def account_metric_range(self, account_id: int, start: date, end: date) -> list[dict]:
        return [dict(r) for r in self.account_series if start <= r["stat_date"] <= end]

    def latest_account_metric(self, account_id: int) -> dict | None:
        return dict(self.account_series[-1]) if self.account_series else None

    def latest_video_metrics(self, account_id: int) -> list[dict]:
        rows = []
        for item_id, meta in self.videos.items():
            series = self.video_series.get(item_id) or []
            if not series:
                continue
            last = max(series, key=lambda s: s["stat_date"])
            rows.append({
                **meta,
                "account_id": account_id,
                **{k: last.get(k) for k in
                   ("stat_date", "play_count", "like_count", "comment_count", "share_count",
                    "collect_count", "finish_rate", "bounce_rate", "avg_play_duration_ms",
                    "cover_click_rate", "full_play_rate")},
                "interaction_rate": round(
                    ((last.get("like_count") or 0) + (last.get("comment_count") or 0)
                     + (last.get("share_count") or 0) + (last.get("collect_count") or 0))
                    / (last.get("play_count") or 1), 5),
            })
        return rows

    def videos_published_between(self, account_id: int, start: date, end: date) -> list[dict]:
        return [row for row in self.latest_video_metrics(account_id)
                if row.get("create_time") and start <= row["create_time"].date() <= end]

    def video_window_growth(self, account_id: int, start: date, end: date) -> list[dict]:
        out = []
        for item_id, series in self.video_series.items():
            in_win = [s for s in series if start <= s["stat_date"] <= end]
            if not in_win:
                continue
            last = max(in_win, key=lambda s: s["stat_date"])
            before = [s for s in series if s["stat_date"] < start]
            base = max(before, key=lambda s: s["stat_date"]) if before else None
            row = {"item_id": item_id, "last_date": last["stat_date"],
                   "play_end": last.get("play_count"), "like_end": last.get("like_count"),
                   "comment_end": last.get("comment_count"), "share_end": last.get("share_count"),
                   "collect_end": last.get("collect_count"),
                   "finish_rate": last.get("finish_rate"),
                   "bounce_rate": last.get("bounce_rate"),
                   "cover_click_rate": last.get("cover_click_rate"),
                   "full_play_rate": last.get("full_play_rate")}
            for key, end_key in (("play", "play_end"), ("like", "like_end"),
                                 ("comment", "comment_end"), ("share", "share_end"),
                                 ("collect", "collect_end")):
                src = {"play": "play_count", "like": "like_count", "comment": "comment_count",
                       "share": "share_count", "collect": "collect_count"}[key]
                row[f"{key}_before"] = base.get(src) if base else None
            out.append(row)
        return out

    def video_metric_history(self, account_id: int, item_id: str) -> list[dict]:
        return self.video_series.get(item_id, [])

    def count_videos(self, account_id: int) -> int:
        return len(self.videos)

    def upsert_video(self, account_id: int, meta: Any) -> None:
        self.video_upserts.append({"account_id": account_id, "item_id": getattr(meta, "item_id", None)})

    def upsert_video_metric(self, account_id: int, item_id: str, stat_date: date,
                            stats: dict, raw: dict | None = None) -> None:
        self.metric_upserts.append({"account_id": account_id, "item_id": item_id,
                                    "stat_date": stat_date, "stats": dict(stats)})

    def upsert_account_metric(self, account_id: int, stat_date: date, stats: dict,
                              raw: dict | None = None) -> None:
        self.metric_upserts.append({"account_id": account_id, "item_id": "__account__",
                                    "stat_date": stat_date, "stats": dict(stats)})

    def upsert_fan_profile(self, account_id: int, stat_date: date, dimension: str,
                           payload: list[dict]) -> None:
        self.fan_profile = [p for p in self.fan_profile if p["dimension"] != dimension]
        self.fan_profile.append({"dimension": dimension, "stat_date": stat_date,
                                 "payload": payload})

    # =================================================================
    # 采集批次
    # =================================================================
    def start_run(self, account_id: int | None, kind: str) -> int:
        self._run_seq += 1
        self.runs.append({"id": self._run_seq, "account_id": account_id, "kind": kind,
                          "status": "running"})
        return self._run_seq

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None:
        for run in self.runs:
            if run["id"] == run_id:
                run.update(status=status, **fields)

    def latest_fan_profile(self, account_id: int) -> list[dict]:
        return list(self.fan_profile)

    def data_freshness(self, account_id: int) -> dict:
        latest = max((r["stat_date"] for r in self.account_series), default=None)
        return {
            "account_id": account_id,
            "account_name": self.account.account_name,
            "last_collect_at": datetime.now(timezone.utc),
            "last_success_run_at": datetime.now(timezone.utc),
            "latest_account_stat_date": latest,
            "latest_video_stat_date": latest,
            "video_total": len(self.videos),
        }

    # =================================================================
    # 复盘工单
    # =================================================================
    def create_review(self, account_id: int, period_start: date, period_end: date, *,
                      source: str = "feishu", request_text: str | None = None) -> int:
        self._review_seq += 1
        self.reviews[self._review_seq] = {
            "id": self._review_seq, "account_id": account_id,
            "period_start": period_start, "period_end": period_end,
            "status": "pending", "source": source, "request_text": request_text,
            "facts": None, "report_md": None, "created_at": datetime.now(timezone.utc),
            "validation": None, "llm_model": None, "input_tokens": 0, "output_tokens": 0,
        }
        return self._review_seq

    def get_review(self, review_id: int) -> dict | None:
        return self.reviews.get(int(review_id))

    def update_review(self, review_id: int, **fields: Any) -> None:
        row = self.reviews.get(int(review_id))
        if row is not None:
            row.update(fields)

    def recent_review(self, account_id: int, minutes: int,
                      statuses: tuple[str, ...] = ("rendered", "pushed")) -> dict | None:
        if minutes <= 0:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        for review in reversed(list(self.reviews.values())):
            if review["account_id"] == account_id and review["status"] in statuses \
                    and review["created_at"] >= cutoff:
                return review
        return None

    def list_reviews(self, account_id: int | None = None, limit: int = 20) -> list[dict]:
        rows = list(self.reviews.values())
        if account_id:
            rows = [r for r in rows if r["account_id"] == account_id]
        return rows[-limit:]

    def log_llm_usage(self, **kwargs: Any) -> None:
        self.llm_usage.append(kwargs)

    # =================================================================
    # KV / 锁 / 运维
    # =================================================================
    def kv_get(self, key: str) -> Any:
        item = self.kv.get(key)
        if not item:
            return None
        value, expires = item
        if expires and expires < datetime.now(timezone.utc):
            return None
        return value

    def kv_set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)) \
            if ttl_seconds else None
        self.kv[key] = (value, expires)

    def acquire_task_lock(self, lock_key: str, owner: str, ttl_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        expires = self.task_locks.get(lock_key)
        if expires and expires > now:
            return False
        self.task_locks[lock_key] = now + timedelta(seconds=ttl_seconds)
        return True

    def release_task_lock(self, lock_key: str) -> None:
        self.task_locks.pop(lock_key, None)

    def prune(self, keep_days: int) -> dict:
        return {"dry_run": False, "note": "MemoryRepo 不清理"}


def sample_window(today: date | None = None, days: int = 7,
                  repo: MemoryRepo | None = None) -> ReviewWindow:
    end = (repo.today if repo else (today or datetime.now(timezone.utc).date()))
    return ReviewWindow(end - timedelta(days=days - 1), end)
