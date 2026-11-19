"""产地认证审核（UC-12）。

这个用例的业务规则最密集，是「AI 一次生成、人工补状态机」的典型（报告 5.2.4）。
实现上把三条硬规则显式落到代码里，而不是靠注释约定：

- BR-07 同一产地同一时间仅允许一份在途申请 → submit 前置校验 + 唯一索引兜底；
- BR-08 审核结论一经提交不可修改 → review 只接受 submitted 状态，终态不可再进；
- BR-09 认证有效期 12 个月，到期前 30 天提醒 → valid_to / remind_at + 到期扫描。

另外一条隐含规则来自用例异常流 4a：检测报告超过有效期时不允许「通过」，
但允许在提交阶段先存下来（农户常常先交材料、报告后补），因此过期校验放在审核环节。
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.account.service import AuthService, now
from app.common.models import Farm, User
from app.core.audit import write_audit
from app.core.config import settings
from app.core.errors import Conflict, ErrorCode, NotFound, RuleViolation
from app.db.base import engine, has_table
from app.ops.models import CertApplication, CertStatus

logger = logging.getLogger("agritrace.cert")

REQUIRED_MATERIALS: tuple[tuple[str, str], ...] = (
    ("land_proof_no", "土地承包经营权证"),
    ("test_report_no", "质量检测报告"),
    ("test_report_at", "检测报告出具日期"),
    ("commitment_signed", "质量安全承诺书"),
)


def add_months(day: date, months: int) -> date:
    """按月加日期，月末自动收敛（1/31 + 1 月 → 2/28）。"""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _to_dict(cert: CertApplication) -> dict:
    return {
        "cert_id": cert.cert_id,
        "cert_no": cert.cert_no,
        "farm_id": cert.farm_id,
        "applicant_id": cert.applicant_id,
        "crop_scope": cert.crop_scope,
        "status": cert.status,
        "status_label": CertStatus.LABELS.get(cert.status, cert.status),
        "land_proof_no": cert.land_proof_no,
        "land_proof_url": cert.land_proof_url,
        "test_report_no": cert.test_report_no,
        "test_report_at": cert.test_report_at.isoformat() if cert.test_report_at else None,
        "test_report_url": cert.test_report_url,
        "commitment_signed": cert.commitment_signed,
        "missing_items": cert.missing_items or [],
        "submitted_at": cert.submitted_at.isoformat(sep=" ", timespec="seconds")
        if cert.submitted_at
        else None,
        "reviewer_name": cert.reviewer_name,
        "review_opinion": cert.review_opinion,
        "reviewed_at": cert.reviewed_at.isoformat(sep=" ", timespec="seconds")
        if cert.reviewed_at
        else None,
        "reject_reason": cert.reject_reason,
        "valid_from": cert.valid_from.isoformat() if cert.valid_from else None,
        "valid_to": cert.valid_to.isoformat() if cert.valid_to else None,
        "remind_at": cert.remind_at.isoformat() if cert.remind_at else None,
    }


class CertService:
    # ------------------------------------------------------------ 提交
    @staticmethod
    def _missing(payload) -> list[str]:
        missing = [label for field, label in REQUIRED_MATERIALS if not getattr(payload, field, None)]
        return missing

    @staticmethod
    def submit(db: Session, user: User, payload) -> dict:
        farm = AuthService.get_farm_owned(db, user, payload.farm_id)
        missing = CertService._missing(payload)

        # 已通过且在有效期内的产地不再接受新申请，避免重复认证
        if farm.cert_status == "approved":
            in_flight = db.scalar(
                select(CertApplication).where(
                    CertApplication.farm_id == farm.farm_id,
                    CertApplication.status == CertStatus.SUBMITTED,
                )
            )
            if in_flight is None:
                raise Conflict("该产地认证已通过且在有效期内，如需变更请先申请重新认证")

        if missing:
            cert = CertService._save_draft(db, user, farm, payload, missing)
            return {
                "saved_as": CertStatus.DRAFT,
                "missing_items": missing,
                "cert": _to_dict(cert),
                "hint": "材料缺项已存为草稿，补齐后可再次提交；草稿不进入待审队列",
            }

        # BR-07：同一产地仅允许一份在途申请
        exists = db.scalar(
            select(CertApplication).where(
                CertApplication.farm_id == farm.farm_id,
                CertApplication.status == CertStatus.SUBMITTED,
            )
        )
        if exists is not None:
            raise Conflict(
                "该产地已有一份待审核的认证申请，请等待审核结论",
                data={
                    "cert_id": exists.cert_id,
                    "submitted_at": exists.submitted_at.isoformat(sep=" ", timespec="seconds")
                    if exists.submitted_at
                    else None,
                },
            )

        cert = CertService._save_draft(db, user, farm, payload, [])
        cert.status = CertStatus.SUBMITTED
        cert.submitted_at = now()
        cert.missing_items = []
        db.commit()
        db.refresh(cert)

        write_audit(
            db,
            action="cert.submit",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            resource=f"cert:{cert.cert_id}",
            detail={"farm_id": farm.farm_id, "crop_scope": cert.crop_scope},
        )
        return {
            "saved_as": CertStatus.SUBMITTED,
            "missing_items": [],
            "cert": _to_dict(cert),
            "hint": f"已进入待审队列，平台承诺 {settings.cert_review_sla_days} 个工作日内出结论",
        }

    @staticmethod
    def _save_draft(db: Session, user: User, farm: Farm, payload, missing: list[str]) -> CertApplication:
        """草稿复用：同一产地已有草稿则覆盖更新，避免草稿无限堆积。"""
        cert = db.scalar(
            select(CertApplication).where(
                CertApplication.farm_id == farm.farm_id,
                CertApplication.status == CertStatus.DRAFT,
            )
        )
        if cert is None:
            cert = CertApplication(
                cert_no=_build_cert_no(farm.farm_id),
                farm_id=farm.farm_id,
                applicant_id=user.user_id,
                status=CertStatus.DRAFT,
            )
            db.add(cert)
        cert.crop_scope = payload.crop_scope or farm.farm_name
        cert.land_proof_no = payload.land_proof_no
        cert.land_proof_url = payload.land_proof_url
        cert.test_report_no = payload.test_report_no
        cert.test_report_at = payload.test_report_at
        cert.test_report_url = payload.test_report_url
        cert.commitment_signed = payload.commitment_signed
        cert.materials = {
            "land_proof": {"no": payload.land_proof_no, "url": payload.land_proof_url},
            "test_report": {
                "no": payload.test_report_no,
                "at": payload.test_report_at.isoformat() if payload.test_report_at else None,
                "url": payload.test_report_url,
            },
            "commitment_signed": payload.commitment_signed,
        }
        cert.missing_items = missing
        db.commit()
        db.refresh(cert)
        return cert

    # ------------------------------------------------------------ 审核
    @staticmethod
    def review(db: Session, operator: User, cert_id: str, payload) -> dict:
        cert = db.get(CertApplication, cert_id)
        if cert is None:
            raise NotFound("认证申请不存在")

        # BR-08：只允许 submitted → approved / rejected 的单向流转
        if cert.status != CertStatus.SUBMITTED:
            raise RuleViolation(
                f"当前状态为「{CertStatus.LABELS.get(cert.status, cert.status)}」，不可再审核；"
                "审核结论一经提交不可修改，如需变更请重新发起申请",
                data={"status": cert.status},
                code=ErrorCode.STATE_INVALID,
            )

        farm = db.get(Farm, cert.farm_id)
        if farm is None:
            raise NotFound("产地不存在")

        if payload.decision == "reject":
            if not payload.opinion or len(payload.opinion.strip()) < 2:
                raise RuleViolation("驳回必须填写审核意见")
            cert.status = CertStatus.REJECTED
            cert.reject_reason = payload.reject_reason or payload.opinion
            cert.review_opinion = payload.opinion
            cert.reviewer_id = operator.user_id
            cert.reviewer_name = operator.display_name or operator.username
            cert.reviewed_at = now()
            farm.cert_status = "rejected"
            db.commit()
            db.refresh(cert)
            write_audit(
                db,
                action="cert.reject",
                user_id=operator.user_id,
                username=operator.username,
                role=operator.role,
                resource=f"cert:{cert.cert_id}",
                detail={"opinion": payload.opinion, "farm_id": cert.farm_id},
            )
            return _to_dict(cert)

        # ---- 通过分支：先做材料有效期校验（用例异常流 4a）----
        expired = CertService._report_expired(cert)
        if expired:
            raise RuleViolation(
                "检测报告超出有效期，不能审核通过；请驳回并注明原因",
                data={"test_report_at": cert.test_report_at.isoformat() if cert.test_report_at else None},
                code=ErrorCode.MATERIAL_EXPIRED,
            )

        today = date.today()
        cert.status = CertStatus.APPROVED
        cert.review_opinion = payload.opinion
        cert.reviewer_id = operator.user_id
        cert.reviewer_name = operator.display_name or operator.username
        cert.reviewed_at = now()
        cert.valid_from = today
        cert.valid_to = add_months(today, settings.cert_valid_months)
        cert.remind_at = cert.valid_to - timedelta(days=settings.cert_remind_days)
        farm.cert_status = "approved"
        db.commit()
        db.refresh(cert)

        # 通过后为产地下的商品打认证标识（交易域表存在时生效，不影响主流程）
        propagated = CertService._propagate_badge(db, farm.farm_id, cert)

        write_audit(
            db,
            action="cert.approve",
            user_id=operator.user_id,
            username=operator.username,
            role=operator.role,
            resource=f"cert:{cert.cert_id}",
            detail={"farm_id": cert.farm_id, "valid_to": cert.valid_to.isoformat(), "products": propagated},
        )
        result = _to_dict(cert)
        result["certified_products"] = propagated
        return result

    @staticmethod
    def _report_expired(cert: CertApplication) -> bool:
        if cert.test_report_at is None:
            return True
        limit = add_months(date.today(), -settings.test_report_valid_months)
        return cert.test_report_at < limit

    @staticmethod
    def _propagate_badge(db: Session, farm_id: str, cert: CertApplication) -> int:
        """给该产地全部商品打认证标识。

        交易域 model 不在本分支时（单独 checkout 本分支演示），这里直接跳过并返回 0，
        保证审核流程不会被跨域依赖卡住 —— 这也是「先降级再联调」的工程取舍。
        """
        if not has_table("product"):
            return 0
        columns = {col["name"] for col in inspect(engine).get_columns("product")}
        if "cert_badge" not in columns:
            return 0
        badge = f"GAP-{cert.cert_no[-6:]}"
        try:
            result = db.execute(
                text("UPDATE product SET cert_badge = :badge WHERE farm_id = :farm_id"),
                {"badge": badge, "farm_id": farm_id},
            )
            db.commit()
            return int(result.rowcount or 0)
        except Exception as exc:  # pragma: no cover - 跨域写入失败不阻断审核
            logger.warning("商品认证标识回写失败：%s", exc)
            db.rollback()
            return 0

    @staticmethod
    def current_badge(db: Session, farm_id: str) -> dict | None:
        """供交易域/溯源域查询某产地当前生效的认证标识。"""
        cert = db.scalar(
            select(CertApplication)
            .where(CertApplication.farm_id == farm_id, CertApplication.status == CertStatus.APPROVED)
            .order_by(CertApplication.valid_to.desc())
        )
        if cert is None:
            return None
        return {
            "badge": f"GAP-{cert.cert_no[-6:]}",
            "cert_no": cert.cert_no,
            "valid_from": cert.valid_from.isoformat() if cert.valid_from else None,
            "valid_to": cert.valid_to.isoformat() if cert.valid_to else None,
        }

    # ------------------------------------------------------------ 查询
    @staticmethod
    def list_queue(db: Session, *, status: str | None, page_no: int, size: int) -> dict:
        stmt = select(CertApplication)
        if status:
            stmt = stmt.where(CertApplication.status == status)
        else:
            stmt = stmt.where(CertApplication.status == CertStatus.SUBMITTED)
        total = len(list(db.scalars(stmt)))
        rows = list(
            db.scalars(
                stmt.order_by(CertApplication.submitted_at.asc(), CertApplication.created_at.asc())
                .offset((page_no - 1) * size)
                .limit(size)
            )
        )
        items = []
        for cert in rows:
            item = _to_dict(cert)
            farm = db.get(Farm, cert.farm_id)
            item["farm_name"] = farm.farm_name if farm else ""
            item["farm_region"] = f"{farm.province}{farm.city}" if farm else ""
            item["report_expired"] = CertService._report_expired(cert)
            item["overdue"] = CertService._is_overdue(cert)
            items.append(item)
        return {"items": items, "total": total, "page": page_no, "size": size}

    @staticmethod
    def _is_overdue(cert: CertApplication) -> bool:
        """超过承诺时效（默认 3 个工作日）未审，前端标红并计入运营考核。"""
        if cert.status != CertStatus.SUBMITTED or cert.submitted_at is None:
            return False
        return cert.submitted_at + timedelta(days=settings.cert_review_sla_days) < now()

    @staticmethod
    def list_mine(db: Session, user: User) -> list[dict]:
        rows = list(
            db.scalars(
                select(CertApplication)
                .where(CertApplication.applicant_id == user.user_id)
                .order_by(CertApplication.created_at.desc())
            )
        )
        out = []
        for cert in rows:
            item = _to_dict(cert)
            farm = db.get(Farm, cert.farm_id)
            item["farm_name"] = farm.farm_name if farm else ""
            out.append(item)
        return out

    @staticmethod
    def detail(db: Session, user: User, cert_id: str) -> dict:
        cert = db.get(CertApplication, cert_id)
        if cert is None:
            raise NotFound("认证申请不存在")
        # 资源级归属校验：农户只能看自己的申请
        if user.role == "farmer" and cert.applicant_id != user.user_id:
            raise NotFound("认证申请不存在")
        item = _to_dict(cert)
        farm = db.get(Farm, cert.farm_id)
        item["farm_name"] = farm.farm_name if farm else ""
        item["report_expired"] = CertService._report_expired(cert)
        item["overdue"] = CertService._is_overdue(cert)
        return item

    # ------------------------------------------------------------ 到期处理
    @staticmethod
    def sweep_expired(db: Session) -> dict:
        """到期失效 + 续期提醒。

        由调度器每天调用一次：过期的认证置为 expired 并同步 farm.cert_status，
        同时把 30 天内到期的申请整理成提醒清单（本期只做清单，不接短信通道）。
        """
        today = date.today()
        expired_rows = list(
            db.scalars(
                select(CertApplication).where(
                    CertApplication.status == CertStatus.APPROVED,
                    CertApplication.valid_to.is_not(None),
                    CertApplication.valid_to < today,
                )
            )
        )
        for cert in expired_rows:
            cert.status = CertStatus.EXPIRED
            farm = db.get(Farm, cert.farm_id)
            if farm is not None:
                farm.cert_status = "pending"
        if expired_rows:
            db.commit()

        remind_rows = list(
            db.scalars(
                select(CertApplication).where(
                    CertApplication.status == CertStatus.APPROVED,
                    CertApplication.remind_at.is_not(None),
                    CertApplication.remind_at <= today,
                )
            )
        )
        return {
            "expired": len(expired_rows),
            "reminders": [
                {
                    "cert_id": c.cert_id,
                    "farm_id": c.farm_id,
                    "valid_to": c.valid_to.isoformat() if c.valid_to else None,
                }
                for c in remind_rows
            ],
        }


def _build_cert_no(farm_id: str) -> str:
    return f"CERT-{datetime.now().strftime('%Y%m%d')}-{farm_id[-6:]}"
