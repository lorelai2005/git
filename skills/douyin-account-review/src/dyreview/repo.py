"""所有 SQL 访问的唯一入口。

约定
----
* 表名统一 `{s}.` 前缀，`s` 来自配置并已做标识符校验。
* JSONB 参数用 `_json()` 包一层，避免 psycopg 类型适配问题。
* 写操作幂等：视频/指标一律 upsert，重跑不会产生脏数据。
* 拿不到值的列不覆盖已有值（COALESCE 保护），避免某次接口少返回字段把历史洗成 NULL。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Sequence

from .db import Database
from .models import Account, TokenRecord, VideoMeta

log = logging.getLogger(__name__)


def _json(obj: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
        return Jsonb(obj)
    except ImportError:  # pragma: no cover - 测试桩
        return obj


def _row_to_account(row: dict | None) -> Account | None:
    return Account.from_row(row) if row else None


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.s = db.schema

    def _q(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        return self.db.query(sql, params)

    def _q1(self, sql: str, params: Sequence[Any] | None = None) -> dict | None:
        return self.db.query_one(sql, params)

    def _x(self, sql: str, params: Sequence[Any] | None = None) -> int:
        return self.db.execute(sql, params)

    # =====================================================================
    # 账号
    # =====================================================================
    def list_accounts(self, status: str | None = "active") -> list[Account]:
        if status:
            rows = self._q(f"SELECT * FROM {self.s}.account WHERE status = %s ORDER BY account_name",
                           (status,))
        else:
            rows = self._q(f"SELECT * FROM {self.s}.account ORDER BY account_name")
        return [Account.from_row(r) for r in rows]

    def get_account(self, account_id: int) -> Account | None:
        return _row_to_account(
            self._q1(f"SELECT * FROM {self.s}.account WHERE id = %s", (account_id,)))

    def get_account_by_open_id(self, open_id: str, client_key: str = "default") -> Account | None:
        return _row_to_account(self._q1(
            f"SELECT * FROM {self.s}.account WHERE open_id = %s AND client_key = %s",
            (open_id, client_key)))

    def search_accounts(self, term: str, limit: int = 10) -> list[Account]:
        """飞书指令里的“客户账号id”可能是 open_id / 抖音号 / 内部名称 / 昵称 / 机构号。"""
        term = (term or "").strip()
        if not term:
            return []
        like = f"%{term}%"
        rows = self._q(
            f"""
            SELECT * FROM {self.s}.account
             WHERE status <> 'revoked'
               AND ( open_id = %(t)s
                  OR douyin_id = %(t)s
                  OR lower(account_name) = lower(%(t)s)
                  OR lower(coalesce(nickname,'')) = lower(%(t)s)
                  OR org_id = %(t)s
                  OR account_name ILIKE %(like)s
                  OR coalesce(nickname,'') ILIKE %(like)s
                  OR coalesce(douyin_id,'') ILIKE %(like)s )
             ORDER BY (open_id = %(t)s) DESC,
                      (coalesce(douyin_id,'') = %(t)s) DESC,
                      (lower(account_name) = lower(%(t)s)) DESC,
                      account_name
             LIMIT %(limit)s
            """,
            {"t": term, "like": like, "limit": limit},
        )
        return [Account.from_row(r) for r in rows]

    def upsert_account(self, *, open_id: str, account_name: str, client_key: str = "default",
                       douyin_id: str | None = None, nickname: str | None = None,
                       avatar_url: str | None = None, org_id: str | None = None,
                       status: str = "active", data_scope: str | None = None,
                       feishu_chat_id: str | None = None) -> Account:
        row = self._q1(
            f"""
            INSERT INTO {self.s}.account
                (open_id, client_key, account_name, douyin_id, nickname, avatar_url,
                 org_id, status, data_scope, feishu_chat_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_key, open_id) DO UPDATE SET
                account_name = COALESCE(EXCLUDED.account_name, {self.s}.account.account_name),
                douyin_id    = COALESCE(EXCLUDED.douyin_id,    {self.s}.account.douyin_id),
                nickname     = COALESCE(EXCLUDED.nickname,     {self.s}.account.nickname),
                avatar_url   = COALESCE(EXCLUDED.avatar_url,   {self.s}.account.avatar_url),
                org_id       = COALESCE(EXCLUDED.org_id,       {self.s}.account.org_id),
                data_scope   = COALESCE(EXCLUDED.data_scope,   {self.s}.account.data_scope),
                feishu_chat_id = COALESCE(EXCLUDED.feishu_chat_id, {self.s}.account.feishu_chat_id),
                status       = CASE WHEN {self.s}.account.status = 'revoked'
                                    THEN {self.s}.account.status ELSE EXCLUDED.status END,
                updated_at   = now()
            RETURNING *
            """,
            (open_id, client_key, account_name, douyin_id, nickname, avatar_url,
             org_id, status, data_scope, feishu_chat_id),
        )
        assert row is not None
        return Account.from_row(row)

    def update_account_profile(self, account_id: int, *, douyin_id: str | None = None,
                               nickname: str | None = None, avatar_url: str | None = None) -> None:
        self._x(
            f"""
            UPDATE {self.s}.account SET
                douyin_id  = COALESCE(%s, douyin_id),
                nickname   = COALESCE(%s, nickname),
                avatar_url = COALESCE(%s, avatar_url)
             WHERE id = %s
            """,
            (douyin_id, nickname, avatar_url, account_id),
        )

    def set_account_status(self, account_id: int, status: str, remark: str | None = None) -> None:
        self._x(
            f"UPDATE {self.s}.account SET status = %s, remark = COALESCE(%s, remark) WHERE id = %s",
            (status, remark, account_id),
        )

    def touch_last_collect(self, account_id: int, when: datetime | None = None) -> None:
        self._x(f"UPDATE {self.s}.account SET last_collect_at = COALESCE(%s, now()) WHERE id = %s",
                (when, account_id))

    # =====================================================================
    # Token
    # =====================================================================
    def get_token(self, account_id: int) -> TokenRecord | None:
        row = self._q1(f"SELECT * FROM {self.s}.oauth_token WHERE account_id = %s", (account_id,))
        if not row:
            return None
        return TokenRecord(
            account_id=int(row["account_id"]),
            access_token=row["access_token"],
            access_expires_at=row["access_expires_at"],
            refresh_token=row["refresh_token"],
            refresh_expires_at=row["refresh_expires_at"],
            access_issued_at=row["access_issued_at"],
            access_ttl_seconds=row["access_ttl_seconds"],
            scope=row["scope"],
            refreshed_at=row["refreshed_at"],
            refresh_count=int(row["refresh_count"] or 0),
            version=int(row["version"] or 0),
            last_refresh_error=row["last_refresh_error"],
        )

    def insert_token(self, rec: TokenRecord) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.oauth_token
                (account_id, access_token, refresh_token, access_expires_at, refresh_expires_at,
                 access_issued_at, access_ttl_seconds, scope)
            VALUES (%s,%s,%s,%s,%s, COALESCE(%s, now()), %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                access_token       = EXCLUDED.access_token,
                refresh_token      = COALESCE(EXCLUDED.refresh_token, {self.s}.oauth_token.refresh_token),
                access_expires_at  = EXCLUDED.access_expires_at,
                refresh_expires_at = COALESCE(EXCLUDED.refresh_expires_at, {self.s}.oauth_token.refresh_expires_at),
                access_issued_at   = COALESCE(EXCLUDED.access_issued_at, now()),
                access_ttl_seconds = EXCLUDED.access_ttl_seconds,
                scope              = COALESCE(EXCLUDED.scope, {self.s}.oauth_token.scope),
                refreshed_at       = now(),
                version            = {self.s}.oauth_token.version + 1,
                last_refresh_error = NULL
            """,
            (rec.account_id, rec.access_token, rec.refresh_token, rec.access_expires_at,
             rec.refresh_expires_at, rec.access_issued_at, rec.access_ttl_seconds, rec.scope),
        )

    def save_refreshed_token(self, rec: TokenRecord, expected_version: int) -> int | None:
        """乐观锁写入；返回新 version，None 表示版本冲突（别人已经刷过了）。"""
        row = self._q1(
            f"""
            UPDATE {self.s}.oauth_token SET
                access_token       = %s,
                refresh_token      = COALESCE(%s, refresh_token),
                access_expires_at  = %s,
                refresh_expires_at = COALESCE(%s, refresh_expires_at),
                access_issued_at   = COALESCE(%s, now()),
                access_ttl_seconds = %s,
                scope              = COALESCE(%s, scope),
                refreshed_at       = now(),
                refresh_count      = refresh_count + 1,
                last_refresh_error = NULL,
                version            = version + 1
             WHERE account_id = %s AND version = %s
            RETURNING version
            """,
            (rec.access_token, rec.refresh_token, rec.access_expires_at, rec.refresh_expires_at,
             rec.access_issued_at, rec.access_ttl_seconds, rec.scope, rec.account_id, expected_version),
        )
        return int(row["version"]) if row else None

    def set_token_error(self, account_id: int, error: str) -> None:
        self._x(f"UPDATE {self.s}.oauth_token SET last_refresh_error = %s WHERE account_id = %s",
                (error[:500], account_id))

    def log_oauth_event(self, account_id: int, event: str, *, ok: bool = True,
                        error_code: str | None = None, description: str | None = None,
                        logid: str | None = None, detail: dict | None = None) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.oauth_event
                (account_id, event, ok, error_code, description, logid, detail)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (account_id, event, ok, error_code, (description or "")[:1000], logid,
             _json(detail) if detail else None),
        )

    def token_health(self) -> list[dict]:
        return self._q(f"SELECT * FROM {self.s}.v_token_health ORDER BY account_name")

    # =====================================================================
    # 采集批次
    # =====================================================================
    def start_run(self, account_id: int | None, kind: str) -> int:
        row = self._q1(
            f"INSERT INTO {self.s}.collect_run (account_id, kind, status) VALUES (%s,%s,'running') RETURNING id",
            (account_id, kind))
        return int(row["id"])  # type: ignore[index]

    def finish_run(self, run_id: int, status: str, *, api_calls: int = 0, video_pages: int = 0,
                   videos_seen: int = 0, videos_upserted: int = 0, token_refreshed: bool = False,
                   error_code: str | None = None, error_msg: str | None = None,
                   detail: dict | None = None) -> None:
        self._x(
            f"""
            UPDATE {self.s}.collect_run SET
                status = %s, finished_at = now(), api_calls = %s, video_pages = %s,
                videos_seen = %s, videos_upserted = %s, token_refreshed = %s,
                error_code = %s, error_msg = %s, detail = %s
             WHERE id = %s
            """,
            (status, api_calls, video_pages, videos_seen, videos_upserted, token_refreshed,
             error_code, (error_msg or "")[:1000] or None,
             _json(detail) if detail else None, run_id),
        )

    # =====================================================================
    # 作品与指标
    # =====================================================================
    def upsert_video(self, account_id: int, v: VideoMeta) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.video
                (account_id, item_id, title, description, cover_url, share_url, create_time,
                 duration_sec, media_type, is_top, topics, raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id, item_id) DO UPDATE SET
                title        = COALESCE(EXCLUDED.title,       {self.s}.video.title),
                description  = COALESCE(EXCLUDED.description, {self.s}.video.description),
                cover_url    = COALESCE(EXCLUDED.cover_url,   {self.s}.video.cover_url),
                share_url    = COALESCE(EXCLUDED.share_url,   {self.s}.video.share_url),
                create_time  = COALESCE(EXCLUDED.create_time, {self.s}.video.create_time),
                duration_sec = COALESCE(EXCLUDED.duration_sec,{self.s}.video.duration_sec),
                media_type   = COALESCE(EXCLUDED.media_type,  {self.s}.video.media_type),
                is_top       = EXCLUDED.is_top,
                topics       = COALESCE(EXCLUDED.topics,      {self.s}.video.topics),
                last_seen_at = now(),
                raw          = COALESCE(EXCLUDED.raw,         {self.s}.video.raw)
            """,
            (account_id, v.item_id, v.title, v.description, v.cover_url, v.share_url,
             v.create_time, v.duration_sec, v.media_type, v.is_top, v.topics or None,
             _json(v.raw) if v.raw else None),
        )

    def upsert_video_metric(self, account_id: int, item_id: str, stat_date: date,
                            stats: dict, raw: dict | None = None) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.video_metric
                (account_id, item_id, stat_date, play_count, like_count, comment_count,
                 share_count, collect_count, forward_count, finish_rate, full_play_rate,
                 bounce_rate, avg_play_duration_ms, cover_click_rate, raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id, item_id, stat_date) DO UPDATE SET
                play_count           = COALESCE(EXCLUDED.play_count,           {self.s}.video_metric.play_count),
                like_count           = COALESCE(EXCLUDED.like_count,           {self.s}.video_metric.like_count),
                comment_count        = COALESCE(EXCLUDED.comment_count,        {self.s}.video_metric.comment_count),
                share_count          = COALESCE(EXCLUDED.share_count,          {self.s}.video_metric.share_count),
                collect_count        = COALESCE(EXCLUDED.collect_count,        {self.s}.video_metric.collect_count),
                forward_count        = COALESCE(EXCLUDED.forward_count,        {self.s}.video_metric.forward_count),
                finish_rate          = COALESCE(EXCLUDED.finish_rate,          {self.s}.video_metric.finish_rate),
                full_play_rate       = COALESCE(EXCLUDED.full_play_rate,       {self.s}.video_metric.full_play_rate),
                bounce_rate          = COALESCE(EXCLUDED.bounce_rate,          {self.s}.video_metric.bounce_rate),
                avg_play_duration_ms = COALESCE(EXCLUDED.avg_play_duration_ms, {self.s}.video_metric.avg_play_duration_ms),
                cover_click_rate     = COALESCE(EXCLUDED.cover_click_rate,     {self.s}.video_metric.cover_click_rate),
                raw                  = COALESCE(EXCLUDED.raw,                  {self.s}.video_metric.raw),
                collected_at         = now()
            """,
            (account_id, item_id, stat_date,
             stats.get("play_count"), stats.get("like_count"), stats.get("comment_count"),
             stats.get("share_count"), stats.get("collect_count"), stats.get("forward_count"),
             stats.get("finish_rate"), stats.get("full_play_rate"), stats.get("bounce_rate"),
             stats.get("avg_play_duration_ms"), stats.get("cover_click_rate"),
             _json(raw) if raw else None),
        )

    def latest_video_metrics(self, account_id: int) -> list[dict]:
        return self._q(f"SELECT * FROM {self.s}.v_video_latest WHERE account_id = %s",
                       (account_id,))

    def videos_published_between(self, account_id: int, start: date, end: date) -> list[dict]:
        return self._q(
            f"""
            SELECT * FROM {self.s}.v_video_latest
             WHERE account_id = %s
               AND create_time >= %s::date
               AND create_time <  (%s::date + interval '1 day')
            """,
            (account_id, start, end),
        )

    def video_metric_history(self, account_id: int, item_id: str) -> list[dict]:
        return self._q(
            f"""
            SELECT stat_date, play_count, like_count, comment_count, share_count,
                   collect_count, finish_rate
              FROM {self.s}.video_metric
             WHERE account_id = %s AND item_id = %s
             ORDER BY stat_date
            """,
            (account_id, item_id),
        )

    def count_videos(self, account_id: int) -> int:
        return int(self.db.scalar(f"SELECT count(*) AS c FROM {self.s}.video WHERE account_id = %s",
                                  (account_id,)) or 0)

    def video_window_growth(self, account_id: int, start: date, end: date) -> list[dict]:
        """窗口内每条作品的指标增量。

        抖音返回的播放/点赞是**累计值**，复盘要看“这段时间涨了多少”，
        所以取「窗口内最后一次快照」减去「窗口开始前最后一次快照」。
        窗口内新发的作品（没有窗口前快照）按全量计入。
        """
        return self._q(
            f"""
            WITH win AS (
                SELECT item_id,
                       max(stat_date)  AS last_date,
                       max(play_count)    AS play_end,
                       max(like_count)    AS like_end,
                       max(comment_count) AS comment_end,
                       max(share_count)   AS share_end,
                       max(collect_count) AS collect_end,
                       max(finish_rate)   AS finish_rate,
                       max(bounce_rate)   AS bounce_rate,
                       max(cover_click_rate) AS cover_click_rate,
                       max(full_play_rate)   AS full_play_rate
                  FROM {self.s}.video_metric
                 WHERE account_id = %s AND stat_date BETWEEN %s AND %s
                 GROUP BY item_id
            )
            SELECT w.item_id, w.last_date, w.play_end, w.like_end, w.comment_end,
                   w.share_end, w.collect_end, w.finish_rate, w.bounce_rate,
                   w.cover_click_rate, w.full_play_rate,
                   (SELECT b.play_count    FROM {self.s}.video_metric b
                     WHERE b.account_id = %s AND b.item_id = w.item_id AND b.stat_date < %s
                     ORDER BY b.stat_date DESC LIMIT 1) AS play_before,
                   (SELECT b.like_count    FROM {self.s}.video_metric b
                     WHERE b.account_id = %s AND b.item_id = w.item_id AND b.stat_date < %s
                     ORDER BY b.stat_date DESC LIMIT 1) AS like_before,
                   (SELECT b.comment_count FROM {self.s}.video_metric b
                     WHERE b.account_id = %s AND b.item_id = w.item_id AND b.stat_date < %s
                     ORDER BY b.stat_date DESC LIMIT 1) AS comment_before,
                   (SELECT b.share_count   FROM {self.s}.video_metric b
                     WHERE b.account_id = %s AND b.item_id = w.item_id AND b.stat_date < %s
                     ORDER BY b.stat_date DESC LIMIT 1) AS share_before,
                   (SELECT b.collect_count FROM {self.s}.video_metric b
                     WHERE b.account_id = %s AND b.item_id = w.item_id AND b.stat_date < %s
                     ORDER BY b.stat_date DESC LIMIT 1) AS collect_before
              FROM win w
            """,
            (account_id, start, end,
             account_id, start, account_id, start, account_id, start,
             account_id, start, account_id, start),
        )

    # =====================================================================
    # 账号指标 / 粉丝画像
    # =====================================================================
    def upsert_account_metric(self, account_id: int, stat_date: date, stats: dict,
                              raw: dict | None = None) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.account_metric
                (account_id, stat_date, follower_count, new_follower_count, unfollower_count,
                 total_favorited, video_count, profile_visit_count, home_visit_count, raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id, stat_date) DO UPDATE SET
                follower_count      = COALESCE(EXCLUDED.follower_count,      {self.s}.account_metric.follower_count),
                new_follower_count  = COALESCE(EXCLUDED.new_follower_count,  {self.s}.account_metric.new_follower_count),
                unfollower_count    = COALESCE(EXCLUDED.unfollower_count,    {self.s}.account_metric.unfollower_count),
                total_favorited     = COALESCE(EXCLUDED.total_favorited,     {self.s}.account_metric.total_favorited),
                video_count         = COALESCE(EXCLUDED.video_count,         {self.s}.account_metric.video_count),
                profile_visit_count = COALESCE(EXCLUDED.profile_visit_count, {self.s}.account_metric.profile_visit_count),
                home_visit_count    = COALESCE(EXCLUDED.home_visit_count,    {self.s}.account_metric.home_visit_count),
                raw                 = COALESCE(EXCLUDED.raw,                 {self.s}.account_metric.raw),
                collected_at        = now()
            """,
            (account_id, stat_date, stats.get("follower_count"), stats.get("new_follower_count"),
             stats.get("unfollower_count"), stats.get("total_favorited"), stats.get("video_count"),
             stats.get("profile_visit_count"), stats.get("home_visit_count"),
             _json(raw) if raw else None),
        )

    def account_metric_range(self, account_id: int, start: date, end: date) -> list[dict]:
        return self._q(
            f"""
            SELECT * FROM {self.s}.account_metric
             WHERE account_id = %s AND stat_date BETWEEN %s AND %s
             ORDER BY stat_date
            """,
            (account_id, start, end),
        )

    def latest_account_metric(self, account_id: int) -> dict | None:
        return self._q1(
            f"SELECT * FROM {self.s}.account_metric WHERE account_id = %s ORDER BY stat_date DESC LIMIT 1",
            (account_id,))

    def upsert_fan_profile(self, account_id: int, stat_date: date, dimension: str,
                           payload: list[dict]) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.fan_profile (account_id, stat_date, dimension, payload)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (account_id, stat_date, dimension)
            DO UPDATE SET payload = EXCLUDED.payload, collected_at = now()
            """,
            (account_id, stat_date, dimension, _json(payload)),
        )

    def latest_fan_profile(self, account_id: int) -> list[dict]:
        return self._q(
            f"""
            SELECT DISTINCT ON (dimension) dimension, stat_date, payload
              FROM {self.s}.fan_profile
             WHERE account_id = %s
             ORDER BY dimension, stat_date DESC
            """,
            (account_id,),
        )

    def data_freshness(self, account_id: int) -> dict:
        row = self._q1(f"SELECT * FROM {self.s}.v_data_freshness WHERE account_id = %s",
                       (account_id,))
        return row or {}

    # =====================================================================
    # 复盘工单
    # =====================================================================
    REVIEW_FIELDS = {
        "status", "facts", "diagnosis", "prompt_md", "report_md", "llm_model", "llm_mode",
        "input_tokens", "output_tokens", "latency_ms", "validation", "feishu_chat_id",
        "feishu_msg_ids", "error", "request_text", "period_start", "period_end", "source",
    }

    def create_review(self, account_id: int, period_start: date, period_end: date,
                      *, source: str = "feishu", request_text: str | None = None) -> int:
        row = self._q1(
            f"""
            INSERT INTO {self.s}.review (account_id, period_start, period_end, source, request_text)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
            """,
            (account_id, period_start, period_end, source, request_text),
        )
        return int(row["id"])  # type: ignore[index]

    def get_review(self, review_id: int) -> dict | None:
        return self._q1(f"SELECT * FROM {self.s}.review WHERE id = %s", (review_id,))

    def update_review(self, review_id: int, **fields: Any) -> None:
        bad = set(fields) - self.REVIEW_FIELDS
        if bad:
            raise ValueError(f"不允许更新的字段：{bad}")
        if not fields:
            return
        sets, params = [], []
        for key, value in fields.items():
            sets.append(f"{key} = %s")
            if key in ("facts", "diagnosis", "validation") and value is not None:
                params.append(_json(value))
            elif key == "feishu_msg_ids":
                params.append(value)
            else:
                params.append(value)
        params.append(review_id)
        self._x(
            f"UPDATE {self.s}.review SET {', '.join(sets)}, updated_at = now() WHERE id = %s",
            tuple(params),
        )

    def recent_review(self, account_id: int, minutes: int,
                      statuses: Sequence[str] = ("rendered", "pushed")) -> dict | None:
        if minutes <= 0:
            return None
        return self._q1(
            f"""
            SELECT * FROM {self.s}.review
             WHERE account_id = %s
               AND status = ANY(%s)
               AND created_at > now() - (%s || ' minutes')::interval
             ORDER BY created_at DESC LIMIT 1
            """,
            (account_id, list(statuses), str(minutes)),
        )

    def list_reviews(self, account_id: int | None = None, limit: int = 20) -> list[dict]:
        if account_id:
            return self._q(
                f"""SELECT id, account_id, period_start, period_end, status, source,
                           llm_model, input_tokens, output_tokens, created_at
                      FROM {self.s}.review WHERE account_id = %s
                     ORDER BY created_at DESC LIMIT %s""",
                (account_id, limit))
        return self._q(
            f"""SELECT id, account_id, period_start, period_end, status, source,
                       llm_model, input_tokens, output_tokens, created_at
                  FROM {self.s}.review ORDER BY created_at DESC LIMIT %s""",
            (limit,))

    # =====================================================================
    # 用量 / KV / 任务锁
    # =====================================================================
    def log_llm_usage(self, *, account_id: int | None, review_id: int | None, model: str | None,
                      mode: str, input_tokens: int = 0, output_tokens: int = 0,
                      latency_ms: int | None = None, trace_id: str | None = None,
                      skill: str = "douyin-account-review") -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.llm_usage
                (skill, account_id, review_id, trace_id, model, mode,
                 input_tokens, output_tokens, latency_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (skill, account_id, review_id, trace_id, model, mode,
             int(input_tokens or 0), int(output_tokens or 0), latency_ms),
        )

    def kv_get(self, key: str) -> Any:
        row = self._q1(
            f"""SELECT value FROM {self.s}.kv
                 WHERE key = %s AND (expires_at IS NULL OR expires_at > now())""",
            (key,))
        return row["value"] if row else None

    def kv_set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        self._x(
            f"""
            INSERT INTO {self.s}.kv (key, value, expires_at)
            VALUES (%s,%s, CASE WHEN %s IS NULL THEN NULL ELSE now() + (%s || ' seconds')::interval END)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                            expires_at = EXCLUDED.expires_at,
                                            updated_at = now()
            """,
            (key, _json(value), ttl_seconds, str(ttl_seconds) if ttl_seconds else None),
        )

    def acquire_task_lock(self, lock_key: str, owner: str, ttl_seconds: int) -> bool:
        row = self._q1(
            f"""
            INSERT INTO {self.s}.task_lock (lock_key, owner, expires_at)
            VALUES (%s, %s, now() + (%s || ' seconds')::interval)
            ON CONFLICT (lock_key) DO UPDATE SET
                owner = EXCLUDED.owner, acquired_at = now(), expires_at = EXCLUDED.expires_at
             WHERE {self.s}.task_lock.expires_at < now()
            RETURNING lock_key
            """,
            (lock_key, owner, str(ttl_seconds)),
        )
        return row is not None

    def release_task_lock(self, lock_key: str) -> None:
        self._x(f"DELETE FROM {self.s}.task_lock WHERE lock_key = %s", (lock_key,))

    # =====================================================================
    # 运维
    # =====================================================================
    def prune(self, keep_days: int) -> dict:
        """清理历史快照，控制在可接受的库容量。"""
        out = {}
        out["video_metric"] = self._x(
            f"DELETE FROM {self.s}.video_metric WHERE stat_date < current_date - %s", (keep_days,))
        out["account_metric"] = self._x(
            f"DELETE FROM {self.s}.account_metric WHERE stat_date < current_date - %s", (keep_days,))
        out["collect_run"] = self._x(
            f"DELETE FROM {self.s}.collect_run WHERE started_at < now() - (%s || ' days')::interval",
            (str(keep_days),))
        out["oauth_event"] = self._x(
            f"DELETE FROM {self.s}.oauth_event WHERE created_at < now() - (%s || ' days')::interval",
            (str(keep_days),))
        out["task_lock"] = self._x(f"DELETE FROM {self.s}.task_lock WHERE expires_at < now()")
        return out
