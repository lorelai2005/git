"""access_token 生命周期测试：缓存、过期检测、刷新、轮换回写、并发单飞、失效重授权。

这些用例对应需求里最关键的那一环：**自动缓存 token -> 检测过期 -> refresh 刷新 -> 回写**。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import ClosedLocks, FakeTransport, NullLocks, SeqRepo, err, oauth_ok
from dyreview.douyin_client import DouyinClient, TransportError
from dyreview.errors import DouyinApiError, ReauthRequired, TokenInvalid
from dyreview.models import Account, TokenRecord
from dyreview.offline import MemoryRepo
from dyreview.secrets_box import SecretsBox
from dyreview.token_manager import TokenManager

DAY = 86400


def build(cfg, clock, *, locks=None, router=None, repo=None):
    transport = FakeTransport(router or (lambda *a, **k: oauth_ok({
        "access_token": "AT_NEW", "expires_in": 15 * DAY,
        "refresh_token": "RT_NEW", "refresh_expires_in": 30 * DAY,
        "open_id": "open-1", "scope": "user_info,video.list"})))
    client = DouyinClient(cfg, transport=transport, sleep=lambda s: None)
    box = SecretsBox("")
    repo = repo or MemoryRepo()
    manager = TokenManager(cfg, repo, client, box, locks or NullLocks(),
                           clock=clock, sleep=lambda s: None, wait_timeout=0.01)
    return manager, repo, client, box, transport


def seed(repo, box, clock, *, remaining=10 * DAY, ttl=15 * DAY, refresh_days=30,
         account_id=1, access="AT_OLD", refresh="RT_OLD", version=0):
    rec = TokenRecord(
        account_id=account_id,
        access_token=box.seal(access),
        access_expires_at=clock() + timedelta(seconds=remaining),
        refresh_token=box.seal(refresh),
        refresh_expires_at=clock() + timedelta(days=refresh_days),
        access_issued_at=clock() - timedelta(seconds=ttl - remaining),
        access_ttl_seconds=ttl,
        scope="user_info",
        version=version,
    )
    repo.insert_token(rec)
    return rec


@pytest.fixture
def account():
    return Account(id=1, open_id="open-1", account_name="测试账号")


# =====================================================================
# 缓存与过期判定
# =====================================================================
def test_valid_token_is_cache_hit_without_any_api_call(cfg, clock, account):
    manager, repo, client, box, transport = build(cfg, clock)
    seed(repo, box, clock, remaining=10 * DAY)
    access = manager.get_access_token(account)
    assert access.token == "AT_OLD"
    assert access.refreshed is False
    assert transport.calls == []          # 没打接口，纯缓存命中


def test_prefill_window_uses_ratio_of_server_ttl(cfg, clock, account):
    manager, *_ = build(cfg, clock)
    # 15 天有效期，比例 0.3 -> 窗口 4.5 天（大于固定下限 1 天，取较大值）
    assert manager.refresh_window(15 * DAY) == pytest.approx(4.5 * DAY)
    # 1 小时有效期：比例窗口 18 分钟 vs 固定下限截到 30 分钟 -> 取 30 分钟，不会刚拿到就刷新
    assert manager.refresh_window(3600) == pytest.approx(1800)
    rec = TokenRecord(account_id=1, access_token="x",
                      access_expires_at=clock() + timedelta(days=4),
                      access_ttl_seconds=15 * DAY)
    assert manager.needs_refresh(rec) is True          # 剩余 4 天 < 4.5 天窗口
    rec.access_expires_at = clock() + timedelta(days=10)
    assert manager.needs_refresh(rec) is False


def test_token_without_cache_record_requires_reauth(cfg, clock, account):
    manager, *_ = build(cfg, clock)
    with pytest.raises(ReauthRequired) as exc:
        manager.get_access_token(account)
    assert exc.value.code == "REAUTH_REQUIRED"


# =====================================================================
# 自动刷新 + 轮换回写
# =====================================================================
def test_expired_token_is_refreshed_and_rotation_written_back(cfg, clock, account):
    manager, repo, client, box, transport = build(cfg, clock)
    before = seed(repo, box, clock, remaining=-60)      # 已过期
    assert before.version == 0

    access = manager.get_access_token(account)
    assert access.refreshed is True
    assert access.token == "AT_NEW"

    stored = repo.get_token(1)
    assert stored is not None
    assert box.open(stored.access_token) == "AT_NEW"
    assert box.open(stored.refresh_token) == "RT_NEW"     # 轮换后的 refresh_token 必须落库
    assert stored.version == 1
    assert stored.refresh_count == 1
    assert stored.access_ttl_seconds == 15 * DAY
    assert transport.count("/oauth/refresh_token/") == 1
    assert any(e["event"] == "refreshed" for e in repo.events)


def test_refresh_rotation_value_replaces_old_refresh_token(cfg, clock, account):
    """刷新两次后库里必须是最新一次返回的 refresh_token，不能是旧的。"""
    state = {"n": 0}

    def router(method, url, params, body):
        if "/oauth/refresh_token/" in url:
            state["n"] += 1
            n = state["n"]
            return oauth_ok({"access_token": f"AT_{n}", "expires_in": 15 * DAY,
                             "refresh_token": f"RT_{n}", "refresh_expires_in": 30 * DAY})
        return oauth_ok({})

    manager, repo, client, box, transport = build(cfg, clock, router=router)
    seed(repo, box, clock, remaining=-10)
    assert manager.get_access_token(account).token == "AT_1"
    assert box.open(repo.get_token(1).refresh_token) == "RT_1"

    # 再让它进入过期，第二次刷新必须用 RT_1 而不是 RT_OLD
    repo.get_token(1).access_expires_at = clock() - timedelta(seconds=1)
    assert manager.get_access_token(account).token == "AT_2"
    sent = [c["params"]["refresh_token"] for c in transport.calls
            if "/oauth/refresh_token/" in c["url"]]
    assert sent == ["RT_OLD", "RT_1"]
    assert box.open(repo.get_token(1).refresh_token) == "RT_2"


def test_refresh_failure_marks_account_reauth_required(cfg, clock, account):
    manager, repo, client, box, transport = build(
        cfg, clock, router=lambda *a, **k: err(2190002, "refresh token expired"))
    seed(repo, box, clock, remaining=-10)
    with pytest.raises(ReauthRequired) as exc:
        manager.get_access_token(account)
    assert exc.value.account_id == 1
    assert repo.get_account(1).status == "reauth_required"
    assert any(e["event"] == "reauth_required" for e in repo.events)


def test_missing_refresh_token_marks_reauth(cfg, clock, account):
    manager, repo, client, box, transport = build(cfg, clock)
    seed(repo, box, clock, remaining=-10, refresh=None)
    with pytest.raises(ReauthRequired):
        manager.get_access_token(account)


def test_network_error_is_retried_then_raises(cfg, clock, account):
    calls = {"n": 0}

    def router(method, url, params, body):
        calls["n"] += 1
        raise TransportError("boom")

    manager, repo, client, box, transport = build(cfg, clock, router=router)
    seed(repo, box, clock, remaining=-10)
    with pytest.raises(DouyinApiError):
        manager.get_access_token(account)
    assert calls["n"] >= 2          # 至少重试了一次


# =====================================================================
# 并发单飞
# =====================================================================
def test_peer_already_refreshed_is_reused_without_second_refresh(cfg, clock, account):
    base = MemoryRepo()
    box = SecretsBox("")
    old = seed(base, box, clock, remaining=-10)
    peer = TokenRecord(account_id=1, access_token=box.seal("AT_PEER"),
                       access_expires_at=clock() + timedelta(days=15),
                       refresh_token=box.seal("RT_PEER"),
                       access_ttl_seconds=15 * DAY, version=1)
    repo = SeqRepo(base, [old, peer, peer])
    manager, _, client, _, transport = build(cfg, clock, repo=repo)
    access = manager.get_access_token(account)
    assert access.token == "AT_PEER"
    assert access.refreshed is True
    assert transport.calls == []          # 没有发起第二次刷新


def test_locked_refresh_waits_for_peer_result(cfg, clock, account):
    base = MemoryRepo()
    box = SecretsBox("")
    old = seed(base, box, clock, remaining=-10)
    peer = TokenRecord(account_id=1, access_token=box.seal("AT_PEER"),
                       access_expires_at=clock() + timedelta(days=15),
                       access_ttl_seconds=15 * DAY, version=1)
    repo = SeqRepo(base, [old, peer, peer])
    manager, _, client, _, transport = build(cfg, clock, repo=repo, locks=ClosedLocks())
    access = manager.get_access_token(account)
    assert access.token == "AT_PEER"
    assert transport.calls == []


def test_write_conflict_falls_back_to_db_value(cfg, clock, account):
    base = MemoryRepo()
    box = SecretsBox("")
    seed(base, box, clock, remaining=-10)

    class ConflictRepo(MemoryRepo):
        def save_refreshed_token(self, rec, expected_version):
            self.tokens[1] = TokenRecord(account_id=1, access_token=self._box.seal("AT_WINNER"),
                                         access_expires_at=self._clock() + timedelta(days=15),
                                         access_ttl_seconds=15 * DAY, version=99)
            return None

    repo = ConflictRepo()
    repo.tokens = dict(base.tokens)
    repo._box, repo._clock = box, clock
    manager, _, client, _, transport = build(cfg, clock, repo=repo)
    access = manager.get_access_token(account)
    assert access.token == "AT_WINNER"


# =====================================================================
# 调用侧自愈
# =====================================================================
def test_call_with_token_refreshes_once_on_token_invalid(cfg, clock, account):
    manager, repo, client, box, transport = build(cfg, clock)
    seed(repo, box, clock, remaining=10 * DAY)
    seen = []

    def fn(token: str) -> str:
        seen.append(token)
        if token == "AT_OLD":
            raise TokenInvalid("/api/douyin/v1/video/video_list/", "2190008", "expired")
        return "ok"

    assert manager.call_with_token(account, fn) == "ok"
    assert seen == ["AT_OLD", "AT_NEW"]


def test_keepalive_renews_refresh_token_when_due(cfg, clock, account):
    manager, repo, client, box, transport = build(
        cfg, clock, router=lambda *a, **k: oauth_ok({
            "refresh_token": "RT_RENEWED", "refresh_expires_in": 30 * DAY}))
    seed(repo, box, clock, remaining=10 * DAY, refresh_days=3)   # 30 天里只剩 3 天
    result = manager.keepalive(account)
    assert result["action"] == "renewed"
    stored = repo.get_token(1)
    assert box.open(stored.refresh_token) == "RT_RENEWED"
    assert box.open(stored.access_token) == "AT_OLD"              # access_token 不变


def test_keepalive_skips_when_not_due(cfg, clock, account):
    manager, repo, client, box, transport = build(cfg, clock)
    seed(repo, box, clock, remaining=10 * DAY, refresh_days=25)
    assert manager.keepalive(account)["action"] == "skip"
    assert transport.calls == []


def test_status_reports_expiring_state(cfg, clock, account):
    manager, repo, client, box, transport = build(cfg, clock)
    seed(repo, box, clock, remaining=2 * DAY)     # 在 4.5 天预刷新窗口内
    status = manager.status(account)
    assert status["state"] == "expiring"
    assert status["refresh_window_seconds"] == int(4.5 * DAY)
