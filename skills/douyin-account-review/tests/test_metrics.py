"""指标归一化测试：抖音字段别名、百分比/万单位、时间戳、作品解析、粉丝画像。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dyreview.metrics import (collection_rate, interaction_rate, normalize_account_metric,
                              normalize_fan_profile, normalize_video_metric, parse_epoch,
                              parse_topics, parse_video_item, to_int, to_ratio)


def test_video_metric_alias_mapping_and_derivation():
    raw = {"statistics": {"digg_count": 1200, "comment_count": 30, "share_count": 12,
                          "collect_count": 60, "play_count": 24000,
                          "finish_rate": "18.5%", "bounce_rate": 0.42}}
    stats = normalize_video_metric(raw)
    assert stats["like_count"] == 1200            # digg_count -> like_count
    assert stats["play_count"] == 24000
    assert stats["finish_rate"] == pytest.approx(0.185)   # 百分比字符串
    assert stats["bounce_rate"] == pytest.approx(0.42)
    # (1200+30+12+60)/24000
    assert stats["interaction_rate"] == pytest.approx(0.05425, rel=1e-3)


def test_missing_field_stays_none_instead_of_zero():
    stats = normalize_video_metric({"play_count": 100})
    assert stats["like_count"] is None            # 不能把“没拿到”写成 0
    assert stats["play_count"] == 100
    assert stats["interaction_rate"] is None


def test_chinese_units_parsed():
    assert to_int("1.2万") == 12000
    assert to_int("3.5亿") == 350000000
    assert to_int("8.5w") == 85000
    assert to_int("有 1234 次") is None            # 非纯数字不猜
    assert to_ratio("25%") == pytest.approx(0.25)
    assert to_ratio(0.2537) == pytest.approx(0.2537)
    assert to_ratio(25) == pytest.approx(0.25)     # 大于 1 视为百分数
    assert to_ratio(None) is None


def test_account_metric_aliases():
    raw = {"data": {"follower_count": 41230, "new_fans": 120, "cancel_fans": 8,
                    "total_favorited": 180000, "aweme_count": 42}}
    stats = normalize_account_metric(raw)
    assert stats["follower_count"] == 41230
    assert stats["new_follower_count"] == 120
    assert stats["unfollower_count"] == 8
    assert stats["video_count"] == 42


def test_parse_epoch_seconds_and_milliseconds():
    assert parse_epoch(1760000000) == datetime(2025, 10, 9, 8, 53, 20, tzinfo=timezone.utc)
    assert parse_epoch(1760000000000) == datetime(2025, 10, 9, 8, 53, 20, tzinfo=timezone.utc)
    assert parse_epoch("2026-01-20T03:00:00Z") == datetime(2026, 1, 20, 3, tzinfo=timezone.utc)
    assert parse_epoch(None) is None
    assert parse_epoch("not-a-time") is None


def test_parse_video_item_ms_duration_and_topics():
    item = {"item_id": "750000000000001", "title": "装修避坑 #装修 #全屋定制",
            "duration": 28500, "create_time": 1760000000, "is_top": True,
            "cover": {"url_list": ["https://cdn/x.jpg"]},
            "share_url": "https://v.douyin.com/x/"}
    meta = parse_video_item(item)
    assert meta is not None
    assert meta.item_id == "750000000000001"
    assert meta.duration_sec == 28                 # 毫秒 -> 秒
    assert meta.is_top is True
    assert meta.topics == ["装修", "全屋定制"]
    assert meta.cover_url == "https://cdn/x.jpg"


def test_parse_video_item_without_id_is_skipped():
    assert parse_video_item({"title": "no id"}) is None


def test_parse_topics_dedup_and_limit():
    tags = parse_topics("#装修 #装修 #避坑", "#定制")
    assert tags == ["装修", "避坑", "定制"]


def test_fan_profile_dict_and_list_forms():
    dims = normalize_fan_profile({
        "gender": {"man": 0.37, "woman": 0.63},
        "age": [{"name": "25-34", "rate": "46%"}, {"name": "35-44", "rate": "29%"}],
        "unknown_key": {"x": 1},
    })
    assert [b["bucket"] for b in dims["gender"]] == ["woman", "man"]  # 按占比降序，键名原样保留
    assert dims["age"][0] == {"bucket": "25-34", "pct": pytest.approx(0.46)}
    assert "unknown_key" not in dims


def test_collection_rate():
    assert collection_rate({"play_count": 1000, "collect_count": 25}) == pytest.approx(0.025)
    assert collection_rate({"play_count": 0, "collect_count": 25}) is None
    assert interaction_rate({"play_count": None}) is None
