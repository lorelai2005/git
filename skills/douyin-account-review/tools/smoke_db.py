#!/usr/bin/env python3
"""真库冒烟测试：在 193 服务器的**真实 PostgreSQL** 上跑通全链路。

不碰抖音、不碰飞书、不碰任何客户真实数据：
    * 抖音接口用内置假响应（FakeTransport）；
    * 造一个 open_id 以 `smoke_` 开头的一次性账号；
    * 跑完自动删除（account 的级联会带走 video/metric/review 等行）。

用法
----
    export DYREVIEW_DB_DSN='postgresql://dyreview:***@193.x.x.x:5432/renwei'
    python tools/smoke_db.py            # 跑完即清理
    python tools/smoke_db.py --keep     # 保留数据，便于手查（会打印 open_id）

退出码：0 全通过；1 有断言失败（输出 JSON 里 failures 有原因）。
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dyreview.collect import Collector                                     # noqa: E402
from dyreview.config import load_config                                    # noqa: E402
from dyreview.db import Database                                           # noqa: E402
from dyreview.douyin_client import DouyinClient                            # noqa: E402
from dyreview.facts import build_facts                                     # noqa: E402
from dyreview.logging_util import print_json, setup_logging, utcnow        # noqa: E402
from dyreview.models import ReviewWindow                                   # noqa: E402
from dyreview.repo import Repo                                             # noqa: E402
from dyreview.report import render_markdown, validate_numbers               # noqa: E402
from dyreview.secrets_box import SecretsBox                                # noqa: E402
from dyreview.service import ReviewService                                 # noqa: E402
from dyreview.token_manager import TokenManager                            # noqa: E402

DAY = 86400


def fake_douyin_router(method, url, params, body):
    """按 URL 关键字返回假数据，结构与官方 envelope 一致。"""
    def ok(data):
        return 200, {"data": data, "extra": {"error_code": 0, "description": "",
                                             "logid": "smoke-logid"}}

    if "user_data" in url:
        return ok({"follower_count": 12345, "new_fans": 88, "cancel_fans": 6,
                   "total_favorited": 50000, "aweme_count": 3})
    if "video_list" in url:
        items = [{"item_id": f"smoke_item_{i}",
                  "title": f"冒烟测试作品 {i} #test",
                  "duration": 20000 + i * 1000,
                  "create_time": int(utcnow().timestamp()) - i * 2 * DAY,
                  "share_url": f"https://v.douyin.com/smoke{i}/"} for i in range(3)]
        return ok({"list": items, "cursor": 3, "has_more": False})
    if "video_data" in url:
        ids = (body or {}).get("item_ids") or []
        return ok({"list": [{"item_id": i, "statistics": {
            "play_count": 8000, "digg_count": 320, "comment_count": 42,
            "share_count": 18, "collect_count": 60, "finish_rate": "21.5%",
            "bounce_rate": 0.41}} for i in ids]})
    if "fan_data" in url:
        return ok({"gender": {"man": 0.38, "woman": 0.62},
                   "age": [{"name": "25-34", "rate": "45%"}]})
    return ok({})


class _Transport:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, params=None, json_body=None, headers=None):
        self.calls += 1
        return fake_douyin_router(method, url, params or {}, json_body)


def main() -> int:
    parser = argparse.ArgumentParser(description="真库冒烟测试（不碰抖音/飞书/客户数据）")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--keep", action="store_true", help="保留测试数据")
    parser.add_argument("--no-init", action="store_true", help="表不存在时不自动建表")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    setup_logging("INFO", force=True)
    cfg = load_config(args.config)
    failures: list[str] = []
    steps: list[str] = []
    open_id = f"smoke_{uuid.uuid4().hex[:12]}"
    account_id: int | None = None
    review_id: int | None = None
    db = Database(cfg)

    def check(name: str, cond: bool, detail=None) -> None:
        steps.append(name)
        if not cond:
            failures.append(f"{name}: {detail}")

    try:
        # ---------- 1) 表结构 ----------
        info = db.verify_schema()
        if not info["ok"] and not args.no_init:
            info = db.init_schema()
        check("schema_ready", bool(info["ok"]), info)
        if not info["ok"]:
            print_json({"ok": False, "failures": failures, "steps": steps}, pretty=args.pretty)
            return 1
        db.require_schema()

        repo = Repo(db)
        box = SecretsBox(cfg.get("crypto.key", "") or "")
        transport = _Transport()
        client = DouyinClient(cfg, transport=transport, sleep=lambda s: None)
        tokens = TokenManager(cfg, repo, client, box, db)
        collector = Collector(cfg, repo, tokens, client, today=lambda: utcnow().date())
        service = ReviewService(cfg, repo, tokens, collector, feishu=None,
                               today=lambda: utcnow().date())

        # ---------- 2) 造账号 + 装 token ----------
        account = repo.upsert_account(open_id=open_id, account_name="SMOKE-TEST 冒烟账号",
                                      douyin_id="smoke_test", status="active")
        account_id = account.id
        now = utcnow()
        payload = {"access_token": "smoke_access_token", "expires_in": 15 * DAY,
                   "refresh_token": "smoke_refresh_token", "refresh_expires_in": 30 * DAY,
                   "open_id": open_id, "scope": "user_info,video.list,video.data"}
        tokens.store_initial_token(account, payload)
        check("account_created", account.id > 0, account.id)
        check("token_stored", repo.get_token(account.id) is not None)

        # ---------- 3) 采集（full 模式会顺带拉粉丝画像） ----------
        rep = collector.collect_account(account, kind="full")
        check("collect_status", rep.status in ("success", "partial"), rep.to_dict())
        check("collect_videos", rep.videos_upserted == 3, rep.videos_upserted)
        check("collect_metrics", rep.metrics_written >= 3, rep.metrics_written)
        check("account_metric_written", rep.account_metric_written is True)
        check("collect_run_logged", bool(repo.db.scalar(
            f'SELECT count(*) FROM "{repo.s}".collect_run WHERE account_id = %s',
            (account.id,))))

        # ---------- 4) 事实包 ----------
        window = ReviewWindow(utcnow().date() - timedelta(days=6), utcnow().date())
        facts = build_facts(repo, account, window, cfg)
        check("facts_plays", (facts["kpi"]["current"]["plays"] or 0) > 0,
              facts["kpi"]["current"]["plays"])
        check("facts_diagnosis", isinstance(facts["diagnosis"], list))
        check("facts_numbers", len(facts["numbers"]) > 20, len(facts["numbers"]))
        check("facts_audience", bool(facts["audience"]), facts["audience"])

        # ---------- 5) 渲染 + 数字校验 ----------
        started = service.start("smoke_test", days=7, auto_collect_hours=0)
        review_id = started.get("review_id")
        check("review_start", started.get("status") == "facts_ready", started.get("status"))
        cur = started["kpi"]["current"]
        grounded = (f"## 一、一句话结论\n本期播放 {cur['plays']}，"
                    f"互动率 {round((cur['engagement_rate'] or 0) * 100, 1)}%。\n")
        rendered = service.render(started["review_id"], grounded)
        check("review_render", rendered.get("ok") is True, rendered.get("validation"))
        check("numbers_clean", rendered["validation"]["ok"] is True,
              rendered["validation"]["violations"])
        report_md = rendered["report_md"]
        check("report_has_appendix", "附：关键数据明细" in report_md)
        check("report_has_account", account.account_name in report_md)

        bad = validate_numbers("播放暴涨到 987654321 次", facts)
        check("validation_catches_fabrication", bad["ok"] is False)

        # ---------- 6) 幂等重采 ----------
        rep2 = collector.collect_account(account)
        check("recollect_ok", rep2.status in ("success", "partial"), rep2.to_dict())
        rows = repo.db.scalar(
            f'SELECT count(*) FROM "{repo.s}".video_metric WHERE account_id = %s',
            (account.id,))
        check("no_duplicate_metric_rows", rows == 3, rows)

        # ---------- 7) 视图可用 ----------
        latest = repo.latest_video_metrics(account.id)
        check("view_v_video_latest", len(latest) == 3, len(latest))
        fresh = repo.data_freshness(account.id)
        check("view_data_freshness", fresh.get("latest_video_stat_date") is not None, fresh)

    except Exception as exc:  # 冒烟脚本要把异常变成可读 JSON
        import traceback
        traceback.print_exc(file=sys.stderr)
        failures.append(f"exception: {type(exc).__name__}: {exc}")
    finally:
        cleaned = False
        if account_id is not None and not args.keep:
            try:
                s = f'"{db.schema}"'
                db.execute(f"DELETE FROM {s}.review WHERE account_id = %s", (account_id,))
                db.execute(f"DELETE FROM {s}.collect_run WHERE account_id = %s", (account_id,))
                db.execute(f"DELETE FROM {s}.oauth_event WHERE account_id = %s", (account_id,))
                db.execute(f"DELETE FROM {s}.llm_usage WHERE account_id = %s", (account_id,))
                db.execute(f"DELETE FROM {s}.account WHERE id = %s", (account_id,))
                db.execute(f"DELETE FROM {s}.task_lock WHERE lock_key = %s",
                           (f"review:{account_id}",))
                if review_id:
                    db.execute(f"DELETE FROM {s}.kv WHERE key = %s",
                               (f"review:{review_id}:steps",))
                cleaned = True
            except Exception as exc:
                failures.append(f"cleanup failed: {exc}")
        db.close()

    result = {
        "ok": not failures,
        "schema": db.schema,
        "open_id": open_id,
        "account_id": account_id,
        "kept": bool(args.keep),
        "cleaned": cleaned if account_id else None,
        "steps_run": len(steps),
        "failures": failures,
        "steps": steps,
    }
    print_json(result, pretty=args.pretty)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
