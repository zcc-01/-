"""交易域数据模型（UC-06～UC-10）。

设计要点：
- 金额一律用「分」存整数（price_cents / total_cents），避免浮点累加误差。
  展示层除以 100，接口同时给出 *_{cents} 与元值，前端不用自己做除法取整。
- 订单明细保存商品快照（名称、单价），商品后续改价不影响已下单订单，
  对应报告 5.2.3 的「订单金额由服务端按商品快照重算，忽略前端传值」。
- payment 表在 order_id 上建唯一索引，从数据库层面兜住 BR-04
  「同一订单仅允许一条成功支付流水」，而不是只靠应用层判断。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


class ProductStatus:
    ON_SALE = "on_sale"
    OFF_SHELF = "off_shelf"

    ALL = (ON_SALE, OFF_SHELF)
    LABELS = {ON_SALE: "在售", OFF_SHELF: "已下架"}


class OrderStatus:
    """订单状态机：单向流转，仅 pending 与 paid 允许取消/退款分支。"""

    PENDING = "pending"      # 待支付（已预占库存）
    PAID = "paid"            # 已支付
    SHIPPED = "shipped"      # 已发货
    RECEIVED = "received"    # 已收货
    CANCELLED = "cancelled"  # 已取消（库存已释放）

    ALL = (PENDING, PAID, SHIPPED, RECEIVED, CANCELLED)

    LABELS = {
        PENDING: "待支付",
        PAID: "已支付",
        SHIPPED: "已发货",
        RECEIVED: "已收货",
        CANCELLED: "已取消",
    }

    TRANSITIONS: dict[str, tuple[str, ...]] = {
        PENDING: (PAID, CANCELLED),
        PAID: (SHIPPED, CANCELLED),  # 已支付未发货可取消（走退款）
        SHIPPED: (RECEIVED,),
        RECEIVED: (),
        CANCELLED: (),
    }


class PayStatus:
    SUCCESS = "success"
    FAILED = "failed"


class Product(Base, TimestampMixin):
    """商品：从某个已质检的批次上架而来，因此与溯源域天然关联。"""

    __tablename__ = "product"

    product_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("PRD")
    )
    # 商品必须挂在批次上，这是交易域与溯源域之间唯一的硬依赖
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("batch.batch_id"), nullable=False, index=True
    )
    farm_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("farm.farm_id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="其他")
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="份")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProductStatus.ON_SALE, index=True
    )
    # 由运营域认证审核通过后回写（UC-12 步骤 7），交易域只读
    certified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sales_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def price_yuan(self) -> float:
        return round(self.price_cents / 100, 2)


class CartItem(Base, TimestampMixin):
    """购物车条目：同一用户同一商品只保留一行，重复加购累加数量。"""

    __tablename__ = "cart_item"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_cart_user_product"),)

    cart_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CRT"))
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("product.product_id"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Order(Base, TimestampMixin):
    __tablename__ = "shop_order"

    order_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("ORD")
    )
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    farmer_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderStatus.PENDING, index=True
    )

    receiver_name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    receiver_phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    remark: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    expire_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    cancel_reason: Mapped[str | None] = mapped_column(String(128), default=None)

    # UC-08 异常流 7a：支付失败允许重试三次，超过则只允许取消
    pay_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_fail_reason: Mapped[str | None] = mapped_column(String(128), default=None)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="save-update", lazy="selectin"
    )

    @property
    def total_yuan(self) -> float:
        return round(self.total_cents / 100, 2)


class OrderItem(Base, TimestampMixin):
    """订单明细：保存下单时刻的商品快照，商品改价不回溯影响历史订单。"""

    __tablename__ = "order_item"

    item_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("OIT"))
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("shop_order.order_id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(64), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base, TimestampMixin):
    """支付流水。

    BR-04「同一订单仅允许一条成功支付流水」的落地方式：**部分唯一索引**——
    只对 status='success' 的行做唯一约束。为什么不直接对 order_id 建全量唯一：
    支付失败也需要留痕，失败流水会占住唯一键，导致「失败可重试三次」无法实现。
    不加任何约束、只靠应用层先查后写，在并发下会出现双写成功。

    部分唯一索引在 SQLite 上完整生效（sqlite_where）；MySQL 不支持该语法，
    退化为普通索引，此时靠事务内「先 SELECT ... FOR UPDATE 再 INSERT」兜底。
    """

    __tablename__ = "payment"
    __table_args__ = (
        Index(
            "uq_payment_success",
            "order_id",
            unique=True,
            sqlite_where=text("status = 'success'"),
        ),
    )

    payment_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("PAY")
    )
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("shop_order.order_id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PayStatus.SUCCESS)
    transaction_no: Mapped[str | None] = mapped_column(String(64), default=None)
    fail_reason: Mapped[str | None] = mapped_column(String(128), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class Review(Base, TimestampMixin):
    """评价晒单：仅有收货后才能评价（UC-10 扩展 UC-09）。"""

    __tablename__ = "review"
    __table_args__ = (UniqueConstraint("order_id", "product_id", name="uq_review_once"),)

    review_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("REV")
    )
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("shop_order.order_id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 合规要求（表 3-6）：评价内容不含个人身份信息，落库前做脱敏
    display_name: Mapped[str] = mapped_column(String(32), nullable=False, default="匿名用户")
