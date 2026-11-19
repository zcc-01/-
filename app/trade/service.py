"""交易域业务逻辑（控制类，UC-06～UC-10）。

事务边界一律在 Service 层，路由只做参数校验、鉴权与响应包装（与溯源域同一约定）。
交易域比溯源域多两条纪律，写在类注释里防止后人改坏：

1. **库存在数据库里判断，不在 Python 里判断。**
   扣减统一走 ``UPDATE product SET stock = stock - n WHERE product_id = ? AND stock >= n``，
   受影响行数为 0 即判定失败并回滚整个下单事务。任何「先 SELECT 再比较再 UPDATE」
   的写法都会在并发下超卖（报告 5.3.2 记录的正是这个坑）。
2. **支付调用必须在事务外。**
   下单事务提交后才调用网关，网关返回后再开一个短事务更新订单与流水；
   外部调用超时不会长时间占用数据库连接。

订单拆单：一个订单只对应一个产地（报告表 4-5 的 farm_id），
购物车跨产地时按 farm_id 拆成多个订单，同一事务内全部成功或全部回滚。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.errors import Conflict, Forbidden, NotFound, RuleViolation
from app.trade import gateway as gateway_module
from app.trade.models import (
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Product,
    ProductStatus,
    Review,
    TradeRule,
    new_order_no,
    new_payment_no,
    new_product_no,
)
from app.trade.schemas import CartItemIn, OrderCreate, ProductCreate, ProductUpdate, ReviewIn

logger = logging.getLogger("agritrace.trade")

_CENT = Decimal("0.01")


def _now() -> datetime:
    """统一用不带时区的 UTC，与公共基础层的 TimestampMixin 保持一致。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_CENT)


def _money(value: Any) -> float:
    """对外输出统一转 float 并保留两位，避免 JSON 里出现长尾小数。"""
    return float(_dec(value))


def mask_name(name: str, fallback: str = "匿名买家") -> str:
    """评价展示脱敏：报告 3.6 要求评价不含可识别身份信息。"""
    text = (name or "").strip()
    if not text:
        return fallback
    if len(text) == 1:
        return text
    return text[0] + "*" * (len(text) - 1)


class TradeBase:
    """交易域共用的取数与权限校验。"""

    @staticmethod
    def get_order(db: Session, order_id: str) -> Order:
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        return order

    @staticmethod
    def get_product(db: Session, product_id: str) -> Product:
        product = db.get(Product, product_id)
        if product is None:
            raise NotFound("商品不存在或已下架")
        return product

    @staticmethod
    def assert_farm_owner(db: Session, farm_id: str, user: User) -> Farm:
        farm = db.get(Farm, farm_id)
        if farm is None:
            raise NotFound("产地不存在")
        if farm.owner_id != user.user_id:
            raise Forbidden("只能管理本产地的商品与订单")
        return farm

    @staticmethod
    def assert_order_visible(db: Session, order: Order, user: User) -> None:
        """买家本人、供货农户、平台运营/监管均可查看，其他人一律 403。"""
        if order.buyer_id == user.user_id:
            return
        if user.role in (Role.OPERATOR, Role.REGULATOR):
            return
        farm = db.get(Farm, order.farm_id)
        if farm is not None and farm.owner_id == user.user_id:
            return
        raise Forbidden("无权查看该订单")

    @staticmethod
    def farm_map(db: Session, farm_ids: set[str]) -> dict[str, Farm]:
        if not farm_ids:
            return {}
        rows = db.scalars(select(Farm).where(Farm.farm_id.in_(farm_ids))).all()
        return {farm.farm_id: farm for farm in rows}

    @staticmethod
    def rating_map(db: Session, product_ids: set[str]) -> dict[str, tuple[float, int]]:
        if not product_ids:
            return {}
        rows = db.execute(
            select(Review.product_id, func.avg(Review.rating), func.count(Review.review_id))
            .where(Review.product_id.in_(product_ids))
            .group_by(Review.product_id)
        ).all()
        return {pid: (round(float(avg or 0), 1), int(cnt)) for pid, avg, cnt in rows}


class ProductService(TradeBase):
    """商品上架管理（UC-06）与检索筛选（UC-07）。"""

    @classmethod
    def create_product(cls, db: Session, payload: ProductCreate, user: User) -> Product:
        cls.assert_farm_owner(db, payload.farm_id, user)

        if payload.batch_id:
            from app.trace.models import Batch

            batch = db.get(Batch, payload.batch_id)
            if batch is None:
                raise NotFound("关联批次不存在")
            if batch.farm_id != payload.farm_id:
                # 跨产地挂批次会让溯源码指向别人的地块，必须挡在上架之前
                raise Forbidden("只能关联本产地的生产批次")

        duplicated = db.scalar(
            select(Product).where(
                Product.farm_id == payload.farm_id,
                Product.batch_id == payload.batch_id,
                Product.title == payload.title,
                Product.spec == payload.spec,
            )
        )
        if duplicated is not None:
            raise Conflict("同批次同规格的商品已存在，请直接修改价格或库存")

        product = Product(
            product_no=new_product_no(),
            farm_id=payload.farm_id,
            batch_id=payload.batch_id,
            title=payload.title,
            crop_name=payload.crop_name,
            spec=payload.spec,
            unit=payload.unit,
            price=_dec(payload.price),
            stock=payload.stock,
            status=ProductStatus.ON_SHELF,
            cert_badge=payload.cert_badge or "",
            description=payload.description,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product

    @classmethod
    def update_product(cls, db: Session, product_id: str, payload: ProductUpdate, user: User) -> Product:
        product = cls.get_product(db, product_id)
        cls.assert_farm_owner(db, product.farm_id, user)
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "price" in data:
            data["price"] = _dec(data["price"])
        for key, value in data.items():
            setattr(product, key, value)
        db.commit()
        db.refresh(product)
        return product

    @classmethod
    def change_status(cls, db: Session, product_id: str, status: str, user: User) -> Product:
        product = cls.get_product(db, product_id)
        cls.assert_farm_owner(db, product.farm_id, user)
        if status not in ProductStatus.ALL:
            raise RuleViolation("非法的商品状态")
        product.status = status
        db.commit()
        db.refresh(product)
        return product

    @classmethod
    def list_products(
        cls,
        db: Session,
        *,
        keyword: str | None = None,
        farm_id: str | None = None,
        province: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        cert_only: bool = False,
        in_stock_only: bool = False,
        sort: str = "newest",
        user: User | None = None,
        mine: bool = False,
        page_no: int = 1,
        size: int = 20,
    ) -> tuple[list[Product], int]:
        """检索筛选（UC-07）：默认只返回在售商品，农户 ``mine=1`` 时可看到自己的下架商品。"""
        stmt = select(Product).join(Farm, Farm.farm_id == Product.farm_id)

        if mine and user is not None and user.role == Role.FARMER:
            stmt = stmt.where(Farm.owner_id == user.user_id)
        else:
            stmt = stmt.where(Product.status == ProductStatus.ON_SHELF)

        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(Product.title.like(like) | Product.crop_name.like(like))
        if farm_id:
            stmt = stmt.where(Product.farm_id == farm_id)
        if province:
            stmt = stmt.where(Farm.province == province)
        if min_price is not None:
            stmt = stmt.where(Product.price >= _dec(min_price))
        if max_price is not None:
            stmt = stmt.where(Product.price <= _dec(max_price))
        if cert_only:
            stmt = stmt.where(Product.cert_badge != "")
        if in_stock_only:
            stmt = stmt.where(Product.stock > 0)

        order_by = {
            "price_asc": (Product.price.asc(), Product.product_id.asc()),
            "price_desc": (Product.price.desc(), Product.product_id.asc()),
            "sales_desc": (Product.sales.desc(), Product.product_id.asc()),
            "newest": (Product.created_at.desc(), Product.product_id.asc()),
        }.get(sort, (Product.created_at.desc(), Product.product_id.asc()))

        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(
            stmt.order_by(*order_by).offset((page_no - 1) * size).limit(size)
        ).all()
        return list(rows), int(total)

    @classmethod
    def detail(cls, db: Session, product_id: str, user: User | None = None) -> dict[str, Any]:
        """商品详情：串上产地信息与溯源批次摘要，下架商品仅本产地农户可见。"""
        product = cls.get_product(db, product_id)
        if product.status != ProductStatus.ON_SHELF:
            is_owner = (
                user is not None
                and user.role == Role.FARMER
                and _is_farm_owner(db, product.farm_id, user.user_id)
            )
            if not is_owner:
                raise NotFound("商品已下架")

        farm = db.get(Farm, product.farm_id)
        batch_info = None
        if product.batch_id:
            from app.trace.models import Batch

            batch = db.get(Batch, product.batch_id)
            if batch is not None:
                batch_info = {
                    "batch_id": batch.batch_id,
                    "batch_no": batch.batch_no,
                    "crop_name": batch.crop_name,
                    "plot_no": batch.plot_no,
                    "harvest_date": batch.harvest_date.isoformat(),
                    "status": batch.status,
                    "event_count": batch.event_count,
                }
        avg, count = cls.rating_map(db, {product_id}).get(product_id, (0.0, 0))
        return {
            "product": product,
            "farm": farm,
            "batch": batch_info,
            "rating_avg": avg,
            "review_count": count,
            "can_buy": product.status == ProductStatus.ON_SHELF and product.stock > 0,
        }


class OrderService(TradeBase):
    """下单与支付（UC-08）、发货与确认收货（UC-09）。"""

    # ------------------------------------------------------------- 试算
    @classmethod
    def quote(cls, db: Session, items: list[CartItemIn], user: User | None = None) -> list[dict[str, Any]]:
        """购物车试算：按产地分组给出金额与运费，供结算页展示。

        试算只做「友好提示」，不是库存的最终裁决——真正的裁决在下单时的条件更新，
        两步分开是为了让用户先看到哪件商品缺货，而不是提交后只收到一句「下单失败」。
        """
        grouped = cls._group_by_farm(db, items)
        quotes: list[dict[str, Any]] = []
        for farm_id, lines in grouped.items():
            farm = db.get(Farm, farm_id)
            goods = Decimal("0.00")
            out_lines: list[dict[str, Any]] = []
            reasons: list[str] = []
            for product, qty in lines:
                available = product.status == ProductStatus.ON_SHELF and product.stock >= qty
                subtotal = _dec(product.price) * qty
                if available:
                    goods += subtotal
                elif product.status != ProductStatus.ON_SHELF:
                    reasons.append(f"{product.title} 已下架")
                else:
                    reasons.append(f"{product.title} 库存不足（仅剩 {product.stock} 件）")
                out_lines.append(
                    {
                        "product_id": product.product_id,
                        "title": product.title,
                        "spec": product.spec,
                        "unit": product.unit,
                        "price": _money(product.price),
                        "quantity": qty,
                        "subtotal": _money(subtotal),
                        "stock": product.stock,
                        "available": available,
                        "reason": "" if available else reasons[-1],
                    }
                )
            freight = cls.calc_freight(goods)
            quotes.append(
                {
                    "farm_id": farm_id,
                    "farm_name": farm.farm_name if farm else "",
                    "lines": out_lines,
                    "goods_amount": _money(goods),
                    "freight": _money(freight),
                    "pay_amount": _money(goods + freight),
                    "available": not reasons,
                    "reason": "；".join(reasons),
                }
            )
        return quotes

    @staticmethod
    def calc_freight(goods_amount: Decimal) -> Decimal:
        """满包邮门槛免运费，否则收固定运费（TradeRule 里集中配置）。"""
        if goods_amount <= 0:
            return Decimal("0.00")
        if goods_amount >= _dec(TradeRule.FREE_FREIGHT_THRESHOLD):
            return Decimal("0.00")
        return _dec(TradeRule.FREIGHT_FLAT)

    # ------------------------------------------------------------- 下单
    @classmethod
    def create_orders(cls, db: Session, payload: OrderCreate, user: User) -> list[Order]:
        """下单（UC-08 基本流程 1～4）：单事务内完成扣库存 + 写订单 + 写明细。"""
        if user.role != Role.CONSUMER:
            raise Forbidden("仅消费者可以下单")

        grouped = cls._group_by_farm(db, payload.items)
        address = payload.address.snapshot()
        orders: list[Order] = []

        try:
            for farm_id, lines in grouped.items():
                goods = Decimal("0.00")
                snapshots: list[dict[str, Any]] = []
                for product, qty in lines:
                    # 跨域契约（docs/分工与协作说明.md 第 3 节）：下单前确认该批次的溯源码未被全部作废。
                    # 交易域只读 trace_code，不写溯源表；批次尚未赋码不算异常（赋码是建批后的后续流程）。
                    cls._assert_trace_usable(db, product)
                    # 条件更新：affected_rows == 0 说明「已下架」或「库存不够」，一律失败
                    affected = db.execute(
                        update(Product)
                        .where(
                            Product.product_id == product.product_id,
                            Product.status == ProductStatus.ON_SHELF,
                            Product.stock >= qty,
                        )
                        .values(stock=Product.stock - qty)
                    ).rowcount
                    if not affected:
                        db.refresh(product)
                        reason = (
                            "已下架"
                            if product.status != ProductStatus.ON_SHELF
                            else f"库存不足（仅剩 {product.stock} 件）"
                        )
                        raise RuleViolation(
                            f"「{product.title}」{reason}，请调整数量后重试",
                            data={"product_id": product.product_id, "stock": product.stock},
                        )
                    subtotal = _dec(product.price) * qty
                    goods += subtotal
                    snapshots.append(
                        {
                            "product_id": product.product_id,
                            "batch_id": product.batch_id,
                            "product_title": product.title,
                            "spec": product.spec,
                            "unit": product.unit,
                            "unit_price": _dec(product.price),
                            "quantity": qty,
                            "subtotal": subtotal,
                        }
                    )

                freight = cls.calc_freight(goods)
                order = Order(
                    order_no=new_order_no(),
                    buyer_id=user.user_id,
                    farm_id=farm_id,
                    total_amount=goods,
                    freight=freight,
                    pay_amount=goods + freight,
                    status=OrderStatus.PENDING,
                    address_snapshot=address,
                    remark=payload.remark,
                )
                db.add(order)
                db.flush()
                for snapshot in snapshots:
                    db.add(OrderItem(order_id=order.order_id, **snapshot))
                orders.append(order)

            # 扣减与订单写入必须同生共死：任何一步失败都不允许留下「扣了库存没订单」
            db.commit()
        except Exception:
            db.rollback()
            raise

        for order in orders:
            db.refresh(order)
        return orders

    # ------------------------------------------------------------- 支付
    @classmethod
    def pay(cls, db: Session, order_id: str, user: User, channel: str = "mockpay") -> dict[str, Any]:
        """支付（UC-08 基本流程 5～8）。

        顺序：校验可支付 → 事务外调用网关 → 短事务更新订单与流水。
        「重复支付」由 payment.order_id 的唯一索引兜底，应用层只负责把结果翻译成幂等返回。
        """
        order = cls.get_order(db, order_id)
        if order.buyer_id != user.user_id:
            raise Forbidden("只能支付本人的订单")

        payment = db.scalar(select(Payment).where(Payment.order_id == order.order_id))

        # 幂等：已支付的订单再次点击支付，直接返回既有成功流水，不再调用网关
        if order.status in (OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.RECEIVED):
            return _pay_payload(order, payment, message="订单已支付，无需重复支付")

        if order.status != OrderStatus.PENDING:
            raise cls._illegal(order, "支付")

        if cls.is_expired(order):
            cls._cancel(db, order, reason="超时未支付，系统自动取消", restore_stock=True)
            raise RuleViolation("订单已超过支付时限并自动取消，请重新下单")

        attempts = payment.attempt_count if payment else 0
        if attempts >= TradeRule.MAX_PAY_ATTEMPTS:
            cls._cancel(db, order, reason=f"支付失败达到 {attempts} 次，系统取消订单", restore_stock=True)
            raise RuleViolation("支付重试次数已达上限，订单已取消")

        # ---- 事务外调用外部网关（此处不允许出现任何 Session 写操作）
        result = gateway_module.gateway.pay(
            order_no=order.order_no, amount=_money(order.pay_amount), channel=channel
        )

        if not result.ok:
            cls._record_failure(db, order, payment, channel, result.message)
            raise RuleViolation(f"支付失败：{result.message}")

        try:
            order.status = OrderStatus.PAID
            order.paid_at = _now()
            if payment is None:
                payment = Payment(
                    payment_no=new_payment_no(),
                    order_id=order.order_id,
                    channel=channel,
                    amount=_dec(order.pay_amount),
                    status=PaymentStatus.SUCCESS,
                    attempt_count=1,
                    trade_no=result.trade_no,
                    paid_at=order.paid_at,
                )
                db.add(payment)
            else:
                payment.channel = channel
                payment.amount = _dec(order.pay_amount)
                payment.status = PaymentStatus.SUCCESS
                payment.attempt_count = payment.attempt_count + 1
                payment.trade_no = result.trade_no
                payment.fail_reason = None
                payment.paid_at = order.paid_at
            # 销量在支付成功后累加，未支付订单不占用销量口径
            for item in order.items:
                db.execute(
                    update(Product)
                    .where(Product.product_id == item.product_id)
                    .values(sales=Product.sales + item.quantity)
                )
            db.commit()
        except IntegrityError:
            # 并发下同一订单被点了两次支付：唯一索引拒掉第二条流水，回滚后按幂等处理
            db.rollback()
            logger.warning("订单 %s 出现并发支付，已按幂等返回既有流水", order.order_no)
            existing_order = cls.get_order(db, order_id)
            existing_payment = db.scalar(select(Payment).where(Payment.order_id == order_id))
            return _pay_payload(existing_order, existing_payment, message="订单已支付，无需重复支付")
        except Exception:
            db.rollback()
            raise

        db.refresh(order)
        db.refresh(payment)
        return _pay_payload(order, payment, message="支付成功")

    @staticmethod
    def _assert_trace_usable(db: Session, product: Product) -> None:
        """溯源可用性校验：批次的码全部作废说明该批次出了质量事故，必须停售。

        只在「有码且全部作废」时拦截：批次刚建档、尚未赋码属于正常中间状态，
        此时拦下单会误伤刚上架的新品。
        """
        if not product.batch_id:
            return
        from app.trace.models import TraceCode

        total = db.scalar(
            select(func.count()).select_from(TraceCode).where(TraceCode.batch_id == product.batch_id)
        ) or 0
        if not total:
            return
        active = db.scalar(
            select(func.count())
            .select_from(TraceCode)
            .where(TraceCode.batch_id == product.batch_id, TraceCode.status == "active")
        ) or 0
        if not active:
            raise RuleViolation(
                f"「{product.title}」所属批次的溯源码已全部作废，平台暂停该商品销售",
                data={"product_id": product.product_id, "batch_id": product.batch_id},
            )

    @staticmethod
    def _illegal(order: Order, action: str) -> RuleViolation:
        """状态机拒绝的统一句式：把「当前什么状态、为什么不行」一次说清楚。"""
        label = OrderStatus.LABELS.get(order.status, order.status)
        return RuleViolation(f"当前订单状态为「{label}」，不可{action}")

    @staticmethod
    def is_expired(order: Order) -> bool:
        deadline = order.created_at + timedelta(minutes=TradeRule.PAY_TIMEOUT_MINUTES)
        return order.status == OrderStatus.PENDING and _now() > deadline

    @classmethod
    def _record_failure(
        cls, db: Session, order: Order, payment: Payment | None, channel: str, reason: str
    ) -> None:
        """失败也要留痕：累加重试次数，达到上限即取消订单并释放库存。"""
        try:
            if payment is None:
                payment = Payment(
                    payment_no=new_payment_no(),
                    order_id=order.order_id,
                    channel=channel,
                    amount=_dec(order.pay_amount),
                    status=PaymentStatus.FAILED,
                    attempt_count=1,
                    fail_reason=reason[:128],
                )
                db.add(payment)
            else:
                payment.channel = channel
                payment.status = PaymentStatus.FAILED
                payment.attempt_count = payment.attempt_count + 1
                payment.fail_reason = reason[:128]
            if payment.attempt_count >= TradeRule.MAX_PAY_ATTEMPTS:
                cls._cancel(db, order, reason="支付失败次数达到上限，系统自动取消", restore_stock=True)
            else:
                db.commit()
        except Exception:
            db.rollback()
            raise

    # ------------------------------------------------------------- 取消
    @classmethod
    def cancel(cls, db: Session, order_id: str, user: User, reason: str | None = None) -> Order:
        order = cls.get_order(db, order_id)
        if order.buyer_id != user.user_id and user.role != Role.OPERATOR:
            raise Forbidden("只能取消本人的订单")
        if order.status != OrderStatus.PENDING:
            raise cls._illegal(order, "取消")
        return cls._cancel(db, order, reason=reason or "买家主动取消", restore_stock=True)

    @classmethod
    def _cancel(cls, db: Session, order: Order, *, reason: str, restore_stock: bool) -> Order:
        """取消订单并原路释放库存（BR-06：库存只允许通过状态流转释放，不允许人工改数）。"""
        try:
            if restore_stock:
                for item in order.items:
                    db.execute(
                        update(Product)
                        .where(Product.product_id == item.product_id)
                        .values(stock=Product.stock + item.quantity)
                    )
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = _now()
            order.cancel_reason = reason[:128]
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(order)
        return order

    # ------------------------------------------------------------- 履约
    @classmethod
    def ship(cls, db: Session, order_id: str, express_no: str, user: User) -> Order:
        order = cls.get_order(db, order_id)
        cls.assert_farm_owner(db, order.farm_id, user)
        if not OrderStatus.can_transit(order.status, OrderStatus.SHIPPED):
            raise cls._illegal(order, "发货")
        order.status = OrderStatus.SHIPPED
        order.shipped_at = _now()
        order.express_no = express_no
        db.commit()
        db.refresh(order)
        return order

    @classmethod
    def receive(cls, db: Session, order_id: str, user: User) -> Order:
        order = cls.get_order(db, order_id)
        if order.buyer_id != user.user_id:
            raise Forbidden("只能确认本人的订单")
        if not OrderStatus.can_transit(order.status, OrderStatus.RECEIVED):
            raise cls._illegal(order, "确认收货")
        order.status = OrderStatus.RECEIVED
        order.received_at = _now()
        db.commit()
        db.refresh(order)
        return order

    # ------------------------------------------------------------- 查询
    @classmethod
    def list_orders(
        cls,
        db: Session,
        user: User,
        *,
        scope: str = "mine",
        status: str | None = None,
        page_no: int = 1,
        size: int = 20,
    ) -> tuple[list[Order], int]:
        stmt = select(Order)
        if scope == "sales":
            if user.role not in (Role.FARMER, Role.OPERATOR):
                raise Forbidden("只有农户或平台运营可以查看销售订单")
            if user.role == Role.FARMER:
                farm_ids = db.scalars(select(Farm.farm_id).where(Farm.owner_id == user.user_id)).all()
                if not farm_ids:
                    return [], 0
                stmt = stmt.where(Order.farm_id.in_(farm_ids))
        else:
            stmt = stmt.where(Order.buyer_id == user.user_id)

        if status:
            stmt = stmt.where(Order.status == status)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(
            stmt.order_by(Order.created_at.desc()).offset((page_no - 1) * size).limit(size)
        ).all()
        return list(rows), int(total)

    @classmethod
    def sweep_expired(cls, db: Session, limit: int = 200) -> int:
        """定时任务：把超时未支付的订单批量取消并释放库存（UC-08 异常流程 6a）。

        做惰性过期（支付/查询时判断）+ 定时兜底两层：前者保证用户视角即时正确，
        后者保证没人访问的僵尸订单也会被清理，库存不会长期被死单占住。
        """
        cutoff = _now() - timedelta(minutes=TradeRule.PAY_TIMEOUT_MINUTES)
        rows = db.scalars(
            select(Order)
            .where(Order.status == OrderStatus.PENDING, Order.created_at < cutoff)
            .limit(limit)
        ).all()
        cancelled = 0
        for order in rows:
            try:
                cls._cancel(db, order, reason="超时未支付，系统自动取消", restore_stock=True)
                cancelled += 1
            except Exception as exc:  # 单笔失败不影响其余订单
                logger.warning("自动取消订单 %s 失败：%s", order.order_no, exc)
                db.rollback()
        if cancelled:
            logger.info("已自动取消 %s 笔超时未支付订单", cancelled)
        return cancelled

    @classmethod
    def _group_by_farm(
        cls, db: Session, items: list[CartItemIn]
    ) -> dict[str, list[tuple[Product, int]]]:
        """按产地分组并合并同商品的多行，顺带做重复项归一。"""
        merged: dict[str, int] = {}
        for item in items:
            merged[item.product_id] = merged.get(item.product_id, 0) + item.quantity
            if merged[item.product_id] > TradeRule.MAX_QTY_PER_ITEM:
                raise RuleViolation(f"单个商品数量不得超过 {TradeRule.MAX_QTY_PER_ITEM} 件")

        grouped: dict[str, list[tuple[Product, int]]] = {}
        for product_id, qty in merged.items():
            product = cls.get_product(db, product_id)
            grouped.setdefault(product.farm_id, []).append((product, qty))
        return grouped


class ReviewService(TradeBase):
    """评价晒单（UC-10）：收货后才允许评价，一单一商品一条。"""

    @classmethod
    def create(cls, db: Session, order_id: str, payload: ReviewIn, user: User) -> Review:
        order = cls.get_order(db, order_id)
        if order.buyer_id != user.user_id:
            raise Forbidden("只能评价本人的订单")
        if order.status != OrderStatus.RECEIVED:
            # 扩展关系：UC-10 扩展 UC-09，只有收货后才进入评价分支
            raise RuleViolation("订单确认收货后才能评价")

        item = next((i for i in order.items if i.product_id == payload.product_id), None)
        if item is None:
            raise RuleViolation("该商品不在本订单中")

        duplicated = db.scalar(
            select(Review).where(Review.order_id == order_id, Review.product_id == payload.product_id)
        )
        if duplicated is not None:
            raise Conflict("该订单中的这个商品已评价，请勿重复提交")

        review = Review(
            order_id=order_id,
            product_id=payload.product_id,
            batch_id=item.batch_id,
            user_id=user.user_id,
            rating=payload.rating,
            content=payload.content,
        )
        db.add(review)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise Conflict("该订单中的这个商品已评价，请勿重复提交") from None
        db.refresh(review)
        return review

    @classmethod
    def list_by_product(cls, db: Session, product_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = db.scalars(
            select(Review)
            .where(Review.product_id == product_id)
            .order_by(Review.created_at.desc())
            .limit(limit)
        ).all()
        user_ids = {row.user_id for row in rows}
        names: dict[str, str] = {}
        if user_ids:
            users = db.scalars(select(User).where(User.user_id.in_(user_ids))).all()
            names = {u.user_id: u.display_name for u in users}
        return [
            {
                "review_id": row.review_id,
                "order_id": row.order_id,
                "product_id": row.product_id,
                "batch_id": row.batch_id,
                "rating": row.rating,
                "content": row.content,
                "user_display": mask_name(names.get(row.user_id, "")),
                "created_at": row.created_at,
            }
            for row in rows
        ]


def _is_farm_owner(db: Session, farm_id: str, user_id: str) -> bool:
    farm = db.get(Farm, farm_id)
    return farm is not None and farm.owner_id == user_id


def _pay_payload(order: Order, payment: Payment | None, message: str) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "order_no": order.order_no,
        "status": order.status,
        "status_label": OrderStatus.LABELS.get(order.status, order.status),
        "payment_no": payment.payment_no if payment else None,
        "trade_no": payment.trade_no if payment else None,
        "pay_amount": _money(order.pay_amount),
        "attempt_count": payment.attempt_count if payment else 0,
        "message": message,
    }
