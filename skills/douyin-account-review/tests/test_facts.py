"""事实包与规则引擎测试：数字要对得上库，诊断要按阈值触发。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from dyreview.errors import DataUnavailable
from dyreview.facts import Thresholds, _diagnose, build_facts, collect_numbers, fmt_count, fmt_pct
from dyreview.models import ReviewWindow
from dyreview.offline import MemoryRepo


@pytest.fixture
def repo():
    return MemoryRepo()


@pytest.fixture
def account(repo):
    return repo.get_account(1)


def facts_for(repo, account, cfg, days=7):
    window = ReviewWindow(repo.today - timedelta(days=days - 1), repo.today)
    return build_facts(repo, account, window, cfg)


def test_plays_equal_sum_of_window_deltas(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    window = facts["meta"]["period"]
    start = date.fromisoformat(window["start"])
    end = date.fromisoformat(window["end"])
    rows = repo.video_window_growth(account.id, start, end)
    expected = sum(max((r["play_end"] or 0) - (r["play_before"] or 0), 0) for r in rows)
    assert facts["kpi"]["current"]["plays"] == expected


def test_facts_have_all_required_sections(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    for key in ("meta", "kpi", "funnel", "videos", "structure", "audience",
                "data_gaps", "diagnosis", "thresholds", "display", "numbers"):
        assert key in facts
    assert facts["meta"]["account_name"] == account.account_name
    assert facts["kpi"]["previous"]["days"] == 7
    assert isinstance(facts["numbers"], list) and len(facts["numbers"]) > 50


def test_video_top_ranking_and_share(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    videos = facts["videos"]
    assert videos["top"], "样例数据应该有序"
    plays = [v["plays"] for v in videos["top"]]
    assert plays == sorted(plays, reverse=True)
    assert 0 <= (videos["top1_play_share"] or 0) <= 1
    if videos["top"]:
        assert videos["top"][0]["play_share"] == videos["top1_play_share"]


def test_empty_data_raises_data_unavailable(cfg):
    class EmptyRepo(MemoryRepo):
        def account_metric_range(self, *a, **k):
            return []

        def video_window_growth(self, *a, **k):
            return []

    repo = EmptyRepo()
    account = repo.get_account(1)
    window = ReviewWindow(repo.today - timedelta(days=6), repo.today)
    with pytest.raises(DataUnavailable):
        build_facts(repo, account, window, cfg)


def test_play_drop_rule_fires_on_big_drop(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["kpi"]["delta"]["plays_pct"] = -0.55
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "PLAY_DROP" in codes


def test_finish_rate_rule_fires_below_warn_line(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["funnel"]["finish_rate"] = 0.08
    findings = _diagnose(facts, Thresholds(), account)
    hit = next(d for d in findings if d["code"] == "FINISH_RATE_LOW")
    assert hit["level"] == "warn"
    assert hit["evidence"]["finish_rate"] == 0.08


def test_follower_stall_rule(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["kpi"]["current"]["followers_net"] = -3
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "FOLLOWER_STALL" in codes


def test_publish_gap_rule(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["videos"]["publish_gap_days"] = 15
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "PUBLISH_GAP" in codes


def test_concentration_rule_needs_multiple_videos(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["videos"]["top1_play_share"] = 0.8
    facts["videos"]["active_video_count"] = 1
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "CONCENTRATION_RISK" not in codes      # 只有 1 条作品时不判“依赖单条”
    facts["videos"]["active_video_count"] = 5
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "CONCENTRATION_RISK" in codes


def test_healthy_rule_when_nothing_wrong(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["kpi"]["delta"]["plays_pct"] = 0.12
    facts["funnel"]["finish_rate"] = 0.30
    facts["funnel"]["engagement_rate"] = 0.03      # 介于 warn 与 good 之间 -> 不触发规则
    facts["kpi"]["current"]["followers_net"] = 300
    facts["kpi"]["current"]["publish_count"] = 2
    facts["videos"]["publish_gap_days"] = 1
    facts["videos"]["top1_play_share"] = 0.2
    facts["videos"]["active_video_count"] = 6
    facts["videos"]["new_video_plays"] = 5000
    facts["videos"]["window_total_plays"] = 10000
    facts["data_gaps"] = []
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert codes == ["HEALTHY"]


def test_data_stale_rule_only_on_real_staleness(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["data_gaps"] = ["缺少封面点击率（该指标需要额外数据权限）"]
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "DATA_STALE" not in codes
    facts["data_gaps"] = ["最新指标快照距今 6 天，可能采集断更"]
    codes = [d["code"] for d in _diagnose(facts, Thresholds(), account)]
    assert "DATA_STALE" in codes


def test_diagnosis_sorted_by_severity(repo, account, cfg):
    facts = facts_for(repo, account, cfg)
    facts["kpi"]["delta"]["plays_pct"] = -0.6
    facts["funnel"]["finish_rate"] = 0.05
    facts["kpi"]["current"]["followers_net"] = -5
    levels = [d["level"] for d in _diagnose(facts, Thresholds(), account)]
    order = {"critical": 0, "warn": 1, "info": 2}
    assert levels == sorted(levels, key=lambda x: order[x])


def test_collect_numbers_includes_scaled_forms():
    nums = collect_numbers({"a": 0.2537, "b": 12000, "c": "标题里有 10万 和 90%"})
    assert 0.2537 in nums
    assert 25.37 in nums
    assert 12000.0 in nums
    assert 100000.0 in nums
    assert 0.9 in nums


def test_formatters():
    assert fmt_count(12345) == "1.23万"
    assert fmt_count(123456789) == "1.23亿"
    assert fmt_count(42) == "42"
    assert fmt_count(None) == "—"
    assert fmt_pct(0.2537) == "25.4%"
    assert fmt_pct(-0.1, signed=True) == "-10.0%"
    assert fmt_pct(None) == "—"
