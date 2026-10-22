"""pytest 公共设施：配置夹具 + 固定时钟 + 依赖注入的假实现。"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "src"))

from dyreview.config import Config, config_from_dict  # noqa: E402

BASE_CONFIG = yaml.safe_load((SKILL_ROOT / "config" / "config.example.yaml")
                             .read_text(encoding="utf-8"))


def _set_dotted(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


@pytest.fixture
def make_cfg():
    """make_cfg({"douyin.client_key": "abc"}) -> Config（基于 config.example.yaml）。"""
    def _make(overrides: dict | None = None) -> Config:
        data = copy.deepcopy(BASE_CONFIG)
        for key, value in (overrides or {}).items():
            _set_dotted(data, key, value)
        return config_from_dict(data)
    return _make


@pytest.fixture
def cfg(make_cfg):
    """默认带上假的飞书/大模型凭据（测试里用注入的 transport，不会真发请求）。"""
    return make_cfg({
        "feishu.app_id": "cli_test", "feishu.app_secret": "sec_test",
        "feishu.default_chat_id": "oc_test",
        "llm.mode": "builtin", "llm.base_url": "https://llm.invalid",
        "llm.api_key": "sk-test", "llm.model": "gpt-test",
    })


class FrozenClock:
    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime(2026, 1, 20, 3, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return FrozenClock()


class FakeTransport:
    """抖音客户端传输层替身：按 URL 关键字路由，并记录每次请求。"""

    def __init__(self, router) -> None:
        self.router = router
        self.calls: list[dict] = []

    def request(self, method, url, *, params=None, json_body=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "json_body": json_body})
        return self.router(method, url, params or {}, json_body)

    def count(self, keyword: str) -> int:
        return sum(1 for c in self.calls if keyword in c["url"])


def ok(data: dict, *, logid: str = "logid-test") -> tuple[int, dict]:
    """抖音成功返回（extra.error_code=0）。"""
    return 200, {"data": data, "extra": {"error_code": 0, "description": "", "logid": logid}}


def err(code: int | str, description: str = "boom") -> tuple[int, dict]:
    """抖音业务错误（HTTP 200 + extra.error_code）。"""
    return 200, {"data": {}, "extra": {"error_code": int(code), "description": description,
                                       "logid": "logid-err"}}


def oauth_ok(data: dict) -> tuple[int, dict]:
    """oauth 接口成功（错误码在 data 里，且为 0）。"""
    return 200, {"data": {**data, "error_code": 0, "description": ""}}


class NullLocks:
    """不做互斥的锁提供者（单进程测试用）。"""

    def advisory_lock(self, key):
        from contextlib import nullcontext
        return nullcontext(True)


class ClosedLocks:
    """模拟“锁被别人占着”。"""

    def advisory_lock(self, key):
        from contextlib import nullcontext
        return nullcontext(False)


class FakeFeishuTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._seq = 0

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body})
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        self._seq += 1
        return {"code": 0, "data": {"message_id": f"om_{self._seq}"}}


class SeqRepo:
    """按顺序返回不同的 token 记录，用于模拟并发场景。"""

    def __init__(self, base, sequence):
        self.base = base
        self.sequence = sequence
        self.calls = 0

    def get_token(self, account_id):
        idx = min(self.calls, len(self.sequence) - 1)
        self.calls += 1
        return self.sequence[idx]

    def __getattr__(self, item):
        return getattr(self.base, item)
