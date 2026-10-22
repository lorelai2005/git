"""Token 落库加密信封。

数据库里存的是信封字符串，而不是裸 token：

    enc:<fernet token>     用 crypto.key 加密（生产必须）
    raw:<明文>             仅测试/无 cryptography 时降级，会打警告

支持密钥轮换：用 `key_previous` 解出来、用 `key` 重新封装（`reseal`）。
"""
from __future__ import annotations

import logging

from .errors import ConfigError

log = logging.getLogger(__name__)

ENC_PREFIX = "enc:"
RAW_PREFIX = "raw:"


class SecretsBox:
    def __init__(self, key: str = "", key_previous: str = "") -> None:
        self._primary = self._build(key, required=False)
        self._previous = self._build(key_previous, required=False)
        if self._primary is None and key:
            raise ConfigError("crypto.key 无效：需要 Fernet base64 密钥（32 字节）")

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _build(key: str, *, required: bool):
        key = (key or "").strip()
        if not key:
            return None
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover
            if required:
                raise ConfigError("已配置 crypto.key 但未安装 cryptography，"
                                  "请 pip install cryptography") from exc
            log.warning("已配置 crypto.key 但未安装 cryptography，降级为明文存储（不安全）")
            return None
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as exc:
            raise ConfigError(f"crypto.key 格式错误：{exc}") from exc

    # ---------------------------------------------------------------- 接口
    @property
    def enabled(self) -> bool:
        return self._primary is not None

    def seal(self, plaintext: str) -> str:
        if plaintext is None:
            return ""
        if not plaintext.startswith(ENC_PREFIX) and not plaintext.startswith(RAW_PREFIX):
            if self._primary is not None:
                return ENC_PREFIX + self._primary.encrypt(plaintext.encode()).decode()
            log.debug("crypto.key 未配置，token 以明文入库；生产环境请务必配置")
            return RAW_PREFIX + plaintext
        return plaintext  # 已是信封，幂等

    def open(self, sealed: str) -> str:
        if not sealed:
            return ""
        if sealed.startswith(RAW_PREFIX):
            return sealed[len(RAW_PREFIX):]
        if sealed.startswith(ENC_PREFIX):
            payload = sealed[len(ENC_PREFIX):].encode()
            for box in (self._primary, self._previous):
                if box is None:
                    continue
                try:
                    return box.decrypt(payload).decode()
                except Exception:
                    continue
            raise ConfigError("token 解密失败：crypto.key 与入库时的密钥不匹配")
        # 兼容早期没有前缀的明文数据
        return sealed

    def reseal(self, sealed: str) -> str:
        """用当前主密钥重新封装（密钥轮换用）；解密失败原样返回。"""
        try:
            return self.seal(self.open(sealed))
        except ConfigError:
            return sealed

    def fingerprint(self, sealed: str) -> str:
        """日志里安全展示：只看后 6 位。"""
        try:
            plain = self.open(sealed)
        except ConfigError:
            return "***"
        return "***" + plain[-6:] if len(plain) > 6 else "***"
