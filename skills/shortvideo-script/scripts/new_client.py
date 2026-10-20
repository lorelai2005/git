# -*- coding: utf-8 -*-
"""new_client.py — 首次合作的客户资料入库脚手架。

用法:
  python new_client.py --list
  python new_client.py --id shuqingzheyi --name "淑情哲依" --industry "女装实体店" --city "武汉"
  python new_client.py --id shuqingzheyi --add-plan kaiye-5tiao --plan-title "开业庆典 5 条短视频"
  python new_client.py --id shuqingzheyi --add-benchmark

建完之后要人工/对话补全 profile.json 的 facts（status=confirmed 才能写进脚本），
再跑 python retrieve.py all --client <id> --query "..." 看素材够不够。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

TODAY = dt.date.today().isoformat()


def scaffold(client_id, name, industry="", city="", district=""):
    d = lib.client_dir(client_id)
    if (d / "profile.json").exists():
        print(f"客户已存在，未覆盖：{d}")
        return 1
    (d / "plans").mkdir(parents=True, exist_ok=True)
    (d / "benchmarks").mkdir(parents=True, exist_ok=True)

    prof = lib.load_json(lib.TEMPLATES / "profile.template.json")
    prof.update({"client_id": client_id, "name": name or client_id,
                 "industry": industry, "city": city, "district": district,
                 "created": TODAY, "updated": TODAY})
    lib.save_json(d / "profile.json", prof)

    mats = lib.load_json(lib.TEMPLATES / "materials.template.json")
    mats["client_id"] = client_id
    mats["updated"] = TODAY
    lib.save_json(d / "materials.json", mats)

    print(f"已建立客户目录：{d}")
    print("下一步：")
    print("  1) 补全 profile.json：定位、客群、facts（confirmed 才能进脚本）、pending 待确认项")
    print("  2) 补全 materials.json：scenes / actions / people / props / segments / voice_lines")
    print("  3) 用 --add-plan 登记本次策划案目标")
    print("  4) 有对标视频时用 --add-benchmark 建拆解文件")
    return 0


def add_plan(client_id, goal_id, title=""):
    d = lib.client_dir(client_id)
    if not (d / "profile.json").exists():
        print(f"客户不存在：{client_id}")
        return 3
    path = d / "plans" / f"{goal_id}.json"
    if path.exists():
        print(f"策划案已存在，未覆盖：{path}")
        return 1
    plan = lib.load_json(lib.TEMPLATES / "plan.template.json")
    plan.update({"client_id": client_id, "goal_id": goal_id,
                 "title": title or goal_id, "confirmed_at": TODAY})
    lib.save_json(path, plan)
    print(f"已建立策划案：{path}")
    return 0


def add_benchmark(client_id, benchmark_id=""):
    d = lib.client_dir(client_id)
    if not (d / "profile.json").exists():
        print(f"客户不存在：{client_id}")
        return 3
    bid = benchmark_id or f"{dt.date.today().strftime('%Y%m%d')}-01"
    path = d / "benchmarks" / f"{bid}.json"
    if path.exists():
        print(f"对标拆解已存在，未覆盖：{path}")
        return 1
    tpl = lib.load_json(lib.TEMPLATES / "benchmark.template.json")
    tpl.update({"client_id": client_id, "benchmark_id": bid, "teardown_at": TODAY})
    lib.save_json(path, tpl)
    print(f"已建立对标拆解：{path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="客户入库脚手架")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--id")
    ap.add_argument("--name", default="")
    ap.add_argument("--industry", default="")
    ap.add_argument("--city", default="")
    ap.add_argument("--district", default="")
    ap.add_argument("--add-plan", default=None, metavar="GOAL_ID")
    ap.add_argument("--plan-title", default="")
    ap.add_argument("--add-benchmark", nargs="?", const="", default=None,
                    metavar="BENCHMARK_ID")
    args = ap.parse_args()

    if args.list:
        clients = lib.list_clients()
        if not clients:
            print("还没有客户。用 --id 建立第一个。")
        for c in clients:
            print(f"{c['client_id']:<16} {c['name']:<12} {c['industry']:<14} {c['city']}  ({c['updated']})")
        return 0

    if not args.id:
        ap.print_help()
        return 2

    if args.add_plan:
        return add_plan(args.id, args.add_plan, args.plan_title)
    if args.add_benchmark is not None:
        return add_benchmark(args.id, args.add_benchmark)

    return scaffold(args.id, args.name, args.industry, args.city, args.district)


if __name__ == "__main__":
    raise SystemExit(main())
