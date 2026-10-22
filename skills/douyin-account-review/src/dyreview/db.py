"""PostgreSQL 连接、事务、会话级 advisory lock、建表校验。

所有 SQL 集中在 repo.py；本模块只负责“怎么连、怎么保证并发安全”。
"""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import SKILL_ROOT, Config
from .errors import ConfigError, SchemaError

log = logging.getLogger(__name__)

SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REQUIRED_TABLES = (
    "account", "oauth_token", "oauth_event", "collect_run", "video",
    "video_metric", "account_metric", "fan_profile", "review", "llm_usage",
    "kv", "task_lock",
)
EXPECTED_SCHEMA_VERSION = 1


def lock_key_int(key: str) -> int:
    """把字符串锁名稳定映射为 PostgreSQL bigint advisory lock id。"""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class Database:
    """薄封装：一个实例一条连接，命令结束后 close()。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.schema = cfg.schema
        if not SCHEMA_NAME_RE.match(self.schema):
            raise ConfigError(f"database.schema 非法：{self.schema!r}")
        self._conn: Any = None

    # ------------------------------------------------------------ 连接
    @property
    def conn(self) -> Any:
        if self._conn is None or getattr(self._conn, "closed", True):
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("缺少 psycopg，请 pip install 'psycopg[binary]'") from exc

        dsn = self.cfg.require("database.dsn", "193 服务器 PostgreSQL 连接串")
        stmt_ms = self.cfg.get_int("database.statement_timeout_ms", 30000)
        conn_timeout = self.cfg.get_int("database.connect_timeout_seconds", 10)
        conn = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=conn_timeout,
            options=f"-c statement_timeout={int(stmt_ms)}",
            application_name="dyreview",
        )
        log.debug("已连接 PostgreSQL：%s（schema=%s）", conn.info.dbname, self.schema)
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover
                pass
            raise

    # ------------------------------------------------------------ 基础查询
    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
        self.conn.commit()
        return list(rows)

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        self.conn.commit()
        if row is None:
            return None
        return next(iter(row.values())) if isinstance(row, dict) else row[0]

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.rowcount

    # ------------------------------------------------------------ 会话锁
    def try_advisory_lock(self, key: str) -> bool:
        got = self.scalar("SELECT pg_try_advisory_lock(%s) AS locked", (lock_key_int(key),))
        return bool(got)

    def unlock_advisory(self, key: str) -> None:
        try:
            self.scalar("SELECT pg_advisory_unlock(%s)", (lock_key_int(key),))
        except Exception:  # pragma: no cover
            log.warning("释放 advisory lock 失败（连接可能已断开）：%s", key)

    @contextmanager
    def advisory_lock(self, key: str) -> Iterator[bool]:
        """拿不到锁就返回 False，由调用方决定“跳过”还是“等待”。"""
        got = False
        try:
            got = self.try_advisory_lock(key)
            yield got
        finally:
            if got:
                self.unlock_advisory(key)

    # ------------------------------------------------------------ 建表
    def _load_ddl(self) -> str:
        ddl_path = Path(SKILL_ROOT) / "config" / "schema.sql"
        if not ddl_path.exists():
            raise ConfigError(f"找不到建表脚本：{ddl_path}")
        sql = ddl_path.read_text(encoding="utf-8")
        if self.schema != "dy":
            sql = re.sub(r"\bdy\.", f'"{self.schema}".', sql)
            sql = sql.replace("CREATE SCHEMA IF NOT EXISTS dy;",
                              f'CREATE SCHEMA IF NOT EXISTS "{self.schema}";')
        # psycopg 自己带事务，去掉脚本里的显式边界，避免 “already a transaction” 噪音
        sql = re.sub(r"(?m)^\s*(BEGIN|COMMIT)\s*;\s*$", "", sql)
        return sql

    def init_schema(self, *, dry_run: bool = False) -> dict:
        sql = self._load_ddl()
        if dry_run:
            return {"dry_run": True, "schema": self.schema, "statements_chars": len(sql)}
        conn = self.conn
        old_autocommit = conn.autocommit
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = old_autocommit
        return self.verify_schema()

    def verify_schema(self) -> dict:
        rows = self.query(
            """
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = %s
            """,
            (self.schema,),
        )
        present = {r["table_name"] for r in rows}
        missing = [t for t in REQUIRED_TABLES if t not in present]
        version = None
        if "schema_migration" in present:
            version = self.scalar(
                f'SELECT max(version) AS v FROM "{self.schema}".schema_migration')
        return {
            "schema": self.schema,
            "tables_present": len(present & set(REQUIRED_TABLES)),
            "tables_expected": len(REQUIRED_TABLES),
            "missing": missing,
            "version": version,
            "ok": not missing and (version or 0) >= EXPECTED_SCHEMA_VERSION,
        }

    def require_schema(self) -> dict:
        info = self.verify_schema()
        if not info["ok"]:
            raise SchemaError(
                f"数据库 schema `{self.schema}` 未就绪：缺少表 {info['missing']}，"
                f"版本 {info['version']}（期望 {EXPECTED_SCHEMA_VERSION}）。请先执行 init-db。",
                detail=str(info),
            )
        return info

    def ping(self) -> dict:
        row = self.query_one(
            "SELECT current_database() AS db, current_user AS usr, version() AS version, now() AS ts"
        )
        return row or {}
