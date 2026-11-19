"""用户与运营域 · 认证材料与审核的请求模型（UC-12）。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class CertSubmitRequest(BaseModel):
    """提交产地认证材料。

    材料允许缺项提交 —— 缺项时落草稿并返回缺项清单（用例异常流 2a），
    而不是直接报错，农户可以先存草稿再补。
    """

    farm_id: str = Field(..., min_length=6, max_length=32)
    crop_scope: str = Field(default="", max_length=128, description="认证品类范围，如「红富士苹果」")
    land_proof_no: str | None = Field(default=None, max_length=64, description="土地承包经营权证编号")
    land_proof_url: str | None = Field(default=None, max_length=256)
    test_report_no: str | None = Field(default=None, max_length=64, description="检测报告编号")
    test_report_at: date | None = Field(default=None, description="检测报告出具日期")
    test_report_url: str | None = Field(default=None, max_length=256)
    commitment_signed: bool = Field(default=False, description="是否已签署质量安全承诺书")


class CertReviewRequest(BaseModel):
    """审核结论。

    注意：本接口只接受 submitted 状态 —— 「结论不可修改」由服务层状态机保证，
    接口层面不提供「复审」入口（如需变更须重新发起申请）。
    """

    decision: str = Field(..., pattern="^(approve|reject)$")
    opinion: str = Field(..., min_length=2, max_length=256, description="审核意见，驳回时必须写明原因")
    reject_reason: str | None = Field(default=None, max_length=128)

    @field_validator("opinion")
    @classmethod
    def _strip_opinion(cls, v: str) -> str:
        return v.strip()


class CertQuery(BaseModel):
    status: str | None = None
    page: int = 1
    size: int = 20
