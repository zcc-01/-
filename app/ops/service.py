"""用户与运营域业务逻辑（控制类，UC-12～UC-15）。

依赖方向（无环）：
    ops.service ──> trace.models / trade.models （只读，用于看板聚合）
    ops.service ──> ops.alerting
    trace.service ──> ops.alerting （扫码断链时建单，单向）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.errors import Conflict, Forbidden, NotFound, RuleViolation
from app.core.ids import new_biz_no
from app.ops import alerting
from app.ops.models import (
    Alert,
    AlertStatus,
    AlertType,
    AuditLog,
    CertApplication,
    CertStatus,
    ReviewLog,
)
from app.ops.schemas import CertReview, CertSubmit

CERT_VALID_DAYS = 365


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _user_names(db: Session, user_ids: set[str]) -> dict[str, str]:
    ids = [i for i in user_ids if i]
    if not ids:
        return {}
    rows = db.scalars(select(User).where(User.user_id.in_(ids))).all()
    return {u.user_id: u.display_name or u.username for u in rows}


def cert_out(app: CertApplication) -> dict[str, Any]:
    return {
        "app_id": app.app_id,
        "app_no": app.app_no,
        "farm_id": app.farm_id,
        "applicant_id": app.applicant_id,
        "status": app.status,
        "status_label": CertStatus.LABELS.get(app.status, app.status),
        "land_cert_no": app.land_cert_no,
        "report_no": app.report_no,
        "report_expire_at": app.report_expire_at,
        "commitment_signed": app.commitment_signed,
        "submitted_at": app.submitted_at,
        "reviewed_at": app.reviewed_at,
        "reviewer_id": app.reviewer_id,
        "review_comment": app.review_comment,
        "valid_until": app.valid_until,
        "created_at": app.created_at,
    }


def _review_log_out(log: ReviewLog, name: str = "") -> dict[str, Any]:
    return {
        "log_id": log.log_id,
        "app_id": log.app_id,
        "reviewer_id": log.reviewer_id,
        "reviewer_name": name,
        "action": log.action,
        "from_status": log.from_status,
        "to_status": log.to_status,
        "comment": log.comment,
        "created_at": log.created_at,
    }


def audit(
    db: Session,
    user: User | None,
    action: str,
    target: str,
    result: str = "allowed",
    ip: str | None = None,
) -> None:
    """敏感操作留痕。调用方负责 commit，与业务动作同一事务。"""
    db.add(
        AuditLog(
            user_id=user.user_id if user else None,
            username=(user.username if user else ""),
            action=action,
            target=target,
            result=result,
            ip=ip,
        )
    )


# ================================================================== UC-12 认证
class CertService:
    @staticmethod
    def submit(db: Session, user: User, payload: CertSubmit) -> dict[str, Any]:
        """农户提交产地认证申请。

        BR-07：同一产地同一时间仅允许一份在途申请（draft 与 pending 都算在途）。
        异常流 2a：材料缺失时只存草稿，不进入待审队列。
        """
        farm = db.get(Farm, payload.farm_id)
        if farm is None:
            raise NotFound("产地不存在")
        if farm.owner_id != user.user_id:
            raise Forbidden("只能为自己名下的产地提交认证")

        active = db.scalar(
            select(CertApplication).where(
                CertApplication.farm_id == payload.farm_id,
                CertApplication.status.in_(list(CertStatus.ACTIVE)),
            )
        )
        if active is not None:
            raise Conflict(
                f"该产地已有在途申请（{active.app_no}，状态："
                f"{CertStatus.LABELS.get(active.status, active.status)}），请勿重复提交",
                )
        missing = []
        if not payload.land_cert_no.strip():
            missing.append("土地证明编号")
        if not payload.report_no.strip():
            missing.append("检测报告编号")
        if not payload.commitment_signed:
            missing.append("质量安全承诺书")

        # 材料不全：只存草稿，不进待审队列
        want_submit = payload.submit and not missing
        app = CertApplication(
            app_no=new_biz_no("CA"),
            farm_id=payload.farm_id,
            applicant_id=user.user_id,
            status=CertStatus.PENDING if want_submit else CertStatus.DRAFT,
            materials=json.dumps(payload.materials, ensure_ascii=False, sort_keys=True),
            land_cert_no=payload.land_cert_no.strip(),
            report_no=payload.report_no.strip(),
            report_expire_at=payload.report_expire_at,
            commitment_signed=1 if payload.commitment_signed else 0,
            submitted_at=_now() if want_submit else None,
        )
        db.add(app)
        db.flush()
        db.add(
            ReviewLog(
                app_id=app.app_id,
                reviewer_id=user.user_id,
                action="submit" if want_submit else "save_draft",
                from_status="",
                to_status=app.status,
                comment="" if want_submit else f"材料缺失：{'、'.join(missing)}",
            )
        )
        # 首次申请时产地状态置为 pending，供扫码页展示「认证审核中」
        if farm.cert_status not in ("approved",) and want_submit:
            farm.cert_status = "pending"
        db.commit()
        db.refresh(app)

        data = cert_out(app)
        data["missing"] = missing
        data["message"] = (
            "已提交，等待平台审核"
            if want_submit
            else f"已保存为草稿，缺少材料：{'、'.join(missing)}"
        )
        return data

    @staticmethod
    def list_apps(
        db: Session,
        user: User,
        *,
        status: str | None = None,
        page_no: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        conds = []
        if user.role == Role.FARMER:
            conds.append(CertApplication.applicant_id == user.user_id)
        if status:
            conds.append(CertApplication.status == status)
        where = and_(*conds) if conds else None

        count_stmt = select(func.count()).select_from(CertApplication)
        stmt = select(CertApplication)
        if where is not None:
            count_stmt = count_stmt.where(where)
            stmt = stmt.where(where)
        total = db.scalar(count_stmt) or 0
        rows = db.scalars(
            stmt.order_by(CertApplication.created_at.desc())
            .offset((max(1, page_no) - 1) * size)
            .limit(size)
        ).all()
        return {"items": [cert_out(a) for a in rows], "total": total, "page": page_no, "size": size}

    @staticmethod
    def detail(db: Session, user: User, app_id: str) -> dict[str, Any]:
        app = db.get(CertApplication, app_id)
        if app is None:
            raise NotFound("认证申请不存在")
        if user.role == Role.FARMER and app.applicant_id != user.user_id:
            raise Forbidden("无权查看他人申请")

        data = cert_out(app)
        farm = db.get(Farm, app.farm_id)
        data["farm_name"] = farm.farm_name if farm else ""
        try:
            data["materials"] = json.loads(app.materials) if app.materials else {}
        except (TypeError, ValueError):
            data["materials"] = {}

        names = _user_names(db, {app.applicant_id, app.reviewer_id or ""})
        data["applicant_name"] = names.get(app.applicant_id, "")
        data["reviewer_name"] = names.get(app.reviewer_id or "", "")

        logs = db.scalars(
            select(ReviewLog)
            .where(ReviewLog.app_id == app_id)
            .order_by(ReviewLog.created_at.asc())
        ).all()
        log_names = _user_names(db, {log.reviewer_id for log in logs})
        data["logs"] = [_review_log_out(log, log_names.get(log.reviewer_id, "")) for log in logs]
        return data

    @staticmethod
    def review(db: Session, user: User, app_id: str, payload: CertReview) -> dict[str, Any]:
        """UC-12 审核。

        BR-08：结论一经提交不可修改——已 approved/rejected 的申请直接拒绝再次审核，
        要变更必须重新发起。这是硬约束，不是「前端隐藏按钮」。
        异常流 4a：检测报告过期时必须驳回并注明原因。
        """
        app = db.get(CertApplication, app_id)
        if app is None:
            raise NotFound("认证申请不存在")
        if app.status in (CertStatus.APPROVED, CertStatus.REJECTED):
            raise Conflict(
                f"该申请已出具结论（{CertStatus.LABELS.get(app.status)}），"
                "按 BR-08 不可修改，如需变更请重新发起申请"
            )
        if app.status != CertStatus.PENDING:
            raise RuleViolation("仅待审核状态的申请可出具结论")

        comment = payload.comment.strip()
        if payload.decision == "reject" and not comment:
            raise RuleViolation("驳回必须填写审核意见")

        now = _now()
        expired_report = app.report_expire_at is not None and app.report_expire_at < now
        if expired_report and payload.decision == "approve":
            raise RuleViolation(
                "检测报告已超出有效期，不能通过审核",
                data={"report_expire_at": app.report_expire_at.isoformat()},
            )

        farm = db.get(Farm, app.farm_id)
        from_status = app.status

        if payload.decision == "approve":
            app.status = CertStatus.APPROVED
            app.valid_until = now + timedelta(days=CERT_VALID_DAYS)
            if farm is not None:
                farm.cert_status = "approved"
            # UC-12 步骤 7：通过时向该产地全部商品打认证标识（跨域写，仅此一处）
            from app.trade.models import Product

            db.execute(
                update(Product).where(Product.farm_id == app.farm_id).values(certified=1)
            )
        else:
            app.status = CertStatus.REJECTED
            if farm is not None:
                farm.cert_status = "rejected"

        app.reviewed_at = now
        app.reviewer_id = user.user_id
        app.review_comment = comment

        db.add(
            ReviewLog(
                app_id=app.app_id,
                reviewer_id=user.user_id,
                action=payload.decision,
                from_status=from_status,
                to_status=app.status,
                comment=comment,
            )
        )
        audit(db, user, f"cert.{payload.decision}", app.app_no)
        db.commit()
        db.refresh(app)
        return CertService.detail(db, user, app_id)

    @staticmethod
    def pending_count(db: Session) -> int:
        return db.scalar(
            select(func.count())
            .select_from(CertApplication)
            .where(CertApplication.status == CertStatus.PENDING)
        ) or 0

    @staticmethod
    def sweep_expiring(db: Session, days: int = 30) -> int:
        """BR-09：认证到期前 30 天自动提醒续期（惰性扫描，无调度器也能演示）。"""
        now = _now()
        deadline = now + timedelta(days=days)
        rows = db.scalars(
            select(CertApplication).where(
                CertApplication.status == CertStatus.APPROVED,
                CertApplication.valid_until.is_not(None),
                CertApplication.valid_until <= deadline,
                CertApplication.valid_until > now,
            )
        ).all()
        created = 0
        for app in rows:
            farm = db.get(Farm, app.farm_id)
            days_left = max(0, (app.valid_until - now).days) if app.valid_until else 0
            alert = alerting.raise_cert_expiring(
                db,
                farm_id=app.farm_id,
                farm_name=farm.farm_name if farm else app.farm_id,
                valid_until=app.valid_until.isoformat() if app.valid_until else "",
                days_left=days_left,
            )
            if alert is not None:
                created += 1
        if created:
            db.commit()
        return created


# ================================================================== UC-13 看板
class DashboardService:
    @staticmethod
    def summary(db: Session, user: User) -> dict[str, Any]:
        """经营看板：农户看自己，运营/监管看全平台。

        口径写死在这里，避免前端各自算导致数字对不上。
        「已支付口径」统一为 paid/shipped/received 三态，不含 pending 与 cancelled。
        """
        from app.trace.models import Batch, ScanLog, TraceEvent
        from app.trade.models import Order, OrderStatus, Product

        scope = "platform"
        farm_ids: list[str] | None = None
        if user.role == Role.FARMER:
            scope = "farm"
            farm_ids = list(
                db.scalars(select(Farm.farm_id).where(Farm.owner_id == user.user_id)).all()
            )

        def count_of(model, cond=None) -> int:
            stmt = select(func.count()).select_from(model)
            if cond is not None:
                stmt = stmt.where(cond)
            return db.scalar(stmt) or 0

        if farm_ids is not None:
            batch_ids = (
                list(db.scalars(select(Batch.batch_id).where(Batch.farm_id.in_(farm_ids))).all())
                or ["__none__"]
            )
            batch_cond = Batch.farm_id.in_(farm_ids)
            event_cond = TraceEvent.batch_id.in_(batch_ids)
            scan_cond = ScanLog.batch_id.in_(batch_ids)
            product_cond = Product.farm_id.in_(farm_ids)
            order_cond = Order.farmer_id.in_(farm_ids)
            alert_cond = and_(Alert.status != AlertStatus.CLOSED, Alert.farm_id.in_(farm_ids))
        else:
            batch_cond = event_cond = scan_cond = product_cond = order_cond = None
            alert_cond = Alert.status != AlertStatus.CLOSED

        paid_cond = Order.status.in_(
            [OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.RECEIVED]
        )
        if order_cond is not None:
            paid_cond = and_(order_cond, paid_cond)

        gmv_cents = int(
            db.scalar(select(func.coalesce(func.sum(Order.total_cents), 0)).where(paid_cond)) or 0
        )

        status_stmt = select(Order.status, func.count())
        if order_cond is not None:
            status_stmt = status_stmt.where(order_cond)
        order_by_status = {
            row[0]: row[1] for row in db.execute(status_stmt.group_by(Order.status)).all()
        }

        alert_by_type = {
            row[0]: row[1]
            for row in db.execute(
                select(Alert.alert_type, func.count()).where(alert_cond).group_by(Alert.alert_type)
            ).all()
        }

        top_stmt = select(Product)
        if product_cond is not None:
            top_stmt = top_stmt.where(product_cond)
        top_rows = db.scalars(top_stmt.order_by(Product.sales_count.desc()).limit(5)).all()

        return {
            "scope": scope,
            "batch_total": count_of(Batch, batch_cond),
            "event_total": count_of(TraceEvent, event_cond),
            "scan_total": count_of(ScanLog, scan_cond),
            "product_total": count_of(Product, product_cond),
            "order_total": count_of(Order, order_cond),
            "order_paid": count_of(Order, paid_cond),
            "gmv_cents": gmv_cents,
            "gmv": round(gmv_cents / 100, 2),
            "pending_cert": CertService.pending_count(db),
            "open_alert": count_of(Alert, alert_cond),
            "top_products": [
                {
                    "product_id": p.product_id,
                    "name": p.name,
                    "sales_count": p.sales_count,
                    "price": p.price_yuan,
                    "stock": p.stock,
                }
                for p in top_rows
            ],
            "alert_by_type": alert_by_type,
            "order_by_status": order_by_status,
        }


# ================================================================== UC-14 预警
class AlertService:
    @staticmethod
    def list(
        db: Session,
        *,
        status: str | None = None,
        alert_type: str | None = None,
        level: str | None = None,
        page_no: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        rows, total = alerting.list_alerts(
            db, status=status, alert_type=alert_type, level=level, page_no=page_no, size=size
        )
        return {
            "items": [alerting.alert_out(a) for a in rows],
            "total": total,
            "page": page_no,
            "size": size,
        }

    @staticmethod
    def handle(db: Session, user: User, alert_id: str, status: str, note: str) -> dict[str, Any]:
        alert = db.get(Alert, alert_id)
        if alert is None:
            raise NotFound("预警单不存在")
        if status not in (AlertStatus.PROCESSING, AlertStatus.CLOSED):
            raise RuleViolation("处置状态只能是 processing 或 closed")
        alert.status = status
        alert.handler_id = user.user_id
        alert.handled_at = _now()
        alert.handle_note = note
        audit(db, user, f"alert.{status}", alert.alert_no)
        db.commit()
        db.refresh(alert)
        return alerting.alert_out(alert)

    @staticmethod
    def stats(db: Session) -> dict[str, Any]:
        rows = db.execute(select(Alert.alert_type, Alert.status, func.count()).group_by(
            Alert.alert_type, Alert.status
        )).all()
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for atype, status, cnt in rows:
            by_type[atype] = by_type.get(atype, 0) + cnt
            by_status[status] = by_status.get(status, 0) + cnt
        return {
            "by_type": by_type,
            "by_status": by_status,
            "type_labels": AlertType.LABELS,
            "open": by_status.get(AlertStatus.OPEN, 0),
        }


# ================================================================== UC-15 监管
class RegulatorService:
    @staticmethod
    def trace_back(db: Session, code_value: str) -> dict[str, Any]:
        """监管回溯：复用溯源域的扫码聚合逻辑，不另写一套查询。

        这里只增补监管视角的上下文（该批次是否已有预警、是否有订单），
        避免「监管看到的链路和消费者看到的不一致」这类口径问题。
        """
        from app.trace.service import TraceService

        data = TraceService.scan(db, code_value)
        batch = data["batch"]
        batch_id = batch.batch_id
        # scan 返回的 batch 是 ORM 对象（扫码页模板直接点属性用），
        # 监管接口要 JSON 序列化，这里转成字典，顺带只暴露需要对外字段。
        data["batch"] = {
            "batch_id": batch.batch_id,
            "batch_no": batch.batch_no,
            "farm_id": batch.farm_id,
            "crop_name": batch.crop_name,
            "plot_no": batch.plot_no,
            "status": batch.status,
            "plant_date": batch.plant_date,
            "harvest_date": batch.harvest_date,
            "event_count": batch.event_count,
        }

        alerts = db.scalars(
            select(Alert)
            .where(Alert.batch_id == batch_id)
            .order_by(Alert.created_at.desc())
            .limit(10)
        ).all()

        from app.trade.models import OrderItem, Product

        product_ids = list(
            db.scalars(select(Product.product_id).where(Product.batch_id == batch_id)).all()
        )
        order_count = 0
        if product_ids:
            order_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(OrderItem)
                    .where(OrderItem.product_id.in_(product_ids))
                )
                or 0
            )

        data["regulator_context"] = {
            "batch_id": batch_id,
            "alert_count": len(alerts),
            "alerts": [alerting.alert_out(a) for a in alerts],
            "order_count": int(order_count),
            "suggestion": (
                "该批次存在未关闭预警，建议优先核查"
                if any(a.status != AlertStatus.CLOSED for a in alerts)
                else "未发现异常预警"
            ),
        }
        return data

    @staticmethod
    def dispose(db: Session, user: User, code_value: str, action: str, note: str) -> dict:
        """监管处置动作：只记审计与预警状态，不直接改业务数据。

        为什么不直接下线商品：处置权与经营权分离，监管的动作要留痕可回溯，
        实际执行由平台运营完成，避免监管绕过审核流程直接改数据。
        """
        from app.trace.service import TraceService

        data = TraceService.scan(db, code_value)
        batch = data["batch"]
        batch_id = batch.batch_id
        # scan 返回的 batch 是 ORM 对象（扫码页模板直接点属性用），
        # 监管接口要 JSON 序列化，这里转成字典，顺带只暴露需要对外字段。
        data["batch"] = {
            "batch_id": batch.batch_id,
            "batch_no": batch.batch_no,
            "farm_id": batch.farm_id,
            "crop_name": batch.crop_name,
            "plot_no": batch.plot_no,
            "status": batch.status,
            "plant_date": batch.plant_date,
            "harvest_date": batch.harvest_date,
            "event_count": batch.event_count,
        }

        if action == "freeze_batch":
            alert = alerting.create_alert(
                db,
                alert_type=AlertType.CHAIN_BROKEN,
                level="critical",
                title=f"监管冻结批次 {batch_id}",
                detail={"action": action, "note": note, "operator": user.username},
                batch_id=batch_id,
                code_value=code_value,
            )
            if alert is not None:
                alert.handle_note = note

        audit(db, user, f"regulator.{action}", f"{code_value}/{batch_id}")
        db.commit()
        return {
            "code_value": code_value,
            "batch_id": batch_id,
            "action": action,
            "note": note,
            "handled_at": _now().isoformat(),
        }
