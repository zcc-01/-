"""模板环境与页面公共上下文（三域共用）。

单独放一个模块的原因：main.py 要挂载路由，web/router.py 又要用模板环境，
若把 Jinja2Templates 定义在 main.py 里，两边就形成循环导入。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app.core.config import settings

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# 顶栏导航：三个业务域的统一入口，顺序即用户动线
NAV_ITEMS: list[dict[str, str]] = [
    {"href": "/shop", "label": "产地直供"},
    {"href": "/cart", "label": "购物车"},
    {"href": "/orders", "label": "订单"},
    {"href": "/console", "label": "运营控制台"},
]


def ns(obj: Any) -> Any:
    """把 dict 递归转成 SimpleNamespace，供模板用点号访问。

    为什么需要：Jinja2 的 `foo.bar` 会先试属性再试下标，而 dict 自带 `items` 方法，
    所以 `cart.items` 拿到的是内建方法而不是「items 这个键」，模板会直接报
    TypeError: 'builtin_function_or_method' object is not iterable。
    把 service 层返回的 dict 转一层即可绕开，且不用改动 API 契约。
    """
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [ns(x) for x in obj]
    return obj


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """统一注入全局变量，避免每个页面各自拼 app_name / nav。"""
    ctx: dict[str, Any] = {
        "app_name": settings.app_name,
        "nav_items": NAV_ITEMS,
        "nav_user": getattr(request.state, "web_user", None),
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
