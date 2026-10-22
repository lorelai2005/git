"""复盘服务编排层（CLI 和大模型之间的那一层）。

一次复盘 = 四个可独立调用的阶段，天然构成 Crabot 的「数据 -> 模型 -> 推送」链路：

    start()    事实包 + prompt          -> status=facts_ready
    generate() skill 自己调大模型（可选）-> status=rendered
    render()   接收 Crabot 大模型输出     -> status=rendered（含数字校验）
    push()     推送到飞书                 -> status=pushed

每个阶段都是独立进程可调的（CLI 一个子命令 = 一步），
所以 `max_tool_steps_per_review` 用 dy.kv 里的步数计数器强制约束，防止 Agent 死循环。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from .account_resolver import resolve_account
from .collect import Collector
from .config import Config
from .douyin_client import DouyinClient
from .errors import DataUnavailable, DyReviewError, LlmError
from .facts import Thresholds, build_facts
from .feishu import FeishuClient
from .logging_util import local_today, new_trace_id, utcnow
from .models import Account, ReviewWindow
from .repo import Repo
from .report import (build_package, call_llm, clean_llm_output, render_markdown,
                     validate_numbers, write_artifacts)
from .token_manager import TokenManager

log = logging.getLogger(__name__)


class ReviewService:
    def __init__(self, cfg: Config, repo: Repo, tokens: TokenManager,
                 collector: Collector, feishu: FeishuClient | None = None,
                 *, today=None) -> None:
        self.cfg = cfg
        self.repo = repo
        self.tokens = tokens
        self.collector = collector
        self.feishu = feishu
        self._today = today or (lambda: local_today(cfg.timezone))
        self.max_steps = cfg.get_int("skill.max_tool_steps_per_review", 8)
        self.dedupe_minutes = cfg.get_int("skill.review_dedupe_minutes", 10)
        self.llm_mode = str(cfg.get("llm.mode", "handoff") or "handoff")
        self.strict_numbers = cfg.get_bool("review.report.numbers_strict", True)

    # =================================================================
    # 步数闸门（防递归）
    # =================================================================
    def bump_step(self, review_id: int) -> int:
        key = f"review:{review_id}:steps"
        current = self.repo.kv_get(key) or 0
        steps = int(current) + 1
        if steps > self.max_steps:
            raise DyReviewError(
                f"本次复盘已消耗 {steps - 1} 个工具步，超过上限 {self.max_steps}。"
                f"请直接使用已有结论收尾，禁止继续循环调用。")
        self.repo.kv_set(key, steps, ttl_seconds=3600)
        return steps

    # =================================================================
    # 账号
    # =================================================================
    def resolve(self, term: str) -> Account:
        return resolve_account(self.repo, term)

    # =================================================================
    # 阶段 1：事实包
    # =================================================================
    def start(self, term: str, *, days: int | None = None, source: str = "feishu",
              request_text: str | None = None, force: bool = False,
              auto_collect_hours: float = 0.0) -> dict:
        account = self.resolve(term)

        if not force and self.dedupe_minutes > 0:
            recent = self.repo.recent_review(account.id, self.dedupe_minutes)
            if recent:
                log.info("账号 %s 在 %d 分钟内已生成过复盘 #%s，直接复用",
                         account.account_name, self.dedupe_minutes, recent["id"])
                return {
                    "ok": True, "status": "deduped", "review_id": recent["id"],
                    "account": account.to_dict(),
                    "report_md": recent.get("report_md"),
                    "note": f"{self.dedupe_minutes} 分钟内已生成过同一账号的复盘，"
                            f"如需重新生成请加 --force",
                }

        lock_key = f"review:{account.id}"
        if not self.repo.acquire_task_lock(lock_key, f"pid:{__import__('os').getpid()}",
                                           ttl_seconds=900):
            return {"ok": False, "status": "busy", "code": "REVIEW_BUSY",
                    "account": account.to_dict(),
                    "message": "该账号正在复盘，请稍后再试（并发任务已存在）"}
        try:
            if auto_collect_hours and self.collector.is_stale(account, auto_collect_hours):
                log.info("账号 %s 数据超过 %.1f 小时未更新，先触发一次采集",
                         account.account_name, auto_collect_hours)
                rep = self.collector.collect_account(account, kind="incremental")
                log.info("补采结果：%s", rep.status)

            window = self._window(account, days)
            facts = build_facts(self.repo, account, window, self.cfg)
            review_id = self.repo.create_review(
                account.id, window.start, window.end, source=source, request_text=request_text)
            package = build_package(review_id, facts)
            self.repo.update_review(review_id, facts=facts,
                                    diagnosis=facts.get("diagnosis"),
                                    prompt_md=package.prompt_md, status="facts_ready")
            paths = write_artifacts(self.cfg.var_dir, review_id, facts, package.prompt_md)
            steps = self.bump_step(review_id)
            return {
                "ok": True,
                "status": "facts_ready",
                "review_id": review_id,
                "steps_used": steps,
                "account": account.to_dict(),
                "period": window.to_dict(),
                "kpi": facts["kpi"],
                "diagnosis": facts.get("diagnosis"),
                "data_gaps": facts.get("data_gaps"),
                "display": facts.get("display"),
                "prompt": package.prompt_md,
                "paths": paths,
                "next": "把 prompt 交给大模型，拿到报告正文后调用 render（或直接 run --llm builtin）",
            }
        finally:
            self.repo.release_task_lock(lock_key)

    def _window(self, account: Account, days: int | None) -> ReviewWindow:
        days = int(days or self.cfg.get_int("review.default_window_days", 7))
        days = max(1, min(days, 90))
        end = self._today()
        # 数据轻微滞后（<=3 天）时把窗口右端贴到最新快照，避免报告里一半是空数据
        fresh = self.repo.data_freshness(account.id)
        latest = fresh.get("latest_account_stat_date") or fresh.get("latest_video_stat_date")
        if latest:
            try:
                latest_date = date.fromisoformat(str(latest)[:10])
                if 0 < (end - latest_date).days <= 3:
                    end = latest_date
            except ValueError:
                pass
        return ReviewWindow(end - timedelta(days=days - 1), end)

    # =================================================================
    # 阶段 2（可选）：skill 自己调大模型
    # =================================================================
    def generate(self, review_id: int) -> dict:
        review = self._require_review(review_id)
        facts = review.get("facts")
        if not facts:
            raise DataUnavailable(f"复盘 #{review_id} 还没有事实包，请先执行 review")
        package = build_package(review_id, facts)
        if review.get("prompt_md"):
            package.user = self._extract_user_prompt(review["prompt_md"])
        self.bump_step(review_id)
        started = utcnow()
        result = call_llm(package, self.cfg)
        latency_ms = int((utcnow() - started).total_seconds() * 1000)
        return self.render(review_id, result.text, model=result.model,
                           input_tokens=result.input_tokens,
                           output_tokens=result.output_tokens,
                           latency_ms=latency_ms, mode="builtin")

    @staticmethod
    def _extract_user_prompt(prompt_md: str) -> str:
        marker = "\n\n---\n\n"
        return prompt_md.split(marker, 1)[1] if marker in prompt_md else prompt_md

    # =================================================================
    # 阶段 3：接收模型输出 -> 校验 -> 渲染 -> 落库
    # =================================================================
    def render(self, review_id: int, llm_text: str, *, model: str | None = None,
               input_tokens: int = 0, output_tokens: int = 0,
               latency_ms: int | None = None, mode: str | None = None,
               force: bool = False) -> dict:
        review = self._require_review(review_id)
        facts = review.get("facts")
        if not facts:
            raise DataUnavailable(f"复盘 #{review_id} 缺少事实包，无法渲染")

        text = clean_llm_output(llm_text or "")
        validation = validate_numbers(text, facts) if text else {
            "ok": True, "checked": 0, "violations": [], "violation_count": 0,
            "note": "无模型正文，走数据兜底报告"}
        if text and self.strict_numbers and not validation["ok"] and not force:
            self.repo.update_review(review_id, status="failed", validation=validation,
                                    error="报告包含事实包中不存在的数字，已拒绝渲染")
            return {
                "ok": False, "status": "numbers_invalid", "review_id": review_id,
                "code": "NUMBERS_INVALID",
                "message": "报告里出现了事实包里没有的数字（疑似编造），已拒绝推送。"
                           "请让模型重写，或加 --allow-unsourced-numbers 强制渲染。",
                "validation": validation,
            }

        report_md = render_markdown(facts, text, self.cfg)
        paths = write_artifacts(self.cfg.var_dir, review_id, facts, "", report_md)
        self.repo.update_review(
            review_id, status="rendered", report_md=report_md, validation=validation,
            llm_model=model, llm_mode=mode or self.llm_mode,
            input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
            latency_ms=latency_ms, error=None)
        if input_tokens or output_tokens:
            self.repo.log_llm_usage(account_id=review["account_id"], review_id=review_id,
                                    model=model, mode=mode or self.llm_mode,
                                    input_tokens=input_tokens, output_tokens=output_tokens,
                                    latency_ms=latency_ms, trace_id=new_trace_id())
        steps = self.bump_step(review_id)
        return {"ok": True, "status": "rendered", "review_id": review_id,
                "steps_used": steps, "validation": validation,
                "report_md": report_md, "paths": paths,
                "next": "调用 push 推送到飞书"}

    # =================================================================
    # 阶段 4：推送
    # =================================================================
    def push(self, review_id: int, chat_id: str | None = None) -> dict:
        review = self._require_review(review_id)
        report_md = review.get("report_md")
        if not report_md:
            raise DyReviewError(f"复盘 #{review_id} 还没有报告正文，请先 render")
        if self.feishu is None:
            raise DyReviewError("未初始化飞书客户端")
        account = self.repo.get_account(int(review["account_id"]))
        target = chat_id or (account.feishu_chat_id if account else None) \
            or self.cfg.get("feishu.default_chat_id")
        facts = review.get("facts") or {}
        period = facts.get("display", {}).get("period") or \
            f"{review['period_start']} ~ {review['period_end']}"
        title = f"{account.account_name if account else '账号'} 数据复盘｜{period}"
        footer = "数据来源：抖音开放平台 · 仁维 Crabot 定时采集"
        result = self.feishu.push_report(receive_id=target, title=title,
                                         markdown=report_md, footer=footer)
        if result.get("ok"):
            self.repo.update_review(review_id, status="pushed",
                                    feishu_chat_id=target,
                                    feishu_msg_ids=result.get("message_ids") or [])
        else:
            self.repo.update_review(review_id, error=f"推送失败：{result.get('error')}")
        steps = self.bump_step(review_id)
        return {"ok": bool(result.get("ok")), "status": "pushed" if result.get("ok") else "push_failed",
                "review_id": review_id, "steps_used": steps,
                "channel": result.get("channel"), "message_ids": result.get("message_ids"),
                "error": result.get("error"), "account": account.to_dict() if account else None}

    # =================================================================
    # 一步到位（定时任务 / 单轮对话）
    # =================================================================
    def run(self, term: str, *, days: int | None = None, push: bool = True,
            llm: str | None = None, auto_collect_hours: float = 6.0,
            force: bool = False, chat_id: str | None = None,
            request_text: str | None = None) -> dict:
        mode = (llm or self.llm_mode).lower()
        started = self.start(term, days=days,
                             source="feishu" if request_text else "cron",
                             request_text=request_text, force=force,
                             auto_collect_hours=auto_collect_hours)
        if not started.get("ok"):
            return started
        if started["status"] == "deduped":
            return started

        review_id = started["review_id"]
        if mode == "builtin":
            rendered = self.generate(review_id)
        else:
            started["next"] = ("把 prompt 交给大模型生成报告正文，然后调用 "
                               "`dyreview render --review-id %d --llm-output <file>`" % review_id)
            started["llm_mode"] = "handoff"
            return started

        if not rendered.get("ok"):
            return rendered
        if not push:
            return {**rendered, "status": "rendered", "llm_mode": mode}
        pushed = self.push(review_id, chat_id)
        return {**pushed, "validation": rendered.get("validation"),
                "report_md": rendered.get("report_md"), "llm_mode": mode}

    # =================================================================
    def _require_review(self, review_id: int) -> dict:
        review = self.repo.get_review(int(review_id))
        if not review:
            raise DyReviewError(f"复盘记录 #{review_id} 不存在", detail="检查 review_id 是否正确")
        return review
