"""飞书推送：tenant_access_token 缓存、长文自动分片、卡片/文本/群机器人三种通道。

为什么不用飞书 SDK：193 服务器上少一个依赖、少一次版本冲突。
飞书的两个坑这里都处理了：
  * tenant_access_token 有效期 2 小时且**有频控**，必须缓存（内存 + dy.kv 双缓存）；
  * 单条消息内容有长度上限，长报告必须**按块切片**，且不能把代码块切两半。

推送失败不抛给用户，只记录并返回 status，避免“报告生成成功但推送失败”被误报成整体失败。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .config import Config
from .errors import ConfigError
from .logging_util import to_utc, utcnow

log = logging.getLogger(__name__)

TOKEN_KV_KEY = "feishu:tenant_access_token"


class KV(Protocol):
    def kv_get(self, key: str) -> Any: ...
    def kv_set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...


class FeishuError(Exception):
    pass


# =====================================================================
# 分片
# =====================================================================
def split_markdown(text: str, limit: int) -> list[str]:
    """按行切分，保持 ``` 代码块完整；不产生空片。"""
    if limit <= 0:
        return [text]
    if len(text) <= limit:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        is_fence = line.lstrip().startswith("```")
        if is_fence:
            in_fence = not in_fence
        # 围栏行本身不切：否则会把代码块的开/闭标记拆到两个卡片里
        if size + len(line) > limit and buf and not in_fence and not is_fence:
            chunks.append("".join(buf).rstrip())
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf).rstrip())
    return [c for c in chunks if c and c.strip()]


def _receive_id_type(receive_id: str) -> str:
    if receive_id.startswith("oc_"):
        return "chat_id"
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("on_"):
        return "union_id"
    return "chat_id"


def _card(title: str, markdown: str, *, template: str = "blue",
          footer: str | None = None) -> dict:
    elements: list[dict] = [{
        "tag": "div",
        "text": {"tag": "lark_md", "content": markdown},
    }]
    if footer:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": footer}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title[:100]},
        },
        "elements": elements,
    }


# =====================================================================
# 客户端
# =====================================================================
class FeishuClient:
    def __init__(self, cfg: Config, *, kv: KV | None = None,
                 transport=None, sleep=time.sleep) -> None:
        self.cfg = cfg
        self.base_url = str(cfg.get("feishu.base_url", "https://open.feishu.cn")).rstrip("/")
        self.app_id = cfg.get("feishu.app_id", "") or ""
        self.app_secret = cfg.get("feishu.app_secret", "") or ""
        self.webhook_url = cfg.get("feishu.webhook_url", "") or ""
        self.card_max_chars = cfg.get_int("feishu.card_max_chars", 3500)
        self.max_cards = cfg.get_int("feishu.chunk_max_cards", 4)
        self.push_mode = str(cfg.get("feishu.push_mode", "card") or "card")
        self.kv = kv
        self._transport = transport or self._default_transport
        self._sleep = sleep
        self._mem_token: dict | None = None

    # ---------------------------------------------------------------- HTTP
    def _default_transport(self, method: str, url: str, body: dict | None,
                           headers: dict | None) -> dict:
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") \
            if body is not None else None
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = (exc.read() or b"").decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FeishuError(f"飞书接口不可达：{exc}") from exc
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise FeishuError(f"飞书返回非 JSON：{raw[:300]}") from exc

    def _call(self, method: str, path: str, body: dict | None = None, *,
              token: str | None = None, retries: int = 2) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        attempt = 0
        while True:
            payload = self._transport(method, url, body, headers)
            code = payload.get("code")
            if code in (0, None) and payload.get("StatusCode") in (0, None):
                return payload
            # 99991400 = 飞书限流
            if code in (99991400, 99991401) and attempt < retries:
                wait = 1.0 + attempt
                log.warning("飞书接口限流，%.1fs 后重试", wait)
                self._sleep(wait)
                attempt += 1
                continue
            raise FeishuError(f"飞书接口 {path} 返回 code={code} msg={payload.get('msg')}")

    # ---------------------------------------------------------------- token
    def tenant_access_token(self, *, force: bool = False) -> str:
        if not force:
            cached = self._cached_token()
            if cached:
                return cached
        if not self.app_id or not self.app_secret:
            raise ConfigError("飞书推送需要 feishu.app_id / feishu.app_secret（或用 webhook_url 兜底）")
        payload = self._call("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                             {"app_id": self.app_id, "app_secret": self.app_secret})
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuError(f"未取得 tenant_access_token：{payload}")
        # 提前 5 分钟过期，避免边界失败
        ttl = max(int(payload.get("expire") or 7200) - 300, 60)
        expires_at = utcnow() + timedelta(seconds=ttl)
        self._mem_token = {"token": token, "expires_at": expires_at.isoformat()}
        if self.kv is not None:
            try:
                self.kv.kv_set(TOKEN_KV_KEY, self._mem_token, ttl_seconds=ttl)
            except Exception:  # pragma: no cover - 缓存失败不影响主流程
                log.debug("飞书 token 写入 KV 失败（忽略）", exc_info=True)
        return token

    def _cached_token(self) -> str | None:
        candidates: list[dict] = []
        if self._mem_token:
            candidates.append(self._mem_token)
        if self.kv is not None:
            try:
                cached = self.kv.kv_get(TOKEN_KV_KEY)
                if isinstance(cached, dict):
                    candidates.append(cached)
            except Exception:  # pragma: no cover
                pass
        now = utcnow()
        for item in candidates:
            exp = item.get("expires_at")
            token = item.get("token")
            if not token or not exp:
                continue
            try:
                exp_dt = to_utc(datetime.fromisoformat(str(exp)))
            except ValueError:
                continue
            if exp_dt and exp_dt > now:
                self._mem_token = item
                return token
        return None

    # ---------------------------------------------------------------- 发送
    def send_text(self, receive_id: str, text: str) -> list[str]:
        token = self.tenant_access_token()
        payload = self._call(
            "POST",
            f"/open-apis/im/v1/messages?receive_id_type={_receive_id_type(receive_id)}",
            {"receive_id": receive_id, "msg_type": "text",
             "content": json.dumps({"text": text}, ensure_ascii=False)},
            token=token)
        return [str((payload.get("data") or {}).get("message_id") or "")]

    def send_card(self, receive_id: str, title: str, markdown: str, *,
                  footer: str | None = None) -> list[str]:
        token = self.tenant_access_token()
        chunks = split_markdown(markdown, self.card_max_chars)
        message_ids: list[str] = []
        for idx, chunk in enumerate(chunks):
            if idx >= self.max_cards:
                log.warning("报告分片超过 %d 张卡片，剩余内容未推送（完整内容已落地文件）",
                            self.max_cards)
                break
            card_title = title if idx == 0 else f"{title}（续 {idx + 1}）"
            card = _card(card_title, chunk, footer=footer if idx == len(chunks) - 1 else None)
            payload = self._call(
                "POST",
                f"/open-apis/im/v1/messages?receive_id_type={_receive_id_type(receive_id)}",
                {"receive_id": receive_id, "msg_type": "interactive",
                 "content": json.dumps(card, ensure_ascii=False)},
                token=token)
            message_ids.append(str((payload.get("data") or {}).get("message_id") or ""))
        return message_ids

    def send_webhook(self, title: str, markdown: str) -> list[str]:
        if not self.webhook_url:
            raise ConfigError("未配置 feishu.webhook_url")
        ids: list[str] = []
        for chunk in split_markdown(markdown, self.card_max_chars)[: self.max_cards]:
            self._transport("POST", self.webhook_url,
                            {"msg_type": "interactive", "card": _card(title, chunk)}, {})
            ids.append("webhook")
        return ids

    def push_report(self, *, receive_id: str | None, title: str, markdown: str,
                    footer: str | None = None) -> dict:
        """统一入口：按 push_mode 选择通道，失败只记录不抛出。"""
        mode = self.push_mode
        target = receive_id or self.cfg.get("feishu.default_chat_id", "")
        try:
            if mode == "webhook" or (not target and self.webhook_url):
                ids = self.send_webhook(title, markdown)
                return {"ok": True, "channel": "webhook", "message_ids": ids}
            if not target:
                raise ConfigError("未指定接收方，且未配置 feishu.default_chat_id / webhook_url")
            if mode == "text":
                chunks = split_markdown(markdown, self.card_max_chars)
                ids = []
                for chunk in chunks[: self.max_cards]:
                    ids += self.send_text(target, chunk)
                return {"ok": True, "channel": "text", "message_ids": ids}
            ids = self.send_card(target, title, markdown, footer=footer)
            return {"ok": True, "channel": "card", "message_ids": ids}
        except (FeishuError, ConfigError) as exc:
            log.error("飞书推送失败：%s", exc)
            return {"ok": False, "channel": mode, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            log.exception("飞书推送出现未预期异常")
            return {"ok": False, "channel": mode, "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- 互动
    def reply_ambiguous(self, receive_id: str, term: str, candidates: list[dict]) -> list[str]:
        """账号匹配到多个时，给出可点选的追问（纯文本，兼容性最好）。"""
        lines = [f"「{term}」匹配到 {len(candidates)} 个账号，请回复序号或完整抖音号："]
        for i, cand in enumerate(candidates, 1):
            lines.append(f"{i}. {cand.get('account_name')}（抖音号 {cand.get('douyin_id') or '未登记'}）")
        return self.send_text(receive_id, "\n".join(lines))
