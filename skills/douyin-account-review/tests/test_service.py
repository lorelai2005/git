"""服务编排测试：四阶段（facts -> render -> push）、去重、步数闸门、账号解析。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import FakeFeishuTransport, NullLocks
from dyreview.account_resolver import resolve_account
from dyreview.collect import Collector
from dyreview.douyin_client import DouyinClient
from dyreview.errors import AccountAmbiguous, AccountNotFound, DyReviewError
from dyreview.feishu import FeishuClient
from dyreview.offline import MemoryRepo
from dyreview.secrets_box import SecretsBox
from dyreview.service import ReviewService
from dyreview.token_manager import TokenManager


def build_service(cfg, clock, repo=None, *, feishu=True, max_steps=8, dedupe=10):
    repo = repo or MemoryRepo()
    box = SecretsBox("")
    client = DouyinClient(cfg, transport=lambda *a, **k: (200, {"data": {}}), sleep=lambda s: None)
    tokens = TokenManager(cfg, repo, client, box, NullLocks(), clock=clock, sleep=lambda s: None)
    collector = Collector(cfg, repo, tokens, client, clock=clock, today=lambda: repo.today)
    feishu_client = None
    if feishu:
        transport = FakeFeishuTransport()
        feishu_client = FeishuClient(cfg, transport=transport, sleep=lambda s: None)
        feishu_client.transport_spy = transport  # type: ignore[attr-defined]
    service = ReviewService(cfg, repo, tokens, collector, feishu_client, today=lambda: repo.today)
    service.max_steps = max_steps
    service.dedupe_minutes = dedupe
    return service, repo, feishu_client


@pytest.fixture
def service(cfg, clock):
    svc, repo, feishu = build_service(cfg, clock)
    return svc, repo


def test_start_returns_facts_and_prompt(service):
    svc, repo = service
    result = svc.start("环宇家居", days=7, auto_collect_hours=0)
    assert result["ok"] is True
    assert result["status"] == "facts_ready"
    assert result["kpi"]["current"]["plays"] > 0
    assert "## 报告对象" in result["prompt"]
    assert "只能使用我提供的数据" in result["prompt"] or "禁止编造" in result["prompt"]
    assert result["paths"]["facts"].endswith("facts.json")
    review = repo.get_review(result["review_id"])
    assert review["status"] == "facts_ready"
    assert review["facts"]["meta"]["account_name"]


def test_resolve_by_douyin_id_and_name(service):
    svc, repo = service
    assert svc.resolve("huanyu_home").id == 1
    assert svc.resolve("环宇家居（示例）").id == 1
    assert svc.resolve("sample_open_id_0001").id == 1
    with pytest.raises(AccountNotFound):
        svc.resolve("不存在的账号xyz")


def test_resolve_ambiguous(service):
    svc, repo = service
    repo.upsert_account(open_id="open_x", account_name="环宇家居 二号店", douyin_id="huanyu2")
    repo.accounts[2] = repo.accounts[repo._review_seq + 2] if False else repo.accounts[2]
    repo.accounts[2].id = 2
    repo.accounts[2].nickname = "环宇家居二号店"
    with pytest.raises(AccountAmbiguous) as exc:
        resolve_account(repo, "环宇家居")
    assert len(exc.value.candidates) >= 2


def test_dedupe_returns_previous_review(service):
    svc, repo = service
    first = svc.start("环宇家居", days=7)
    svc.render(first["review_id"], "## 一、一句话结论\n本期数据正常。")
    second = svc.start("环宇家居", days=7)
    assert second["status"] == "deduped"
    assert second["review_id"] == first["review_id"]


def test_force_skips_dedupe(service):
    svc, repo = service
    first = svc.start("环宇家居", days=7)
    svc.render(first["review_id"], "## 一、一句话结论\n正常。")
    second = svc.start("环宇家居", days=7, force=True)
    assert second["status"] == "facts_ready"
    assert second["review_id"] != first["review_id"]


def test_render_rejects_fabricated_numbers(service):
    svc, repo = service
    started = svc.start("环宇家居", days=7)
    result = svc.render(started["review_id"], "## 一、一句话结论\n播放量 999999999 次，暴涨 300%。")
    assert result["ok"] is False
    assert result["code"] == "NUMBERS_INVALID"
    assert result["validation"]["violation_count"] >= 1
    assert repo.get_review(started["review_id"])["status"] == "failed"


def test_render_accepts_grounded_report_and_pushes(service):
    svc, repo = service
    started = svc.start("环宇家居", days=7)
    cur = started["kpi"]["current"]
    text = (f"## 一、一句话结论\n本期播放 {cur['plays']}，互动率 "
            f"{round(cur['engagement_rate'] * 100, 1)}%。\n\n"
            "## 三、问题诊断\n1. 流量集中度高。\n")
    rendered = svc.render(started["review_id"], text)
    assert rendered["ok"] is True, rendered.get("validation")
    assert repo.get_review(started["review_id"])["status"] == "rendered"

    pushed = svc.push(started["review_id"])
    assert pushed["ok"] is True
    assert pushed["message_ids"]
    assert repo.get_review(started["review_id"])["status"] == "pushed"


def test_render_with_force_allows_unsourced_numbers(service):
    svc, repo = service
    started = svc.start("环宇家居", days=7)
    result = svc.render(started["review_id"], "## 一、结论\n播放 123456789 次。", force=True)
    assert result["ok"] is True
    assert result["validation"]["ok"] is False


def test_run_handoff_mode_stops_at_facts(cfg, clock):
    svc, repo, feishu = build_service(cfg, clock)
    result = svc.run("环宇家居", llm="handoff", push=False, auto_collect_hours=0)
    assert result["status"] == "facts_ready"
    assert "render" in result["next"]


def test_run_builtin_mode_with_injected_llm(cfg, clock, monkeypatch):
    svc, repo, feishu = build_service(cfg, clock)
    cfg._data["llm"] = {"mode": "builtin", "model": "gpt-test",
                        "base_url": "https://fake", "api_key": "k"}
    svc.llm_mode = "builtin"

    class FakeResult:
        text = "## 一、一句话结论\n本期数据平稳，建议保持更新节奏。"
        model = "gpt-test"
        input_tokens = 100
        output_tokens = 50
        latency_ms = 10

    import dyreview.service as service_mod
    monkeypatch.setattr(service_mod, "call_llm", lambda package, cfg: FakeResult())
    result = svc.run("环宇家居", llm="builtin", push=True, auto_collect_hours=0)
    assert result["ok"] is True
    assert result["status"] == "pushed"
    assert repo.llm_usage and repo.llm_usage[0]["input_tokens"] == 100


def test_step_cap_blocks_runaway_loop(cfg, clock):
    svc, repo, feishu = build_service(cfg, clock, max_steps=2)
    started = svc.start("环宇家居", days=7)                 # step 1
    svc.render(started["review_id"], "## 一、结论\n正常。")   # step 2
    with pytest.raises(DyReviewError) as exc:
        svc.push(started["review_id"])                      # step 3 -> 超限
    assert "工具步" in str(exc.value)


def test_push_without_report_raises(service):
    svc, repo = service
    started = svc.start("环宇家居", days=7)
    with pytest.raises(DyReviewError):
        svc.push(started["review_id"])


def test_busy_lock_returns_busy(service):
    svc, repo = service
    repo.acquire_task_lock("review:1", "other", ttl_seconds=300)
    result = svc.start("环宇家居", days=7)
    assert result["ok"] is False
    assert result["code"] == "REVIEW_BUSY"
