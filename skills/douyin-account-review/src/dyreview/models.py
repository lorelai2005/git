"""跨模块共享的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class TokenRecord:
    """dy.oauth_token 一行的内存表示。token 字段是信封字符串（enc:/raw:）。"""

    account_id: int
    access_token: str
    access_expires_at: datetime
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None
    access_issued_at: datetime | None = None
    access_ttl_seconds: int | None = None
    scope: str | None = None
    refreshed_at: datetime | None = None
    refresh_count: int = 0
    version: int = 0
    last_refresh_error: str | None = None

    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)


@dataclass
class VideoMeta:
    item_id: str
    title: str | None = None
    description: str | None = None
    cover_url: str | None = None
    share_url: str | None = None
    create_time: datetime | None = None
    duration_sec: int | None = None
    media_type: str | None = None
    is_top: bool = False
    topics: list[str] = field(default_factory=list)
    raw: dict | None = None


@dataclass
class Account:
    id: int
    open_id: str
    account_name: str
    client_key: str = "default"
    douyin_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    org_id: str | None = None
    status: str = "active"
    feishu_chat_id: str | None = None
    owner_feishu_ids: list[str] | None = None
    last_collect_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Account":
        return cls(
            id=int(row["id"]),
            open_id=row["open_id"],
            account_name=row["account_name"],
            client_key=row.get("client_key") or "default",
            douyin_id=row.get("douyin_id"),
            nickname=row.get("nickname"),
            avatar_url=row.get("avatar_url"),
            org_id=row.get("org_id"),
            status=row.get("status") or "active",
            feishu_chat_id=row.get("feishu_chat_id"),
            owner_feishu_ids=row.get("owner_feishu_ids"),
            last_collect_at=row.get("last_collect_at"),
        )

    def to_dict(self) -> dict:
        return {
            "account_id": self.id,
            "open_id": self.open_id,
            "account_name": self.account_name,
            "douyin_id": self.douyin_id,
            "nickname": self.nickname,
            "status": self.status,
        }


@dataclass
class CollectReport:
    account_id: int
    account_name: str
    status: str = "success"          # success|partial|failed|skipped
    token_refreshed: bool = False
    api_calls: int = 0
    video_pages: int = 0
    videos_seen: int = 0
    videos_upserted: int = 0
    metrics_written: int = 0
    account_metric_written: bool = False
    error_code: str | None = None
    error_msg: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "status": self.status,
            "token_refreshed": self.token_refreshed,
            "api_calls": self.api_calls,
            "video_pages": self.video_pages,
            "videos_seen": self.videos_seen,
            "videos_upserted": self.videos_upserted,
            "metrics_written": self.metrics_written,
            "account_metric_written": self.account_metric_written,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
            "warnings": self.warnings,
        }


@dataclass
class ReviewWindow:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days}
