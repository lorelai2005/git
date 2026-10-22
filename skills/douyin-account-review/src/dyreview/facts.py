"""复盘事实包 + 规则引擎诊断。

这是整个 Skill 的“防幻觉地基”：
    * 所有数字先在这里算好，大模型只负责组织语言；
    * 诊断结论由确定性规则给出（阈值可配），大模型不得新增结论；
    * 同时产出 `numbers` 白名单，报告渲染阶段用它校验模型有没有编数字。

一句话：**数据归数据，文采归模型。**
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config
from .errors import DataUnavailable
from .logging_util import to_utc, utcnow
from .models import Account, ReviewWindow
from .repo import Repo

log = logging.getLogger(__name__)

DURATION_BUCKETS = ((0, 15, "<15s"), (15, 30, "15-30s"), (30, 60, "30-60s"), (60, 10**6, ">60s"))


@dataclass
class Thresholds:
    finish_good: float = 0.25
    finish_warn: float = 0.15
    interact_good: float = 0.05
    interact_warn: float = 0.02
    play_drop_warn: float = -0.30
    follower_growth_warn: float = 0.0
    publish_gap_days_warn: int = 7
    low_new_video_ratio_warn: float = 0.30

    @classmethod
    def from_config(cls, cfg: Config) -> "Thresholds":
        return cls(
            finish_good=cfg.get_float("review.thresholds.finish_rate_good", 0.25),
            finish_warn=cfg.get_float("review.thresholds.finish_rate_warn", 0.15),
            interact_good=cfg.get_float("review.thresholds.interaction_rate_good", 0.05),
            interact_warn=cfg.get_float("review.thresholds.interaction_rate_warn", 0.02),
            play_drop_warn=cfg.get_float("review.thresholds.play_drop_warn", -0.30),
            follower_growth_warn=cfg.get_float("review.thresholds.follower_growth_warn", 0.0),
            publish_gap_days_warn=cfg.get_int("review.thresholds.publish_gap_days_warn", 7),
            low_new_video_ratio_warn=cfg.get_float("review.thresholds.low_new_video_ratio_warn", 0.30),
        )


# =====================================================================
# 小工具
# =====================================================================
def _r(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _div(a: Any, b: Any) -> float | None:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if b == 0:
        return None
    return a / b


def _delta(end: Any, before: Any) -> int | None:
    """累计值 -> 窗口增量。窗口前没有快照（新作品）时按全量计入。"""
    if end is None:
        return None
    end_i = int(end)
    before_i = int(before) if before is not None else 0
    return max(end_i - before_i, 0)


def fmt_count(value: Any) -> str:
    """数字 -> 中文可读：12345 -> 1.23万"""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(num) >= 1e8:
        return f"{num / 1e8:.2f}亿"
    if abs(num) >= 1e4:
        return f"{num / 1e4:.2f}万"
    if num == int(num):
        return str(int(num))
    return f"{num:.2f}"


def fmt_pct(value: Any, digits: int = 1, signed: bool = False) -> str:
    if value is None:
        return "—"
    try:
        num = float(value) * 100
    except (TypeError, ValueError):
        return "—"
    sign = "+" if signed and num > 0 else ""
    return f"{sign}{num:.{digits}f}%"


def _short(text: Any, limit: int = 28) -> str:
    s = (str(text) if text is not None else "").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _weighted_rate(rows: list[dict], rate_key: str, weight_key: str) -> float | None:
    total_w = 0.0
    acc = 0.0
    for row in rows:
        rate = row.get(rate_key)
        weight = row.get(weight_key) or 0
        if rate is None or weight <= 0:
            continue
        acc += float(rate) * float(weight)
        total_w += float(weight)
    return _r(acc / total_w) if total_w > 0 else None


# =====================================================================
# 窗口聚合
# =====================================================================
def _aggregate(growth: list[dict], series: list[dict], published: list[dict],
               days: int) -> dict:
    plays = sum(_delta(r.get("play_end"), r.get("play_before")) or 0 for r in growth)
    likes = sum(_delta(r.get("like_end"), r.get("like_before")) or 0 for r in growth)
    comments = sum(_delta(r.get("comment_end"), r.get("comment_before")) or 0 for r in growth)
    shares = sum(_delta(r.get("share_end"), r.get("share_before")) or 0 for r in growth)
    collects = sum(_delta(r.get("collect_end"), r.get("collect_before")) or 0 for r in growth)
    interactions = likes + comments + shares + collects

    followers = [r for r in series if r.get("follower_count") is not None]
    followers_start = followers[0]["follower_count"] if followers else None
    followers_end = followers[-1]["follower_count"] if followers else None

    enriched = []
    for row in growth:
        play_delta = _delta(row.get("play_end"), row.get("play_before")) or 0
        likes_delta = _delta(row.get("like_end"), row.get("like_before")) or 0
        comments_delta = _delta(row.get("comment_end"), row.get("comment_before")) or 0
        shares_delta = _delta(row.get("share_end"), row.get("share_before")) or 0
        collects_delta = _delta(row.get("collect_end"), row.get("collect_before")) or 0
        enriched.append({
            **row,
            "play_delta": play_delta,
            "like_delta": likes_delta,
            "comment_delta": comments_delta,
            "share_delta": shares_delta,
            "collect_delta": collects_delta,
            "interaction_delta": likes_delta + comments_delta + shares_delta + collects_delta,
            "engagement_rate": _r(_div(likes_delta + comments_delta + shares_delta + collects_delta,
                                       play_delta)),
        })

    return {
        "days": days,
        "plays": plays,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "collects": collects,
        "interactions": interactions,
        "engagement_rate": _r(_div(interactions, plays)),
        "finish_rate": _weighted_rate(growth, "finish_rate", "play_end"),
        "bounce_rate": _weighted_rate(growth, "bounce_rate", "play_end"),
        "cover_click_rate": _weighted_rate(growth, "cover_click_rate", "play_end"),
        "publish_count": len(published),
        "followers_start": followers_start,
        "followers_end": followers_end,
        "followers_net": (int(followers_end) - int(followers_start))
        if followers_start is not None and followers_end is not None else None,
        "new_followers": sum(int(r.get("new_follower_count") or 0) for r in series) or None,
        "unfollowers": sum(int(r.get("unfollower_count") or 0) for r in series) or None,
        "total_favorited": next((int(r["total_favorited"]) for r in reversed(series)
                                 if r.get("total_favorited") is not None), None),
        "avg_plays_per_video": _r(_div(plays, len(growth)), 1) if growth else None,
        "_rows": enriched,
    }


def _window_shift(window: ReviewWindow) -> ReviewWindow:
    days = window.days
    prev_end = window.start - timedelta(days=1)
    return ReviewWindow(prev_end - timedelta(days=days - 1), prev_end)


# =====================================================================
# 主构建
# =====================================================================
def build_facts(repo: Repo, account: Account, window: ReviewWindow, cfg: Config, *,
                now: datetime | None = None, include_diagnosis: bool = True) -> dict:
    now = to_utc(now) or utcnow()
    th = Thresholds.from_config(cfg)

    cur_series = repo.account_metric_range(account.id, window.start, window.end)
    prev_win = _window_shift(window)
    prev_series = repo.account_metric_range(account.id, prev_win.start, prev_win.end)
    growth = repo.video_window_growth(account.id, window.start, window.end)
    prev_growth = repo.video_window_growth(account.id, prev_win.start, prev_win.end)
    published = repo.videos_published_between(account.id, window.start, window.end)
    all_videos = repo.latest_video_metrics(account.id)
    fresh = repo.data_freshness(account.id)
    fan_rows = repo.latest_fan_profile(account.id)

    if not cur_series and not growth:
        raise DataUnavailable(
            f"账号 {account.account_name}(#{account.id}) 在 "
            f"{window.start}~{window.end} 无任何指标快照，请先执行 collect。")

    cur = _aggregate(growth, cur_series, published, window.days)
    prev = _aggregate(prev_growth, prev_series,
                      repo.videos_published_between(account.id, prev_win.start, prev_win.end),
                      prev_win.days)

    meta_map = {str(v["item_id"]): v for v in all_videos}
    rows = cur.pop("_rows")
    prev_rows = prev.pop("_rows")

    videos = _video_section(rows, meta_map, published, cur)
    structure = _structure_section(rows, meta_map, published)
    audience = _audience_section(fan_rows)
    funnel = {
        "cover_click_rate": cur.get("cover_click_rate"),
        "finish_rate": cur.get("finish_rate"),
        "full_play_rate": _weighted_rate(growth, "full_play_rate", "play_end"),
        "bounce_rate": cur.get("bounce_rate"),
        "engagement_rate": cur.get("engagement_rate"),
        "follow_conversion": _r(_div(cur.get("followers_net") or 0, cur.get("plays") or 0), 6),
    }

    facts: dict = {
        "meta": {
            "account_id": account.id,
            "account_name": account.account_name,
            "douyin_id": account.douyin_id,
            "nickname": account.nickname,
            "open_id": account.open_id,
            "org_id": account.org_id,
            "period": window.to_dict(),
            "previous_period": prev_win.to_dict(),
            "generated_at": now.isoformat(),
            "timezone": cfg.timezone,
            "data_freshness": {
                "last_collect_at": _iso(fresh.get("last_collect_at")),
                "last_success_run_at": _iso(fresh.get("last_success_run_at")),
                "latest_account_stat_date": _iso(fresh.get("latest_account_stat_date")),
                "latest_video_stat_date": _iso(fresh.get("latest_video_stat_date")),
                "video_total": fresh.get("video_total"),
            },
        },
        "kpi": {
            "current": cur,
            "previous": prev,
            "delta": _compare(cur, prev),
        },
        "funnel": funnel,
        "videos": videos,
        "structure": structure,
        "audience": audience,
        "data_gaps": [],
    }

    facts["data_gaps"] = _data_gaps(facts, growth, all_videos, fan_rows)
    facts["diagnosis"] = _diagnose(facts, th, account) if include_diagnosis else []
    facts["thresholds"] = {
        "finish_rate_good": th.finish_good, "finish_rate_warn": th.finish_warn,
        "interaction_rate_good": th.interact_good, "interaction_rate_warn": th.interact_warn,
        "play_drop_warn": th.play_drop_warn, "publish_gap_days_warn": th.publish_gap_days_warn,
    }
    facts["display"] = _display(facts)
    facts["numbers"] = collect_numbers(facts)
    return facts


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _compare(cur: dict, prev: dict) -> dict:
    def rel(a, b):
        if a is None or b in (None, 0):
            return None
        return _r((float(a) - float(b)) / float(b))

    return {
        "plays_pct": rel(cur.get("plays"), prev.get("plays")),
        "interactions_pct": rel(cur.get("interactions"), prev.get("interactions")),
        "engagement_rate_diff": _r((cur.get("engagement_rate") or 0)
                                   - (prev.get("engagement_rate") or 0))
        if cur.get("engagement_rate") is not None else None,
        "followers_net_diff": (cur.get("followers_net") - prev.get("followers_net"))
        if cur.get("followers_net") is not None and prev.get("followers_net") is not None else None,
        "publish_count_diff": cur.get("publish_count", 0) - prev.get("publish_count", 0),
    }


def _video_section(rows: list[dict], meta_map: dict, published: list[dict], cur: dict) -> dict:
    total_plays = sum(r["play_delta"] for r in rows) or 0

    def enrich(row: dict) -> dict:
        meta = meta_map.get(str(row["item_id"]), {})
        return {
            "item_id": str(row["item_id"]),
            "title": _short(meta.get("title") or meta.get("description")),
            "publish_date": _iso(meta.get("create_time"))[:10] if meta.get("create_time") else None,
            "duration_sec": meta.get("duration_sec"),
            "plays": row["play_delta"],
            "likes": row["like_delta"],
            "comments": row["comment_delta"],
            "shares": row["share_delta"],
            "collects": row["collect_delta"],
            "engagement_rate": row["engagement_rate"],
            "finish_rate": _r(row.get("finish_rate")),
            "play_share": _r(_div(row["play_delta"], total_plays)),
        }

    enriched = [enrich(r) for r in rows]
    # 播放太小的作品不参与“最差”排名，避免 0 播放噪声
    ranked = [e for e in enriched if e["plays"] > 0]
    top = sorted(ranked, key=lambda x: x["plays"], reverse=True)[:5]
    bottom = sorted([e for e in ranked if e["plays"] >= 1], key=lambda x: x["plays"])[:3]
    best_engage = sorted([e for e in ranked if (e["engagement_rate"] or 0) > 0],
                         key=lambda x: x["engagement_rate"] or 0, reverse=True)[:3]

    new_video_ids = {str(v["item_id"]) for v in published}
    new_videos = [e for e in enriched if e["item_id"] in new_video_ids]
    new_videos.sort(key=lambda x: x["plays"], reverse=True)

    concentration = top[0]["play_share"] if top else None
    return {
        "window_total_plays": total_plays,
        "active_video_count": len(ranked),
        "top": top,
        "bottom": bottom,
        "best_engagement": best_engage,
        "new_videos": new_videos[:10],
        "new_video_plays": sum(e["plays"] for e in new_videos),
        "top1_play_share": concentration,
        "publish_gap_days": _publish_gap_days(published),
        "latest_publish_date": max((_iso(v.get("create_time"))[:10]
                                    for v in published if v.get("create_time")),
                                   default=None),
    }


def _publish_gap_days(published: list[dict]) -> int | None:
    dates = [v.get("create_time") for v in published if v.get("create_time")]
    if not dates:
        return None
    latest = max(to_utc(d) for d in dates if to_utc(d) is not None)  # type: ignore[arg-type]
    return (utcnow() - latest).days


def _structure_section(rows: list[dict], meta_map: dict, published: list[dict]) -> dict:
    buckets = []
    for low, high, label in DURATION_BUCKETS:
        plays = 0
        count = 0
        for row in rows:
            meta = meta_map.get(str(row["item_id"]), {})
            dur = meta.get("duration_sec")
            if dur is None:
                continue
            if low <= int(dur) < high:
                plays += row["play_delta"]
                count += 1
        buckets.append({"bucket": label, "count": count,
                        "plays": plays, "avg_plays": _r(_div(plays, count), 1)})

    hour_stats: dict[int, dict] = {}
    for row in rows:
        meta = meta_map.get(str(row["item_id"]), {})
        created = to_utc(meta.get("create_time")) if meta.get("create_time") else None
        if created is None:
            continue
        hour = created.hour
        slot = hour_stats.setdefault(hour, {"hour": hour, "count": 0, "plays": 0})
        slot["count"] += 1
        slot["plays"] += row["play_delta"]
    hours = sorted(hour_stats.values(), key=lambda x: x["plays"], reverse=True)[:3]

    topic_stats: dict[str, dict] = {}
    for row in rows:
        meta = meta_map.get(str(row["item_id"]), {})
        for tag in (meta.get("topics") or []):
            slot = topic_stats.setdefault(tag, {"topic": tag, "count": 0, "plays": 0})
            slot["count"] += 1
            slot["plays"] += row["play_delta"]
    topics = sorted(topic_stats.values(), key=lambda x: x["plays"], reverse=True)[:5]

    return {
        "duration_buckets": buckets,
        "top_publish_hours": hours,
        "top_topics": topics,
        "publish_count": len(published),
    }


def _audience_section(fan_rows: list[dict]) -> dict:
    out: dict[str, Any] = {}
    for row in fan_rows:
        dim = row.get("dimension")
        payload = row.get("payload") or []
        if isinstance(payload, dict):
            payload = [{"bucket": k, "pct": v} for k, v in payload.items()]
        out[str(dim)] = {
            "stat_date": _iso(row.get("stat_date")),
            "top": payload[:3] if isinstance(payload, list) else [],
        }
    return out


def _data_gaps(facts: dict, growth: list[dict], all_videos: list[dict],
               fan_rows: list[dict]) -> list[str]:
    gaps: list[str] = []
    fresh = facts["meta"]["data_freshness"]
    latest = fresh.get("latest_video_stat_date") or fresh.get("latest_account_stat_date")
    if not latest:
        gaps.append("没有任何指标快照")
    else:
        try:
            latest_date = date.fromisoformat(str(latest)[:10])
            lag = (utcnow().date() - latest_date).days
            if lag >= 3:
                gaps.append(f"最新指标快照距今 {lag} 天，可能采集断更")
        except ValueError:
            pass
    if not all_videos:
        gaps.append("作品维度无数据")
    if not fan_rows:
        gaps.append("缺少粉丝画像数据（可能未授权数据权限或未到采集周期）")
    if facts["funnel"].get("cover_click_rate") is None:
        gaps.append("缺少封面点击率（该指标需要额外数据权限）")
    if not growth:
        gaps.append("窗口内无作品指标增量")
    return gaps


# =====================================================================
# 规则引擎
# =====================================================================
def _diagnose(facts: dict, th: Thresholds, account: Account) -> list[dict]:
    out: list[dict] = []
    cur = facts["kpi"]["current"]
    prev = facts["kpi"]["previous"]
    delta = facts["kpi"]["delta"]
    videos = facts["videos"]
    funnel = facts["funnel"]

    def add(code: str, level: str, title: str, evidence: dict, suggestion: str) -> None:
        out.append({"code": code, "level": level, "title": title,
                    "evidence": evidence, "suggestion": suggestion})

    # 1) 数据新鲜度：只认“真的没有/真的断更”，不要被“缺封面点击率”之类的小缺口触发
    stale_gaps = [g for g in facts["data_gaps"]
                  if g.startswith("最新指标快照距今") or g in ("没有任何指标快照", "作品维度无数据")]
    if stale_gaps:
        add("DATA_STALE", "critical", "数据不完整或已断更",
            {"data_gaps": stale_gaps},
            "先修复采集链路（token 是否失效、定时任务是否在跑），再基于完整数据做决策。")

    # 2) 播放走势
    plays_pct = delta.get("plays_pct")
    if plays_pct is not None and plays_pct <= th.play_drop_warn:
        add("PLAY_DROP", "critical", "播放量明显下滑",
            {"current": cur.get("plays"), "previous": prev.get("plays"),
             "change_pct": plays_pct},
            "优先排查选题与开头 3 秒：把上周期表现最好的钩子结构复用到新选题，"
            "暂停与数据下滑期同时发布的同类内容。")

    # 3) 完播率
    finish = funnel.get("finish_rate")
    if finish is not None and finish < th.finish_warn:
        add("FINISH_RATE_LOW", "warn", "完播率低于健康线",
            {"finish_rate": finish, "warn_line": th.finish_warn,
             "good_line": th.finish_good},
            "压缩前置铺垫：把核心结论/冲突前置到 3 秒内，"
            "单条时长控制在 15-30 秒，结尾砍掉无效自述。")
    elif finish is not None and finish < th.finish_good:
        add("FINISH_RATE_MID", "info", "完播率处于中位区间",
            {"finish_rate": finish, "good_line": th.finish_good},
            "已接近健康线，可做 A/B 测试：同一选题出 2 版开头，只换前 3 秒，看哪个完播更高。")

    # 4) 互动率
    engage = funnel.get("engagement_rate")
    if engage is not None and engage < th.interact_warn:
        add("INTERACTION_LOW", "warn", "互动率偏低",
            {"engagement_rate": engage, "warn_line": th.interact_warn},
            "增加“可反驳观点/可代入提问”，并把评论区高赞问题做成下一条选题；"
            "结尾明确引导收藏（收藏率是内容有用度的直接信号）。")
    elif engage is not None and engage >= th.interact_good:
        add("INTERACTION_GOOD", "info", "互动率健康",
            {"engagement_rate": engage, "good_line": th.interact_good},
            "当前互动结构有效，把这条内容的形式固化为模板，批量复制同类选题。")

    # 5) 涨粉
    net = cur.get("followers_net")
    if net is not None and net <= th.follower_growth_warn:
        add("FOLLOWER_STALL", "warn", "粉丝净增停滞或下降",
            {"net_change": net, "new_followers": cur.get("new_followers"),
             "unfollowers": cur.get("unfollowers")},
            "强化“账号身份”而非单条内容：在视频结尾用一句话说明关注后能持续得到什么；"
            "检查近期是否发了与账号定位不符的内容导致取关。")

    # 6) 更新频率
    gap = videos.get("publish_gap_days")
    if gap is not None and gap >= th.publish_gap_days_warn:
        add("PUBLISH_GAP", "warn", "更新节奏断档",
            {"publish_gap_days": gap, "warn_line": th.publish_gap_days_warn},
            f"恢复稳定更新（建议每周 ≥3 条）：先用库存素材/口播补位，"
            f"避免超过 {th.publish_gap_days_warn} 天不发。")

    # 7) 内容集中度
    share = videos.get("top1_play_share")
    if share is not None and share >= 0.5 and (videos.get("active_video_count") or 0) >= 3:
        add("CONCENTRATION_RISK", "warn", "流量高度依赖单条作品",
            {"top1_play_share": share, "active_video_count": videos.get("active_video_count")},
            "把爆款的选题结构拆成可复用的公式（同钩子、换场景/换人群），"
            "用 3-5 条内容验证可复制性，降低单条依赖。")

    # 8) 新内容占比
    new_plays = videos.get("new_video_plays") or 0
    total_plays = videos.get("window_total_plays") or 0
    new_ratio = _div(new_plays, total_plays)
    if new_ratio is not None and total_plays > 0:
        if new_ratio < th.low_new_video_ratio_warn:
            add("OLD_CONTENT_DRIVEN", "info", "增量主要来自老作品",
                {"new_video_play_ratio": new_ratio,
                 "warn_line": th.low_new_video_ratio_warn},
                "老作品仍在带量说明选题有长尾能力，但账号增长要靠新内容："
                "把老爆款的评论区需求整理成新选题清单。")

    # 9) 无新内容但有流量
    if (cur.get("publish_count") or 0) == 0 and (cur.get("plays") or 0) > 0:
        add("NO_NEW_PUBLISH", "warn", "本周期没有发布新作品",
            {"publish_count": cur.get("publish_count"), "plays": cur.get("plays")},
            "把现有素材排期发出，并复用上周期数据最好的结构；"
            "同时用评论区问题反推选题，避免长期停更导致流量池掉级。")

    # 10) 粉丝画像缺失
    if not facts["audience"]:
        add("NO_AUDIENCE_DATA", "info", "缺少粉丝画像",
            {"audience": None},
            "补授权“数据开放服务”的粉丝画像权限，或先按内容评论区人工归纳人群，再校准选题。")

    if not out:
        add("HEALTHY", "info", "整体指标健康",
            {"plays": cur.get("plays"), "engagement_rate": engage,
             "finish_rate": finish},
            "维持当前节奏，把本周期表现最好的 2-3 条作品结构整理成模板，"
            "作为下周期的批量选题来源。")

    order = {"critical": 0, "warn": 1, "info": 2}
    out.sort(key=lambda x: order.get(x["level"], 3))
    return out


# =====================================================================
# 展示层与数字白名单
# =====================================================================
def _display(facts: dict) -> dict:
    cur = facts["kpi"]["current"]
    prev = facts["kpi"]["previous"]
    delta = facts["kpi"]["delta"]
    videos = facts["videos"]
    return {
        "period": f"{facts['meta']['period']['start']} ~ {facts['meta']['period']['end']}",
        "plays": fmt_count(cur.get("plays")),
        "plays_prev": fmt_count(prev.get("plays")),
        "plays_change": fmt_pct(delta.get("plays_pct"), signed=True),
        "likes": fmt_count(cur.get("likes")),
        "comments": fmt_count(cur.get("comments")),
        "shares": fmt_count(cur.get("shares")),
        "collects": fmt_count(cur.get("collects")),
        "engagement_rate": fmt_pct(cur.get("engagement_rate")),
        "finish_rate": fmt_pct(facts["funnel"].get("finish_rate")),
        "followers": fmt_count(cur.get("followers_end")),
        "followers_net": (f"{cur['followers_net']:+d}" if cur.get("followers_net") is not None
                          else "—"),
        "new_followers": fmt_count(cur.get("new_followers")),
        "publish_count": str(cur.get("publish_count") or 0),
        "publish_gap_days": str(videos.get("publish_gap_days") if videos.get("publish_gap_days") is not None else "—"),
        "top1_play_share": fmt_pct(videos.get("top1_play_share")),
        "avg_plays_per_video": fmt_count(cur.get("avg_plays_per_video")),
    }


NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(%|％|万|亿|w|W|k|K)?")
_UNIT_FACTOR = {"%": 0.01, "％": 0.01, "万": 1e4, "亿": 1e8,
                "w": 1e3, "W": 1e3, "k": 1e3, "K": 1e3}


def collect_numbers(obj: Any, out: set[float] | None = None) -> list[float]:
    """收集事实包里所有数字（含百分比/万/亿形态），作为报告数字校验的白名单。

    字符串里的数字也要收（作品标题里常出现 "10万装出30万效果" 这类数字，
    校验报告时引用了标题必须能通过），所以要带单位换算。
    """
    if out is None:
        out = set()

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            add_num(float(node))
            return
        if isinstance(node, str):
            for num_str, unit in NUM_RE.findall(node):
                try:
                    value = float(num_str) * _UNIT_FACTOR.get(unit or "", 1.0)
                except ValueError:
                    continue
                add_num(value)
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "numbers":
                    continue
                walk(v)
            return
        if isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    def add_num(num: float) -> None:
        out.add(round(num, 6))
        out.add(round(num * 100, 4))        # 0.2537 -> 25.37
        out.add(round(abs(num), 6))
        out.add(round(abs(num) * 100, 4))
        out.add(round(num, 1))
        out.add(round(num, 2))
        out.add(round(num * 100, 1))
        out.add(round(num * 100, 2))
        if num >= 1e4:
            out.add(round(num / 1e4, 2))    # 12.34万
            out.add(round(num / 1e4, 1))
        if num >= 1e8:
            out.add(round(num / 1e8, 2))

    walk(obj)
    out.discard(0.0)
    return sorted(out)
