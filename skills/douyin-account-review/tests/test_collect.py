"""采集编排测试：翻页、批量取指标、token 失效自愈、单账号失败不影响整批。"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import FakeTransport, NullLocks, err, ok, oauth_ok
from dyreview.collect import Collector
from dyreview.douyin_client import DouyinClient
from dyreview.errors import ReauthRequired
from dyreview.offline import MemoryRepo
from dyreview.secrets_box import SecretsBox
from dyreview.token_manager import TokenManager

STAT_DAY = date(2026, 1, 20)


def page(items, cursor, has_more):
    return ok({"list": items, "cursor": cursor, "has_more": has_more})


def item(i, title="作品"):
    return {"item_id": f"item_{i}", "title": title, "duration": 20000,
            "create_time": 1768000000 + i * 3600, "share_url": f"https://x/{i}"}


class Harness:
    def __init__(self, cfg, clock, router, repo=None, box=None):
        self.repo = repo or MemoryRepo()
        self.box = box or SecretsBox("")
        transport = FakeTransport(router)
        self.client = DouyinClient(cfg, transport=transport, sleep=lambda s: None)
        self.tokens = TokenManager(cfg, self.repo, self.client, self.box, NullLocks(),
                                   clock=clock, sleep=lambda s: None)
        self.collector = Collector(cfg, self.repo, self.tokens, self.client,
                                   clock=clock, today=lambda: STAT_DAY)
        self.transport = transport


def seed_token(repo, box, clock, days=10):
    from datetime import timedelta

    from dyreview.models import TokenRecord
    repo.insert_token(TokenRecord(
        account_id=1, access_token=box.seal("AT_OK"),
        access_expires_at=clock() + timedelta(days=days),
        refresh_token=box.seal("RT_OK"),
        access_ttl_seconds=15 * 86400))


def test_collect_paginates_and_writes_metrics(cfg, clock):
    calls = {"list": 0}

    def router(method, url, params, body):
        if "user_data" in url:
            return ok({"follower_count": 50000, "new_fans": 120, "aweme_count": 7})
        if "video_list" in url:
            calls["list"] += 1
            if calls["list"] == 1:
                return page([item(1), item(2)], cursor=2, has_more=True)
            return page([item(3)], cursor=3, has_more=False)
        if "video_data" in url:
            ids = body["item_ids"]
            return ok({"list": [{"item_id": i, "statistics": {
                "play_count": 1000, "digg_count": 40, "comment_count": 5,
                "share_count": 2, "collect_count": 10, "finish_rate": "20%"}}
                for i in ids]})
        if "fan_data" in url:
            return ok({"gender": {"man": 0.37, "woman": 0.63}})
        return ok({})

    h = Harness(cfg, clock, router)
    seed_token(h.repo, h.box, clock)
    account = h.repo.get_account(1)
    rep = h.collector.collect_account(account, kind="full")

    assert rep.status == "success", rep.error_msg
    assert rep.video_pages == 2
    assert rep.videos_seen == 3
    assert rep.videos_upserted == 3
    assert rep.account_metric_written is True
    metric_rows = [m for m in h.repo.metric_upserts if m["item_id"] != "__account__"]
    assert len(metric_rows) == 3
    assert metric_rows[0]["stats"]["finish_rate"] == pytest.approx(0.2)
    assert metric_rows[0]["stat_date"] == STAT_DAY
    assert h.repo.runs[-1]["status"] == "success"


def test_collect_refreshes_token_when_api_says_expired(cfg, clock):
    state = {"refreshed": False}

    def router(method, url, params, body):
        if "refresh_token" in url:
            state["refreshed"] = True
            return oauth_ok({"access_token": "AT_NEW", "expires_in": 15 * 86400,
                             "refresh_token": "RT_NEW", "refresh_expires_in": 30 * 86400})
        if "video_list" in url:
            if params.get("access_token") == "AT_OK":
                return err(2190008, "access token expired")
            return page([item(1)], cursor=1, has_more=False)
        if "video_data" in url:
            return ok({"list": [{"item_id": "item_1", "statistics": {"play_count": 500}}]})
        return ok({"follower_count": 100})

    h = Harness(cfg, clock, router)
    seed_token(h.repo, h.box, clock)
    rep = h.collector.collect_account(h.repo.get_account(1), kind="full")

    assert state["refreshed"] is True
    assert rep.token_refreshed is True
    assert rep.status in ("success", "partial"), rep.error_msg
    assert rep.videos_upserted == 1


def test_collect_marks_failure_on_reauth_required(cfg, clock):
    def router(method, url, params, body):
        return err(2190002, "refresh token invalid")

    h = Harness(cfg, clock, router)
    from dyreview.models import TokenRecord
    from datetime import timedelta
    h.repo.insert_token(TokenRecord(
        account_id=1, access_token=h.box.seal("AT_DEAD"),
        access_expires_at=clock() - timedelta(days=1),
        refresh_token=h.box.seal("RT_DEAD"), access_ttl_seconds=86400))
    rep = h.collector.collect_account(h.repo.get_account(1))
    assert rep.status == "failed"
    assert rep.error_code in ("REAUTH_REQUIRED", "2190002")
    assert h.repo.get_account(1).status == "reauth_required"


def test_one_account_failure_does_not_stop_batch(cfg, clock):
    def router(method, url, params, body):
        if "user_data" in url:
            if params.get("open_id") == "open-1":
                return err(50000, "boom")
            return ok({"follower_count": 10})
        if "video_list" in url:
            return page([], cursor=0, has_more=False)
        return ok({})

    h = Harness(cfg, clock, router)
    seed_token(h.repo, h.box, clock)
    second = h.repo.upsert_account(open_id="open-2", account_name="第二个账号")
    second.id = 2
    h.repo.accounts[2] = second
    h.repo.tokens[2] = h.repo.tokens[1]
    h.repo.tokens[2].account_id = 2

    reports = h.collector.run(list(h.repo.accounts.values()), kind="incremental")
    assert len(reports) == 2
    assert reports[0].status in ("success", "partial", "failed")
    assert reports[1].status in ("success", "partial", "skipped")


def test_dry_run_makes_no_api_calls(cfg, clock):
    def router(method, url, params, body):
        raise AssertionError("dry-run 不应发起任何请求")

    h = Harness(cfg, clock, router)
    seed_token(h.repo, h.box, clock)
    rep = h.collector.collect_account(h.repo.get_account(1), dry_run=True)
    assert rep.status == "skipped"
    assert h.transport.calls == []


def test_paused_account_is_skipped(cfg, clock):
    h = Harness(cfg, clock, lambda *a, **k: ok({}))
    account = h.repo.get_account(1)
    account.status = "paused"
    reports = h.collector.run([account])
    assert reports[0].status == "skipped"
    assert h.transport.calls == []


def test_metric_targets_respect_batch_cap(make_cfg, clock):
    cfg = make_cfg({"douyin.batch.video_data_batch": 2, "douyin.batch.video_data_rounds": 1,
                    "douyin.batch.video_list_count": 50})
    h = Harness(cfg, clock, lambda *a, **k: ok({}))
    from dyreview.models import VideoMeta
    metas = [VideoMeta(item_id=f"i{i}") for i in range(10)]
    targets = h.collector._pick_metric_targets(metas, kind="full")
    assert len(targets) == 2       # 1 轮 × 2 条
