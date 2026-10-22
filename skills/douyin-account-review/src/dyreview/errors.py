"""异常体系。

设计要点：每种异常都带 `code`，方便 Crabot 侧按 code 决定话术，
而不是把 Python 栈抛给用户。
"""
from __future__ import annotations


class DyReviewError(Exception):
    """所有本 Skill 异常的基类。"""

    code = "DYREVIEW_ERROR"
    user_message = "复盘任务执行失败，请联系运营同学。"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"ok": False, "code": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


class ConfigError(DyReviewError):
    code = "CONFIG_INVALID"
    user_message = "Skill 配置不完整，缺少必要参数。"


class SchemaError(DyReviewError):
    code = "SCHEMA_NOT_READY"
    user_message = "数据库表结构未初始化或版本不匹配，请先执行 init-db。"


class DataUnavailable(DyReviewError):
    code = "DATA_UNAVAILABLE"
    user_message = "该账号数据不足（未采集或数据断更），暂时无法生成复盘报告。"


class AccountNotFound(DyReviewError):
    code = "ACCOUNT_NOT_FOUND"
    user_message = "没有找到对应账号，请确认账号名称或抖音号。"


class AccountAmbiguous(DyReviewError):
    code = "ACCOUNT_AMBIGUOUS"
    user_message = "匹配到多个账号，请选择具体一个。"

    def __init__(self, message: str, candidates: list[dict]) -> None:
        super().__init__(message)
        self.candidates = candidates

    def to_dict(self) -> dict:
        out = super().to_dict()
        out["candidates"] = self.candidates
        return out


class ReauthRequired(DyReviewError):
    """refresh_token 也失效：只能让客户重新扫码授权。"""

    code = "REAUTH_REQUIRED"
    user_message = "该账号的抖音授权已失效，需要客户重新授权后才能继续采集数据。"

    def __init__(self, account_id: int, account_name: str, description: str = "",
                 authorize_url: str | None = None) -> None:
        super().__init__(
            f"账号 {account_name}(#{account_id}) 授权失效：{description or 'refresh_token 无效'}",
        )
        self.account_id = account_id
        self.account_name = account_name
        self.description = description
        self.authorize_url = authorize_url

    def to_dict(self) -> dict:
        out = super().to_dict()
        out.update({
            "account_id": self.account_id,
            "account_name": self.account_name,
            "authorize_url": self.authorize_url,
        })
        return out


class DouyinApiError(DyReviewError):
    code = "DOUYIN_API_ERROR"

    def __init__(self, path: str, error_code: str, description: str,
                 logid: str | None = None, retryable: bool = False) -> None:
        super().__init__(f"抖音接口 {path} 返回错误 {error_code}: {description}")
        self.path = path
        self.error_code = str(error_code)
        self.description = description
        self.logid = logid
        self.retryable = retryable

    def to_dict(self) -> dict:
        out = super().to_dict()
        out.update({"path": self.path, "error_code": self.error_code, "logid": self.logid})
        return out


class TokenInvalid(DouyinApiError):
    """access_token 过期/无效：调用方应触发一次刷新后重试。"""
    code = "ACCESS_TOKEN_INVALID"


class RateLimited(DouyinApiError):
    code = "RATE_LIMITED"


class LlmError(DyReviewError):
    code = "LLM_ERROR"
    user_message = "报告生成服务暂时不可用，请稍后再试。"
