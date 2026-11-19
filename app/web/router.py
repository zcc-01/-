"""页面路由（Jinja2 服务端渲染 + 少量原生 JS 调用 API）。

页面只做两件事：给出静态骨架、把请求交给 /api/**。
鉴权仍然走 Bearer Token（存在 localStorage），这样页面上看到的数据与接口测试
用的是同一套权限判断，不会出现「页面能看、接口 403」的两套逻辑。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core import rbac
from app.core.config import settings
from app.ops.models import AlertLevel, AlertStatus, CertStatus

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

router = APIRouter(include_in_schema=False)

_CTX = {
    "app_name": settings.app_name,
    "dev_mode": settings.dev_mode,
    "cert_labels": CertStatus.LABELS,
    "alert_levels": AlertLevel.LABELS,
    "alert_status": AlertStatus.LABELS,
    "role_labels": rbac.ROLE_LABELS,
}


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _CTX)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", _CTX)


@router.get("/farmer", response_class=HTMLResponse)
def farmer_page(request: Request):
    return templates.TemplateResponse(request, "farmer.html", _CTX)


@router.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request):
    return templates.TemplateResponse(request, "ops.html", _CTX)


@router.get("/regulator", response_class=HTMLResponse)
def regulator_page(request: Request):
    return templates.TemplateResponse(request, "regulator.html", _CTX)
