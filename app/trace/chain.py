"""哈希链存证组件（基础设施层，张×× 主导设计）。

为什么单独抽成组件：报告 4.1 的创新点——把「防篡改」下沉为平台级能力，
业务层只管按序追加记录，链的串联与校验对业务代码透明。

一致性设计（报告 5.3.1 三轮迭代后的结论）：
1. 序号分配不用 `SELECT MAX(seq_no)+1`，而是对 batch 行做原子 `event_count + 1`，
   在 MySQL/PG 上等价于行级锁，在 SQLite 上取到库级写锁，天然串行化；
2. 唯一索引 (batch_id, seq_no) 作为最终兜底，冲突则回滚重试；
3. 不使用 Python 线程锁——多 worker 部署下无效，且会把校验流程一起锁死。

哈希口径（报告表 4-4）：
    curr_hash = SHA-256(prev_hash | batch_id | seq_no | stage | operator_id | occurred_at | detail)
occurred_at 统一截断到秒并以 ISO 无时区格式参与计算，detail 统一做 JSON 规范化，
保证「任何时刻、任何机器重算都得到同一结果」。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import BizError, ErrorCode, NotFound
from app.trace.models import Batch, TraceEvent


def normalize_time(value: datetime | None) -> datetime:
    """统一为「naive UTC + 秒精度」，避免时区/微秒造成哈希不可重现。"""
    if value is None:
        return datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0)


def canonical_detail(detail: dict[str, Any] | str | None) -> str:
    """detail 规范化为紧凑 JSON：键排序、无多余空格，保证重算一致。"""
    if detail is None:
        return "{}"
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return detail
    return json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class VerifyResult:
    batch_id: str
    ok: bool
    total: int
    expected_total: int
    broken_at: int | None = None
    reason: str | None = None
    broken_event_id: str | None = None
    message: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REASON_TEXT = {
    "seq_gap": "记录序号不连续，疑似有记录被删除",
    "prev_hash_mismatch": "前序摘要不匹配，疑似记录被插入或前序被改动",
    "content_tampered": "记录内容与摘要不一致，疑似字段被篡改",
    "tail_truncated": "链尾与批次锚点不一致，疑似尾部记录被删除",
    "count_mismatch": "记录总数与批次锚点不一致，疑似记录被删除",
}


class HashChain:
    """链式存证：追加与校验。"""

    @staticmethod
    def genesis_prev_hash(batch_id: str) -> str:
        """首条记录的 prev_hash：固定盐值 + 批次号 的摘要，可重现、不可预测。"""
        raw = f"{settings.genesis_salt}|{batch_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_hash(
        *,
        prev_hash: str,
        batch_id: str,
        seq_no: int,
        stage: str,
        operator_id: str,
        occurred_at: datetime,
        detail: str,
    ) -> str:
        raw = "|".join(
            [
                prev_hash,
                batch_id,
                str(seq_no),
                stage,
                operator_id,
                normalize_time(occurred_at).strftime("%Y-%m-%dT%H:%M:%S"),
                detail,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---------------------------------------------------------------- append
    @classmethod
    def append(
        cls,
        db: Session,
        batch: Batch,
        *,
        stage: str,
        operator_id: str,
        occurred_at: datetime | None = None,
        location: str | None = None,
        detail: dict[str, Any] | str | None = None,
        max_retry: int | None = None,
    ) -> TraceEvent:
        """在批次链尾追加一条记录（不提交事务，由调用方决定事务边界）。"""
        occurred = normalize_time(occurred_at)
        detail_json = canonical_detail(detail)
        retries = max_retry or settings.chain_max_retry

        for attempt in range(retries):
            try:
                # 1) 原子占用序号：DB 层行锁，等价于 SELECT ... FOR UPDATE 但少一次往返
                db.execute(
                    update(Batch)
                    .where(Batch.batch_id == batch.batch_id)
                    .values(event_count=Batch.event_count + 1)
                )
                seq_no, prev_hash = db.execute(
                    select(Batch.event_count, Batch.last_event_hash).where(
                        Batch.batch_id == batch.batch_id
                    )
                ).one()
                prev_hash = prev_hash or cls.genesis_prev_hash(batch.batch_id)

                curr_hash = cls.compute_hash(
                    prev_hash=prev_hash,
                    batch_id=batch.batch_id,
                    seq_no=seq_no,
                    stage=stage,
                    operator_id=operator_id,
                    occurred_at=occurred,
                    detail=detail_json,
                )
                event = TraceEvent(
                    batch_id=batch.batch_id,
                    seq_no=seq_no,
                    stage=stage,
                    operator_id=operator_id,
                    occurred_at=occurred,
                    location=location,
                    detail=detail_json,
                    prev_hash=prev_hash,
                    curr_hash=curr_hash,
                )
                db.add(event)
                db.execute(
                    update(Batch)
                    .where(Batch.batch_id == batch.batch_id)
                    .values(last_event_hash=curr_hash)
                )
                db.flush()
                db.expire(batch)
                return event
            except IntegrityError:
                # 唯一索引兜底：别的请求刚好抢到同一序号，回滚后重来
                db.rollback()
                db.refresh(batch)
                time.sleep(0.01 * (attempt + 1))
            except OperationalError:
                # SQLite 在极端并发下仍可能 lock，重试即可
                db.rollback()
                db.refresh(batch)
                time.sleep(0.02 * (attempt + 1))

        raise BizError(
            ErrorCode.CHAIN_CONFLICT,
            "溯源链并发写入冲突，请稍后重试",
            http_status=503,
        )

    # ----------------------------------------------------------------- verify
    @classmethod
    def verify(
        cls,
        db: Session,
        batch_id: str,
        page_size: int | None = None,
    ) -> VerifyResult:
        """重算整条链并定位断链位置。

        返回结果而非抛异常：UC-04 要求「先展示已有记录再提示异常」（报告 5.2.2）。
        分批流式读取（默认 500 条/批），避免 5000 条以上记录一次性进内存。
        """
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise NotFound("批次不存在")

        size = page_size or settings.chain_page_size
        stmt = (
            select(TraceEvent)
            .where(TraceEvent.batch_id == batch_id)
            .order_by(TraceEvent.seq_no)
            .execution_options(yield_per=size)
        )

        prev_hash = cls.genesis_prev_hash(batch_id)
        expected_seq = 1
        total = 0
        last_hash: str | None = None
        last_event_id: str | None = None

        for event in db.scalars(stmt):
            total += 1
            if event.seq_no != expected_seq:
                return cls._broken(
                    batch, total, expected_seq, "seq_gap", event.event_id
                )
            if event.prev_hash != prev_hash:
                return cls._broken(
                    batch, total, event.seq_no, "prev_hash_mismatch", event.event_id
                )
            recomputed = cls.compute_hash(
                prev_hash=event.prev_hash,
                batch_id=event.batch_id,
                seq_no=event.seq_no,
                stage=event.stage,
                operator_id=event.operator_id,
                occurred_at=event.occurred_at,
                detail=event.detail,
            )
            if recomputed != event.curr_hash:
                return cls._broken(
                    batch, total, event.seq_no, "content_tampered", event.event_id
                )
            prev_hash = event.curr_hash
            last_hash = event.curr_hash
            last_event_id = event.event_id
            expected_seq += 1

        if total != batch.event_count:
            return cls._broken(batch, total, total + 1, "count_mismatch", None)
        if total > 0 and last_hash != batch.last_event_hash:
            return cls._broken(batch, total, total, "tail_truncated", last_event_id)

        return VerifyResult(
            batch_id=batch_id,
            ok=True,
            total=total,
            expected_total=batch.event_count,
            message="溯源链完整",
        )

    @classmethod
    def _broken(
        cls,
        batch: Batch,
        total: int,
        seq_no: int | None,
        reason: str,
        event_id: str | None,
    ) -> VerifyResult:
        return VerifyResult(
            batch_id=batch.batch_id,
            ok=False,
            total=total,
            expected_total=batch.event_count,
            broken_at=seq_no,
            reason=reason,
            broken_event_id=event_id,
            message=f"第 {seq_no} 条记录校验失败：{REASON_TEXT.get(reason, '未知原因')}",
        )
