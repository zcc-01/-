"""交易域接口层（Boundary，UC-06～UC-10）。

路由只做三件事：参数校验、鉴权、把 Service 结果包成统一响应（与溯源域同一约定）。
商品浏览与详情对匿名开放——消费者不注册也应能看商品，这与溯源扫码的匿名策略一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.models import Role, User
from app.core.config import settings
from app.core.deps import get_current_user, get_session, require_roles
from app.core.response import ok, page
from app.trade.schemas import (
    CartAdd,
    CartUpdate,
    OrderCreate,
    PayRequest,
    ProductCreate,
    ProductUpdate,
    ReviewCreate,
)
from app.trade.service import CartService, OrderService, ProductService, ReviewService

router = APIRouter(prefix="/api", tags=["交易域"])


# ============================================================= UC-06 商品管理
@router.post("/products", summary="商品上架（UC-06）")
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR)),
):
    return ok(ProductService.create(db, user, payload), message="商品上架成功")


@router.patch("/products/{product_id}", summary="修改商品（改价/改库存/上下架）")
def update_product(
    product_id: str,
    payload: ProductUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR)),
):
    return ok(ProductService.update(db, user, product_id, payload), message="商品已更新")


@router.get("/products/categories", summary="商品类目列表")
def list_categories(db: Session = Depends(get_session)):
    return ok(ProductService.categories(db))


# ============================================================= UC-07 检索筛选
@router.get("/products", summary="检索与筛选商品（UC-07）")
def list_products(
    keyword: str | None = Query(default=None, description="名称/描述/标签/产地关键词"),
    category: str | None = Query(default=None, description="分类 code（fruit-citrus）或分类名"),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    origin: str | None = Query(default=None, description="产地关键词，省/市/农场名均可"),
    certified_only: bool = Query(default=False, description="只看已认证产地"),
    in_stock_only: bool = Query(default=False, description="只看有货"),
    recommended_only: bool = Query(default=False, description="只看首页推荐位"),
    include_off_shelf: bool = Query(default=False),
    sort: str = Query(default="new", pattern="^(new|price_asc|price_desc|sales|stock)$"),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
):
    data = ProductService.list_products(
        db,
        keyword=keyword,
        category=category,
        price_min=price_min,
        price_max=price_max,
        origin=origin,
        certified_only=certified_only,
        in_stock_only=in_stock_only,
        recommended_only=recommended_only,
        include_off_shelf=include_off_shelf,
        sort=sort,
        page_no=page_no,
        size=size,
    )
    return page(data["items"], data["total"], data["page"], data["size"])


@router.get("/products/{product_id}", summary="商品详情（含溯源信息与评价）")
def get_product(product_id: str, db: Session = Depends(get_session)):
    return ok(ProductService.detail(db, product_id))


@router.get("/products/{product_id}/reviews", summary="商品评价列表（UC-10）")
def list_reviews(
    product_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_session),
):
    return ok(ReviewService.list_by_product(db, product_id, limit=limit))


# ================================================================= UC-08 购物车
@router.get("/cart", summary="查看购物车")
def get_cart(
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(CartService.list_cart(db, user))


@router.post("/cart", summary="加入购物车")
def add_to_cart(
    payload: CartAdd,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.CONSUMER, Role.FARMER, Role.OPERATOR)),
):
    return ok(CartService.add(db, user, payload), message="已加入购物车")


@router.patch("/cart/{cart_id}", summary="修改购物车数量")
def update_cart(
    cart_id: str,
    payload: CartUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(CartService.update(db, user, cart_id, payload.quantity), message="数量已更新")


@router.delete("/cart/{cart_id}", summary="移除购物车条目")
def delete_cart_item(
    cart_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(CartService.remove(db, user, cart_id), message="已移除")


@router.delete("/cart", summary="清空购物车")
def clear_cart(
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(CartService.clear(db, user), message="购物车已清空")


# ============================================================= UC-08 下单支付
@router.post("/orders", summary="创建订单（UC-08，服务端重算金额并原子扣库存）")
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.CONSUMER, Role.FARMER, Role.OPERATOR)),
):
    return ok(OrderService.create(db, user, payload), message="订单创建成功，请尽快支付")


@router.get("/orders", summary="我的订单列表（UC-09）")
def list_orders(
    status: str | None = Query(default=None),
    page_no: int = Query(default=1, ge=1, alias="page"),
    size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    data = OrderService.list_orders(db, user, status=status, page_no=page_no, size=size)
    return page(data["items"], data["total"], data["page"], data["size"])


@router.get("/orders/{order_id}", summary="订单详情")
def get_order(
    order_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(OrderService.get(db, user, order_id))


@router.post("/orders/{order_id}/pay", summary="支付订单（UC-08，幂等 + 失败可重试 3 次）")
def pay_order(
    order_id: str,
    payload: PayRequest | None = None,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    req = payload or PayRequest()
    return ok(OrderService.pay(db, user, order_id, req.channel, force_fail=req.force_fail))


@router.post("/orders/{order_id}/cancel", summary="取消订单并释放库存（BR-06）")
def cancel_order(
    order_id: str,
    reason: str = Query(default="用户主动取消", max_length=128),
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(OrderService.cancel(db, user, order_id, reason), message="订单已取消，库存已释放")


# ============================================================= UC-09 发货收货
@router.post("/orders/{order_id}/ship", summary="发货（UC-09，农户/运营）")
def ship_order(
    order_id: str,
    tracking_no: str = Query(default="", max_length=64),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.FARMER, Role.OPERATOR)),
):
    return ok(OrderService.ship(db, user, order_id, tracking_no), message="已发货")


@router.post("/orders/{order_id}/receive", summary="确认收货（UC-09）")
def receive_order(
    order_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(OrderService.receive(db, user, order_id), message="已确认收货")


# ============================================================= UC-10 评价晒单
@router.post("/orders/{order_id}/reviews", summary="发表评价（UC-10，仅收货后）")
def create_review(
    order_id: str,
    payload: ReviewCreate,
    db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return ok(ReviewService.create(db, user, order_id, payload), message="评价已提交")
