"""SQL 语法校验（用 pglast 这个真 PostgreSQL 解析器，不是正则糊弄）。

为什么要这个测试：193 服务器上的表结构和 SQL 在这里跑不了真库，
但**语法错误**可以在本地全部拦住——括号、`ON CONFLICT`、`FILTER`、`::interval`
这类手写 SQL 的高频坑，解析器一过就知道。

做法：把 repo.py / db.py 里所有 f-string SQL 抽出来，把 `{self.s}` 之类占位
替换成 schema 名、把 `%s` 占位替换成字面量，再交给 pglast 解析。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pglast = pytest.importorskip("pglast", reason="需要 pglast 才能做 SQL 语法校验：pip install pglast")

SRC = Path(__file__).resolve().parents[1] / "src" / "dyreview"
SQL_MODULES = ("repo.py", "db.py", "collect.py", "facts.py")

PYFORMAT_RE = re.compile(r"%\([a-zA-Z_][a-zA-Z0-9_]*\)s")


def extract_sql_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        pieces: list[str] = []
        dynamic = False
        for value in node.values:
            if isinstance(value, ast.Constant):
                pieces.append(str(value.value))
                continue
            # 只把 {self.s} / {self.schema} 这类 schema 占位替换成 schema 名；
            # 其它动态拼接（如 ", ".join(sets)）无法静态还原，标记后跳过。
            if isinstance(value.value, ast.Attribute) and value.value.attr in ("s", "schema"):
                pieces.append("dy")
            else:
                dynamic = True
        text = "".join(pieces)
        if dynamic:
            continue
        if _looks_like_sql(text):
            out.append(text)
    return out


def _looks_like_sql(text: str) -> bool:
    upper = text.upper()
    return ("SELECT " in upper or "INSERT INTO " in upper or "UPDATE " in upper
            or "DELETE FROM " in upper or "CREATE " in upper)


def _bind_placeholders(sql: str) -> str:
    sql = PYFORMAT_RE.sub("'x'", sql)
    return sql.replace("%s", "'x'")


@pytest.mark.parametrize("module", SQL_MODULES)
def test_all_sql_statement_syntax_valid(module):
    path = SRC / module
    statements = extract_sql_strings(path)
    if module == "repo.py":
        assert len(statements) >= 20, f"repo.py 里应该有很多手写 SQL，实际只抽到 {len(statements)}"
    failures = []
    for sql in statements:
        bound = _bind_placeholders(sql)
        try:
            pglast.parse_sql(bound)
        except Exception as exc:  # pragma: no cover - 出错时才走
            failures.append({"error": str(exc), "sql": bound[:400]})
    assert not failures, failures


def test_schema_sql_parses_into_expected_objects():
    sql = (Path(__file__).resolve().parents[1] / "config" / "schema.sql").read_text(encoding="utf-8")
    parsed = pglast.parse_sql(sql)
    assert len(parsed) >= 40, "建表脚本应包含全部 DDL"
    text = sql.lower()
    for table in ("account", "oauth_token", "oauth_event", "collect_run", "video",
                  "video_metric", "account_metric", "fan_profile", "review",
                  "llm_usage", "kv", "task_lock"):
        assert f"create table if not exists dy.{table}" in text, f"缺少表 {table}"
    for view in ("v_video_latest", "v_account_metric_delta", "v_token_health",
                 "v_data_freshness"):
        assert f"create or replace view dy.{view}" in text, f"缺少视图 {view}"


def test_schema_uses_postgres_specific_types_not_mysql():
    """需求明确是 PostgreSQL：检查没有混入 MySQL 写法（注释里的说明不算）。"""
    raw = (Path(__file__).resolve().parents[1] / "config" / "schema.sql").read_text(encoding="utf-8")
    sql = "\n".join(line for line in raw.splitlines()
                    if not line.lstrip().startswith("--")).lower()
    for mysql_only in ("auto_increment", "engine=innodb", "`", "mediumint",
                       "datetime default current_timestamp"):
        assert mysql_only not in sql, f"schema.sql 混入了 MySQL 写法：{mysql_only}"
    for pg_specific in ("bigserial", "jsonb", "timestamptz", "text[]", "numeric(8,5)",
                        "on conflict", "plpgsql"):
        assert pg_specific in sql, f"schema.sql 缺少 PostgreSQL 特征：{pg_specific}"


def test_dynamic_update_review_sql_is_valid():
    """update_review 的 SET 是动态拼的，单独构造一次验证语法。"""
    from dyreview.repo import Repo

    repo = object.__new__(Repo)
    repo.s = "dy"
    captured: list[str] = []

    class FakeDB:
        pass

    repo.db = FakeDB()  # type: ignore[assignment]
    repo._x = lambda sql, params=None: captured.append(sql) or 1  # type: ignore[assignment]
    repo.update_review(1, status="pushed", report_md="正文", validation={"ok": True},
                       feishu_msg_ids=["om_1"])
    assert captured, "没有捕获到 SQL"
    bound = _bind_placeholders(captured[0])
    pglast.parse_sql(bound)
