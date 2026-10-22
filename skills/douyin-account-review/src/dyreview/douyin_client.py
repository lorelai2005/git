"""抖音开放平台 API 客户端。

铁律
----
1. **不猜返回结构**：抖音的 envelope 有两种（`extra.error_code` 与 `data.error_code`），
   两种都处理；字段缺失一律容错，不抛 KeyError。
2. **错误码分类**：token 失效 / refresh 失效 / 限流 / 参数错误分开抛，
   上层才知道该刷新、该重新授权、还是该退避重试。
3. **只剩标准库**：用 urllib 而不是 requests/httpx，193 服务器少一个依赖就少一个坑。

接口路径与 HTTP 方法全部走配置（`douyin.endpoints` / `douyin.methods`），
官方改版只改配置，不动代码。
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

from .config import Config
from .errors import DouyinApiError, RateLimited, TokenInvalid

log = logging.getLogger(__name__)

DEFAULT_METHODS = {
    "oauth_access_token": "GET",
    "oauth_refresh_token": "GET",
    "oauth_renew_refresh_token": "GET",
    "oauth_client_token": "GET",
    "userinfo": "POST",
    "user_data": "POST",
    "fan_data": "POST",
    "video_list": "POST",
    "video_data": "POST",
}

# 服务端没下发 expires_in 时的兜底寿命（保守值：宁可多刷一次，也不要拿过期 token 去撞接口）
FALLBACK_ACCESS_TTL = 7 * 24 * 3600


class TransportError(Exception):
    """网络层失败（DNS/连接/超时），与业务错误码区分。"""


class Transport(Protocol):
    def request(self, method: str, url: str, *, params: dict | None = None,
                json_body: dict | None = None,
                headers: dict | None = None) -> tuple[int, Any]: ...


class RateLimiter:
    """极简令牌桶：保证全局限速不超 qps。"""

    def __init__(self, qps: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = 1.0 / qps if qps and qps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = self._clock()
        if now < self._next_at:
            self._sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + self.min_interval


class UrllibTransport:
    """标准库实现；只做“一次请求”，重试交给 DouyinClient。"""

    def __init__(self, timeout: float = 15.0, user_agent: str = "dyreview/1.0") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def request(self, method: str, url: str, *, params: dict | None = None,
                json_body: dict | None = None,
                headers: dict | None = None) -> tuple[int, Any]:
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(clean, doseq=True)}"
        data = None
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            status = exc.code
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            payload = {"_raw": text[:2000]}
        return status, payload


class DouyinClient:
    def __init__(self, cfg: Config, transport: Transport | None = None, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.base_url = str(cfg.get("douyin.base_url", "https://open.douyin.com")).rstrip("/")
        self.client_key = cfg.get("douyin.client_key", "") or ""
        self.client_secret = cfg.get("douyin.client_secret", "") or ""
        self.retries = cfg.get_int("douyin.retries", 3)
        self.backoff = cfg.get_float("douyin.retry_backoff_seconds", 1.5)
        self.timeout = cfg.get_float("douyin.timeout_seconds", 15.0)
        self.transport: Transport = transport or UrllibTransport(timeout=self.timeout)
        self.limiter = RateLimiter(cfg.get_float("douyin.qps", 4))
        self._sleep = sleep
        self.call_count = 0
        self._endpoints = cfg.section("douyin.endpoints")
        self._methods = {**DEFAULT_METHODS, **cfg.section("douyin.methods")}
        self._codes = {k: {str(x) for x in (v or [])}
                       for k, v in cfg.section("douyin.error_codes").items()}

    # ------------------------------------------------------------------ 基础
    def _endpoint(self, name: str) -> str:
        path = self._endpoints.get(name)
        if not path:
            raise DouyinApiError(name, "CONFIG", f"未配置接口路径 douyin.endpoints.{name}")
        return str(path)

    def _classify(self, path: str, code: str, description: str,
                  logid: str | None, status: int) -> DouyinApiError:
        if code in self._codes.get("token_invalid", ()) or status == 401:
            return TokenInvalid(path, code, description or "access_token 失效", logid, retryable=False)
        if code in self._codes.get("rate_limited", ()) or status == 429:
            return RateLimited(path, code, description or "触发限流", logid, retryable=True)
        if code in self._codes.get("refresh_invalid", ()):
            err = DouyinApiError(path, code, description or "refresh_token 失效", logid, retryable=False)
            setattr(err, "refresh_invalid", True)
            return err
        if code in self._codes.get("scope_missing", ()):
            err = DouyinApiError(path, code, description or "缺少接口权限", logid, retryable=False)
            setattr(err, "scope_missing", True)
            return err
        return DouyinApiError(path, code, description or "接口返回错误", logid,
                              retryable=status >= 500)

    @staticmethod
    def _extract(payload: Any) -> tuple[dict, str, str, str | None]:
        """返回 (data, error_code, description, logid)，兼容两种 envelope。"""
        if not isinstance(payload, dict):
            return {}, "BAD_PAYLOAD", "接口返回非 JSON 对象", None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        logid = extra.get("logid") or payload.get("logid") or data.get("logid")

        candidates = []
        if extra:
            candidates.append(extra)
        if data:
            candidates.append(data)
        for src in candidates:
            code = src.get("error_code")
            if code not in (None, 0, "0"):
                desc = src.get("description") or src.get("sub_description") or ""
                return data, str(code), str(desc), logid
        # 全部为 0 / 缺省：当成功处理
        return data, "0", "", logid

    def call(self, endpoint: str, *, params: dict | None = None,
             json_body: dict | None = None) -> dict:
        path = self._endpoint(endpoint)
        method = str(self._methods.get(endpoint, "POST")).upper()
        url = f"{self.base_url}{path}"
        attempt = 0
        last_err: Exception | None = None
        while attempt <= self.retries:
            self.limiter.wait()
            self.call_count += 1
            try:
                status, payload = self.transport.request(
                    method, url, params=params, json_body=json_body)
            except TransportError as exc:
                last_err = exc
                if attempt >= self.retries:
                    raise DouyinApiError(path, "NETWORK", str(exc), retryable=True) from exc
                wait = self.backoff ** attempt
                log.warning("抖音接口 %s 网络异常（第 %d 次），%.1fs 后重试：%s",
                            path, attempt + 1, wait, exc)
                self._sleep(wait)
                attempt += 1
                continue

            data, code, desc, logid = self._extract(payload)
            if code == "0":
                return data

            err = self._classify(path, code, desc, logid, status)
            if isinstance(err, TokenInvalid):
                raise err
            if getattr(err, "refresh_invalid", False) or getattr(err, "scope_missing", False):
                raise err
            if err.retryable and attempt < self.retries:
                wait = self.backoff ** attempt
                log.warning("抖音接口 %s 返回 %s（%s），%.1fs 后重试", path, code, desc, wait)
                self._sleep(wait)
                attempt += 1
                last_err = err
                continue
            raise err
        raise DouyinApiError(path, "RETRY_EXHAUSTED", str(last_err or "重试耗尽"), retryable=False)

    # ------------------------------------------------------------------ OAuth
    def auth_url(self, redirect_uri: str, scope: str, state: str = "") -> str:
        q = urllib.parse.urlencode({
            "client_key": self.client_key,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
        })
        return f"{self.base_url}/platform/oauth/connect/?{q}"

    def exchange_auth_code(self, code: str) -> dict:
        """授权码换 token（首次接入客户账号时用一次）。"""
        return self.call("oauth_access_token", params={
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        })

    def refresh_access_token(self, refresh_token: str) -> dict:
        """用 refresh_token 刷新；抖音会轮换 refresh_token，返回值必须回写。"""
        return self.call("oauth_refresh_token", params={
            "client_key": self.client_key,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    def renew_refresh_token(self, refresh_token: str) -> dict:
        """续期 refresh_token（access_token 不变），用来避免客户被动掉授权。"""
        return self.call("oauth_renew_refresh_token", params={
            "client_key": self.client_key,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    def client_token(self) -> dict:
        return self.call("oauth_client_token", params={
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "grant_type": "client_credential",
        })

    # ------------------------------------------------------------------ 数据
    def userinfo(self, open_id: str, access_token: str) -> dict:
        return self.call("userinfo", params={"open_id": open_id, "access_token": access_token})

    def user_data(self, open_id: str, access_token: str) -> dict:
        return self.call("user_data", params={"open_id": open_id, "access_token": access_token})

    def fan_data(self, open_id: str, access_token: str) -> dict:
        return self.call("fan_data", params={"open_id": open_id, "access_token": access_token})

    def video_list(self, open_id: str, access_token: str, *, cursor: int = 0,
                   count: int = 20) -> dict:
        return self.call("video_list", params={
            "open_id": open_id, "access_token": access_token,
            "cursor": cursor, "count": count,
        })

    def video_data(self, open_id: str, access_token: str, item_ids: list[str]) -> dict:
        return self.call("video_data",
                         params={"open_id": open_id, "access_token": access_token},
                         json_body={"item_ids": item_ids})

    # ------------------------------------------------------------------ 便捷
    @staticmethod
    def list_items(payload: dict) -> list[dict]:
        """从 video_list / video_data 的返回里取出列表，兼容 list/items/data 等键名。"""
        if not isinstance(payload, dict):
            return []
        for key in ("list", "items", "video_list", "data_list"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return []

    @staticmethod
    def has_more(payload: dict) -> bool:
        for key in ("has_more", "hasMore"):
            if key in payload:
                return bool(payload[key])
        return False

    @staticmethod
    def next_cursor(payload: dict, fallback: int) -> int:
        for key in ("cursor", "next_cursor"):
            val = payload.get(key)
            if isinstance(val, (int, str)) and str(val).isdigit():
                return int(val)
        return fallback
