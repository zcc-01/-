"""用户与运营域 · 预警与监管的请求模型（UC-14 / UC-15）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AlertDisposeRequest(BaseModel):
    action: str = Field(..., pattern="^(acknowledge|close|reopen)$")
    resolution: str = Field(default="", max_length=256, description="处置结论，关闭工单时必填")


class RuleToggleRequest(BaseModel):
    enabled: bool


class AlertSimulateRequest(BaseModel):
    rule_code: str = Field(..., max_length=16)
    target_id: str = Field(..., max_length=64)
