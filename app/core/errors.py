"""统一业务异常与错误码（公共基础层）。

约定所有对外错误都长成 {code, message, data}，HTTP 状态码只表达协议语义，
业务原因由 code 表达，前端据此做分支（报告 3.3 的异常流程依赖这一约定）。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    OK = 0
    PARAM_INVALID = 40001
    UNAUTHENTICATED = 40101
    FORBIDDEN = 40301
    NOT_FOUND = 40401
    CONFLICT = 40901
    RULE_VIOLATION = 42201
    CODE_REVOKED = 42202
    RATE_LIMITED = 42901
    INTERNAL = 50001
    CHAIN_CONFLICT = 50002


class BizError(Exception):
    """业务异常：由全局处理器转成统一响应。"""

    def __init__(
        self,
        code: int,
        message: str,
        http_status: int = 400,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data


class NotFound(BizError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, 404)


class Conflict(BizError):
    def __init__(self, message: str = "资源冲突") -> None:
        super().__init__(ErrorCode.CONFLICT, message, 409)


class RuleViolation(BizError):
    def __init__(self, message: str, data: Any = None) -> None:
        super().__init__(ErrorCode.RULE_VIOLATION, message, 422, data)


class Unauthenticated(BizError):
    def __init__(self, message: str = "未登录或登录已过期") -> None:
        super().__init__(ErrorCode.UNAUTHENTICATED, message, 401)


class Forbidden(BizError):
    def __init__(self, message: str = "权限不足", data: Any = None) -> None:
        # data 用于回传「缺少哪个权限点」，便于排查越权（兼容 RBAC 矩阵模块）
        super().__init__(ErrorCode.FORBIDDEN, message, 403, data)
