"""交易域出入参模型（接口层，Pydantic v2）。

一条原则：**金额与库存永远不接受前端结论**。
下单请求只收 product_id + quantity，价格由服务端按库中当前价重算；
库存由条件更新在数据库里判断，前端传来的任何 price / stock / total 一律忽略（报告 5.2.3）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.trade.models import OrderStatus, ProductStatus, TradeRule


# ------------------------------------------------------------------ 商品（UC-06）
class ProductCreate(BaseModel):
    farm_id: str = Field(..., max_length=32)
    batch_id: str | None = Field(default=None, max_length=32)
    title: str = Field(..., min_length=2, max_length=128)
    crop_name: str = Field(..., min_length=1, max_length=64)
    spec: str = Field(default="", max_length=32)
    unit: str = Field(default="份", max_length=16)
    price: float = Field(..., gt=0, le=999_999)
    stock: int = Field(default=0, ge=0, le=999_999)
    cert_badge: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=2000)


class ProductUpdate(BaseModel):
    """只允许改价格、库存、描述与认证标识；产地与批次一经上架不可改。"""

    title: str | None = Field(default=None, min_length=2, max_length=128)
    spec: str | None = Field(default=None, max_length=32)
    unit: str | None = Field(default=None, max_length=16)
    price: float | None = Field(default=None, gt=0, le=999_999)
    stock: int | None = Field(default=None, ge=0, le=999_999)
    cert_badge: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=2000)


class ProductStatusUpdate(BaseModel):
    status: Literal["on_shelf", "off_shelf"]


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: str
    product_no: str
    farm_id: str
    batch_id: str | None = None
    title: str
    crop_name: str
    spec: str
    unit: str
    price: float
    stock: int
    sales: int
    status: str
    status_label: str = ""
    cert_badge: str = ""
    description: str = ""

    @field_validator("status_label", mode="before")
    @classmethod
    def _label(cls, value: str) -> str:  # pragma: no cover - 由下发方填充
        return value or ""


# --------------------------------------------------------------- 购物车（UC-07/08）
class CartItemIn(BaseModel):
    product_id: str = Field(..., max_length=32)
    quantity: int = Field(..., ge=1, le=TradeRule.MAX_QTY_PER_ITEM)


class CartQuoteIn(BaseModel):
    items: list[CartItemIn] = Field(..., min_length=1, max_length=TradeRule.MAX_ITEMS_PER_ORDER)


class CartLineOut(BaseModel):
    product_id: str
    title: str
    spec: str
    unit: str
    price: float
    quantity: int
    subtotal: float
    stock: int
    available: bool
    reason: str = ""


class QuoteOut(BaseModel):
    """按产地分组的试算结果：与下单拆单规则保持一致，避免结算页金额与订单不一致。"""

    farm_id: str
    farm_name: str
    lines: list[CartLineOut]
    goods_amount: float
    freight: float
    pay_amount: float
    available: bool
    reason: str = ""


# ------------------------------------------------------------------ 订单（UC-08/09）
class AddressIn(BaseModel):
    receiver: str = Field(..., min_length=2, max_length=16)
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    detail: str = Field(..., min_length=5, max_length=120)

    def snapshot(self) -> str:
        """落库为单行快照，交付时由前端脱敏展示。"""
        return f"{self.receiver} {self.phone[:3]}****{self.phone[-4:]} {self.detail}"


class OrderCreate(BaseModel):
    items: list[CartItemIn] = Field(..., min_length=1, max_length=TradeRule.MAX_ITEMS_PER_ORDER)
    address: AddressIn
    remark: str = Field(default="", max_length=128)


class PayIn(BaseModel):
    channel: Literal["mockpay", "mock_alipay", "mock_wechat"] = "mockpay"


class ShipIn(BaseModel):
    express_no: str = Field(..., min_length=4, max_length=64)


class ReviewIn(BaseModel):
    product_id: str = Field(..., max_length=32)
    rating: int = Field(..., ge=1, le=5)
    content: str = Field(default="", max_length=500)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: str
    product_id: str
    batch_id: str | None = None
    product_title: str
    spec: str
    unit: str
    unit_price: float
    quantity: int
    subtotal: float


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    order_no: str
    buyer_id: str
    farm_id: str
    total_amount: float
    freight: float
    pay_amount: float
    status: str
    status_label: str = ""
    address_snapshot: str
    remark: str = ""
    express_no: str | None = None
    paid_at: datetime | None = None
    shipped_at: datetime | None = None
    received_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime | None = None
    items: list[OrderItemOut] = Field(default_factory=list)


class PayOut(BaseModel):
    order_id: str
    order_no: str
    status: str
    status_label: str
    payment_no: str | None = None
    trade_no: str | None = None
    pay_amount: float
    attempt_count: int = 0
    message: str = ""


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    review_id: str
    order_id: str
    product_id: str
    batch_id: str | None = None
    rating: int
    content: str
    user_display: str = ""
    created_at: datetime | None = None


__all__ = [
    "AddressIn",
    "CartItemIn",
    "CartLineOut",
    "CartQuoteIn",
    "OrderCreate",
    "OrderStatus",
    "OrderItemOut",
    "OrderOut",
    "PayIn",
    "PayOut",
    "ProductCreate",
    "ProductOut",
    "ProductStatus",
    "ProductStatusUpdate",
    "ProductUpdate",
    "QuoteOut",
    "ReviewIn",
    "ReviewOut",
    "ShipIn",
]
