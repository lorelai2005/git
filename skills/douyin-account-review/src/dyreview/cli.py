"""命令行入口。

对 Crabot 的约定：**stdout 永远是单个 JSON 对象**（日志走 stderr），
失败也是 JSON（ok=false + code），Crabot 直接取字段决定话术，不需要解析报错文本。

子命令一览
----------
    init-db     建表/校验表结构（幂等）
    doctor      自检：配置、数据库、schema、token 健康度、数据新鲜度
    demo        离线演示：不连库不连抖音，跑通事实包->诊断->报告->数字校验
    account     账号登记 / 列表 / 改状态
    token       token 状态 / 强制刷新 / refresh_token 续期巡检
    collect     采集（单账号或全部）
    review      阶段1：出事实包 + prompt（交给大模型）
    generate    阶段2（可选）：skill 自己调大模型
    render      阶段3：接收大模型正文 -> 数字校验 -> 渲染报告
    push        阶段4：推送飞书
    run         一步到位（定时任务用）
    prune       清理历史快照
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

from . import SKILL_NAME, __version__
from .account_resolver import list_choices, resolve_account
from .collect import Collector
from .config import load_config
from .db import Database
from .douyin_client import DouyinClient
from .errors import (AccountAmbiguous, DataUnavailable, DouyinApiError,
                     DyReviewError, ReauthRequired)
from .feishu import FeishuClient
from .logging_util import print_json, setup_logging, utcnow
from .offline import MemoryRepo, sample_window
from .repo import Repo
from .secrets_box import SecretsBox
from .service import ReviewService
from .token_manager import TokenManager


class Ctx:
    """按需构建依赖，避免 `demo` 之类命令被迫连数据库。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cfg = load_config(args.config)
        self._db: Database | None = None
        self._repo: Repo | None = None
        self._box: SecretsBox | None = None
        self._client: DouyinClient | None = None
        self._tokens: TokenManager | None = None
        self._collector: Collector | None = None
        self._feishu: FeishuClient | None = None
        self._service: ReviewService | None = None

    # ------------------------------------------------------------------
    @property
    def db(self) -> Database:
        self.cfg.validate({"db"})
        if self._db is None:
            self._db = Database(self.cfg)
        return self._db

    @property
    def repo(self) -> Repo:
        if self._repo is None:
            self._repo = Repo(self.db)
        return self._repo

    @property
    def box(self) -> SecretsBox:
        if self._box is None:
            self._box = SecretsBox(self.cfg.get("crypto.key", "") or "",
                                   self.cfg.get("crypto.key_previous", "") or "")
        return self._box

    @property
    def client(self) -> DouyinClient:
        if self._client is None:
            self.cfg.validate({"douyin"})
            self._client = DouyinClient(self.cfg)
        return self._client

    @property
    def tokens(self) -> TokenManager:
        if self._tokens is None:
            self._tokens = TokenManager(self.cfg, self.repo, self.client, self.box, self.db)
        return self._tokens

    @property
    def collector(self) -> Collector:
        if self._collector is None:
            self._collector = Collector(self.cfg, self.repo, self.tokens, self.client)
        return self._collector

    @property
    def feishu(self) -> FeishuClient:
        self.cfg.validate({"feishu"})
        if self._feishu is None:
            self._feishu = FeishuClient(self.cfg, kv=self.repo if self._repo else None)
        return self._feishu

    @property
    def service(self) -> ReviewService:
        if self._service is None:
            self._service = ReviewService(self.cfg, self.repo, self.tokens,
                                          self.collector, self.feishu)
        return self._service

    def close(self) -> None:
        if self._db is not None:
            self._db.close()


# =====================================================================
# 各子命令
# =====================================================================
def cmd_init_db(ctx: Ctx) -> dict:
    db = ctx.db
    if ctx.args.check:
        return {"ok": True, "status": "checked", **db.verify_schema()}
    info = db.init_schema(dry_run=ctx.args.dry_run)
    ping = {} if ctx.args.dry_run else db.ping()
    return {"ok": bool(info.get("ok", True)), "status": "initialized" if not ctx.args.dry_run else "dry_run",
            "database": ping.get("db"), "schema": ctx.cfg.schema, **info}


def cmd_doctor(ctx: Ctx) -> dict:
    cfg = ctx.cfg
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("config_loaded", True, {"source": cfg.source, "is_example": cfg.is_example,
                                "unresolved_env": sorted(cfg.unresolved_env)})
    add("crypto_enabled", ctx.box.enabled,
        "token 加密落库已启用" if ctx.box.enabled else "未配置 crypto.key，token 明文入库（生产必须修）")

    db_info: dict = {}
    try:
        db_info = ctx.db.ping()
        add("db_connect", True, {"db": db_info.get("db"), "user": db_info.get("usr")})
        schema = ctx.db.verify_schema()
        add("db_schema", bool(schema.get("ok")), schema)
    except DyReviewError as exc:
        add("db_connect", False, exc.message)
    except Exception as exc:  # pragma: no cover
        add("db_connect", False, f"{type(exc).__name__}: {exc}")

    if db_info:
        try:
            health = ctx.repo.token_health()
            need_reauth = [h["account_name"] for h in health
                           if h.get("account_status") == "reauth_required"]
            add("accounts", not need_reauth,
                {"total": len(health), "reauth_required": need_reauth})
            expiring = [h["account_name"] for h in health
                        if h.get("access_hours_left") is not None
                        and 0 < h["access_hours_left"] < 24]
            add("token_expiring_soon", not expiring, expiring or "无")
        except Exception as exc:
            add("accounts", False, str(exc))

    if ctx.args.network:
        try:
            payload = ctx.client.client_token()
            add("douyin_api", True, {"has_access_token": bool(payload.get("access_token"))})
        except DyReviewError as exc:
            add("douyin_api", False, exc.to_dict())
        try:
            ctx.feishu.tenant_access_token()
            add("feishu_api", True, "tenant_access_token 获取成功")
        except Exception as exc:
            add("feishu_api", False, f"{type(exc).__name__}: {exc}")

    failed = [c["check"] for c in checks if not c["ok"]]
    return {"ok": not failed, "status": "healthy" if not failed else "issues",
            "version": __version__, "checks": checks, "failed": failed}


def cmd_demo(ctx: Ctx) -> dict:
    """离线演示：真跑 build_facts + 诊断 + 渲染 + 数字校验，只是数据是样例。"""
    from .facts import build_facts
    from .report import build_package, render_markdown, validate_numbers, write_artifacts

    repo = MemoryRepo()
    account = repo.get_account(1)
    assert account is not None
    window = sample_window(repo=repo, days=ctx.args.days)
    facts = build_facts(repo, account, window, ctx.cfg)
    package = build_package(99, facts)
    report_md = render_markdown(facts, None, ctx.cfg)     # 兜底报告（不调大模型）
    validation = validate_numbers(report_md, facts)
    paths = write_artifacts(ctx.cfg.var_dir / "demo", 99, facts, package.prompt_md, report_md)
    return {
        "ok": True, "status": "demo", "note": "样例数据，非真实账号；用于验证链路与报告排版",
        "account": account.to_dict(),
        "period": window.to_dict(),
        "kpi": facts["kpi"]["current"],
        "diagnosis": [d["code"] for d in facts["diagnosis"]],
        "data_gaps": facts["data_gaps"],
        "numbers_allowlist_size": len(facts["numbers"]),
        "validation": validation,
        "paths": paths,
        "report_md": report_md,
    }


def cmd_account(ctx: Ctx) -> dict:
    sub = ctx.args.account_cmd
    if sub == "list":
        return {"ok": True, "accounts": list_choices(ctx.repo, limit=ctx.args.limit)}
    if sub == "search":
        acc = resolve_account(ctx.repo, ctx.args.term)
        return {"ok": True, "account": acc.to_dict()}
    if sub == "status":
        acc = resolve_account(ctx.repo, ctx.args.term)
        ctx.repo.set_account_status(acc.id, ctx.args.status, ctx.args.remark)
        return {"ok": True, "account": acc.to_dict(), "new_status": ctx.args.status}
    if sub == "add":
        return _account_add(ctx)
    raise DyReviewError(f"未知 account 子命令：{sub}")


def _account_add(ctx: Ctx) -> dict:
    args = ctx.args
    if args.code:
        payload = ctx.client.exchange_auth_code(args.code)
        open_id = str(payload.get("open_id") or "")
        if not open_id:
            raise DouyinApiError("oauth_access_token", "NO_OPEN_ID",
                                 "授权码换 token 未返回 open_id", detail=str(payload)[:300])
    else:
        open_id = args.open_id or ""
        payload = {}
    if not open_id:
        raise DyReviewError("新增账号需要 --open-id，或提供 --code 让 Skill 换 token")
    if not args.name:
        raise DyReviewError("新增账号需要 --name（运营内部称呼，飞书里用它点名）")

    account = ctx.repo.upsert_account(
        open_id=open_id, account_name=args.name,
        douyin_id=args.douyin_id, nickname=args.nickname, org_id=args.org,
        status="active", feishu_chat_id=args.chat_id,
        data_scope=str(payload.get("scope") or "") or None)
    token_saved = False
    if payload.get("access_token"):
        ctx.tokens.store_initial_token(account, payload)
        token_saved = True
    return {"ok": True, "account": account.to_dict(), "token_saved": token_saved,
            "next": "执行 dyreview collect --account %s 拉取首批数据" % account.account_name}


def cmd_token(ctx: Ctx) -> dict:
    sub = ctx.args.token_cmd
    if sub == "status":
        accounts = ([resolve_account(ctx.repo, ctx.args.account)]
                    if ctx.args.account else ctx.repo.list_accounts(status=None))
        rows = [ctx.tokens.status(a) for a in accounts]
        return {"ok": True, "tokens": rows}
    if sub == "refresh":
        accounts = ([resolve_account(ctx.repo, ctx.args.account)]
                    if ctx.args.account else ctx.repo.list_accounts())
        out = []
        for acc in accounts:
            try:
                access = ctx.tokens.get_access_token(acc, force_refresh=True)
                out.append({"account_id": acc.id, "account_name": acc.account_name,
                            "ok": True, **access.to_dict()})
            except DyReviewError as exc:
                out.append({"account_id": acc.id, "account_name": acc.account_name,
                            "ok": False, **exc.to_dict()})
        return {"ok": all(r["ok"] for r in out), "refreshed": out}
    if sub == "keepalive":
        accounts = ([resolve_account(ctx.repo, ctx.args.account)]
                    if ctx.args.account else ctx.repo.list_accounts())
        out = []
        for acc in accounts:
            try:
                out.append({**ctx.tokens.keepalive(acc), "account_name": acc.account_name})
            except ReauthRequired as exc:
                out.append({"account_id": acc.id, "account_name": acc.account_name,
                            "action": "reauth_required", "detail": exc.to_dict()})
            except DyReviewError as exc:
                out.append({"account_id": acc.id, "account_name": acc.account_name,
                            "action": "failed", "detail": exc.to_dict()})
        return {"ok": True, "keepalive": out}
    raise DyReviewError(f"未知 token 子命令：{sub}")


def cmd_collect(ctx: Ctx) -> dict:
    args = ctx.args
    if args.account and args.account.lower() != "all":
        accounts = [resolve_account(ctx.repo, args.account)]
    else:
        accounts = ctx.repo.list_accounts()
    if not accounts:
        return {"ok": False, "status": "no_account", "message": "没有可采集的账号，请先 account add"}
    reports = ctx.collector.run(accounts, kind=args.kind, dry_run=args.dry_run)
    ok = all(r.status in ("success", "partial", "skipped") for r in reports)
    return {"ok": ok, "status": "collected", "dry_run": args.dry_run,
            "results": [r.to_dict() for r in reports]}


def cmd_review(ctx: Ctx) -> dict:
    args = ctx.args
    return ctx.service.start(
        args.account, days=args.days, source="feishu" if args.request_text else "cli",
        request_text=args.request_text, force=args.force,
        auto_collect_hours=0.0 if args.no_collect else args.auto_collect_hours)


def cmd_generate(ctx: Ctx) -> dict:
    return ctx.service.generate(ctx.args.review_id)


def cmd_render(ctx: Ctx) -> dict:
    args = ctx.args
    if args.llm_output == "-":
        text = sys.stdin.read()
    else:
        path = Path(args.llm_output)
        if not path.exists():
            raise DyReviewError(f"模型输出文件不存在：{path}")
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise DyReviewError("模型输出为空，无法渲染")
    return ctx.service.render(args.review_id, text, model=args.model,
                             input_tokens=args.input_tokens,
                             output_tokens=args.output_tokens,
                             mode="handoff", force=args.allow_unsourced_numbers)


def cmd_push(ctx: Ctx) -> dict:
    return ctx.service.push(ctx.args.review_id, ctx.args.chat_id)


def cmd_run(ctx: Ctx) -> dict:
    args = ctx.args
    return ctx.service.run(
        args.account, days=args.days, push=not args.no_push, llm=args.llm,
        auto_collect_hours=args.auto_collect_hours, force=args.force,
        chat_id=args.chat_id, request_text=args.request_text)


def cmd_prune(ctx: Ctx) -> dict:
    keep = ctx.args.keep_days or ctx.cfg.get_int("collect.keep_days", 730)
    return {"ok": True, "deleted": ctx.repo.prune(keep)}


def cmd_reviews(ctx: Ctx) -> dict:
    return {"ok": True, "reviews": ctx.repo.list_reviews(
        getattr(ctx.args, "account_id", None), ctx.args.limit)}


# =====================================================================
# 解析器
# =====================================================================
def _add_global_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """全局选项在子命令前后都要能用。

    argparse 的坑：写在主 parser 上的可选参数只有放在子命令**前面**才被识别，
    而用户几乎都写成 `dyreview doctor --pretty`。所以每个子命令也挂一份；
    用 SUPPRESS 默认值避免子命令的默认值覆盖主命令已解析的值。
    """
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--config", default=default if suppress_defaults else None,
                        help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("--pretty", action="store_true",
                        default=argparse.SUPPRESS if suppress_defaults else False,
                        help="缩进输出 JSON")
    parser.add_argument("--verbose", "-v", action="store_true",
                        default=argparse.SUPPRESS if suppress_defaults else False,
                        help="DEBUG 日志")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=SKILL_NAME, description="抖音账号数据复盘 Skill")
    _add_global_options(p)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress_defaults=True)

    def new(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    sp = new("init-db", "建表/校验表结构")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--check", action="store_true", help="只校验不建表")
    sp.set_defaults(func=cmd_init_db)

    sp = new("doctor", "自检")
    sp.add_argument("--network", action="store_true", help="额外探测抖音/飞书接口")
    sp.set_defaults(func=cmd_doctor)

    sp = new("demo", "离线演示（样例数据）")
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(func=cmd_demo)

    sp = new("account", "账号管理")
    asp = sp.add_subparsers(dest="account_cmd", required=True)
    a = asp.add_parser("list"); a.add_argument("--limit", type=int, default=50)
    a.set_defaults(func=cmd_account)
    a = asp.add_parser("search"); a.add_argument("term")
    a.set_defaults(func=cmd_account)
    a = asp.add_parser("status"); a.add_argument("term")
    a.add_argument("status", choices=["active", "paused", "reauth_required", "revoked"])
    a.add_argument("--remark")
    a.set_defaults(func=cmd_account)
    a = asp.add_parser("add", help="登记账号（可用授权码换 token）")
    a.add_argument("--open-id"); a.add_argument("--name", required=True)
    a.add_argument("--douyin-id"); a.add_argument("--nickname")
    a.add_argument("--org"); a.add_argument("--chat-id")
    a.add_argument("--code", help="抖音授权码；提供后会换取并落库 token")
    a.set_defaults(func=cmd_account)

    sp = new("token", "token 管理")
    tsp = sp.add_subparsers(dest="token_cmd", required=True)
    for name in ("status", "refresh", "keepalive"):
        t = tsp.add_parser(name)
        t.add_argument("--account", help="账号名/抖音号/open_id，缺省=全部")
        t.set_defaults(func=cmd_token)

    sp = new("collect", "采集数据")
    sp.add_argument("--account", default="all", help="账号标识，默认 all")
    sp.add_argument("--kind", default="incremental",
                    choices=["incremental", "full", "fan_profile", "token_keepalive"])
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_collect)

    sp = new("review", "阶段1：出事实包 + prompt")
    sp.add_argument("--account", required=True)
    sp.add_argument("--days", type=int, default=None)
    sp.add_argument("--request-text", help="用户原始指令，用于留痕")
    sp.add_argument("--force", action="store_true", help="跳过去重")
    sp.add_argument("--no-collect", action="store_true", help="不要自动补采")
    sp.add_argument("--auto-collect-hours", type=float, default=6.0)
    sp.set_defaults(func=cmd_review)

    sp = new("generate", "阶段2：skill 自己调大模型")
    sp.add_argument("--review-id", type=int, required=True)
    sp.set_defaults(func=cmd_generate)

    sp = new("render", "阶段3：接收模型正文并渲染")
    sp.add_argument("--review-id", type=int, required=True)
    sp.add_argument("--llm-output", required=True, help="文件路径，或 - 表示 stdin")
    sp.add_argument("--model"); sp.add_argument("--input-tokens", type=int, default=0)
    sp.add_argument("--output-tokens", type=int, default=0)
    sp.add_argument("--allow-unsourced-numbers", action="store_true",
                    help="数字校验不通过也强制渲染（不推荐）")
    sp.set_defaults(func=cmd_render)

    sp = new("push", "阶段4：推送飞书")
    sp.add_argument("--review-id", type=int, required=True)
    sp.add_argument("--chat-id")
    sp.set_defaults(func=cmd_push)

    sp = new("run", "一步到位（定时任务）")
    sp.add_argument("--account", required=True)
    sp.add_argument("--days", type=int, default=None)
    sp.add_argument("--no-push", action="store_true")
    sp.add_argument("--llm", choices=["handoff", "builtin"], default=None)
    sp.add_argument("--auto-collect-hours", type=float, default=6.0)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--chat-id"); sp.add_argument("--request-text")
    sp.set_defaults(func=cmd_run)

    sp = new("prune", "清理历史快照")
    sp.add_argument("--keep-days", type=int, default=None)
    sp.set_defaults(func=cmd_prune)

    sp = new("reviews", "复盘记录列表")
    sp.add_argument("--account-id", type=int, default=None)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_reviews)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else None, force=True)
    ctx: Ctx | None = None
    try:
        ctx = Ctx(args)
        result = args.func(ctx)
        print_json(result, pretty=args.pretty)
        return 0 if result.get("ok", True) else 1
    except AccountAmbiguous as exc:
        print_json(exc.to_dict(), pretty=getattr(args, "pretty", False))
        return 2
    except DyReviewError as exc:
        print_json(exc.to_dict(), pretty=getattr(args, "pretty", False))
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print_json({"ok": False, "code": "INTERRUPTED", "message": "已中断"})
        return 130
    except Exception as exc:  # pragma: no cover
        traceback.print_exc(file=sys.stderr)
        print_json({"ok": False, "code": "UNEXPECTED_ERROR",
                    "message": f"{type(exc).__name__}: {exc}"},
                   pretty=getattr(args, "pretty", False))
        return 1
    finally:
        if ctx is not None:
            ctx.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
