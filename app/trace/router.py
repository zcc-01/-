"""溯源链域接口层（Boundary，UC-01～UC-05）。

路由只做三件事：参数校验、鉴权、把 Service 结果包成统一响应。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.common.models import Role, User
from app.core.config import settings
from app.core.deps import get_optional_user, get_session, require_roles
from app.core.response import ok, page
from app.trace.qrcode_export import build_qrcode_zip
from app.trace.schemas import (
    BatchCreate,
    BatchDetail,
    BatchOut,
    BatchStatusUpdate,
    CodeGenerate,
    CodeOut,
    EventCreate,
    EventOut,
    ScanOut,
    VerifyOut,
)
from app.trace.service import TraceService

router = APIRouter(prefix="/api", tags=["溯源链域"])

AnonymousOk = get_optional_user


def _event_out(event, operator_name: str = "") -> dict:
    import json

    try:
        detail = json.loads(event.detail) if event.detail else {}
    except (TypeError, ValueError):
        detail = {}
    from app.trace.models import Stage

    return {
        "event_id": event.event_id,
        "batch_id": event.batch_id,
        "seq_no": event.seq_no,
        "stage": event.stage,
        "stage_label": Stage.LABELS.get(event.stage, event.stage),
        "operator_id": event.operator_id,
        "operator_name": operator_name,
        "occurred_at": event.occurred_at,
        "location": event.location,
        "detail": detail,
        "prev_hash": event.prev_hash,
        "curr_hash": event.curr_hash,
    }


# --------------------------------------------------------------------- UC-01
@router.post("/batches", summary="创建生产批次（UC-01）")
def create_batch(
    payload: BatchCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    batch = TraceService.create_batch(db, payload, user)
    return ok(BatchOut.model_validate(batch).model_dump(), message="批次建档成功")


@router.get("/batches", summary="批次列表")
def list_batches(
    status: str | None = Query(default=None),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR, Role.REGULATOR)),
):
    rows, total = TraceService.list_batches(db, user, status=status, page_no=page_no, size=size)
    items = [BatchOut.model_validate(item).model_dump() for item in rows]
    return page(items, total, page_no, size)


@router.get("/batches/{batch_id}", summary="批次详情（含环节记录）")
def get_batch(
    batch_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR, Role.REGULATOR)),
):
    batch = TraceService.get_batch(db, batch_id)
    if user.role == Role.FARMER:
        TraceService._assert_farm_owner(db, batch, user)
    events = TraceService.list_events(db, batch_id)
    names = TraceService._operator_names(db, {e.operator_id for e in events})
    detail = BatchDetail.model_validate(batch).model_dump()
    detail["farm"] = TraceService._farm_dict(db, batch)
    detail["events"] = [_event_out(e, names.get(e.operator_id, "匿名")) for e in events]
    detail["code_count"] = TraceService.count_codes(db, batch_id)
    return ok(detail)


@router.patch("/batches/{batch_id}/status", summary="批次状态流转")
def update_status(
    batch_id: str,
    payload: BatchStatusUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    batch = TraceService.change_status(db, batch_id, payload.status, user)
    return ok(BatchOut.model_validate(batch).model_dump(), message="状态已更新")


# --------------------------------------------------------------------- UC-02
@router.post("/batches/{batch_id}/events", summary="录入溯源环节（UC-02）")
def append_event(
    batch_id: str,
    payload: EventCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    event, warning = TraceService.append_event(db, batch_id, payload, user)
    data = _event_out(event, TraceService._operator_names(db, {event.operator_id}).get(event.operator_id, ""))
    return ok({"event": data, "warning": warning}, message="环节记录已上链")


@router.get("/batches/{batch_id}/events", summary="环节记录列表")
def list_events(
    batch_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR, Role.REGULATOR)),
):
    batch = TraceService.get_batch(db, batch_id)
    if user.role == Role.FARMER:
        TraceService._assert_farm_owner(db, batch, user)
    events = TraceService.list_events(db, batch_id)
    names = TraceService._operator_names(db, {e.operator_id for e in events})
    return ok([_event_out(e, names.get(e.operator_id, "匿名")) for e in events])


# --------------------------------------------------------------------- UC-03
@router.post("/batches/{batch_id}/codes", summary="生成溯源码（UC-03）")
def generate_codes(
    batch_id: str,
    payload: CodeGenerate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    codes = TraceService.generate_codes(db, batch_id, payload, user)
    items = [CodeOut.model_validate(code).model_dump() for code in codes]
    return ok(
        {"items": items, "total": len(items)},
        message=f"已赋码 {len(items)} 个（重复提交不会产生重复码值）",
    )


@router.get("/batches/{batch_id}/codes", summary="溯源码列表")
def list_codes(
    batch_id: str,
    code_type: str | None = Query(default=None, pattern="^(box|item)$"),
    limit: int = Query(default=100, ge=1, le=5000),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR)),
):
    batch = TraceService.get_batch(db, batch_id)
    if user.role == Role.FARMER:
        TraceService._assert_farm_owner(db, batch, user)
    codes = TraceService.list_codes(db, batch_id, code_type=code_type, limit=limit)
    return ok({"items": [CodeOut.model_validate(code).model_dump() for code in codes], "total": len(codes)})


@router.get("/batches/{batch_id}/qrcodes.zip", summary="导出二维码压缩包（UC-03）")
def export_qrcodes(
    batch_id: str,
    request: Request,
    code_type: str = Query(default="item", pattern="^(box|item)$"),
    limit: int = Query(default=200, ge=1, le=settings.max_code_batch),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR)),
):
    batch = TraceService.get_batch(db, batch_id)
    if user.role == Role.FARMER:
        TraceService._assert_farm_owner(db, batch, user)
    codes = TraceService.list_codes(db, batch_id, code_type=code_type, limit=limit)
    base_url = str(request.base_url).rstrip("/")
    payload = build_qrcode_zip(batch.batch_no, [(c.code_value, c.code_type) for c in codes], base_url)
    filename = f"{batch.batch_no}-{code_type}-qrcodes.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/codes/{code_value}/revoke", summary="作废溯源码")
def revoke_code(
    code_value: str,
    payload: dict,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    reason = str(payload.get("reason", "人工作废"))
    code = TraceService.revoke_code(db, code_value, reason, user)
    return ok(CodeOut.model_validate(code).model_dump(), message="溯源码已作废")


# --------------------------------------------------------------------- UC-05
@router.get("/batches/{batch_id}/verify", summary="溯源链完整性校验（UC-05）")
def verify_batch(
    batch_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR, Role.REGULATOR)),
):
    result = TraceService.verify_batch(db, batch_id)
    return ok(result.to_dict(), message="校验完成")


# --------------------------------------------------------------------- UC-04
@router.get("/trace/{code_value}", summary="扫码溯源查询（UC-04，匿名可访问）")
def scan_code(
    code_value: str,
    request: Request,
    province: str | None = Query(default=None, max_length=32),
    city: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_session),
    user: User | None = Depends(AnonymousOk),
):
    data = TraceService.scan(
        db,
        code_value,
        province=province,
        city=city,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
        user=user,
    )
    data["batch"] = BatchOut.model_validate(data["batch"]).model_dump()
    data["chain"] = VerifyOut.model_validate(data["chain"]).model_dump()
    data["events"] = [EventOut.model_validate(item).model_dump() for item in data["events"]]
    return ok(ScanOut.model_validate(data).model_dump())


@router.get("/batches/{batch_id}/scans", summary="扫码日志（运营）")
def list_scan_logs(
    batch_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR, Role.REGULATOR)),
):
    logs = TraceService.list_scan_logs(db, batch_id, limit=limit)
    return ok(
        [
            {
                "log_id": log.log_id,
                "code_value": log.code_value,
                "batch_id": log.batch_id,
                "scanned_at": log.scanned_at,
                "province": log.province,
                "city": log.city,
                "device": log.device,
            }
            for log in logs
        ]
    )
