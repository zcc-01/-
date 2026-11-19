"""交易域数据模型（李×× 负责，UC-06～UC-10）。

与报告表 4-3「数据库表清单」严格对齐，交易域共 5 张表：
    product / orders / order_item / payment / review

三条硬约束直接落到模型层，而不是只写在业务代码里：
1. 订单状态单向流转（pending→paid→shipped→received，pending→cancelled），
   由 ``OrderStatus.TRANSITIONS`` 表达，Service 只允许按表流转；
2. ``payment.order_id`` 建唯一索引，从数据库层杜绝同一订单出现两条成功流水（BR-04）；
3. ``product.stock`` 的扣减一律走条件更新（``UPDATE ... WHERE stock >= n``），
   模型不提供「先查后改」的入口，从设计上消除超卖（报告 5.3.2）。

字段命名沿用公共基础层的约定：主键为雪花风格字符串 + 业务前缀，
对外单号（order_no / product_no）与主键分离，与溯源域的 batch_id / batch_no 保持一致。
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


def _biz_no(prefix: str) -> str:
    """对外业务单号：{PREFIX}-YYYYMMDD-XXXX，便于人工核对与客诉定位。"""
    return f"{prefix}-{time.strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"


def new_product_no() -> str:
    return _biz_no("SP")


def new_order_no() -> str:
    return _biz_no("ORD")


def new_payment_no() -> str:
    return _biz_no("PAY")


class ProductStatus:
    """商品状态：只区分在售与下架，不做中间态（UC-06）。"""

    ON_SHELF = "on_shelf"
    OFF_SHELF = "off_shelf"

    ALL = (ON_SHELF, OFF_SHELF)
    LABELS = {ON_SHELF: "在售", OFF_SHELF: "已下架"}


class OrderStatus:
    """订单状态机（报告表 4-5 的五个状态）。"""

    PENDING = "pending"      # 待支付
    PAID = "paid"            # 已支付
    SHIPPED = "shipped"      # 已发货
    RECEIVED = "received"    # 已收货
    CANCELLED = "cancelled"  # 已取消

    ALL = (PENDING, PAID, SHIPPED, RECEIVED, CANCELLED)

    LABELS = {
        PENDING: "待支付",
        PAID: "已支付",
        SHIPPED: "已发货",
        RECEIVED: "已收货",
        CANCELLED: "已取消",
    }

    # 单向流转：不允许 received → paid 这类回退，也不允许跳过支付直接发货。
    TRANSITIONS: dict[str, tuple[str, ...]] = {
        PENDING: (PAID, CANCELLED),
        PAID: (SHIPPED,),
        SHIPPED: (RECEIVED,),
        RECEIVED: (),
        CANCELLED: (),
    }

    @classmethod
    def can_transit(cls, src: str, dst: str) -> bool:
        return dst in cls.TRANSITIONS.get(src, ())


class PaymentStatus:
    INITIATED = "initiated"
    SUCCESS = "success"
    FAILED = "failed"

    ALL = (INITIATED, SUCCESS, FAILED)


class TradeRule:
    """交易域业务参数（报告 3.3 的 BR-04～BR-06 与 UC-08 异常流程）。

    集中成常量而非散落在 Service 里，评审时一眼能对上业务规则；
    需要按环境调整的两项（支付超时、重试上限）允许用环境变量覆盖。
    """

    FREIGHT_FLAT = 8.00            # 单笔未满包邮门槛的固定运费
    FREE_FREIGHT_THRESHOLD = 99.00  # 满 99 元包邮，产地直供的常见策略
    MAX_QTY_PER_ITEM = 99
    MAX_ITEMS_PER_ORDER = 20
    PAY_TIMEOUT_MINUTES = int(os.getenv("ORDER_PAY_TIMEOUT_MINUTES", "30"))
    MAX_PAY_ATTEMPTS = int(os.getenv("ORDER_MAX_PAY_ATTEMPTS", "3"))


class Product(Base, TimestampMixin):
    """商品表：一行代表一个「产地 + 批次 + 规格」，即一个可售 SKU。

    把规格摊平成行而不是再拆 SKU 子表，是为了守住报告 4.5 的 13 张表约束；
    同一批次的 500g / 1kg 包装各占一行，库存与价格独立，条件更新按行加锁。
    """

    __tablename__ = "product"
    __table_args__ = (
        # 同一产地、同一批次、同标题同规格不重复建档，避免农户误操作刷出重复商品
        UniqueConstraint("farm_id", "batch_id", "title", "spec", name="uq_product_identity"),
    )

    product_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("PRD"))
    product_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    farm_id: Mapped[str] = mapped_column(String(32), ForeignKey("farm.farm_id"), nullable=False, index=True)
    # 关联生产批次：商品详情据此跳转溯源时间轴，可空（非溯源品类的普通农产品）
    batch_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("batch.batch_id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    crop_name: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="份")
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProductStatus.OFF_SHELF, index=True
    )
    # 产地认证标识：由用户与运营域（UC-12 审核通过）回填，交易域只读不改
    cert_badge: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Order(Base, TimestampMixin):
    """订单主表：一个订单只对应一个供货产地（报告表 4-5 的 farm_id）。

    购物车跨产地时按 farm_id 拆单，这样「按产地统计」与「农户只处理自己订单」
    两个需求都能用同一个字段表达，不需要在明细行上再聚合。
    """

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ORD"))
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    buyer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sys_user.user_id"), nullable=False, index=True
    )
    farm_id: Mapped[str] = mapped_column(String(32), ForeignKey("farm.farm_id"), nullable=False, index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    freight: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    # 实付 = total_amount（商品小计合计）+ freight，落库避免每次查询重算
    pay_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderStatus.PENDING, index=True
    )
    # 地址快照：收货地址后续被修改不影响历史订单（报告表 4-5 的设计意图）
    address_snapshot: Mapped[str] = mapped_column(String(256), nullable=False)
    remark: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    express_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(default=None)
    shipped_at: Mapped[datetime | None] = mapped_column(default=None)
    received_at: Mapped[datetime | None] = mapped_column(default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(default=None)
    cancel_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.item_id"
    )


class OrderItem(Base, TimestampMixin):
    """订单明细：下单瞬间的商品快照，商品后续改价改名不影响历史订单。"""

    __tablename__ = "order_item"

    item_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("OIT"))
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    product_title: Mapped[str] = mapped_column(String(128), nullable=False)
    spec: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="份")
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base, TimestampMixin):
    """支付流水：一单一行的设计（order_id 唯一）。

    报告 BR-04 要求「同一订单仅允许一条成功流水」，直接落在唯一索引上，
    并发重复点击支付时由数据库兜底，而不是靠应用层的 if 判断。
    失败重试不新增行，只累加 attempt_count 并覆盖 fail_reason——
    这既保留了「最多重试三次」的计数，也避免流水表出现大量噪声行。
    """

    __tablename__ = "payment"
    __table_args__ = (UniqueConstraint("order_id", name="uq_payment_order"),)

    payment_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("PAY"))
    payment_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="mockpay")
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaymentStatus.INITIATED, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trade_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(default=None)


class Review(Base, TimestampMixin):
    """评价表：收货后才允许评价，一单一商品一条（UC-10）。"""

    __tablename__ = "review"
    __table_args__ = (UniqueConstraint("order_id", "product_id", name="uq_review_order_product"),)

    review_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("REV"))
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(String(500), nullable=False, default="")
