# -*- coding: utf-8 -*-
"""retrieve.py — 从「爆款逻辑库」与「客户素材库」检索可用素材，输出候选与检索依据。

两种用法：
  # 2) 检索爆款逻辑（先加载脚本目标）
  python retrieve.py logic --query "开业 同城到店 福利" --count 5

  # 3) 为客户检索可用素材
  python retrieve.py material --client shuqingzheyi --query "免费搭配 门头 试穿"

  # 2-3) 一次跑完逻辑与素材检索，输出可直接喂给写稿步骤的 JSON
  python retrieve.py all --client shuqingzheyi --query "开业 5 条 同城到店" --count 5

打分规则（可解释，不用向量库）：
  标签命中 ×3 / 目标类型命中 ×2 / 人群契合 ×2 / 正文 2-gram 命中 ×1
  再叠加：素材可用性（status=available）加权；不可用素材只提示不给分。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def score_text(query_tokens, text, weight=1.0):
    toks = lib.tokenize(text)
    hits = query_tokens & toks
    return len(hits) * weight, sorted(hits)


def rank_logic(query, count=5):
    q = lib.tokenize(query)
    lib_data = lib.viral_library()
    out = []
    for card in lib_data.get("cards", []):
        s = 0.0
        why = []
        for tag in card.get("tags", []):
            if lib.tokenize(tag) & q:
                s += 3
                why.append(f"标签:{tag}")
        for gt in card.get("goal_types", []):
            if lib.tokenize(gt) & q:
                s += 2
                why.append(f"目标:{gt}")
        for af in card.get("audience_fit", []):
            if lib.tokenize(af) & q:
                s += 2
                why.append(f"人群:{af}")
        body = " ".join([card.get("name", ""), card.get("hook_formula", ""),
                         " ".join(b.get("desc", "") for b in card.get("beats", [])),
                         " ".join(card.get("risks", []))])
        add, hits = score_text(q, body, 1.0)
        if add:
            s += add
            why.append("正文:" + "/".join(hits[:6]))
        if s > 0:
            out.append({"score": round(s, 1), "why": why, "card": card})
    out.sort(key=lambda x: -x["score"])
    return out[:count]


def rank_material(client_id, query, count=40):
    q = lib.tokenize(query)
    mats = lib.client_materials(client_id)
    buckets = {"scenes": "场景", "actions": "动作", "people": "人物",
               "props": "道具/物料", "segments": "可拆解卖点段", "voice_lines": "可用原话"}
    out = {}
    for key, label in buckets.items():
        items = mats.get(key, []) or []
        scored = []
        for it in items:
            text = " ".join(str(it.get(k, "")) for k in
                            ("name", "desc", "shot_hint", "text", "who", "proof", "location"))
            text += " " + " ".join(it.get("tags", []))
            s, hits = score_text(q, text, 1.0)
            for tag in it.get("tags", []):
                if lib.tokenize(tag) & q:
                    s += 3
            status = it.get("status") or it.get("consent") or it.get("verified")
            usable = status not in ("unavailable", "missing", "pending", "unconfirmed", False)
            if not usable:
                s = s * 0.2
            if s > 0:
                scored.append({"score": round(s, 1), "usable": bool(usable),
                               "hits": hits, "item": it})
        scored.sort(key=lambda x: (-x["score"], not x["usable"]))
        out[key] = {"label": label, "items": scored[:count]}
    return out


def usable_only(material_result):
    """把检索结果压成「可写进脚本」的清单，供写稿步骤直接引用。"""
    lines = []
    for key, block in material_result.items():
        for it in block["items"]:
            if not it["usable"]:
                continue
            item = it["item"]
            name = item.get("name") or item.get("text") or item.get("who") or ""
            lines.append(f"[{block['label']}] {name}"
                         + (f" — {item.get('shot_hint')}" if item.get("shot_hint") else ""))
    return lines


def cmd_logic(args):
    res = rank_logic(args.query, args.count)
    return {"query": args.query, "count": len(res),
            "cards": [{"id": r["card"]["id"], "name": r["card"]["name"],
                       "score": r["score"], "why": r["why"],
                       "goal_types": r["card"].get("goal_types", []),
                       "duration_range": r["card"].get("duration_range"),
                       "beats": r["card"].get("beats", []),
                       "hook_formula": r["card"].get("hook_formula", ""),
                       "voice_rules": r["card"].get("voice_rules", []),
                       "shoot_rules": r["card"].get("shoot_rules", []),
                       "risks": r["card"].get("risks", [])} for r in res]}


def cmd_material(args):
    res = rank_material(args.client, args.query, args.count)
    return {"client": args.client, "query": args.query,
            "buckets": res, "usable": usable_only(res)}


def cmd_all(args):
    return {"logic": cmd_logic(args), "material": cmd_material(args)}


def main():
    ap = argparse.ArgumentParser(description="爆款逻辑 / 客户素材检索")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("logic", help="检索爆款逻辑卡")
    p1.add_argument("--query", required=True)
    p1.add_argument("--count", type=int, default=5)
    p1.set_defaults(func=cmd_logic)

    p2 = sub.add_parser("material", help="检索客户素材")
    p2.add_argument("--client", required=True)
    p2.add_argument("--query", required=True)
    p2.add_argument("--count", type=int, default=40)
    p2.set_defaults(func=cmd_material)

    p3 = sub.add_parser("all", help="逻辑 + 素材 一次检索")
    p3.add_argument("--client", required=True)
    p3.add_argument("--query", required=True)
    p3.add_argument("--count", type=int, default=5)
    p3.set_defaults(func=cmd_all)

    args = ap.parse_args()
    if args.cmd in ("material", "all"):
        if not lib.client_profile(args.client):
            print(json.dumps({"error": f"客户不存在: {args.client}",
                              "clients": lib.list_clients()}, ensure_ascii=False, indent=2))
            return 3
    if args.cmd == "all":
        args.count = min(args.count, 40)
    print(json.dumps(args.func(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
