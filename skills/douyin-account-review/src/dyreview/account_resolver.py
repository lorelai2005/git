"""把用户在飞书里敲的“客户抖音账号id”解析成库里的账号。

真实场景里用户会这样写，都得认：
    /复盘 A1234567890abcdef          -> open_id
    /复盘 环宇家居                    -> 内部账号名
    /复盘 huanyu_jiaju               -> 抖音号
    /复盘 环宇                        -> 模糊匹配（可能命中多个，必须让用户选）
    /复盘 13800138000                -> 机构/客户编号 org_id
"""
from __future__ import annotations

import logging

from .errors import AccountAmbiguous, AccountNotFound
from .models import Account
from .repo import Repo

log = logging.getLogger(__name__)


def _exact_keys(account: Account) -> set[str]:
    keys = {account.open_id.lower(), account.account_name.lower()}
    if account.douyin_id:
        keys.add(account.douyin_id.lower())
    if account.nickname:
        keys.add(account.nickname.lower())
    if account.org_id:
        keys.add(account.org_id.lower())
    return keys


def resolve_account(repo: Repo, term: str, *, allow_fuzzy: bool = True) -> Account:
    term = (term or "").strip().strip("「」\"'“”")
    if not term:
        raise AccountNotFound("账号标识为空，请提供抖音号、账号名称或 open_id")

    exact = repo.get_account_by_open_id(term)
    if exact is not None:
        return exact

    candidates = repo.search_accounts(term, limit=10)
    if not candidates:
        raise AccountNotFound(
            f"没有找到与「{term}」匹配的账号。可先用 `account list` 查看已接入账号，"
            f"或让运营在 dy.account 里登记该客户账号。")

    exact_hits = [c for c in candidates if term.lower() in _exact_keys(c)]
    if len(exact_hits) == 1:
        return exact_hits[0]
    if len(candidates) == 1 and allow_fuzzy:
        log.info("账号模糊匹配唯一命中：%s -> %s", term, candidates[0].account_name)
        return candidates[0]

    raise AccountAmbiguous(
        f"「{term}」匹配到 {len(candidates)} 个账号，请指定具体一个",
        [c.to_dict() for c in candidates[:8]],
    )


def list_choices(repo: Repo, limit: int = 20) -> list[dict]:
    return [a.to_dict() for a in repo.list_accounts(status=None)[:limit]]
