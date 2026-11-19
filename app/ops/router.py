"""用户与运营域接口层（Boundary，UC-11～UC-15）。

角色边界：
- 农户：提交认证申请、看自己的经营数据
- 平台运营：审核认证、处置预警、看全平台数据
- 监管：回溯任意码、发起处置动作（不改业务数据，只留痕）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import Role, User
from app.core.config import settings
from app.core.deps import get_session, require_roles
from app.core.response import ok, page
from app.ops.schemas import AlertHandle, CertReview, CertSubmit, RegulatorAction
from app.ops.service import AlertService, CertService, DashboardService, RegulatorService

router = APIRouter(prefix="/api", tags=["用户与运营域"])


# ============================================================ UC-12 认证审核
@router.post("/certs", summary="提交产地认证申请（UC-12，材料不全自动存草稿）")
def submit_cert(
    payload: CertSubmit,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    return ok(CertService.submit(db, user, payload))


@router.get("/certs", summary="认证申请列表（农户看自己，运营看全部）")
def list_certs(
    status: str | None = Query(default=None, description="draft/pending/approved/rejected"),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR, Role.REGULATOR)),
):
    data = CertService.list_apps(db, user, status=status, page_no=page_no, size=size)
    return page(data["items"], data["total"], data["page"], data["size"])


@router.get("/certs/{app_id}", summary="认证申请详情（含审核轨迹）")
def get_cert(
    app_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR, Role.REGULATOR)),
):
    return ok(CertService.detail(db, user, app_id))


@router.post("/certs/{app_id}/review", summary="审核认证申请（UC-12，结论不可修改）")
def review_cert(
    app_id: str,
    payload: CertReview,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    return ok(CertService.review(db, user, app_id, payload), message="审核结论已提交")


@router.post("/certs/sweep-expiring", summary="扫描临期认证并生成续期提醒（BR-09）")
def sweep_expiring(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    created = CertService.sweep_expiring(db, days=days)
    return ok({"created": created}, message=f"已生成 {created} 张续期提醒")


# ============================================================== UC-13 看板
@router.get("/dashboard", summary="经营数据看板（UC-13，农户看自己/运营看全平台）")
def dashboard(
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR, Role.REGULATOR)),
):
    return ok(DashboardService.summary(db, user))


# ============================================================== UC-14 预警
@router.get("/alerts", summary="预警单列表（UC-14）")
def list_alerts(
    status: str | None = Query(default=None, description="open/processing/closed"),
    alert_type: str | None = Query(default=None),
    level: str | None = Query(default=None),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR, Role.REGULATOR)),
):
    data = AlertService.list(
        db,
        status=status,
        alert_type=alert_type,
        level=level,
        page_no=page_no,
        size=size,
    )
    return page(data["items"], data["total"], data["page"], data["size"])


@router.get("/alerts/stats", summary="预警统计（按类型与状态）")
def alert_stats(
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR, Role.REGULATOR)),
):
    return ok(AlertService.stats(db))


@router.post("/alerts/{alert_id}/handle", summary="处置预警单（UC-14）")
def handle_alert(
    alert_id: str,
    payload: AlertHandle,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    return ok(
        AlertService.handle(db, user, alert_id, payload.status, payload.note),
        message="预警单已处置",
    )


# ============================================================== UC-15 监管
@router.get("/regulator/trace/{code_value}", summary="监管回溯（UC-15，含预警与交易上下文）")
def trace_back(
    code_value: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.REGULATOR)),
):
    return ok(RegulatorService.trace_back(db, code_value))


@router.post("/regulator/trace/{code_value}/dispose", summary="监管处置动作（UC-15）")
def dispose(
    code_value: str,
    payload: RegulatorAction,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.REGULATOR)),
):
    return ok(
        RegulatorService.dispose(db, user, code_value, payload.action, payload.note),
        message="处置动作已记录",
    )


# ============================================================ UC-11 农户入驻
@router.get("/farms/mine", summary="我的农场（UC-11 入驻后查询）")
def my_farms(
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    from app.common.models import Farm

    rows = db.scalars(select(Farm).where(Farm.owner_id == user.user_id)).all()
    return ok(
        [
            {
                "farm_id": f.farm_id,
                "farm_name": f.farm_name,
                "province": f.province,
                "city": f.city,
                "address": f.address,
                "area_mu": f.area_mu,
                "cert_status": f.cert_status,
            }
            for f in rows
        ]
    )
