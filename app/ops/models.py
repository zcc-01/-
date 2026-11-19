"""用户与运营域数据模型（UC-11～UC-15）。

四张表的定位：
- cert_application：产地认证申请，承载 BR-07（同一产地仅一份在途）与 BR-09（有效期 12 个月）
- review_log：审核动作的追加式流水，承载 BR-08（结论提交后不可修改）
- alert：异常预警单，是溯源域与运营域之间的**唯一真实耦合点**——
  扫码发现断链、环节时间倒挂、认证临期都会在这里落一张单
- audit_log：越权访问与敏感操作留痕（表 3-6 安全要求）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


class CertStatus:
    """认证申请状态机。

    注意 draft 的意义：材料不全时保存为草稿，不进入待审队列（UC-12 异常流 2a）。
    只有 pending 才算「在途」，BR-07 的互斥判断针对 pending。
    """

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = (DRAFT, PENDING, APPROVED, REJECTED)
    ACTIVE = (DRAFT, PENDING)  # 在途：不允许同产地再发起新申请

    LABELS = {
        DRAFT: "草稿",
        PENDING: "待审核",
        APPROVED: "已通过",
        REJECTED: "已驳回",
    }


class AlertType:
    CHAIN_BROKEN = "chain_broken"        # 哈希链断裂（UC-04 异常流 4a）
    TIME_INVERSION = "time_inversion"    # 环节时间倒挂（UC-14 扩展 UC-02）
    CERT_EXPIRING = "cert_expiring"      # 认证临期（BR-09）
    STOCK_DESYNC = "stock_desync"        # 库存异常（预留）
    LOG_FAILED = "log_failed"            # 扫码日志降级补写失败

    ALL = (CHAIN_BROKEN, TIME_INVERSION, CERT_EXPIRING, STOCK_DESYNC, LOG_FAILED)

    LABELS = {
        CHAIN_BROKEN: "溯源链断裂",
        TIME_INVERSION: "环节时间倒挂",
        CERT_EXPIRING: "认证临期",
        STOCK_DESYNC: "库存异常",
        LOG_FAILED: "日志写入失败",
    }


class AlertLevel:
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"

    LABELS = {INFO: "提示", WARN: "警告", CRITICAL: "严重"}


class AlertStatus:
    OPEN = "open"
    PROCESSING = "processing"
    CLOSED = "closed"

    ALL = (OPEN, PROCESSING, CLOSED)
    LABELS = {OPEN: "待处理", PROCESSING: "处理中", CLOSED: "已关闭"}


class CertApplication(Base, TimestampMixin):
    __tablename__ = "cert_application"

    app_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CAP"))
    app_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    farm_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("farm.farm_id"), nullable=False, index=True
    )
    applicant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CertStatus.DRAFT, index=True
    )

    # 材料以 JSON 文本存：土地证明、检测报告、承诺书的编号与有效期
    materials: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    land_cert_no: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    report_no: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    report_expire_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    commitment_signed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    reviewer_id: Mapped[str | None] = mapped_column(String(32), default=None)
    review_comment: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # BR-09：通过后有效期 12 个月
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class ReviewLog(Base, TimestampMixin):
    """审核动作流水：只追加，不更新——这是 BR-08「结论不可修改」的落地方式。

    想改结论只能重新发起申请，历史审核轨迹完整保留，监管回溯时可按时间还原。
    """

    __tablename__ = "review_log"

    log_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RVL"))
    app_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("cert_application.app_id"), nullable=False, index=True
    )
    reviewer_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # submit/approve/reject
    from_status: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    to_status: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    comment: Mapped[str] = mapped_column(String(500), nullable=False, default="")


class Alert(Base, TimestampMixin):
    """异常预警单：运营与监管的处置对象。

    dedup_key 用于幂等：同一批次同类型的未关闭预警只保留一张单，
    避免消费者反复扫码把预警列表刷爆（UC-04 异常流 4a 的实际问题）。
    """

    __tablename__ = "alert"

    alert_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ALT"))
    alert_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    alert_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertLevel.WARN)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AlertStatus.OPEN, index=True
    )

    batch_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    code_value: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    farm_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)

    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dedup_key: Mapped[str | None] = mapped_column(String(128), index=True, default=None)

    handler_id: Mapped[str | None] = mapped_column(String(32), default=None)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    handle_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")


class AuditLog(Base, TimestampMixin):
    """越权与敏感操作审计（表 3-6：越权访问返回 403 并写入审计日志）。"""

    __tablename__ = "audit_log"

    log_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("AUD"))
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    username: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="allowed")
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
