"""报告层：prompt 构建 -> 大模型生成（或 Crabot 交接）-> 数字校验 -> Markdown 渲染。

三种运行形态，共用同一套事实包：

1. `handoff`（默认，Crabot 场景）
   `build_prompt()` 产出 prompt.md + facts.json 交给 Crabot 的大模型，
   Crabot 回传报告正文，再走 `validate_numbers()` + `render_markdown()`。
   好处：Skill 不持有大模型密钥，用哪个模型由 Crabot 决定。

2. `builtin`（独立运行/定时兜底）
   Skill 自己调 OpenAI 兼容接口（配置 llm.*），同样走校验和渲染。

3. `fallback`（大模型不可用）
   只用事实包生成一份纯数据报告，保证“至少给得出一份东西”，不空手。

数字校验是硬闸门：报告里出现事实包里没有的数字（超过 1% 偏差）就会被记为违规项，
`review.report.numbers_strict=true` 时拒绝推送并要求重写。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .config import Config
from .errors import LlmError
from .facts import fmt_count, fmt_pct
from .logging_util import json_dumps, utcnow

log = logging.getLogger(__name__)


# =====================================================================
# Prompt
# =====================================================================
SYSTEM_PROMPT = """你是仁维（Renwei）的抖音账号运营分析师，为短视频代运营客户做账号数据复盘。

【硬性规则】
1. 只能使用我提供的数据。**禁止编造任何数字**，禁止用行业经验补充"大约/通常/一般"的数值。
   数据里没有的指标，就直接写"暂无该维度数据"。
2. 每个结论后面必须能对应到一条证据（哪个指标、涨了多少、哪条作品）。
3. 不要复述我的数据字典，不要解释名词，直接给判断和建议。
4. 建议必须可执行：写清"做什么动作、改哪一部分、预期影响哪个指标"。
   不要出现"提升内容质量""加强运营"这种无法执行的话。
5. 输出纯 Markdown 正文，不要加一级标题（报告标题由系统生成），不要用代码块包裹。
6. 中文，专业但不啰嗦，总长度控制在 900-1400 字。
7. 如果数据存在缺口（data_gaps 非空），必须在"数据说明与风险"里明确说明
   本次结论的可信度限制。

【输出结构】严格按以下 6 个部分，标题用 `## `：
## 一、一句话结论
## 二、关键数据（本期 vs 上期）
## 三、问题诊断
## 四、内容侧优化动作
## 五、下周期执行清单
## 六、数据说明与风险
"""

USER_PROMPT_TEMPLATE = """## 报告对象
账号：{account_name}（抖音号 {douyin_id}）
统计周期：{period}；对比周期：{previous_period}

## 已算好的关键数据（直接引用，不要重算）
{display_block}

## 完整事实包（JSON，唯一数据来源）
```json
{facts_json}
```

## 规则引擎诊断（结论必须与之一致，可按你的判断补充解释，但不得新增无依据的结论）
{diagnosis_block}

## 数据缺口
{gaps_block}

请按系统提示的结构输出报告正文。
"""


def _display_block(facts: dict) -> str:
    disp = facts.get("display") or {}
    cur = facts["kpi"]["current"]
    prev = facts["kpi"]["previous"]
    lines = [
        f"- 本期播放：{disp.get('plays')}（上期 {disp.get('plays_prev')}，环比 {disp.get('plays_change')}）",
        f"- 本期点赞 / 评论 / 转发 / 收藏：{disp.get('likes')} / {disp.get('comments')} / "
        f"{disp.get('shares')} / {disp.get('collects')}",
        f"- 互动率：{disp.get('engagement_rate')}；完播率：{disp.get('finish_rate')}",
        f"- 粉丝总数：{disp.get('followers')}；本周期净增：{disp.get('followers_net')}；"
        f"新增粉丝：{disp.get('new_followers')}",
        f"- 本期发布：{disp.get('publish_count')} 条；单条平均播放：{disp.get('avg_plays_per_video')}",
        f"- 距最近一次发布：{disp.get('publish_gap_days')} 天；TOP1 作品播放占比：{disp.get('top1_play_share')}",
    ]
    if cur.get("cover_click_rate") is not None:
        lines.append(f"- 封面点击率：{fmt_pct(cur.get('cover_click_rate'))}")
    if cur.get("bounce_rate") is not None:
        lines.append(f"- 2 秒跳出率：{fmt_pct(cur.get('bounce_rate'))}")
    return "\n".join(lines)


def _diagnosis_block(facts: dict) -> str:
    rows = facts.get("diagnosis") or []
    if not rows:
        return "（无）"
    return "\n".join(
        f"- [{r['level']}] {r['title']}：证据 {json_dumps(r['evidence'])}；建议方向：{r['suggestion']}"
        for r in rows
    )


def _gaps_block(facts: dict) -> str:
    gaps = facts.get("data_gaps") or []
    return "\n".join(f"- {g}" for g in gaps) if gaps else "（无，数据完整）"


def _trim_facts(facts: dict) -> dict:
    """给大模型的事实包瘦身：去掉 numbers 白名单（几百个数字，纯噪声）。"""
    out = {k: v for k, v in facts.items() if k != "numbers"}
    videos = out.get("videos") or {}
    for key in ("top", "bottom", "best_engagement", "new_videos"):
        if isinstance(videos.get(key), list):
            videos[key] = videos[key][:5]
    return out


def build_prompt(facts: dict) -> str:
    meta = facts["meta"]
    return USER_PROMPT_TEMPLATE.format(
        account_name=meta["account_name"],
        douyin_id=meta.get("douyin_id") or "未登记",
        period=facts["display"]["period"],
        previous_period=f"{meta['previous_period']['start']} ~ {meta['previous_period']['end']}",
        display_block=_display_block(facts),
        facts_json=json.dumps(_trim_facts(facts), ensure_ascii=False, indent=1, default=str),
        diagnosis_block=_diagnosis_block(facts),
        gaps_block=_gaps_block(facts),
    )


@dataclass
class PromptPackage:
    review_id: int
    system: str
    user: str
    facts: dict

    @property
    def prompt_md(self) -> str:
        return f"{self.system}\n\n---\n\n{self.user}"

    def to_dict(self) -> dict:
        return {"review_id": self.review_id, "system": self.system, "user": self.user}


def build_package(review_id: int, facts: dict) -> PromptPackage:
    return PromptPackage(review_id=review_id, system=SYSTEM_PROMPT,
                         user=build_prompt(facts), facts=facts)


# =====================================================================
# 大模型调用（OpenAI 兼容）
# =====================================================================
@dataclass
class LlmResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"model": self.model, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "latency_ms": self.latency_ms}


def call_llm(prompt: PromptPackage, cfg: Config, *,
             transport: Callable[[str, dict, dict], dict] | None = None) -> LlmResult:
    """调用 OpenAI 兼容的 /chat/completions。transport 可注入（测试用）。"""
    base_url = cfg.require("llm.base_url", "大模型接口 base_url").rstrip("/")
    api_key = cfg.require("llm.api_key", "大模型 api_key")
    model = cfg.get("llm.model", "gpt-4o-mini")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "temperature": cfg.get_float("llm.temperature", 0.3),
        "max_tokens": cfg.get_int("llm.max_output_tokens", 3000),
    }
    url = f"{base_url}/chat/completions"
    payload = (transport(url, body, {"Authorization": f"Bearer {api_key}"})
               if transport else _post_json(url, body, {"Authorization": f"Bearer {api_key}"},
                                           cfg.get_float("llm.timeout_seconds", 120.0)))
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"大模型返回结构异常：{str(payload)[:300]}") from exc
    usage = payload.get("usage") or {}
    return LlmResult(
        text=text or "",
        model=str(payload.get("model") or model),
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        raw=payload,
    )


def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", **headers})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", errors="replace")[:500]
        raise LlmError(f"大模型接口 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LlmError(f"大模型接口不可达：{exc}") from exc
    log.debug("LLM 调用耗时 %.2fs", time.monotonic() - started)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmError(f"大模型返回非 JSON：{raw[:300]}") from exc


# =====================================================================
# 数字校验（反幻觉）
# =====================================================================
DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
URL_RE = re.compile(r"https?://\S+")
NUM_TOKEN_RE = re.compile(
    r"(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>%|％|万|亿|w|W|k|K|次|个|条|人|天|秒|分钟|小时|倍)?")
UNIT_FACTOR = {"%": 0.01, "％": 0.01, "万": 1e4, "亿": 1e8, "w": 1e3, "W": 1e3,
               "k": 1e3, "K": 1e3}


def _normalize_token(num: str, unit: str | None) -> float:
    value = float(num)
    factor = UNIT_FACTOR.get(unit or "", 1.0)
    return value * factor


def validate_numbers(report_md: str, facts: dict, *,
                     tolerance: float = 0.012, allow_small_int: int = 60) -> dict:
    """报告里的数字必须能在事实包里找到来源。

    允许：
      * 事实包里出现过的数值（含百分比/万/亿等换算形态），误差 ≤ tolerance；
      * 0~60 的纯整数（用于"第3条""5个建议""7天"这类结构性数字）。
    不允许：其余任何数字 —— 这些就是模型编的。
    """
    allowed = list(facts.get("numbers") or [])
    allowed_sorted = sorted(allowed)

    text = report_md or ""
    text = URL_RE.sub(" ", text)
    text = DATE_RE.sub(" ", text)
    text = TIME_RE.sub(" ", text)

    violations: list[dict] = []
    checked = 0
    for match in NUM_TOKEN_RE.finditer(text):
        num_str, unit = match.group("num"), match.group("unit")
        try:
            value = _normalize_token(num_str, unit)
        except ValueError:
            continue
        checked += 1
        if unit is None and "." not in num_str and abs(value) <= allow_small_int:
            continue          # 结构性小整数
        if _matches(value, allowed_sorted, tolerance):
            continue
        start = max(0, match.start() - 25)
        violations.append({
            "value": num_str + (unit or ""),
            "normalized": round(value, 6),
            "context": text[start:match.end() + 15].replace("\n", " "),
        })

    return {
        "ok": not violations,
        "checked": checked,
        "allowed_size": len(allowed),
        "violations": violations[:20],
        "violation_count": len(violations),
    }


def _matches(value: float, allowed_sorted: list[float], tolerance: float) -> bool:
    if not allowed_sorted:
        return False
    abs_value = abs(value)
    target = value
    # 二分找最近邻
    import bisect
    idx = bisect.bisect_left(allowed_sorted, target)
    for cand_idx in (idx - 1, idx, idx + 1):
        if 0 <= cand_idx < len(allowed_sorted):
            cand = allowed_sorted[cand_idx]
            if abs(cand - target) <= max(0.0105, abs(target) * tolerance):
                return True
    # 百分比/万分位符号可能被拆开匹配，做一次兜底
    idx = bisect.bisect_left(allowed_sorted, abs_value)
    for cand_idx in (idx - 1, idx, idx + 1):
        if 0 <= cand_idx < len(allowed_sorted):
            cand = allowed_sorted[cand_idx]
            if abs(cand - abs_value) <= max(0.0105, abs_value * tolerance):
                return True
    return False


# =====================================================================
# 渲染
# =====================================================================
FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*|\s*```\s*$", re.IGNORECASE)
PREAMBLE_RE = re.compile(r"^\s*(好的|以下是|这是|没问题)[^\n]{0,40}\n+")


def clean_llm_output(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.split("\n")
                         if not line.strip().startswith("```")).strip()
    text = PREAMBLE_RE.sub("", text, count=1)
    # 去掉模型自作多情的一级标题
    lines = text.split("\n")
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _kpi_table(facts: dict) -> str:
    cur, prev = facts["kpi"]["current"], facts["kpi"]["previous"]
    disp = facts["display"]

    def row(name: str, a: Any, b: Any, fmt=fmt_count) -> str:
        change = None
        try:
            if a is not None and b not in (None, 0):
                change = (float(a) - float(b)) / float(b)
        except (TypeError, ValueError):
            change = None
        return (f"| {name} | {fmt(a) if a is not None else '—'} | "
                f"{fmt(b) if b is not None else '—'} | "
                f"{fmt_pct(change, signed=True) if change is not None else '—'} |")

    lines = [
        "| 指标 | 本期 | 上期 | 变化 |",
        "| --- | --- | --- | --- |",
        row("播放量（窗口增量）", cur.get("plays"), prev.get("plays")),
        row("点赞", cur.get("likes"), prev.get("likes")),
        row("评论", cur.get("comments"), prev.get("comments")),
        row("转发", cur.get("shares"), prev.get("shares")),
        row("收藏", cur.get("collects"), prev.get("collects")),
        row("互动率", cur.get("engagement_rate"), prev.get("engagement_rate"), fmt_pct),
        row("完播率", facts["funnel"].get("finish_rate"), prev.get("finish_rate"), fmt_pct),
        row("粉丝净增", cur.get("followers_net"), prev.get("followers_net"),
            lambda v: f"{v:+d}" if isinstance(v, int) else str(v)),
        row("发布条数", cur.get("publish_count"), prev.get("publish_count")),
    ]
    lines.append(f"\n账号总粉丝：{disp.get('followers')}｜本周期新增粉丝：{disp.get('new_followers')}")
    return "\n".join(lines)


def _video_table(facts: dict) -> str:
    top = (facts.get("videos") or {}).get("top") or []
    if not top:
        return ""
    lines = ["| 作品 | 发布日 | 播放 | 互动率 | 完播率 | 播放占比 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for v in top:
        lines.append(
            f"| {v.get('title') or v.get('item_id')} | {v.get('publish_date') or '—'} | "
            f"{fmt_count(v.get('plays'))} | {fmt_pct(v.get('engagement_rate'))} | "
            f"{fmt_pct(v.get('finish_rate'))} | {fmt_pct(v.get('play_share'))} |")
    return "\n".join(lines)


def _diagnosis_lines(facts: dict) -> str:
    rows = facts.get("diagnosis") or []
    if not rows:
        return "（本周期未触发诊断规则）"
    icon = {"critical": "🔴", "warn": "🟠", "info": "🔵"}
    out = []
    for i, r in enumerate(rows, 1):
        out.append(f"{i}. {icon.get(r['level'], '•')} **{r['title']}** — {r['suggestion']}")
    return "\n".join(out)


def render_markdown(facts: dict, llm_text: str | None, cfg: Config) -> str:
    """把模型正文 + 数据附表拼成最终推送稿。llm_text 为空时走兜底报告。"""
    meta = facts["meta"]
    disp = facts["display"]
    body = clean_llm_output(llm_text or "")
    if not body:
        body = fallback_body(facts)

    header = (f"# {meta['account_name']} 账号数据复盘\n\n"
              f"**统计周期**：{disp['period']}　|　**对比周期**："
              f"{meta['previous_period']['start']} ~ {meta['previous_period']['end']}\n\n")

    appendix = [
        "\n\n---\n",
        "### 附：关键数据明细\n",
        _kpi_table(facts),
    ]
    video_table = _video_table(facts)
    if video_table:
        appendix += ["\n### 附：本周期作品表现（TOP5）\n", video_table]

    gaps = facts.get("data_gaps") or []
    footer = [
        "\n\n---\n",
        f"> 数据来源：抖音开放平台（仁维 Crabot 定时采集）　|　生成时间：{utcnow():%Y-%m-%d %H:%M} UTC",
        f"> 数据截止：{meta['data_freshness'].get('latest_account_stat_date') or '—'}",
        "> 口径：播放/互动为累计值的窗口增量；完播率、互动率按播放量加权。",
    ]
    if gaps:
        footer.append("> 数据缺口：" + "；".join(gaps))

    report = header + body + "\n".join(appendix) + "\n".join(footer)
    max_chars = cfg.get_int("review.report.max_chars", 12000)
    if len(report) > max_chars:
        report = report[:max_chars].rsplit("\n", 1)[0] + "\n\n> （内容超长已截断，完整版见 var/reviews）"
    return report


def fallback_body(facts: dict) -> str:
    """不依赖大模型的纯数据报告，保证链路永远有产出。"""
    disp = facts["display"]
    delta = facts["kpi"]["delta"]
    videos = facts.get("videos") or {}
    summary_bits = [f"本周期播放 {disp['plays']}"]
    if delta.get("plays_pct") is not None:
        summary_bits.append(f"环比 {disp['plays_change']}")
    summary_bits.append(f"互动率 {disp['engagement_rate']}")
    summary_bits.append(f"粉丝净增 {disp['followers_net']}")

    return f"""## 一、一句话结论

{'，'.join(summary_bits)}。本周期共发布 {disp['publish_count']} 条作品，\
单条平均播放 {disp['avg_plays_per_video']}，距最近一次发布 {disp['publish_gap_days']} 天。

## 二、关键数据（本期 vs 上期）

见下方明细表。完播率 {disp['finish_rate']}，TOP1 作品播放占比 {disp['top1_play_share']}。

## 三、问题诊断

{_diagnosis_lines(facts)}

## 四、内容侧优化动作

（本报告由数据兜底生成，未调用大模型。请人工补充内容侧动作建议，
或把本次 facts 交给 Crabot 大模型后重新渲染。）

## 五、下周期执行清单

- 依据诊断项的“建议方向”逐条排期，每条对应一个可量化目标（播放/完播率/互动率）。
- 保持更新节奏，避免断更超过 {facts['thresholds']['publish_gap_days_warn']} 天。

## 六、数据说明与风险

{_gaps_block(facts)}
"""


# =====================================================================
# 文件落地
# =====================================================================
def write_artifacts(var_dir, review_id: int, facts: dict, prompt_md: str,
                    report_md: str | None = None) -> dict:
    """把事实包/prompt/报告写成文件，便于 Crabot 交接与人工复查。"""
    from pathlib import Path
    base = Path(var_dir) / "reviews" / f"{review_id:08d}"
    base.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    facts_path = base / "facts.json"
    facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")
    paths["facts"] = str(facts_path)
    if prompt_md:
        prompt_path = base / "prompt.md"
        prompt_path.write_text(prompt_md, encoding="utf-8")
        paths["prompt"] = str(prompt_path)
    if report_md:
        report_path = base / "report.md"
        report_path.write_text(report_md, encoding="utf-8")
        paths["report"] = str(report_path)
    return paths
