"""溯源链域数据模型（张×× 负责，UC-01～UC-05）。

与报告表 4-4 完全对齐：trace_event 只追加、不更新、不删除；
batch 表额外维护 event_count / last_event_hash，用于校验「尾部被截断」这类
单靠逐条重算无法发现的篡改（见 app/trace/chain.py 的说明）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


class BatchStatus:
    DRAFT = "draft"          # 建档
    GROWING = "growing"      # 生长中
    HARVESTED = "harvested"  # 已采收
    QUALIFIED = "qualified"  # 质检合格
    CLOSED = "closed"        # 已结批

    ALL = (DRAFT, GROWING, HARVESTED, QUALIFIED, CLOSED)

    # 状态机：只允许单向流转，禁止回退（报告 5.3 的「单向流转」约束）
    TRANSITIONS: dict[str, tuple[str, ...]] = {
        DRAFT: (GROWING, HARVESTED),
        GROWING: (HARVESTED,),
        HARVESTED: (QUALIFIED,),
        QUALIFIED: (CLOSED,),
        CLOSED: (),
    }


class Stage:
    """溯源环节：生产、加工、质检、物流、销售（报告表 4-4）。"""

    PRODUCTION = "production"
    PROCESSING = "processing"
    QUALITY = "quality"
    LOGISTICS = "logistics"
    SALES = "sales"

    ALL = (PRODUCTION, PROCESSING, QUALITY, LOGISTICS, SALES)

    LABELS = {
        PRODUCTION: "生产",
        PROCESSING: "加工",
        QUALITY: "质检",
        LOGISTICS: "物流",
        SALES: "销售",
    }


class Batch(Base, TimestampMixin):
    __tablename__ = "batch"
    __table_args__ = (
        # 同一产地 + 地块 + 品种 + 采收日期唯一，重复建档返回 409（报告 5.2.1）
        UniqueConstraint("farm_id", "plot_no", "crop_name", "harvest_date", name="uq_batch_identity"),
    )

    batch_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("BAT"))
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    farm_id: Mapped[str] = mapped_column(String(32), ForeignKey("farm.farm_id"), nullable=False, index=True)
    crop_name: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_no: Mapped[str] = mapped_column(String(32), nullable=False)
    plant_date: Mapped[date] = mapped_column(Date, nullable=False)
    harvest_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_yield: Mapped[float | None] = mapped_column(Numeric(10, 2), default=None)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=BatchStatus.DRAFT)

    # 链尾锚点：追加事件时同步 +1 / 更新为最新 curr_hash
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    events: Mapped[list[TraceEvent]] = relationship(
        back_populates="batch", order_by="TraceEvent.seq_no", cascade="save-update"
    )
    codes: Mapped[list[TraceCode]] = relationship(back_populates="batch", cascade="save-update")


class TraceEvent(Base, TimestampMixin):
    """溯源环节记录：仅追加。"""

    __tablename__ = "trace_event"
    __table_args__ = (
        # 并发写入的最终兜底：同一批次序号唯一，冲突即重试（报告 5.3.1）
        UniqueConstraint("batch_id", "seq_no", name="uq_event_seq"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("EVT"))
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("batch.batch_id"), nullable=False, index=True
    )
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), default=None)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    curr_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    batch: Mapped[Batch] = relationship(back_populates="events")


class CodeType:
    BOX = "box"
    ITEM = "item"

    ALL = (BOX, ITEM)
    PREFIX = {BOX: "BOX", ITEM: "ITM"}


class TraceCode(Base, TimestampMixin):
    """溯源码：批次码（箱码）与单品码双粒度。"""

    __tablename__ = "trace_code"

    code_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("COD"))
    code_value: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    code_type: Mapped[str] = mapped_column(String(8), nullable=False)
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("batch.batch_id"), nullable=False, index=True
    )
    item_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    revoked_reason: Mapped[str | None] = mapped_column(String(128), default=None)

    batch: Mapped[Batch] = relationship(back_populates="codes")


class ScanLog(Base, TimestampMixin):
    """扫码日志：既是审计留痕，也是运营侧渠道洞察的数据源。"""

    __tablename__ = "scan_log"

    log_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("LOG"))
    code_value: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    province: Mapped[str | None] = mapped_column(String(32), default=None)
    city: Mapped[str | None] = mapped_column(String(32), default=None)
    device: Mapped[str | None] = mapped_column(String(64), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
