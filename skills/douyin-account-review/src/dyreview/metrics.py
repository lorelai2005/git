"""原始字段 -> 归一化指标。

抖音不同接口/不同版本对同一个指标用的字段名不完全一致（`like_count` / `digg_count`），
这里用别名表做统一；遇到新字段名只需在 ALIASES 里加一行。

原则：宁可返回 None，也不要用 0 冒充缺失数据 —— 0 和“没拿到”在复盘里含义完全不同。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .models import VideoMeta

TOPIC_RE = re.compile(r"#([^\s#@]{1,40})")
_NUM_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([万亿wWkK]?)\s*%?\s*$")

VIDEO_ALIASES: dict[str, tuple[str, ...]] = {
    "play_count": ("play_count", "play_volume", "video_play_count", "play_cnt", "play", "vv"),
    "like_count": ("like_count", "digg_count", "like_cnt", "digg", "like"),
    "comment_count": ("comment_count", "comment_cnt", "comments"),
    "share_count": ("share_count", "share_cnt", "share", "share_volume"),
    "collect_count": ("collect_count", "collect_cnt", "favorite_count", "collect"),
    "forward_count": ("forward_count", "forward_cnt", "forward"),
    "finish_rate": ("finish_rate", "finish_rate_avg", "completion_rate", "play_finish_rate"),
    "full_play_rate": ("full_play_rate", "full_play_rate_avg"),
    "bounce_rate": ("bounce_rate", "bounce_rate_2s", "play_2s_bounce_rate", "jump_rate"),
    "avg_play_duration_ms": ("avg_play_duration_ms", "avg_play_duration", "item_avg_play_duration"),
    "cover_click_rate": ("cover_click_rate", "cover_ctr", "click_rate"),
}

ACCOUNT_ALIASES: dict[str, tuple[str, ...]] = {
    "follower_count": ("follower_count", "fans_count", "follower_cnt", "fans", "total_fans"),
    "new_follower_count": ("new_follower_count", "new_fans", "follower_inc", "new_follower"),
    "unfollower_count": ("unfollower_count", "cancel_fans", "lost_follower", "unfollow_count"),
    "total_favorited": ("total_favorited", "like_total", "total_like", "favorited_count"),
    "video_count": ("video_count", "aweme_count", "item_count", "works_count"),
    "profile_visit_count": ("profile_visit_count", "profile_uv", "home_page_visit"),
    "home_visit_count": ("home_visit_count", "home_visit", "personal_home_visit"),
}


def _scaled(value: Any) -> float | None:
    """解析 12345 / "1.2万" / "3.4亿" / "12%" 这类值。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.match(str(value))
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    factor = {"万": 1e4, "w": 1e4, "W": 1e4, "亿": 1e8, "k": 1e3, "K": 1e3}.get(unit, 1.0)
    return num * factor


def to_int(value: Any) -> int | None:
    val = _scaled(value)
    if val is None:
        return None
    try:
        return int(round(val))
    except (ValueError, OverflowError):
        return None


def to_ratio(value: Any) -> float | None:
    """0.25 / 25 / "25%" 统一成 0~1 的小数。"""
    if value is None or value == "":
        return None
    was_percent = isinstance(value, str) and "%" in value
    val = _scaled(value)
    if val is None:
        return None
    if was_percent or val > 1.0:
        val = val / 100.0
    if val < 0:
        return None
    return round(min(val, 1.0), 5)


def _pick(raw: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _deep_find(payload: dict, keys: Iterable[str], depth: int = 3) -> Any:
    """在嵌套 dict 里找第一个命中的键（接口返回层级不固定时用）。"""
    if depth <= 0 or not isinstance(payload, dict):
        return None
    val = _pick(payload, keys)
    if val is not None:
        return val
    for v in payload.values():
        if isinstance(v, dict):
            found = _deep_find(v, keys, depth - 1)
            if found is not None:
                return found
    return None


def normalize_video_metric(raw: dict) -> dict:
    """把接口返回的作品指标统一成 dy.video_metric 的列。"""
    if not isinstance(raw, dict):
        return {}
    stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else raw
    out: dict[str, Any] = {}
    for canon, aliases in VIDEO_ALIASES.items():
        val = _pick(stats, aliases)
        if val is None:
            val = _deep_find(stats, aliases)
        if canon.endswith("_rate"):
            out[canon] = to_ratio(val)
        elif canon == "avg_play_duration_ms":
            ms = to_int(val)
            # 明确带了 _s 后缀的按秒处理
            if ms is not None and any(a.endswith("_s") and a in stats for a in aliases):
                ms = ms * 1000
            out[canon] = ms
        else:
            out[canon] = to_int(val)
    out["interaction_rate"] = interaction_rate(out)
    return out


def normalize_account_metric(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for canon, aliases in ACCOUNT_ALIASES.items():
        val = _deep_find(raw, aliases)
        out[canon] = to_int(val) if canon != "video_count" else to_int(val)
    return out


def interaction_rate(stats: dict) -> float | None:
    """互动率 = (赞+评+转+藏) / 播放。播放为 0/缺失、或互动字段全缺失时返回 None。"""
    play = stats.get("play_count")
    if not play:
        return None
    keys = ("like_count", "comment_count", "share_count", "collect_count")
    parts = [stats.get(k) for k in keys]
    if all(p is None for p in parts):
        return None          # “没拿到互动数据” 不等于 “0 互动”
    return round(sum(p or 0 for p in parts) / play, 5)


def collection_rate(stats: dict) -> float | None:
    """收藏率单看，是“内容有用”的直接信号。"""
    play = stats.get("play_count")
    if not play:
        return None
    return round((stats.get("collect_count") or 0) / play, 5)


def parse_topics(*texts: str | None) -> list[str]:
    seen: list[str] = []
    for text in texts:
        if not text:
            continue
        for tag in TOPIC_RE.findall(text):
            tag = tag.strip()
            if tag and tag not in seen:
                seen.append(tag)
    return seen[:20]


def parse_epoch(value: Any) -> datetime | None:
    """抖音时间字段多为秒级时间戳（也可能是毫秒）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        # 尝试 ISO 字符串
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if num > 10**11:      # 毫秒
        num = num // 1000
    if num <= 0:
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=num)
    except (OverflowError, ValueError):
        return None


def parse_video_item(item: dict) -> VideoMeta | None:
    """video_list 的一条 -> VideoMeta。"""
    if not isinstance(item, dict):
        return None
    item_id = item.get("item_id") or item.get("itemId") or item.get("aweme_id")
    if not item_id:
        return None
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    cover = item.get("cover") or video.get("cover") or item.get("cover_url")
    if isinstance(cover, dict):
        urls = cover.get("url_list") or cover.get("urls") or []
        cover = urls[0] if urls else None
    duration_raw = item.get("duration") or video.get("duration") or item.get("duration_ms")
    duration_sec = None
    if duration_raw is not None:
        dur = to_int(duration_raw)
        if dur is not None:
            duration_sec = dur // 1000 if dur > 1000 else dur
    title = item.get("title") or item.get("desc")
    description = item.get("description") or item.get("desc") or item.get("title")
    return VideoMeta(
        item_id=str(item_id),
        title=title,
        description=description,
        cover_url=str(cover) if cover else None,
        share_url=item.get("share_url") or item.get("shareUrl"),
        create_time=parse_epoch(item.get("create_time") or item.get("createTime")),
        duration_sec=duration_sec,
        media_type=str(item.get("media_type") or item.get("mediaType") or "") or None,
        is_top=bool(item.get("is_top") or item.get("isTop") or False),
        topics=parse_topics(title, description),
        raw=item,
    )


def normalize_fan_profile(payload: dict) -> dict[str, list[dict]]:
    """粉丝画像 -> {dimension: [{bucket, pct}]}，维度名做归一。"""
    if not isinstance(payload, dict):
        return {}
    dims: dict[str, list[dict]] = {}
    key_map = {
        "gender": "gender", "gender_distribution": "gender", "sex": "gender",
        "age": "age", "age_distribution": "age", "age_range": "age",
        "region": "region", "city": "region", "province": "region", "geo": "region",
        "interest": "interest", "interests": "interest", "tags": "interest",
        "device": "device", "device_brand": "device",
    }
    for key, value in payload.items():
        dim = key_map.get(str(key).lower())
        if not dim:
            continue
        buckets = _buckets(value)
        if buckets:
            dims[dim] = buckets
    return dims


def _buckets(value: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            label = (item.get("bucket") or item.get("name") or item.get("label")
                     or item.get("key") or item.get("tag"))
            pct = (item.get("pct") if item.get("pct") is not None else
                   item.get("rate") if item.get("rate") is not None else
                   item.get("ratio") if item.get("ratio") is not None else item.get("value"))
            ratio = to_ratio(pct)
            if label and ratio is not None:
                out.append({"bucket": str(label), "pct": ratio})
    elif isinstance(value, dict):
        for label, pct in value.items():
            if isinstance(pct, dict):
                pct = _pick(pct, ("pct", "rate", "ratio", "value"))
            ratio = to_ratio(pct)
            if ratio is not None:
                out.append({"bucket": str(label), "pct": ratio})
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out[:10]
