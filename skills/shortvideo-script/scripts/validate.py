# -*- coding: utf-8 -*-
"""validate.py — 生成前的质量闸门。

检查项（errors 会阻断出片，warnings 只提示）：
  S 结构     必填字段、拍摄执行表四列齐全、每段非空
  H 钩子     开头钩子必须等于完整口播的第一段（样例的硬规则）
  T 时间轴   0 起、首尾相接、递增、行数与时长匹配
  F 事实     文案里出现的数字必须能在客户已确认事实里找到（防虚构）
  C 合规     广告法绝对化用语、客户禁用语、未确认表达
  L 口播长度 按 5 字/秒 估算，与时间轴总长偏差 >35% 报警
  G 发布标签 至少 3 个、以 # 开头、含品牌词

用法: python validate.py payload.json --client shuqingzheyi [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

CHARS_PER_SEC = 5.0
TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[—–\-~至]\s*(\d+(?:\.\d+)?)\s*秒?\s*$")

ABSOLUTE_WORDS = ["最好", "最佳", "最低", "最高", "最强", "全网最",
                  "第一", "唯一", "顶级", "国家级", "世界级", "绝无仅有",
                  "100%", "百分百", "保证", "绝对", "永久", "根治", "秒杀全网"]
# 这些词在正常中文里也有合法用法，需要用上下文排除（如「第一种风格」「我最想做的事情」）
AMBIGUOUS_WORDS = {
    "第一": re.compile(r"第一(?![种眼件次步时间印象天条口])"),
    "最好": re.compile(r"最好(?![的看是天])"),
}
DEFAULT_NEED_PROOF = ["高级定制", "限量", "售罄", "爆款"]


def _num_eq(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-6
    except Exception:
        return a == b


def parse_time(label):
    m = TIME_RE.match(str(label))
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def hit_banned(text, word):
    """返回是否命中；AMBIGUOUS_WORDS 里的词按上下文正则判断，避免误伤正常中文。"""
    pat = AMBIGUOUS_WORDS.get(word)
    if pat is not None:
        return bool(pat.search(text))
    return word in text


def check(payload, profile=None):
    errors, warnings, notes = [], [], []

    meta = payload.get("meta") or {}
    for key, label in (("title", "标题"), ("platform", "适用平台"),
                       ("audience", "目标受众"), ("common_goal", "共同目标"),
                       ("unified_facts", "统一事实")):
        if not str(meta.get(key, "")).strip():
            errors.append({"code": "S1", "msg": f"meta.{key}（{label}）为空"})

    scripts = payload.get("scripts") or []
    if not scripts:
        errors.append({"code": "S2", "msg": "没有任何脚本（scripts 为空）"})

    brand = (profile or {}).get("name", "")
    confirmed_text = " ".join(
        str(f.get("value", "")) for f in (profile or {}).get("facts", [])
        if f.get("status") == "confirmed")
    confirmed_digits = lib.digits_in(confirmed_text)

    banned = list(ABSOLUTE_WORDS) + list((profile or {}).get("banned_claims", []))
    if profile:
        banned += [w for w in (profile.get("tone") or {}).get("avoid", [])]
    banned = [w for w in dict.fromkeys(banned) if w]

    total_seconds = 0.0
    for idx, s in enumerate(scripts, 1):
        tag = f"{s.get('no') or idx}"
        for key, label in (("no", "序号"), ("title", "脚本标题"), ("structure", "内容结构"),
                           ("hook", "开头钩子"), ("voiceover", "完整口播"),
                           ("shoot", "拍摄执行"), ("tags", "发布标签")):
            if not s.get(key):
                errors.append({"code": "S3", "msg": f"脚本{tag}缺少 {label}"})

        vo = [str(x) for x in (s.get("voiceover") or [])]
        hook = str(s.get("hook") or "")
        if vo and hook:
            a, b = lib.norm(hook), lib.norm(vo[0])
            if not a or not b:
                pass
            elif a in b or b in a:
                pass
            else:
                ta, tb = lib.tokenize(a), lib.tokenize(b)
                ratio = (len(ta & tb) / len(ta)) if ta else 0.0
                if ratio >= 0.6:
                    warnings.append({"code": "H2",
                                     "msg": f"脚本{tag}开头钩子是口播首句的压缩版（重合 {ratio:.0%}），"
                                            f"拍摄以口播首句为准，字幕可用钩子版",
                                     "hook": hook[:40], "first": vo[0][:40]})
                else:
                    errors.append({"code": "H1",
                                   "msg": f"脚本{tag}开头钩子与口播首段不一致（重合 {ratio:.0%}），"
                                          f"观众听到的和看到的对不上",
                                   "hook": hook[:40], "first": vo[0][:40]})
        if vo and any(len(x.strip()) < 8 for x in vo):
            warnings.append({"code": "L3", "msg": f"脚本{tag}存在过短的口播段，拍摄时可能接不上"})

        # ---- 时间轴
        shoot = s.get("shoot") or []
        rows, prev_end = [], None
        for r_i, row in enumerate(shoot, 1):
            for key, label in (("time", "时间"), ("picture", "具体视频画面"),
                               ("audio", "口播/同期声"), ("note", "执行要点")):
                if not str(row.get(key, "")).strip():
                    errors.append({"code": "S4", "msg": f"脚本{tag}拍摄执行第 {r_i} 行「{label}」为空"})
            if len(str(row.get("note", "")).strip()) < 8:
                warnings.append({"code": "S5",
                                 "msg": f"脚本{tag}第 {r_i} 行执行要点过短，写不出可执行约束"})
            t = parse_time(row.get("time"))
            if t is None:
                errors.append({"code": "T1", "msg": f"脚本{tag}第 {r_i} 行时间格式无法识别：{row.get('time')}"})
                continue
            st, en = t
            if en <= st:
                errors.append({"code": "T2", "msg": f"脚本{tag}第 {r_i} 行结束时间不大于开始时间"})
            if prev_end is None:
                if not _num_eq(st, 0):
                    errors.append({"code": "T3", "msg": f"脚本{tag}时间轴未从 0 秒开始（当前 {st}）"})
            elif not _num_eq(st, prev_end):
                errors.append({"code": "T4",
                               "msg": f"脚本{tag}第 {r_i} 行与上一行不连续：{prev_end} → {st}"})
            prev_end = en
            rows.append((st, en))

        if rows:
            dur = rows[-1][1]
            total_seconds += dur
            chars = sum(len(x) for x in vo)
            estimate = chars / CHARS_PER_SEC
            if estimate and abs(estimate - dur) / dur > 0.35:
                warnings.append({"code": "L1",
                                 "msg": f"脚本{tag}口播约 {estimate:.0f} 秒，时间轴 {dur:.0f} 秒，偏差较大",
                                 "chars": chars})
            if dur < 25:
                warnings.append({"code": "L2", "msg": f"脚本{tag}总长 {dur:.0f} 秒，抖音信息量可能不足"})
            if dur > 90:
                warnings.append({"code": "L2", "msg": f"脚本{tag}总长 {dur:.0f} 秒，建议拆条或压缩"})

        # ---- 事实数字
        if profile:
            body_text = " ".join([str(s.get("structure", "")), hook] + vo
                                 + [str(r.get("picture", "")) + " " + str(r.get("audio", ""))
                                    + " " + str(r.get("note", "")) for r in shoot])
            time_digits = set()
            for r in shoot:
                time_digits |= lib.digits_in(r.get("time", ""))
            unknown = lib.digits_in(body_text) - confirmed_digits - time_digits
            for d in sorted(unknown):
                errors.append({"code": "F1",
                               "msg": f"脚本{tag}出现未在客户事实库确认的数字「{d}」",
                               "hint": "先在 profile.json 里补 facts 并标 confirmed，或改写文案"})

        # ---- 合规
        full = " ".join([str(s.get("title", "")), str(s.get("structure", "")), hook]
                        + vo + [str(r.get("audio", "")) for r in shoot]
                        + [str(r.get("picture", "")) for r in shoot])
        for w in banned:
            if w and hit_banned(full, w):
                errors.append({"code": "C1", "msg": f"脚本{tag}命中禁用/待证实表达「{w}」"})

        # ---- 标签
        tags = str(s.get("tags", ""))
        tag_list = [t for t in re.split(r"[\s，,]+", tags) if t]
        if len(tag_list) < 3:
            warnings.append({"code": "G1", "msg": f"脚本{tag}发布标签少于 3 个"})
        if any(not t.startswith("#") for t in tag_list):
            errors.append({"code": "G2", "msg": f"脚本{tag}发布标签必须以 # 开头：{tags[:40]}"})
        if brand and brand not in tags:
            errors.append({"code": "G3", "msg": f"脚本{tag}发布标签未包含品牌词「{brand}」"})

    # ---- 全篇
    if profile:
        pending = profile.get("pending") or []
        if pending:
            notes.append({"code": "P1",
                          "msg": f"客户仍有 {len(pending)} 项待确认信息",
                          "items": [p.get("question") or p.get("key") for p in pending]})
    if profile and profile.get("banned_claims") and any(
            b in str(meta.get("unified_facts", "")) for b in profile["banned_claims"]):
        warnings.append({"code": "C2", "msg": "统一事实里包含客户禁用表达，请改写"})

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
        "stats": {"scripts": len(scripts), "total_seconds": round(total_seconds, 1)},
    }


def report_text(res):
    lines = []
    lines.append(f"校验结果：{'通过' if res['ok'] else '未通过'}"
                 f"（{res['stats']['scripts']} 条脚本 / 共 {res['stats']['total_seconds']} 秒）")
    for e in res["errors"]:
        lines.append(f"  [错误 {e['code']}] {e['msg']}")
    for w in res["warnings"]:
        lines.append(f"  [提示 {w['code']}] {w['msg']}")
    for n in res["notes"]:
        extra = "、".join(map(str, n.get("items", [])[:4]))
        lines.append(f"  [注意 {n['code']}] {n['msg']}{('：' + extra) if extra else ''}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="脚本 payload 质量校验")
    ap.add_argument("payload")
    ap.add_argument("--client", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    cid = args.client or payload.get("client_id")
    profile = lib.client_profile(cid) if cid else None
    if cid and not profile:
        print(json.dumps({"error": f"客户不存在: {cid}", "clients": lib.list_clients()},
                         ensure_ascii=False, indent=2))
        return 3
    if not profile:
        print("警告：未指定 --client，跳过事实与品牌检查", file=sys.stderr)

    res = check(payload, profile)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(report_text(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
