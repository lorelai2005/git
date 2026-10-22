"""日志与运行上下文小工具。

原则：日志一律走 stderr，stdout 只留给 CLI 的 JSON 结果，
这样 Crabot 侧可以安全地 `json.loads(stdout)`。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_configured = False


def setup_logging(level: str | None = None, *, force: bool = False) -> None:
    global _configured
    if _configured and not force:
        return
    lvl = (level or os.environ.get("DYREVIEW_LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, lvl, logging.INFO))
    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def seconds_until(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    now = now or utcnow()
    target = to_utc(dt)
    assert target is not None
    return (target - now).total_seconds()


def local_now(tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:  # pragma: no cover - 无 tzdata 的极简环境
        return datetime.now(timezone(timedelta(hours=8)))


def local_today(tz_name: str):
    return local_now(tz_name).date()


def json_dumps(obj: Any) -> str:
    """安全的 JSON 序列化（date/datetime/Decimal -> 字符串或数字）。"""
    from decimal import Decimal

    def default(o: Any):
        if isinstance(o, (datetime,)):
            return o.isoformat()
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (set, tuple)):
            return list(o)
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)

    return json.dumps(obj, ensure_ascii=False, default=default)


def print_json(obj: Any, *, pretty: bool = False) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None,
                      default=lambda o: float(o) if hasattr(o, "as_tuple")
                      else (o.isoformat() if hasattr(o, "isoformat") else str(o)))
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
