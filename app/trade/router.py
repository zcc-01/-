"""交易域接口层（Boundary，UC-06～UC-10）。

与溯源域的约定一致：路由只做参数校验、鉴权与响应包装，
业务判断（库存、状态机、金额）全部下沉到 Service，保证同一规则只有一处实现。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.config import settings
from app.core.deps import get_current_user, get_optional_user, get_session, require_roles
from app.core.response import ok, page
from app.trade.models import Order, OrderStatus, Product, ProductStatus, Review, TradeRule
from app.trade.schemas import (
    CartQuoteIn,
    OrderCreate,
    PayIn,
    ProductCreate,
    ProductStatusUpdate,
    ProductUpdate,
    ReviewIn,
    ShipIn,
)
from app.trade.service import OrderService, ProductService, ReviewService, _money, mask_name

router = APIRouter(prefix="/api", tags=["交易域"])


def _product_dict(
    product: Product,
    *,
    farm: Farm | None = None,
    rating: tuple[float, int] | None = None,
    with_description: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "product_id": product.product_id,
        "product_no": product.product_no,
        "farm_id": product.farm_id,
        "farm_name": farm.farm_name if farm else "",
        "province": farm.province if farm else "",
        "city": farm.city if farm else "",
        "batch_id": product.batch_id,
        "title": product.title,
        "crop_name": product.crop_name,
        "spec": product.spec,
        "unit": product.unit,
        "price": _money(product.price),
        "stock": product.stock,
        "sales": product.sales,
        "status": product.status,
        "status_label": ProductStatus.LABELS.get(product.status, product.status),
        "cert_badge": product.cert_badge,
        "rating_avg": round(rating[0], 1) if rating else 0.0,
        "review_count": rating[1] if rating else 0,
        "can_buy": product.status == ProductStatus.ON_SHELF and product.stock > 0,
    }
    if with_description:
        data["description"] = product.description
    return data


def _order_dict(order: Order, *, with_items: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "order_id": order.order_id,
        "order_no": order.order_no,
        "buyer_id": order.buyer_id,
        "farm_id": order.farm_id,
        "total_amount": _money(order.total_amount),
        "freight": _money(order.freight),
        "pay_amount": _money(order.pay_amount),
        "status": order.status,
        "status_label": OrderStatus.LABELS.get(order.status, order.status),
        "address_snapshot": order.address_snapshot,
        "remark": order.remark,
        "express_no": order.express_no,
        "paid_at": order.paid_at,
        "shipped_at": order.shipped_at,
        "received_at": order.received_at,
        "cancelled_at": order.cancelled_at,
        "cancel_reason": order.cancel_reason,
        "created_at": order.created_at,
        "can_pay": order.status == OrderStatus.PENDING,
        "can_cancel": order.status == OrderStatus.PENDING,
        "can_ship": order.status == OrderStatus.PAID,
        "can_receive": order.status == OrderStatus.SHIPPED,
        "can_review": order.status == OrderStatus.RECEIVED,
    }
    if with_items:
        data["items"] = [
            {
                "item_id": item.item_id,
                "product_id": item.product_id,
                "batch_id": item.batch_id,
                "product_title": item.product_title,
                "spec": item.spec,
                "unit": item.unit,
                "unit_price": _money(item.unit_price),
                "quantity": item.quantity,
                "subtotal": _money(item.subtotal),
            }
            for item in order.items
        ]
    return data


def _review_dict(review: Review, user_display: str = "") -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "order_id": review.order_id,
        "product_id": review.product_id,
        "batch_id": review.batch_id,
        "rating": review.rating,
        "content": review.content,
        "user_display": user_display or mask_name(""),
        "created_at": review.created_at,
    }


# --------------------------------------------------------------------- UC-06
@router.post("/products", summary="商品上架（UC-06）")
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    product = ProductService.create_product(db, payload, user)
    farm = db.get(Farm, product.farm_id)
    return ok(_product_dict(product, farm=farm, with_description=True), message="商品已上架")


@router.get("/products", summary="检索与筛选商品（UC-07，匿名可访问）")
def list_products(
    keyword: str | None = Query(default=None, max_length=64),
    farm_id: str | None = Query(default=None, max_length=32),
    province: str | None = Query(default=None, max_length=32),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    cert_only: bool = Query(default=False),
    in_stock_only: bool = Query(default=False),
    sort: str = Query(default="newest", pattern="^(newest|price_asc|price_desc|sales_desc)$"),
    mine: bool = Query(default=False),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    if max_price is not None and min_price is not None and max_price < min_price:
        min_price, max_price = max_price, min_price
    rows, total = ProductService.list_products(
        db,
        keyword=keyword,
        farm_id=farm_id,
        province=province,
        min_price=min_price,
        max_price=max_price,
        cert_only=cert_only,
        in_stock_only=in_stock_only,
        sort=sort,
        user=user,
        mine=mine,
        page_no=page_no,
        size=size,
    )
    farms = ProductService.farm_map(db, {row.farm_id for row in rows})
    ratings = ProductService.rating_map(db, {row.product_id for row in rows})
    items = [
        _product_dict(row, farm=farms.get(row.farm_id), rating=ratings.get(row.product_id))
        for row in rows
    ]
    return page(items, total, page_no, size)


@router.get("/products/{product_id}", summary="商品详情（含产地与溯源批次）")
def get_product(
    product_id: str,
    db: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    detail = ProductService.detail(db, product_id, user)
    data = _product_dict(
        detail["product"],
        farm=detail["farm"],
        rating=(detail["rating_avg"], detail["review_count"]),
        with_description=True,
    )
    data["batch"] = detail["batch"]
    data["can_buy"] = detail["can_buy"]
    return ok(data)


@router.patch("/products/{product_id}", summary="修改商品价格与库存（UC-06）")
def update_product(
    product_id: str,
    payload: ProductUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    product = ProductService.update_product(db, product_id, payload, user)
    farm = db.get(Farm, product.farm_id)
    return ok(_product_dict(product, farm=farm, with_description=True), message="商品已更新")


@router.patch("/products/{product_id}/status", summary="商品上架/下架（UC-06）")
def change_product_status(
    product_id: str,
    payload: ProductStatusUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    product = ProductService.change_status(db, product_id, payload.status, user)
    farm = db.get(Farm, product.farm_id)
    label = ProductStatus.LABELS.get(product.status, product.status)
    return ok(_product_dict(product, farm=farm), message=f"商品已{label}")


@router.get("/products/{product_id}/reviews", summary="商品评价列表（UC-10）")
def list_reviews(
    product_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_session),
):
    rows = ReviewService.list_by_product(db, product_id, limit=limit)
    return ok({"items": rows, "total": len(rows)})


# --------------------------------------------------------------------- UC-08
@router.post("/cart/quote", summary="购物车试算（UC-07/UC-08，匿名可用）")
def quote_cart(
    payload: CartQuoteIn,
    db: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    quotes = OrderService.quote(db, payload.items, user)
    total = sum(item["pay_amount"] for item in quotes)
    return ok({"groups": quotes, "pay_amount": round(total, 2)})


@router.post("/orders", summary="下单（UC-08）")
def create_orders(
    payload: OrderCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.CONSUMER)),
):
    orders = OrderService.create_orders(db, payload, user)
    items = [_order_dict(order) for order in orders]
    return ok(
        {"orders": items, "total": len(items)},
        message=f"下单成功，共生成 {len(items)} 笔订单，请在 {TradeRule.PAY_TIMEOUT_MINUTES} 分钟内完成支付",
    )


@router.get("/orders", summary="订单列表（买家/农户）")
def list_orders(
    scope: str = Query(default="mine", pattern="^(mine|sales)$"),
    status: str | None = Query(default=None, pattern="^(pending|paid|shipped|received|cancelled)$"),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    rows, total = OrderService.list_orders(db, user, scope=scope, status=status, page_no=page_no, size=size)
    items = [_order_dict(order) for order in rows]
    return page(items, total, page_no, size)


@router.get("/orders/{order_id}", summary="订单详情")
def get_order(
    order_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    order = OrderService.get_order(db, order_id)
    OrderService.assert_order_visible(db, order, user)
    return ok(_order_dict(order))


@router.post("/orders/{order_id}/pay", summary="支付订单（UC-08，模拟网关）")
def pay_order(
    order_id: str,
    payload: PayIn | None = None,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.CONSUMER)),
):
    channel = payload.channel if payload else "mockpay"
    data = OrderService.pay(db, order_id, user, channel=channel)
    return ok(data, message=data.get("message", "支付成功"))


@router.post("/orders/{order_id}/cancel", summary="取消订单（UC-08 异常流程）")
def cancel_order(
    order_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    reason = str((payload or {}).get("reason", ""))[:128] or None
    order = OrderService.cancel(db, order_id, user, reason=reason)
    return ok(_order_dict(order), message="订单已取消，库存已释放")


# --------------------------------------------------------------------- UC-09
@router.post("/orders/{order_id}/ship", summary="发货（UC-09，农户）")
def ship_order(
    order_id: str,
    payload: ShipIn,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER)),
):
    order = OrderService.ship(db, order_id, payload.express_no, user)
    return ok(_order_dict(order), message="已发货")


@router.post("/orders/{order_id}/receive", summary="确认收货（UC-09，消费者）")
def receive_order(
    order_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.CONSUMER)),
):
    order = OrderService.receive(db, order_id, user)
    return ok(_order_dict(order), message="已确认收货，可以评价了")


# --------------------------------------------------------------------- UC-10
@router.post("/orders/{order_id}/reviews", summary="评价晒单（UC-10）")
def create_review(
    order_id: str,
    payload: ReviewIn,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.CONSUMER)),
):
    review = ReviewService.create(db, order_id, payload, user)
    return ok(_review_dict(review, mask_name(user.display_name)), message="评价已提交")


@router.post("/orders/admin/sweep-expired", summary="超时订单批量取消（定时任务手动触发）")
def sweep_expired(
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    cancelled = OrderService.sweep_expired(db)
    return ok({"cancelled": cancelled}, message=f"已取消 {cancelled} 笔超时订单")
