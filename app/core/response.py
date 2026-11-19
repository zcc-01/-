"""统一响应封装（公共基础层）。

所有接口返回 {code, message, data}，分页数据为 {items, total, page, size}。
"""

from __future__ import annotations

from typing import Any


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


def page(items: list[Any], total: int, page_no: int, size: int) -> dict[str, Any]:
    return ok({"items": items, "total": total, "page": page_no, "size": size})


def fail(code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}
