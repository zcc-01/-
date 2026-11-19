"""用户与运营域 · 业务接口（UC-12～UC-15）。

路由按「谁在用」分组，而不是按功能分组：

    /api/certs/**          农户视角（提交、查自己的）
    /api/ops/**            平台运营视角（审核、看板、预警）
    /api/regulator/**      监管视角（回溯、导出）

这样分组的原因是权限边界与使用场景完全重合：一个角色只进一个前缀，
越权测试也更直观（消费者把 /api/ops/** 全扫一遍即可）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.common.models import User
from app.core.config import settings
from app.core.deps import get_session, require_permission
from app.core.errors import NotFound
from app.core.response import ok, page
from app.ops import schemas_cert, schemas_ops
from app.ops.alert_service import AlertService
from app.ops.cert_service import CertService
from app.ops.dashboard_service import DashboardService
from app.ops.regulator_service import RegulatorService

# ------------------------------------------------------------------ 农户侧
cert_router = APIRouter(prefix="/api/certs", tags=["用户与运营域 · 产地认证"])


@cert_router.post("", summary="提交产地认证材料（UC-12）")
def submit_cert(
    payload: schemas_cert.CertSubmitRequest,
    user: User = Depends(require_permission("cert:submit")),
    db: Session = Depends(get_session),
):
    data = CertService.submit(db, user, payload)
    message = "材料已提交，进入待审队列" if data["saved_as"] == "submitted" else "材料缺项，已存为草稿"
    return ok(data, message=message)


@cert_router.get("/mine", summary="我的认证申请")
def my_certs(
    user: User = Depends(require_permission("cert:read:self")),
    db: Session = Depends(get_session),
):
    items = CertService.list_mine(db, user)
    return ok({"items": items, "total": len(items)})


@cert_router.get("/{cert_id}", summary="认证申请详情")
def cert_detail(
    cert_id: str,
    user: User = Depends(require_permission("cert:read:self")),
    db: Session = Depends(get_session),
):
    return ok(CertService.detail(db, user, cert_id))


# ------------------------------------------------------------------ 运营侧
ops_router = APIRouter(prefix="/api/ops", tags=["用户与运营域 · 平台运营"])


@ops_router.get("/certs", summary="认证待审队列（UC-12）")
def cert_queue(
    status: str | None = Query(default=None, description="缺省为 submitted"),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_permission("cert:review")),
    db: Session = Depends(get_session),
):
    data = CertService.list_queue(db, status=status, page_no=page_no, size=size)
    return page(data["items"], data["total"], data["page"], data["size"])


@ops_router.post("/certs/{cert_id}/review", summary="提交审核结论（UC-12）")
def review_cert(
    cert_id: str,
    payload: schemas_cert.CertReviewRequest,
    user: User = Depends(require_permission("cert:review")),
    db: Session = Depends(get_session),
):
    data = CertService.review(db, user, cert_id, payload)
    message = "审核通过，已授予产地认证标识" if payload.decision == "approve" else "已驳回并通知农户"
    return ok(data, message=message)


@ops_router.get("/dashboard", summary="平台经营看板（UC-13）")
def platform_dashboard(
    user: User = Depends(require_permission("dashboard:platform")),
    db: Session = Depends(get_session),
):
    return ok(DashboardService.platform_overview(db))


farm_dash_router = APIRouter(prefix="/api/farms", tags=["用户与运营域 · 经营看板"])


@farm_dash_router.get("/{farm_id}/dashboard", summary="农户经营看板（UC-13）")
def farm_dashboard(
    farm_id: str,
    user: User = Depends(require_permission("dashboard:own")),
    db: Session = Depends(get_session),
):
    return ok(DashboardService.farm_overview(db, user, farm_id))


# ------------------------------------------------------------------ 预警
@ops_router.get("/alerts", summary="预警工单列表（UC-14）")
def list_alerts(
    status: str | None = None,
    level: str | None = None,
    rule_code: str | None = None,
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_permission("alert:read")),
    db: Session = Depends(get_session),
):
    data = AlertService.list_alerts(
        db, status=status, level=level, rule_code=rule_code, page_no=page_no, size=size
    )
    payload = page(data["items"], data["total"], data["page"], data["size"])
    payload["data"]["summary"] = data["summary"]
    return payload


@ops_router.post("/alerts/{alert_id}/dispose", summary="预警处置（UC-14 / UC-15）")
def dispose_alert(
    alert_id: str,
    payload: schemas_ops.AlertDisposeRequest,
    user: User = Depends(require_permission("alert:dispose")),
    db: Session = Depends(get_session),
):
    data = AlertService.dispose(
        db, user, alert_id, action=payload.action, resolution=payload.resolution
    )
    return ok(data, message="处置已记录")


@ops_router.post("/alerts/scan", summary="手动触发一轮规则扫描（UC-14）")
def scan_alerts(
    user: User = Depends(require_permission("alert:scan")),
    db: Session = Depends(get_session),
):
    data = AlertService.run_rules(db)
    return ok(data, message=f"扫描完成，新增 {data['created']} 条工单")


@ops_router.get("/alert-rules", summary="预警规则配置（UC-14）")
def list_rules(
    user: User = Depends(require_permission("alert:read")),
    db: Session = Depends(get_session),
):
    return ok({"items": AlertService.list_rules(db)})


@ops_router.post("/alert-rules/{rule_code}", summary="启停预警规则（UC-14）")
def toggle_rule(
    rule_code: str,
    payload: schemas_ops.RuleToggleRequest,
    user: User = Depends(require_permission("alert:scan")),
    db: Session = Depends(get_session),
):
    return ok(AlertService.toggle_rule(db, user, rule_code, payload.enabled), message="规则已更新")


@ops_router.post("/alerts/simulate", summary="生成演示工单（仅开发环境）")
def simulate_alert(
    payload: schemas_ops.AlertSimulateRequest,
    user: User = Depends(require_permission("alert:scan")),
    db: Session = Depends(get_session),
):
    if not settings.dev_mode:
        raise NotFound("接口不存在")
    return ok(
        AlertService.simulate(db, user, rule_code=payload.rule_code, target_id=payload.target_id),
        message="已生成演示工单",
    )


# ------------------------------------------------------------------ 监管
regulator_router = APIRouter(prefix="/api/regulator", tags=["用户与运营域 · 监管回溯"])


@regulator_router.get("/traceback", summary="按条件回溯批次链路（UC-15）")
def traceback(
    farm_id: str | None = None,
    batch_no: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_permission("regulator:traceback")),
    db: Session = Depends(get_session),
):
    return ok(
        RegulatorService.traceback(
            db, farm_id=farm_id, batch_no=batch_no, date_from=date_from, date_to=date_to, limit=limit
        )
    )


@regulator_router.get("/traceback/export", summary="导出监管回溯报表（UC-15）")
def export_traceback(
    farm_id: str | None = None,
    batch_no: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user: User = Depends(require_permission("regulator:export")),
    db: Session = Depends(get_session),
):
    filename, content = RegulatorService.export_csv(
        db, farm_id=farm_id, batch_no=batch_no, date_from=date_from, date_to=date_to
    )
    # 加 BOM，Excel 打开中文 CSV 不乱码
    return Response(
        content="\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@regulator_router.get("/traceback/{batch_id}", summary="单批次链路明细（UC-15）")
def batch_detail(
    batch_id: str,
    user: User = Depends(require_permission("regulator:traceback")),
    db: Session = Depends(get_session),
):
    return ok(RegulatorService.batch_detail(db, batch_id))
