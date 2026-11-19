"""用户与运营域出入参模型（接口层，Pydantic v2）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------- UC-12 认证
class CertSubmit(BaseModel):
    farm_id: str = Field(..., max_length=32)
    land_cert_no: str = Field(default="", max_length=64, description="土地承包/流转证明编号")
    report_no: str = Field(default="", max_length=64, description="检测报告编号")
    report_issue_date: date | None = Field(default=None, description="检测报告出具日期")
    report_expire_at: datetime | None = Field(default=None, description="检测报告有效期至")
    commitment_signed: bool = Field(default=False, description="是否已签署质量安全承诺书")
    materials: dict[str, Any] = Field(default_factory=dict, description="其他材料，键值对形式")
    submit: bool = Field(default=True, description="false 表示仅存草稿（不进入待审队列）")

    @field_validator("materials")
    @classmethod
    def _check_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > 4000:
            raise ValueError("材料内容过长，请精简至 4000 字符以内")
        return value


class CertReview(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)


class CertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    app_id: str
    app_no: str
    farm_id: str
    applicant_id: str
    status: str
    status_label: str = ""
    land_cert_no: str = ""
    report_no: str = ""
    report_expire_at: datetime | None = None
    commitment_signed: int = 0
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewer_id: str | None = None
    review_comment: str = ""
    valid_until: datetime | None = None
    created_at: datetime


class CertDetail(CertOut):
    farm_name: str = ""
    applicant_name: str = ""
    reviewer_name: str = ""
    materials: dict[str, Any] = Field(default_factory=dict)
    logs: list[ReviewLogOut] = Field(default_factory=list)


class ReviewLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: str
    app_id: str
    reviewer_id: str
    reviewer_name: str = ""
    action: str
    from_status: str = ""
    to_status: str = ""
    comment: str = ""
    created_at: datetime


# ------------------------------------------------------------------ UC-14 预警
class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    alert_id: str
    alert_no: str
    alert_type: str
    alert_type_label: str = ""
    level: str
    level_label: str = ""
    status: str
    status_label: str = ""
    batch_id: str | None = None
    code_value: str | None = None
    farm_id: str | None = None
    title: str
    detail: str = ""
    handler_id: str | None = None
    handled_at: datetime | None = None
    handle_note: str = ""
    created_at: datetime


class AlertHandle(BaseModel):
    status: Literal["processing", "closed"] = "closed"
    note: str = Field(default="", max_length=500)


# ------------------------------------------------------------------ UC-15 处置
class RegulatorAction(BaseModel):
    action: Literal["investigate", "freeze_batch", "release_batch", "penalize"]
    note: str = Field(default="", max_length=500)


# ------------------------------------------------------------------ UC-13 看板
class DashboardOut(BaseModel):
    """经营看板：一份数据同时服务农户（我的经营）与运营（平台全貌）。

    口径说明写在这里，避免两人对同一指标理解不一致：
    - 批次总数/环节数/扫码次数 来自溯源域
    - 商品数/订单数/GMV 来自交易域
    - 待审认证/未关闭预警 来自运营域
    """

    scope: str = "platform"
    batch_total: int = 0
    event_total: int = 0
    scan_total: int = 0
    product_total: int = 0
    order_total: int = 0
    order_paid: int = 0
    gmv_cents: int = 0
    gmv: float = 0.0
    pending_cert: int = 0
    open_alert: int = 0
    top_products: list[dict[str, Any]] = Field(default_factory=list)
    alert_by_type: dict[str, int] = Field(default_factory=dict)
    order_by_status: dict[str, int] = Field(default_factory=dict)


# CertDetail 定义时 ReviewLogOut 尚未声明，此处补一次前向引用解析
CertDetail.model_rebuild()
