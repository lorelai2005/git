"""access_token 生命周期管理：缓存 -> 过期检测 -> refresh 自动刷新 -> 轮换回写。

为什么这个模块值得单独存在
--------------------------
抖音的 refresh_token 是**一次性轮换**的：每刷新一次，旧的立刻失效，返回新的。
所以并发刷新 = 直接把自己锁死（后到的那个拿着已作废的 refresh_token）。
这里的解法是三层保护：

1. **数据库层单飞**：`pg_try_advisory_lock('dy:token:<account_id>')`，跨进程互斥；
2. **拿到锁后二次读库**：别人刚刷完，version 变大，直接改用新 token，不重复刷；
3. **乐观锁回写**：`UPDATE ... WHERE version = ?`，冲突则重读，绝不覆盖别人刚写入的轮换值。

另外：预刷新窗口按服务端下发的 `expires_in` 计算，不硬编码“15 天”，
官方改期也不会踩坑。
"""
from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Protocol

from .config import Config
from .douyin_client import FALLBACK_ACCESS_TTL, DouyinClient
from .errors import DouyinApiError, ReauthRequired, TokenInvalid
from .logging_util import to_utc, utcnow
from .models import Account, TokenRecord
from .secrets_box import SecretsBox

log = logging.getLogger(__name__)


@dataclass
class AccessToken:
    token: str
    refreshed: bool
    expires_at: datetime
    version: int = 0

    def to_dict(self) -> dict:
        return {
            "expires_at": self.expires_at.isoformat(),
            "refreshed": self.refreshed,
            "version": self.version,
        }


class TokenRepo(Protocol):
    def get_token(self, account_id: int) -> TokenRecord | None: ...
    def insert_token(self, rec: TokenRecord) -> None: ...
    def save_refreshed_token(self, rec: TokenRecord, expected_version: int) -> int | None: ...
    def set_token_error(self, account_id: int, error: str) -> None: ...
    def set_account_status(self, account_id: int, status: str, remark: str | None = None) -> None: ...
    def log_oauth_event(self, account_id: int, event: str, **kwargs: Any) -> None: ...


class LockProvider(Protocol):
    def advisory_lock(self, key: str) -> ContextManager[bool]: ...


def _seconds(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


class TokenManager:
    def __init__(self, cfg: Config, repo: TokenRepo, client: DouyinClient,
                 box: SecretsBox, locks: LockProvider | None = None, *,
                 clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], None] = time.sleep,
                 wait_timeout: float = 15.0) -> None:
        self.cfg = cfg
        self.repo = repo
        self.client = client
        self.box = box
        self.locks = locks
        self._clock = clock
        self._sleep = sleep
        self.wait_timeout = wait_timeout
        self.ratio = cfg.get_float("douyin.token.refresh_before_ratio", 0.3)
        self.floor_seconds = cfg.get_int("douyin.token.refresh_before_seconds", 86400)
        self.renew_before_days = cfg.get_int("douyin.token.renew_before_days", 7)
        self.refresh_retries = cfg.get_int("douyin.token.refresh_retries", 2)

    # ==================================================================
    # 过期判定
    # ==================================================================
    def refresh_window(self, ttl_seconds: int | None) -> float:
        """预刷新窗口（秒）= max(寿命*比例, min(固定下限, 寿命*50%))。

        这样：15 天 token 提前 4.5 天刷；1 小时 token 提前 18 分钟刷，
        不会出现“刚拿到就判过期”的死循环。
        """
        ttl = int(ttl_seconds or FALLBACK_ACCESS_TTL)
        by_ratio = ttl * self.ratio
        by_floor = min(float(self.floor_seconds), ttl * 0.5)
        return max(by_ratio, by_floor)

    def seconds_left(self, rec: TokenRecord, now: datetime | None = None) -> float:
        now = now or self._clock()
        exp = to_utc(rec.access_expires_at)
        assert exp is not None
        return (exp - now).total_seconds()

    def is_expired(self, rec: TokenRecord, now: datetime | None = None) -> bool:
        return self.seconds_left(rec, now) <= 0

    def needs_refresh(self, rec: TokenRecord, now: datetime | None = None) -> bool:
        window = self.refresh_window(rec.access_ttl_seconds)
        return self.seconds_left(rec, now) <= window

    def refresh_token_expiring(self, rec: TokenRecord, now: datetime | None = None) -> bool:
        if not rec.refresh_expires_at:
            return False
        now = now or self._clock()
        exp = to_utc(rec.refresh_expires_at)
        assert exp is not None
        return (exp - now) <= timedelta(days=self.renew_before_days)

    # ==================================================================
    # 对外主入口
    # ==================================================================
    def get_access_token(self, account: Account, *, force_refresh: bool = False) -> AccessToken:
        rec = self.repo.get_token(account.id)
        if rec is None:
            self.repo.log_oauth_event(account.id, "cache_miss", ok=False,
                                      description="本地无 token 记录，需要客户重新授权")
            raise ReauthRequired(account.id, account.account_name, "本地没有该账号的授权记录")

        if not force_refresh and not self.is_expired(rec) and not self.needs_refresh(rec):
            log.debug("token 缓存命中：account=%s 剩余 %.1fh", account.account_name,
                      self.seconds_left(rec) / 3600)
            return AccessToken(self.box.open(rec.access_token), False,
                               to_utc(rec.access_expires_at), rec.version)  # type: ignore[arg-type]

        if force_refresh:
            log.info("账号 %s 强制刷新 access_token", account.account_name)
        else:
            log.info("账号 %s 的 access_token 进入预刷新窗口（剩余 %.1fh），开始自动刷新",
                     account.account_name, self.seconds_left(rec) / 3600)

        return self._refresh_under_lock(account, rec)

    def call_with_token(self, account: Account, fn: Callable[[str], Any]) -> Any:
        """调用业务接口：token 被判失效时刷新一次并重试一次（不再无限重试）。"""
        access = self.get_access_token(account)
        try:
            return fn(access.token)
        except TokenInvalid as exc:
            log.warning("账号 %s 调用接口时 access_token 失效（%s），刷新后重试一次",
                        account.account_name, exc.error_code)
            access = self.get_access_token(account, force_refresh=True)
            return fn(access.token)

    # ==================================================================
    # 刷新实现
    # ==================================================================
    def _lock_ctx(self, account_id: int) -> ContextManager[bool | None]:
        if self.locks is None:
            return nullcontext(None)
        return self.locks.advisory_lock(f"dy:token:{account_id}")

    def _refresh_under_lock(self, account: Account, rec: TokenRecord) -> AccessToken:
        with self._lock_ctx(account.id) as acquired:
            if acquired is False:
                # 另一个进程正在刷新：等它写完，直接复用结果，避免“双刷”把 refresh_token 撞废
                log.info("账号 %s 的刷新锁被占用，等待其它进程完成…", account.account_name)
                waited = self._wait_for_peer(account, rec)
                if waited is not None:
                    return waited
                if not self.is_expired(rec):
                    log.warning("等待刷新超时，但旧 token 仍未过期，先沿用（剩余 %.1f 分钟）",
                                self.seconds_left(rec) / 60)
                    return AccessToken(self.box.open(rec.access_token), False,
                                       to_utc(rec.access_expires_at), rec.version)  # type: ignore[arg-type]

            # 二次读库：拿锁期间可能已被刷过
            fresh = self.repo.get_token(account.id)
            if fresh is not None and fresh.version != rec.version and \
                    not self.is_expired(fresh) and not self.needs_refresh(fresh):
                log.info("账号 %s 的 token 已被其它进程刷新（version %s -> %s），直接复用",
                         account.account_name, rec.version, fresh.version)
                return AccessToken(self.box.open(fresh.access_token), True,
                                   to_utc(fresh.access_expires_at), fresh.version)  # type: ignore[arg-type]
            if fresh is not None:
                rec = fresh

            return self._do_refresh(account, rec)

    def _wait_for_peer(self, account: Account, rec: TokenRecord) -> AccessToken | None:
        deadline = self._clock().timestamp() + self.wait_timeout
        while self._clock().timestamp() < deadline:
            self._sleep(0.5)
            peer = self.repo.get_token(account.id)
            if peer is None or peer.version == rec.version:
                continue
            if not self.is_expired(peer):
                log.info("等待期间收到其它进程刷新结果（version %s）", peer.version)
                return AccessToken(self.box.open(peer.access_token), True,
                                   to_utc(peer.access_expires_at), peer.version)  # type: ignore[arg-type]
        return None

    def _do_refresh(self, account: Account, rec: TokenRecord) -> AccessToken:
        if not rec.has_refresh_token():
            self._mark_reauth(account, "refresh_token 缺失", None)
            raise ReauthRequired(account.id, account.account_name, "refresh_token 缺失")

        refresh_plain = self.box.open(rec.refresh_token or "")
        last_error: Exception | None = None
        for attempt in range(self.refresh_retries + 1):
            try:
                payload = self.client.refresh_access_token(refresh_plain)
            except DouyinApiError as exc:
                if getattr(exc, "refresh_invalid", False):
                    self._mark_reauth(account, f"refresh_token 已失效（{exc.error_code}）", exc)
                    raise ReauthRequired(account.id, account.account_name,
                                         f"refresh_token 已失效：{exc.description}") from exc
                last_error = exc
                if attempt < self.refresh_retries:
                    wait = 1.5 ** attempt
                    log.warning("账号 %s 刷新失败（%s），%.1fs 后重试",
                                account.account_name, exc.error_code, wait)
                    self._sleep(wait)
                    continue
                self.repo.set_token_error(account.id, str(exc))
                self.repo.log_oauth_event(account.id, "refresh_failed", ok=False,
                                          error_code=exc.error_code,
                                          description=exc.description, logid=exc.logid)
                raise

            new_rec = self._record_from_oauth(account.id, payload, rec)
            new_version = self.repo.save_refreshed_token(new_rec, rec.version)
            if new_version is None:
                # 乐观锁冲突：别人先写成功了，用库里的
                peer = self.repo.get_token(account.id)
                if peer is not None and not self.is_expired(peer):
                    log.info("账号 %s 刷新回写冲突，改用库中最新 token（version %s）",
                             account.account_name, peer.version)
                    self.repo.log_oauth_event(account.id, "refreshed", ok=True,
                                              description="回写冲突，复用并发刷新结果")
                    return AccessToken(self.box.open(peer.access_token), True,
                                       to_utc(peer.access_expires_at), peer.version)  # type: ignore[arg-type]
                self.repo.set_token_error(account.id, "刷新回写冲突且库中无可用 token")
                raise DouyinApiError("oauth_refresh_token", "WRITE_CONFLICT",
                                     "刷新结果回写冲突，且库中无可用 token")

            self.repo.log_oauth_event(
                account.id, "refreshed", ok=True,
                detail={"expires_in": new_rec.access_ttl_seconds,
                        "refresh_expires_at": (new_rec.refresh_expires_at.isoformat()
                                               if new_rec.refresh_expires_at else None),
                        "version": new_version})
            if account.status == "reauth_required":
                self.repo.set_account_status(account.id, "active", "refresh_token 刷新成功，恢复采集")
            log.info("账号 %s 刷新成功：新 access_token 有效期 %ss，已回写 version=%s",
                     account.account_name, new_rec.access_ttl_seconds, new_version)
            return AccessToken(self.box.open(new_rec.access_token), True,
                               new_rec.access_expires_at, new_version or 0)

        raise last_error or DouyinApiError("oauth_refresh_token", "UNKNOWN", "刷新失败")

    # ==================================================================
    # 续期与写入
    # ==================================================================
    def keepalive(self, account: Account) -> dict:
        """refresh_token 快到期时续期，避免客户被动掉授权。"""
        rec = self.repo.get_token(account.id)
        if rec is None or not rec.has_refresh_token():
            return {"account_id": account.id, "action": "skip", "reason": "no_token"}
        if not self.refresh_token_expiring(rec):
            hours = None
            if rec.refresh_expires_at:
                hours = round((to_utc(rec.refresh_expires_at) - self._clock()).total_seconds() / 3600, 1)  # type: ignore[operator]
            return {"account_id": account.id, "action": "skip", "reason": "not_due",
                    "refresh_hours_left": hours}
        try:
            payload = self.client.renew_refresh_token(self.box.open(rec.refresh_token or ""))
        except DouyinApiError as exc:
            if getattr(exc, "refresh_invalid", False):
                self._mark_reauth(account, f"续期时 refresh_token 已失效（{exc.error_code}）", exc)
                raise ReauthRequired(account.id, account.account_name,
                                     "refresh_token 已失效，需重新授权") from exc
            raise
        now = self._clock()
        refresh_ttl = _seconds(payload.get("refresh_expires_in"))
        new_rec = TokenRecord(
            account_id=account.id,
            access_token=rec.access_token,                      # access_token 不变
            access_expires_at=rec.access_expires_at,
            refresh_token=self.box.seal(str(payload.get("refresh_token") or rec.refresh_token)),
            refresh_expires_at=(now + timedelta(seconds=refresh_ttl)) if refresh_ttl
            else rec.refresh_expires_at,
            access_issued_at=rec.access_issued_at,
            access_ttl_seconds=rec.access_ttl_seconds,
            scope=str(payload.get("scope") or rec.scope or "") or None,
            version=rec.version,
        )
        # 复用 refresh 专用写库方法（version+1），保证与刷新互斥语义一致
        new_version = self.repo.save_refreshed_token(new_rec, rec.version)
        if new_version is None:
            log.warning("账号 %s 续期回写冲突，下一轮巡检再处理", account.account_name)
            return {"account_id": account.id, "action": "conflict"}
        self.repo.log_oauth_event(account.id, "renewed", ok=True,
                                  detail={"refresh_expires_at":
                                          new_rec.refresh_expires_at.isoformat()
                                          if new_rec.refresh_expires_at else None})
        log.info("账号 %s 的 refresh_token 已续期", account.account_name)
        return {"account_id": account.id, "action": "renewed",
                "refresh_expires_at": (new_rec.refresh_expires_at.isoformat()
                                       if new_rec.refresh_expires_at else None)}

    def store_initial_token(self, account: Account, payload: dict) -> TokenRecord:
        """首次授权：把授权码换来的 token 落库。"""
        rec = self._record_from_oauth(account.id, payload, None)
        self.repo.insert_token(rec)
        self.repo.log_oauth_event(account.id, "authorized", ok=True,
                                  detail={"scope": rec.scope,
                                          "expires_in": rec.access_ttl_seconds})
        log.info("账号 %s 首次授权已入库", account.account_name)
        return rec

    def _record_from_oauth(self, account_id: int, payload: dict,
                           previous: TokenRecord | None) -> TokenRecord:
        now = self._clock()
        access_ttl = _seconds(payload.get("expires_in"), FALLBACK_ACCESS_TTL) or FALLBACK_ACCESS_TTL
        refresh_ttl = _seconds(payload.get("refresh_expires_in"))
        refresh_token = payload.get("refresh_token") or (previous.refresh_token if previous else None)
        refresh_expires = previous.refresh_expires_at if previous else None
        if refresh_ttl:
            refresh_expires = now + timedelta(seconds=refresh_ttl)
        return TokenRecord(
            account_id=account_id,
            access_token=self.box.seal(str(payload.get("access_token") or "")),
            access_expires_at=now + timedelta(seconds=access_ttl),
            refresh_token=self.box.seal(str(refresh_token)) if refresh_token else None,
            refresh_expires_at=refresh_expires,
            access_issued_at=now,
            access_ttl_seconds=access_ttl,
            scope=str(payload.get("scope") or (previous.scope if previous else "") or "") or None,
            refreshed_at=now,
            refresh_count=(previous.refresh_count if previous else 0) + (1 if previous else 0),
            version=previous.version if previous else 0,
        )

    def _mark_reauth(self, account: Account, description: str, exc: Exception | None) -> None:
        self.repo.set_account_status(account.id, "reauth_required", description)
        self.repo.set_token_error(account.id, description)
        self.repo.log_oauth_event(
            account.id, "reauth_required", ok=False,
            error_code=getattr(exc, "error_code", None),
            description=description,
            logid=getattr(exc, "logid", None))
        log.error("账号 %s 需要重新授权：%s", account.account_name, description)

    # ==================================================================
    # 只读巡检
    # ==================================================================
    def status(self, account: Account) -> dict:
        rec = self.repo.get_token(account.id)
        now = self._clock()
        if rec is None:
            return {"account_id": account.id, "account_name": account.account_name,
                    "state": "no_token", "needs_reauth": True}
        left = self.seconds_left(rec, now)
        return {
            "account_id": account.id,
            "account_name": account.account_name,
            "state": ("expired" if left <= 0
                      else "expiring" if self.needs_refresh(rec, now) else "ready"),
            "access_hours_left": round(left / 3600, 2),
            "access_ttl_seconds": rec.access_ttl_seconds,
            "refresh_window_seconds": int(self.refresh_window(rec.access_ttl_seconds)),
            "refresh_hours_left": (round((to_utc(rec.refresh_expires_at) - now).total_seconds() / 3600, 1)
                                   if rec.refresh_expires_at else None),
            "refresh_count": rec.refresh_count,
            "version": rec.version,
            "needs_reauth": left <= 0 and not rec.has_refresh_token(),
            "last_refresh_error": rec.last_refresh_error,
        }
