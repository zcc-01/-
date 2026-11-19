"""用户与运营域数据模型（王×× 负责，UC-11～UC-15）。

四张自有表 + 一张验证码表，全部为「新增」，不改动 sys_user / farm 的既有字段：

| 表 | 用途 | 对应用例 |
|---|---|---|
| cert_application | 产地认证申请与审核结论（状态机 + 有效期） | UC-12 |
| alert_rule | 预警规则配置（可配置列表，而非硬编码 if-else） | UC-14 |
| alert | 预警工单（级别、状态、处置记录） | UC-14 / UC-15 |
| audit_log | 审计留痕（越权 403 必须留痕） | UC-11 / 全域 |
| login_code | 手机验证码（验证码登录子用例） | UC-11 |

设计取舍：审核结论「一经提交不可修改」不是靠接口自觉，而是靠
`CertService.review` 里的状态机 + 数据库层 status 只被单向前进，二者共同保证。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


class CertStatus:
    """认证申请状态机（单向流转，禁止回退）。"""

    DRAFT = "draft"          # 草稿：材料缺项，未进入待审队列
    SUBMITTED = "submitted"  # 待审：在途申请
    APPROVED = "approved"    # 通过
    REJECTED = "rejected"    # 驳回
    EXPIRED = "expired"      # 到期失效

    ALL = (DRAFT, SUBMITTED, APPROVED, REJECTED, EXPIRED)
    IN_FLIGHT = (SUBMITTED,)
    # 终态：不可再次审核（BR-08 审核结论不可修改）
    TERMINAL = (APPROVED, REJECTED)

    TRANSITIONS: dict[str, tuple[str, ...]] = {
        DRAFT: (SUBMITTED,),
        SUBMITTED: (APPROVED, REJECTED),
        APPROVED: (EXPIRED,),
        REJECTED: (),
        EXPIRED: (),
    }

    LABELS = {
        DRAFT: "草稿",
        SUBMITTED: "待审核",
        APPROVED: "已通过",
        REJECTED: "已驳回",
        EXPIRED: "已失效",
    }


class CertApplication(Base, TimestampMixin):
    """产地认证申请（UC-12）。

    材料用结构化列而非一个 JSON 大字段，原因是三条业务规则都要读单个材料：
    缺项校验要看哪一项为空、过期校验要看 test_report_at、审核工作台要逐项展示。
    """

    __tablename__ = "cert_application"
    __table_args__ = (
        # 同一产地同一时间仅允许一份在途申请（BR-07），由「部分唯一索引」在 DB 层兜底。
        # SQLite 支持带 WHERE 的唯一索引，MySQL 不支持，因此在迁移脚本里用触发器等价实现；
        # 应用层 CertService.submit 亦做一次前置校验，双保险。
        UniqueConstraint("cert_no", name="uq_cert_no"),
    )

    cert_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CER"))
    cert_no: Mapped[str] = mapped_column(String(32), nullable=False)
    farm_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    applicant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    crop_scope: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CertStatus.DRAFT, index=True)

    # --- 材料（结构化）---
    land_proof_no: Mapped[str | None] = mapped_column(String(64), default=None)
    land_proof_url: Mapped[str | None] = mapped_column(String(256), default=None)
    test_report_no: Mapped[str | None] = mapped_column(String(64), default=None)
    test_report_at: Mapped[date | None] = mapped_column(Date, default=None)
    test_report_url: Mapped[str | None] = mapped_column(String(256), default=None)
    commitment_signed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 附件清单快照，便于审计时还原提交内容
    materials: Mapped[dict | None] = mapped_column(JSON, default=None)
    # 缺项清单：草稿态返回给农户，告诉他缺什么
    missing_items: Mapped[list | None] = mapped_column(JSON, default=None)

    # --- 提交与审核 ---
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    reviewer_id: Mapped[str | None] = mapped_column(String(32), default=None)
    reviewer_name: Mapped[str | None] = mapped_column(String(32), default=None)
    review_opinion: Mapped[str | None] = mapped_column(String(256), default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    reject_reason: Mapped[str | None] = mapped_column(String(128), default=None)

    # --- 有效期（BR-09：12 个月，到期前 30 天提醒）---
    valid_from: Mapped[date | None] = mapped_column(Date, default=None)
    valid_to: Mapped[date | None] = mapped_column(Date, default=None)
    remind_at: Mapped[date | None] = mapped_column(Date, default=None)


class RuleCode:
    """三条内置预警规则。code 既做配置主键，也写进工单便于溯源到规则。"""

    NO_LOGISTICS = "R-01"      # 采收后 N 天仍无物流环节
    CROSS_PROVINCE = "R-02"    # 24 小时内同批次扫码跨 N 省
    CHAIN_BROKEN = "R-03"      # 哈希链校验失败

    BUILTIN = (NO_LOGISTICS, CROSS_PROVINCE, CHAIN_BROKEN)


class AlertLevel:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    ALL = (INFO, WARNING, CRITICAL)
    ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}

    LABELS = {INFO: "提示", WARNING: "警告", CRITICAL: "严重"}


class AlertStatus:
    OPEN = "open"                # 待处置
    ACKNOWLEDGED = "acknowledged"  # 处置中
    CLOSED = "closed"            # 已关闭

    ALL = (OPEN, ACKNOWLEDGED, CLOSED)
    LABELS = {OPEN: "待处置", ACKNOWLEDGED: "处置中", CLOSED: "已关闭"}

    TRANSITIONS: dict[str, tuple[str, ...]] = {
        OPEN: (ACKNOWLEDGED, CLOSED),
        ACKNOWLEDGED: (CLOSED, OPEN),
        CLOSED: (),
    }


class AlertRule(Base, TimestampMixin):
    """预警规则配置表（UC-14）。

    报告 5.2.4 的人工修改点：把 AI 给出的硬编码 if-else 抽成可配置列表，
    每条规则含 code / level / condition / message，运营可在页面里启停与调参。
    """

    __tablename__ = "alert_rule"

    rule_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RUL"))
    # unique=True 已隐含唯一索引；这里不再加 index=True，
    # 否则 SQLAlchemy 自动生成的索引名会与 alert.rule_code 的 ix_alert_rule_code 撞名。
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertLevel.WARNING)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 规则参数，例如 {"days": 7} / {"threshold": 3, "hours": 24}
    condition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    message: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Alert(Base, TimestampMixin):
    """预警工单。"""

    __tablename__ = "alert"
    __table_args__ = (
        # 幂等：同一规则 + 同一对象 + 同一去重键只允许一张工单，
        # 调度器每 30 分钟扫一次不会刷出重复工单（对应 BUG-15 类问题的通用解法）。
        UniqueConstraint("dedupe_key", name="uq_alert_dedupe"),
    )

    alert_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ALT"))
    rule_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertLevel.WARNING, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertStatus.OPEN, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, default="batch")
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    handled_by: Mapped[str | None] = mapped_column(String(32), default=None)
    handled_by_name: Mapped[str | None] = mapped_column(String(32), default=None)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    resolution: Mapped[str | None] = mapped_column(String(256), default=None)


class AuditLog(Base):
    """审计日志：只追加，无 update / delete 接口。"""

    __tablename__ = "audit_log"

    log_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("AUD"))
    user_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    username: Mapped[str | None] = mapped_column(String(32), default=None)
    role: Mapped[str | None] = mapped_column(String(16), default=None)
    method: Mapped[str | None] = mapped_column(String(8), default=None)
    path: Mapped[str | None] = mapped_column(String(128), default=None)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource: Mapped[str | None] = mapped_column(String(64), default=None)
    result: Mapped[str] = mapped_column(String(8), nullable=False, default="allow", index=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class LoginCode(Base):
    """手机验证码（验证码登录子用例）。

    只存 code 的 SHA-256 摘要，不存明文 —— 库被读走也拿不到可用验证码。
    """

    __tablename__ = "login_code"

    code_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("SMS"))
    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default="login")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
