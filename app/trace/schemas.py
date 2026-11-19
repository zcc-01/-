"""溯源链域出入参模型（接口层，Pydantic v2）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.trace.models import BatchStatus, CodeType, Stage


class BatchCreate(BaseModel):
    farm_id: str = Field(..., max_length=32)
    crop_name: str = Field(..., min_length=1, max_length=64)
    plot_no: str = Field(..., min_length=1, max_length=32)
    plant_date: date
    harvest_date: date
    expected_yield: float | None = Field(default=None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _check_dates(self) -> BatchCreate:
        if self.harvest_date < self.plant_date:
            raise ValueError("采收日期不得早于播种日期")
        return self


class BatchStatusUpdate(BaseModel):
    status: Literal["growing", "harvested", "qualified", "closed"]


class EventCreate(BaseModel):
    stage: Literal["production", "processing", "quality", "logistics", "sales"]
    occurred_at: datetime | None = None
    location: str | None = Field(default=None, max_length=128)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("detail")
    @classmethod
    def _check_detail_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > 4000:
            raise ValueError("环节明细过长，请精简至 4000 字符以内")
        return value


class CodeGenerate(BaseModel):
    code_type: Literal["box", "item"] = CodeType.ITEM
    count: int = Field(..., ge=1, le=5000, description="单次赋码数量，上限 5000")


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    batch_id: str
    seq_no: int
    stage: str
    stage_label: str = ""
    operator_id: str
    operator_name: str = ""
    occurred_at: datetime
    location: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    curr_hash: str


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    batch_no: str
    farm_id: str
    crop_name: str
    plot_no: str
    plant_date: date
    harvest_date: date
    expected_yield: float | None = None
    status: str
    event_count: int
    last_event_hash: str | None = None
    created_at: datetime
    updated_at: datetime


class BatchDetail(BatchOut):
    farm: dict[str, Any] = Field(default_factory=dict)
    events: list[EventOut] = Field(default_factory=list)
    code_count: int = 0


class CodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code_id: str
    code_value: str
    code_type: str
    batch_id: str
    item_no: int
    status: str
    created_at: datetime


class VerifyOut(BaseModel):
    batch_id: str
    ok: bool
    total: int
    expected_total: int
    broken_at: int | None = None
    reason: str | None = None
    broken_event_id: str | None = None
    message: str
    checked_at: str


class ScanOut(BaseModel):
    code_value: str
    code_type: str
    code_status: str
    scope: str = "batch"
    scope_note: str = ""
    batch: BatchOut
    farm: dict[str, Any]
    events: list[EventOut]
    chain: VerifyOut
    warning: str | None = None


class ScanLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: str
    code_value: str
    batch_id: str | None = None
    scanned_at: datetime
    province: str | None = None
    city: str | None = None
    device: str | None = None


# 便于在 OpenAPI 文档里展示枚举取值
STAGE_CHOICES = Stage.ALL
BATCH_STATUS_CHOICES = BatchStatus.ALL
