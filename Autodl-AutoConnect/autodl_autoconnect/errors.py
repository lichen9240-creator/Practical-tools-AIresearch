"""异常类型与脱敏工具。

约定：任何错误消息里都不得出现 token 明文；只有这里集中做脱敏，
其它模块直接拼接字符串是安全的。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "AutoDLError",
    "AuthError",
    "NetworkError",
    "APIError",
    "BalanceError",
    "ConfigError",
    "AmbiguousResultError",
    "redact",
]


_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(authorization|token|password|passwd|secret|cookie|session)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
]


def redact(value: Any, limit: int = 300) -> str:
    """把可能含凭证的文本脱敏，并截断长度。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + ":[已隐藏]", text)
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


class AutoDLError(Exception):
    """所有本项目异常的基类。"""

    code = "AUTODL_ERROR"

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False,
                 status: int | None = None, detail: Any = None):
        super().__init__(redact(message))
        if code:
            self.code = code
        self.retryable = retryable
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": str(self),
                "retryable": self.retryable,
                "status": self.status,
            },
        }


class AuthError(AutoDLError):
    """Token 无效 / 过期 / 权限不足。"""

    code = "AUTH_FAILED"


class NetworkError(AutoDLError):
    """连接失败、超时、被重置等传输层问题。"""

    code = "NETWORK_ERROR"


class APIError(AutoDLError):
    """接口返回了业务错误码。"""

    code = "API_ERROR"


class BalanceError(AutoDLError):
    """余额不足以覆盖本次下单。"""

    code = "BALANCE_INSUFFICIENT"


class ConfigError(AutoDLError):
    """配置缺失或非法。"""

    code = "CONFIG_ERROR"


class AmbiguousResultError(AutoDLError):
    """下单结果不明（超时/连接中断），必须停下人工核对，绝不重试下单。"""

    code = "AMBIGUOUS_RESULT"
