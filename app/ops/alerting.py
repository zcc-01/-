"""预警单服务（跨域耦合点，单独成模块以避免循环导入）。

依赖方向：
    trace.service ──> ops.alerting <── ops.service
    trade.service ──> ops.alerting

alerting 只依赖 ops.models 与 core，不 import trace / trade，
因此溯源域可以在自己的事务里直接调用它，不会形成 import 环。

幂等设计：dedup_key = f"{alert_type}:{batch_id 或 code_value}"，
同一对象同类型的未关闭预警只建一张单。消费者反复扫码、运营反复校验时，
预警列表不会被刷成几百条同样的记录——这是接真实用户后必然遇到的问题。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ids import new_biz_no
from app.ops.models import Alert, AlertLevel, AlertStatus, AlertType


def _dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def create_alert(
    db: Session,
    *,
    alert_type: str,
    title: str,
    detail: dict[str, Any] | None = None,
    level: str = AlertLevel.WARN,
    batch_id: str | None = None,
    code_value: str | None = None,
    farm_id: str | None = None,
) -> Alert | None:
    """建单（幂等）。返回新建的预警单；若已存在同键未关闭单则返回 None。

    调用方不需要 commit，由外层事务统一提交——预警单必须和触发它的业务动作
    在同一个事务里，否则会出现「链校验发现问题但预警单没落库」的漏报。
    """
    dedup_key = f"{alert_type}:{batch_id or code_value or farm_id or 'unknown'}"
    exists = db.scalar(
        select(Alert).where(
            Alert.dedup_key == dedup_key,
            Alert.status != AlertStatus.CLOSED,
        )
    )
    if exists is not None:
        return None

    alert = Alert(
        alert_no=new_biz_no("AL"),
        alert_type=alert_type,
        level=level,
        status=AlertStatus.OPEN,
        batch_id=batch_id,
        code_value=code_value,
        farm_id=farm_id,
        title=title,
        detail=_dumps(detail or {}),
        dedup_key=dedup_key,
    )
    db.add(alert)
    return alert


def raise_chain_broken(
    db: Session,
    *,
    batch_id: str,
    code_value: str | None = None,
    broken_at: int | None = None,
    reason: str | None = None,
) -> Alert | None:
    """UC-04 异常流 4a：扫码发现断链时自动生成预警单。

    报告原文要求「正常展示已存在记录，同时提示异常，并自动生成预警单」，
    这个函数就是那句话的落地。级别定为 critical——断链意味着数据可能被篡改。
    """
    return create_alert(
        db,
        alert_type=AlertType.CHAIN_BROKEN,
        level=AlertLevel.CRITICAL,
        title=f"批次 {batch_id} 溯源链校验失败",
        detail={
            "batch_id": batch_id,
            "code_value": code_value,
            "broken_at": broken_at,
            "reason": reason,
            "hint": "链校验发现记录失配，可能被改动或删除，请运营核查",
        },
        batch_id=batch_id,
        code_value=code_value,
    )


def raise_time_inversion(
    db: Session,
    *,
    batch_id: str,
    stage: str,
    occurred_at: str,
    prev_stage: str,
    prev_time: str,
) -> Alert | None:
    """UC-14 扩展 UC-02：环节时间早于前序环节时生成预警。

    录入本身不阻断（农户可能有合理的补录场景），只留预警供运营判断。
    """
    return create_alert(
        db,
        alert_type=AlertType.TIME_INVERSION,
        level=AlertLevel.WARN,
        title=f"批次 {batch_id} 环节时间倒挂",
        detail={
            "batch_id": batch_id,
            "stage": stage,
            "occurred_at": occurred_at,
            "prev_stage": prev_stage,
            "prev_time": prev_time,
        },
        batch_id=batch_id,
    )


def raise_cert_expiring(
    db: Session, *, farm_id: str, farm_name: str, valid_until: str, days_left: int
) -> Alert | None:
    """BR-09：认证到期前 30 天自动提醒续期。"""
    return create_alert(
        db,
        alert_type=AlertType.CERT_EXPIRING,
        level=AlertLevel.INFO if days_left > 7 else AlertLevel.WARN,
        title=f"产地「{farm_name}」认证将于 {days_left} 天后到期",
        detail={"farm_id": farm_id, "valid_until": valid_until, "days_left": days_left},
        farm_id=farm_id,
    )


def list_alerts(
    db: Session,
    *,
    status: str | None = None,
    alert_type: str | None = None,
    level: str | None = None,
    page_no: int = 1,
    size: int = 20,
) -> tuple[list[Alert], int]:
    from sqlalchemy import and_, func

    conds = []
    if status:
        conds.append(Alert.status == status)
    if alert_type:
        conds.append(Alert.alert_type == alert_type)
    if level:
        conds.append(Alert.level == level)
    where = and_(*conds) if conds else None

    count_stmt = select(func.count()).select_from(Alert)
    stmt = select(Alert)
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)

    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(Alert.created_at.desc()).offset((max(1, page_no) - 1) * size).limit(size)
    ).all()
    return list(rows), total


def alert_out(alert: Alert) -> dict[str, Any]:
    try:
        detail = json.loads(alert.detail) if alert.detail else {}
    except (TypeError, ValueError):
        detail = {}
    return {
        "alert_id": alert.alert_id,
        "alert_no": alert.alert_no,
        "alert_type": alert.alert_type,
        "alert_type_label": AlertType.LABELS.get(alert.alert_type, alert.alert_type),
        "level": alert.level,
        "level_label": AlertLevel.LABELS.get(alert.level, alert.level),
        "status": alert.status,
        "status_label": AlertStatus.LABELS.get(alert.status, alert.status),
        "batch_id": alert.batch_id,
        "code_value": alert.code_value,
        "farm_id": alert.farm_id,
        "title": alert.title,
        "detail": detail,
        "handler_id": alert.handler_id,
        "handled_at": alert.handled_at,
        "handle_note": alert.handle_note,
        "created_at": alert.created_at,
    }
