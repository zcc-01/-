"""交易域出入参模型（接口层，Pydantic v2）。

金额约定：入参只接受「数量」等业务参数，**不接受金额**——金额一律由服务端
按下单时刻的商品快照重算（报告 5.2.3 人工修改点）。出参同时给 cents 与元值，
前端不需要自己做除法。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


# ------------------------------------------------------------------ 商品
class ProductCreate(BaseModel):
    batch_id: str = Field(..., max_length=32)
    name: str = Field(..., min_length=1, max_length=64)
    category: str = Field(default="其他", max_length=32)
    category_id: str | None = Field(default=None, max_length=32, description="品类域的分类主键")
    price_yuan: float = Field(..., gt=0, le=1_000_000, description="单价，单位元，最多两位小数")
    stock: int = Field(..., ge=0, le=1_000_000)
    unit: str = Field(default="份", max_length=16)
    description: str = Field(default="", max_length=2000)
    spec: str = Field(default="", max_length=32, description="规格，如 5kg/箱")
    grade: str = Field(default="", max_length=16, description="等级，如 一级果")
    origin: str = Field(default="", max_length=64, description="产地，如 江西省赣州市信丰县")
    shelf_life: str = Field(default="", max_length=32, description="保质期，如 常温 7 天")
    storage: str = Field(default="", max_length=64, description="储存方式，如 冷藏 0~4℃")
    image_url: str = Field(default="", max_length=200)
    tags: str = Field(default="", max_length=128, description="逗号分隔的标签")
    is_recommended: bool = Field(default=False)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    category_id: str | None = Field(default=None, max_length=32)
    price_yuan: float | None = Field(default=None, gt=0, le=1_000_000)
    stock: int | None = Field(default=None, ge=0, le=1_000_000)
    unit: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["on_sale", "off_shelf"] | None = None
    spec: str | None = Field(default=None, max_length=32)
    grade: str | None = Field(default=None, max_length=16)
    origin: str | None = Field(default=None, max_length=64)
    shelf_life: str | None = Field(default=None, max_length=32)
    storage: str | None = Field(default=None, max_length=64)
    image_url: str | None = Field(default=None, max_length=200)
    tags: str | None = Field(default=None, max_length=128)
    is_recommended: bool | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: str
    batch_id: str
    farm_id: str
    name: str
    category: str
    category_id: str | None = None
    price_cents: int
    stock: int
    unit: str
    status: str
    certified: int
    description: str
    sales_count: int
    spec: str = ""
    grade: str = ""
    origin: str = ""
    shelf_life: str = ""
    storage: str = ""
    image_url: str = ""
    tags: str = ""
    is_recommended: bool = False
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price(self) -> float:
        return round(self.price_cents / 100, 2)


class ProductDetail(ProductOut):
    """详情在列表字段基础上补溯源侧信息（农场名、批次号、认证状态）。

    reviews 的类型在文件末尾 rebuild，见文件最后的 model_rebuild()。
    """

    farm_name: str = ""
    farm_address: str = ""
    batch_no: str = ""
    crop_name: str = ""
    cert_status: str = "pending"
    trace_code: str | None = None
    category_path: str = ""
    rating: float = 0.0
    reviews: list[ReviewOut] = Field(default_factory=list)


# ------------------------------------------------------------------ 购物车
class CartAdd(BaseModel):
    product_id: str = Field(..., max_length=32)
    quantity: int = Field(default=1, ge=1, le=999)


class CartUpdate(BaseModel):
    quantity: int = Field(..., ge=1, le=999)


class CartItemOut(BaseModel):
    cart_id: str
    product_id: str
    name: str
    price_cents: int
    quantity: int
    stock: int
    status: str
    unit: str
    subtotal_cents: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price(self) -> float:
        return round(self.price_cents / 100, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def subtotal(self) -> float:
        return round(self.subtotal_cents / 100, 2)


class CartOut(BaseModel):
    items: list[CartItemOut] = Field(default_factory=list)
    total_cents: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return round(self.total_cents / 100, 2)


# ------------------------------------------------------------------ 订单
class OrderCreate(BaseModel):
    """两种下单方式：cart_ids 为空表示「立即购买」，此时必须给 product_id。"""

    cart_ids: list[str] = Field(default_factory=list)
    product_id: str | None = Field(default=None, max_length=32)
    quantity: int = Field(default=1, ge=1, le=999)
    receiver_name: str = Field(..., min_length=1, max_length=32)
    receiver_phone: str = Field(..., min_length=6, max_length=20)
    address: str = Field(..., min_length=5, max_length=200)
    remark: str = Field(default="", max_length=200)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: str
    product_id: str
    product_name: str
    price_cents: int
    quantity: int
    subtotal_cents: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    order_no: str
    user_id: str
    farmer_id: str | None = None
    total_cents: int
    status: str
    status_label: str = ""
    receiver_name: str
    receiver_phone: str
    address: str
    remark: str
    expire_at: datetime | None = None
    paid_at: datetime | None = None
    shipped_at: datetime | None = None
    received_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    pay_attempts: int = 0
    last_fail_reason: str | None = None
    created_at: datetime
    items: list[OrderItemOut] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return round(self.total_cents / 100, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def receiver_phone_masked(self) -> str:
        """手机号脱敏（表 3-6 合规要求）。"""
        phone = self.receiver_phone or ""
        return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone


# ------------------------------------------------------------------ 支付
class PayRequest(BaseModel):
    channel: Literal["mock", "wechat", "alipay"] = "mock"
    # 模拟网关：force_fail 用于演示「支付失败可重试三次」的异常流程
    force_fail: bool = Field(default=False, description="仅演示用，强制返回支付失败")


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: str
    order_id: str
    amount_cents: int
    channel: str
    status: str
    transaction_no: str | None = None
    fail_reason: str | None = None
    paid_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount(self) -> float:
        return round(self.amount_cents / 100, 2)


class PayResult(BaseModel):
    order: OrderOut
    payment: PaymentOut | None = None
    retryable: bool = False
    message: str = ""


# ------------------------------------------------------------------ 评价
class ReviewCreate(BaseModel):
    product_id: str = Field(..., max_length=32)
    rating: int = Field(..., ge=1, le=5)
    content: str = Field(default="", max_length=1000)


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    review_id: str
    order_id: str
    product_id: str
    user_id: str
    rating: int
    content: str
    display_name: str
    created_at: datetime


# ProductDetail 定义时 ReviewOut 尚未声明，此处补一次前向引用解析
ProductDetail.model_rebuild()
