"""反幻觉数字校验测试：报告里的数字必须在事实包里找得到来源。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from dyreview.facts import build_facts
from dyreview.models import ReviewWindow
from dyreview.offline import MemoryRepo
from dyreview.report import (clean_llm_output, render_markdown, validate_numbers)


@pytest.fixture
def facts(cfg):
    repo = MemoryRepo()
    account = repo.get_account(1)
    window = ReviewWindow(repo.today - timedelta(days=6), repo.today)
    return build_facts(repo, account, window, cfg)


def test_numbers_from_facts_pass(facts):
    cur = facts["kpi"]["current"]
    plays = cur["plays"]
    text = f"本期播放 {plays} 次，互动率 {cur['engagement_rate'] * 100:.1f}%，粉丝净增 {cur['followers_net']}。"
    result = validate_numbers(text, facts)
    assert result["ok"], result["violations"]


def test_fabricated_number_is_flagged(facts):
    text = "本期播放暴涨到 987654321 次，完播率提升了 88% 。"
    result = validate_numbers(text, facts)
    assert not result["ok"]
    flagged = {v["normalized"] for v in result["violations"]}
    assert 987654321.0 in flagged
    assert 0.88 in flagged


def test_lying_about_percentage_is_caught(facts):
    real = facts["funnel"]["finish_rate"]
    wrong = round((real + 0.3) * 100, 1)          # 编一个高 30 个点的完播率
    result = validate_numbers(f"完播率提高到 {wrong}%", facts)
    assert not result["ok"]


def test_structural_small_integers_allowed(facts):
    text = "给出 3 条建议，第 2 条最关键，保持每周 5 更，覆盖 7 天窗口。"
    result = validate_numbers(text, facts)
    assert result["ok"], result["violations"]


def test_dates_times_and_urls_ignored(facts):
    text = ("数据截止 2026-10-22，生成时间 03:20，"
            "参考 https://open.douyin.com/api/douyin/v1/video/video_data/?x=12345 。")
    result = validate_numbers(text, facts)
    assert result["ok"], result["violations"]


def test_numbers_inside_video_titles_are_allowed(facts):
    """事实包里的作品标题含数字（如“10万装出30万效果”），报告引用标题不能误报。"""
    result = validate_numbers("《10万装出30万效果》这条作品表现最好", facts)
    assert result["ok"], result["violations"]


def test_wan_units_normalized(facts):
    plays = facts["kpi"]["current"]["plays"]
    expected_wan = round(plays / 10000, 2)
    result = validate_numbers(f"本期播放 {expected_wan}万", facts)
    assert result["ok"], result["violations"]


def test_empty_report_is_ok(facts):
    result = validate_numbers("", facts)
    assert result["ok"]
    assert result["checked"] == 0


def test_clean_llm_output_strips_fences_and_preamble():
    raw = "```markdown\n以下是报告：\n# 标题\n## 一、结论\n内容\n```"
    cleaned = clean_llm_output(raw)
    assert "```" not in cleaned
    assert not cleaned.startswith("# 标题")


def test_render_markdown_contains_appendix_and_footer(facts, cfg):
    md = render_markdown(facts, "## 一、一句话结论\n数据正常。", cfg)
    assert facts["meta"]["account_name"] in md
    assert "附：关键数据明细" in md
    assert "数据来源：抖音开放平台" in md
    assert "| 指标 | 本期 | 上期 | 变化 |" in md


def test_render_markdown_falls_back_without_llm_text(facts, cfg):
    md = render_markdown(facts, None, cfg)
    assert "数据兜底生成" in md
    assert "## 三、问题诊断" in md


def test_render_markdown_truncates_when_too_long(facts, make_cfg):
    cfg = make_cfg({"review.report.max_chars": 500})
    md = render_markdown(facts, "长" * 2000, cfg)
    assert len(md) <= 700
    assert "内容超长已截断" in md
