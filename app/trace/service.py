"""溯源链域业务逻辑（控制类，UC-01～UC-05）。

事务边界一律放在 Service 层：路由只做参数校验与鉴权，链组件只负责哈希与串链。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.models import Farm, User
from app.core.config import settings
from app.core.errors import BizError, Conflict, ErrorCode, Forbidden, NotFound, RuleViolation
from app.core.ids import new_batch_no
from app.ops.alerting import raise_chain_broken, raise_time_inversion
from app.trace.chain import HashChain, VerifyResult, canonical_detail, normalize_time
from app.trace.models import Batch, BatchStatus, CodeType, ScanLog, Stage, TraceCode, TraceEvent
from app.trace.schemas import BatchCreate, CodeGenerate, EventCreate

logger = logging.getLogger("agritrace.trace")

_MOBILE_UA = re.compile(r"Mobile|Android|iPhone|iPad", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def mask_name(name: str) -> str:
    """操作人脱敏：报告 3.6「手机号脱敏、评价不含身份信息」的同一口径。"""
    if not name:
        return "匿名"
    if len(name) == 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def parse_device(user_agent: str | None) -> str:
    if not user_agent:
        return "unknown"
    return "mobile" if _MOBILE_UA.search(user_agent) else "desktop"


class TraceService:
    # ------------------------------------------------------------------ 查询
    @staticmethod
    def get_batch(db: Session, batch_id: str) -> Batch:
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        return batch

    @staticmethod
    def _assert_farm_owner(db: Session, batch: Batch, user: User) -> None:
        farm = db.get(Farm, batch.farm_id)
        if farm is None or farm.owner_id != user.user_id:
            raise Forbidden("只能操作本产地的批次")

    # ------------------------------------------------------------------ UC-01
    @classmethod
    def create_batch(cls, db: Session, payload: BatchCreate, user: User) -> Batch:
        farm = db.get(Farm, payload.farm_id)
        if farm is None:
            raise NotFound("产地不存在，请先完成产地入驻")
        if farm.owner_id != user.user_id:
            raise Forbidden("只能为本人产地建档")

        duplicated = db.scalar(
            select(Batch).where(
                Batch.farm_id == payload.farm_id,
                Batch.plot_no == payload.plot_no,
                Batch.crop_name == payload.crop_name,
                Batch.harvest_date == payload.harvest_date,
            )
        )
        if duplicated is not None:
            raise Conflict("同一地块、品种与采收日期已存在批次，请勿重复建档")

        batch = Batch(
            batch_no=new_batch_no(),
            farm_id=payload.farm_id,
            crop_name=payload.crop_name,
            plot_no=payload.plot_no,
            plant_date=payload.plant_date,
            harvest_date=payload.harvest_date,
            expected_yield=payload.expected_yield,
            status=BatchStatus.DRAFT,
        )
        db.add(batch)
        db.flush()

        # 创世记录：与批次在同一事务内写入，保证不会出现「有批次无链」
        genesis_prev = HashChain.genesis_prev_hash(batch.batch_id)
        occurred = normalize_time(None)
        detail = canonical_detail(
            {
                "event": "batch_created",
                "crop_name": batch.crop_name,
                "plot_no": batch.plot_no,
                "plant_date": batch.plant_date.isoformat(),
                "harvest_date": batch.harvest_date.isoformat(),
            }
        )
        curr_hash = HashChain.compute_hash(
            prev_hash=genesis_prev,
            batch_id=batch.batch_id,
            seq_no=1,
            stage=Stage.PRODUCTION,
            operator_id=user.user_id,
            occurred_at=occurred,
            detail=detail,
        )
        db.add(
            TraceEvent(
                batch_id=batch.batch_id,
                seq_no=1,
                stage=Stage.PRODUCTION,
                operator_id=user.user_id,
                occurred_at=occurred,
                location=f"{farm.province}{farm.city}{farm.address}",
                detail=detail,
                prev_hash=genesis_prev,
                curr_hash=curr_hash,
            )
        )
        batch.event_count = 1
        batch.last_event_hash = curr_hash

        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise Conflict("批次重复或批次号冲突，请重试") from exc
        db.refresh(batch)
        return batch

    @classmethod
    def change_status(cls, db: Session, batch_id: str, target: str, user: User) -> Batch:
        batch = cls.get_batch(db, batch_id)
        cls._assert_farm_owner(db, batch, user)
        allowed = BatchStatus.TRANSITIONS.get(batch.status, ())
        if target not in allowed:
            raise RuleViolation(
                f"批次状态不允许从 {batch.status} 变更为 {target}",
                data={"current": batch.status, "allowed": list(allowed)},
            )
        batch.status = target
        db.commit()
        db.refresh(batch)
        return batch

    @classmethod
    def list_batches(
        cls,
        db: Session,
        user: User,
        *,
        status: str | None = None,
        page_no: int = 1,
        size: int = 20,
    ) -> tuple[list[Batch], int]:
        stmt = select(Batch)
        if user.role == "farmer":
            owned = select(Farm.farm_id).where(Farm.owner_id == user.user_id)
            stmt = stmt.where(Batch.farm_id.in_(owned))
        if status:
            stmt = stmt.where(Batch.status == status)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(
            stmt.order_by(Batch.created_at.desc()).offset((page_no - 1) * size).limit(size)
        ).all()
        return list(rows), int(total)

    # ------------------------------------------------------------------ UC-02
    @classmethod
    def append_event(
        cls, db: Session, batch_id: str, payload: EventCreate, user: User
    ) -> tuple[TraceEvent, str | None]:
        batch = cls.get_batch(db, batch_id)
        cls._assert_farm_owner(db, batch, user)
        if batch.status == BatchStatus.CLOSED:
            raise RuleViolation("批次已结批，不能再追加溯源记录")

        occurred = normalize_time(payload.occurred_at)
        warning: str | None = None
        last = db.scalar(
            select(TraceEvent)
            .where(TraceEvent.batch_id == batch_id)
            .order_by(TraceEvent.seq_no.desc())
            .limit(1)
        )
        if last is not None and occurred < last.occurred_at:
            # UC-14 扩展 UC-02：时间倒挂不阻断录入，但生成预警单供运营复核。
            # 预警单与事件在同一事务提交（append 之后才 commit），避免漏报。
            warning = (
                f"环节时间早于上一条记录（{last.occurred_at:%Y-%m-%d %H:%M:%S}），"
                "已记录并提交平台复核"
            )
            raise_time_inversion(
                db,
                batch_id=batch_id,
                stage=payload.stage,
                occurred_at=occurred.strftime("%Y-%m-%d %H:%M:%S"),
                prev_stage=last.stage,
                prev_time=last.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
            )

        event = HashChain.append(
            db,
            batch,
            stage=payload.stage,
            operator_id=user.user_id,
            occurred_at=occurred,
            location=payload.location,
            detail=payload.detail,
        )
        db.commit()
        db.refresh(event)
        db.refresh(batch)
        return event, warning

    @classmethod
    def list_events(cls, db: Session, batch_id: str) -> list[TraceEvent]:
        cls.get_batch(db, batch_id)
        return list(
            db.scalars(
                select(TraceEvent)
                .where(TraceEvent.batch_id == batch_id)
                .order_by(TraceEvent.seq_no)
            ).all()
        )

    # ------------------------------------------------------------------ UC-03
    @classmethod
    def generate_codes(
        cls, db: Session, batch_id: str, payload: CodeGenerate, user: User
    ) -> list[TraceCode]:
        batch = cls.get_batch(db, batch_id)
        cls._assert_farm_owner(db, batch, user)
        if batch.status not in (BatchStatus.HARVESTED, BatchStatus.QUALIFIED):
            raise RuleViolation("批次采收后才允许赋码")
        if payload.count > settings.max_code_batch:
            raise RuleViolation(f"单次赋码不得超过 {settings.max_code_batch} 个")

        prefix = CodeType.PREFIX[payload.code_type]
        wanted = list(range(1, payload.count + 1))
        existing = {
            code.item_no: code
            for code in db.scalars(
                select(TraceCode).where(
                    TraceCode.batch_id == batch_id,
                    TraceCode.code_type == payload.code_type,
                    TraceCode.item_no.in_(wanted),
                )
            ).all()
        }

        created = False
        for item_no in wanted:
            if item_no in existing:
                continue
            db.add(
                TraceCode(
                    # 码值由「批次号 + 码类型 + 四位序号」派生：可重现、可幂等
                    code_value=f"{batch.batch_no}-{prefix}{item_no:04d}",
                    code_type=payload.code_type,
                    batch_id=batch_id,
                    item_no=item_no,
                )
            )
            created = True

        if created:
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise Conflict("赋码冲突，请重试") from exc

        return list(
            db.scalars(
                select(TraceCode)
                .where(
                    TraceCode.batch_id == batch_id,
                    TraceCode.code_type == payload.code_type,
                    TraceCode.item_no.in_(wanted),
                )
                .order_by(TraceCode.item_no)
            ).all()
        )

    @classmethod
    def list_codes(
        cls, db: Session, batch_id: str, code_type: str | None = None, limit: int = 100
    ) -> list[TraceCode]:
        cls.get_batch(db, batch_id)
        stmt = select(TraceCode).where(TraceCode.batch_id == batch_id)
        if code_type:
            stmt = stmt.where(TraceCode.code_type == code_type)
        return list(db.scalars(stmt.order_by(TraceCode.item_no).limit(limit)).all())

    @classmethod
    def revoke_code(cls, db: Session, code_value: str, reason: str, user: User) -> TraceCode:
        code = db.scalar(select(TraceCode).where(TraceCode.code_value == code_value))
        if code is None:
            raise NotFound("溯源码不存在")
        batch = cls.get_batch(db, code.batch_id)
        cls._assert_farm_owner(db, batch, user)
        if code.status == "revoked":
            return code
        # 唯一允许的更新：作废留痕（report 4.5「仅追加」的唯一例外，写入 revoked_at/reason）
        code.status = "revoked"
        code.revoked_at = _now()
        code.revoked_reason = reason[:128]
        db.commit()
        db.refresh(code)
        return code

    # ------------------------------------------------------------------ UC-04
    @classmethod
    def scan(
        cls,
        db: Session,
        code_value: str,
        *,
        province: str | None = None,
        city: str | None = None,
        user_agent: str | None = None,
        ip: str | None = None,
        user: User | None = None,
    ) -> dict[str, Any]:
        code = db.scalar(select(TraceCode).where(TraceCode.code_value == code_value))
        if code is None:
            raise NotFound("未查询到该产品信息，请核对包装上的溯源码")
        if code.status == "revoked":
            raise BizError(
                ErrorCode.CODE_REVOKED, "该溯源码已作废，请核对包装或联系卖家", http_status=410
            )

        batch = cls.get_batch(db, code.batch_id)
        farm = db.get(Farm, batch.farm_id)
        events = list(
            db.scalars(
                select(TraceEvent)
                .where(TraceEvent.batch_id == batch.batch_id)
                .order_by(TraceEvent.seq_no)
            ).all()
        )

        # 校验失败不阻断展示：先给已存在的记录，再提示异常（报告 5.2.2）
        try:
            chain = HashChain.verify(db, batch.batch_id)
        except Exception:  # pragma: no cover - 兜底，避免校验异常导致整页 500
            chain = VerifyResult(
                batch_id=batch.batch_id,
                ok=False,
                total=len(events),
                expected_total=batch.event_count,
                reason="verify_error",
                message="溯源链校验服务暂时不可用，已提交平台复核",
            )

        operators = cls._operator_names(db, {e.operator_id for e in events})
        event_payload = [
            {
                "event_id": e.event_id,
                "batch_id": e.batch_id,
                "seq_no": e.seq_no,
                "stage": e.stage,
                "stage_label": Stage.LABELS.get(e.stage, e.stage),
                "operator_id": e.operator_id,
                "operator_name": operators.get(e.operator_id, "匿名"),
                "occurred_at": e.occurred_at,
                "location": e.location,
                "detail": canonical_detail(e.detail),
                "prev_hash": e.prev_hash,
                "curr_hash": e.curr_hash,
            }
            for e in events
        ]
        for item in event_payload:
            # detail 对外返回结构化对象，便于前端直接渲染
            import json

            try:
                item["detail"] = json.loads(item["detail"])
            except (TypeError, ValueError):
                item["detail"] = {}

        cls._write_scan_log(
            db,
            code_value=code.code_value,
            batch_id=batch.batch_id,
            user_id=user.user_id if user else None,
            province=province,
            city=city,
            user_agent=user_agent,
            ip=ip,
        )

        # UC-04 异常流 4a：校验发现断链时自动生成预警单。
        # 降级要求（表 3-6）：预警建单失败绝不能影响扫码结果返回，
        # 因此单独事务 + 吞异常 + 记日志，失败时消费者仍然看到完整履历与告警条。
        if not chain.ok:
            cls._safe_raise_chain_alert(db, batch.batch_id, code.code_value, chain)

        return {
            "code_value": code.code_value,
            "code_type": code.code_type,
            "code_status": code.status,
            "scope": "item" if code.code_type == CodeType.ITEM else "batch",
            "scope_note": "单品码当前沿用所属批次的环节记录（批次级记录）",
            "batch": batch,
            "farm": {
                "farm_id": farm.farm_id if farm else "",
                "farm_name": farm.farm_name if farm else "未知产地",
                "province": farm.province if farm else "",
                "city": farm.city if farm else "",
                "address": farm.address if farm else "",
                "cert_status": farm.cert_status if farm else "pending",
            },
            "events": event_payload,
            "chain": chain.to_dict(),
            "warning": None if chain.ok else chain.message,
        }

    @staticmethod
    def _farm_dict(db: Session, batch: Batch) -> dict[str, Any]:
        farm = db.get(Farm, batch.farm_id)
        if farm is None:
            return {}
        return {
            "farm_id": farm.farm_id,
            "farm_name": farm.farm_name,
            "province": farm.province,
            "city": farm.city,
            "address": farm.address,
            "cert_status": farm.cert_status,
        }

    @staticmethod
    def count_codes(db: Session, batch_id: str) -> int:
        return int(
            db.scalar(select(func.count()).select_from(TraceCode).where(TraceCode.batch_id == batch_id))
            or 0
        )

    @staticmethod
    def _operator_names(db: Session, operator_ids: set[str]) -> dict[str, str]:
        if not operator_ids:
            return {}
        rows = db.scalars(select(User).where(User.user_id.in_(operator_ids))).all()
        return {u.user_id: mask_name(u.display_name or u.username) for u in rows}

    @staticmethod
    def _safe_raise_chain_alert(
        db: Session, batch_id: str, code_value: str, chain: VerifyResult
    ) -> None:
        """生成断链预警单，失败只记日志、不影响扫码（表 3-6 降级要求）。"""
        try:
            alert = raise_chain_broken(
                db,
                batch_id=batch_id,
                code_value=code_value,
                broken_at=chain.broken_at,
                reason=chain.reason,
            )
            db.commit()
            if alert is not None:
                logger.warning("已为批次 %s 生成断链预警单 %s", batch_id, alert.alert_no)
        except Exception as exc:  # pragma: no cover - 预警失败不应阻断扫码
            db.rollback()
            logger.error("断链预警单生成失败 batch=%s: %s", batch_id, exc)

    @staticmethod
    def _write_scan_log(
        db: Session,
        *,
        code_value: str,
        batch_id: str,
        user_id: str | None,
        province: str | None,
        city: str | None,
        user_agent: str | None,
        ip: str | None,
    ) -> None:
        """日志写入失败不阻断查询（UC-04 异常流程 6a）。

        课程作业量级用「同会话尽力写入 + 吞异常」即可；生产可换成
        消息队列异步补写，接口契约不变。
        """
        try:
            db.add(
                ScanLog(
                    code_value=code_value,
                    batch_id=batch_id,
                    user_id=user_id,
                    scanned_at=_now(),
                    province=province,
                    city=city,
                    device=parse_device(user_agent),
                    ip=ip,
                )
            )
            db.commit()
        except Exception:
            db.rollback()

    # ------------------------------------------------------------------ UC-05
    @classmethod
    def verify_batch(cls, db: Session, batch_id: str) -> VerifyResult:
        return HashChain.verify(db, batch_id)

    @classmethod
    def list_scan_logs(
        cls, db: Session, batch_id: str, limit: int = 20
    ) -> list[ScanLog]:
        cls.get_batch(db, batch_id)
        return list(
            db.scalars(
                select(ScanLog)
                .where(ScanLog.batch_id == batch_id)
                .order_by(ScanLog.scanned_at.desc())
                .limit(limit)
            ).all()
        )
