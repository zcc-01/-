"""页面路由：三个业务域的统一 Web 入口（服务端渲染，Jinja2 + 统一骨架）。

设计取舍：
- 页面与 API 完全分离：`/api/**` 走 JSON 契约，`/shop`、`/orders` 这类走 HTML。
  同一套 service 层，两条呈现通道，避免页面里再写一遍业务规则。
- 页面鉴权用 HttpOnly Cookie 承载同一个 JWT，不引入第二套会话机制。
- 表单提交后一律 303 重定向（POST-Redirect-GET），避免刷新重复下单。
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.deps import get_session
from app.core.errors import BizError, Unauthenticated
from app.core.security import decode_access_token, verify_password
from app.ops.schemas import CertReview
from app.ops.service import AlertService, CertService, DashboardService
from app.trade.models import OrderStatus
from app.trade.schemas import CartAdd, OrderCreate, ReviewCreate
from app.trade.service import CartService, OrderService, ProductService, ReviewService
from app.web.context import ns, render

router = APIRouter(tags=["页面"], include_in_schema=False)

COOKIE_NAME = "access_token"


# ------------------------------------------------------------------ 页面鉴权
def _user_from_cookie(request: Request, db: Session) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except Unauthenticated:
        return None
    user = db.get(User, payload.get("sub", ""))
    return user if user is not None and user.status == "active" else None


def _require_user(request: Request, db: Session) -> User | RedirectResponse:
    """未登录则跳登录页，并把原地址带进 next 参数。"""
    user = _user_from_cookie(request, db)
    if user is None:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)
    request.state.web_user = user
    return user


def _go(path: str, flash: str = "", error: str = "") -> RedirectResponse:
    """303 重定向并携带一次性提示，避免刷新重复提交。"""
    params = {}
    if flash:
        params["flash"] = flash
    if error:
        params["error"] = error
    url = f"{path}?{urlencode(params)}" if params else path
    return RedirectResponse(url, status_code=303)


def _flash(request: Request) -> dict[str, str]:
    return {
        "flash": request.query_params.get("flash", ""),
        "error": request.query_params.get("error", ""),
    }


# ------------------------------------------------------------------ 登录 UC-11
@router.get("/login")
def login_page(request: Request, next: str = "/shop", db: Session = Depends(get_session)):
    if _user_from_cookie(request, db) is not None:
        return RedirectResponse(next or "/shop", status_code=303)
    return render(
        request,
        "login.html",
        {"next_url": next, "username": "", "error": request.query_params.get("error", "")},
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/shop"),
    db: Session = Depends(get_session),
):
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        return render(
            request,
            "login.html",
            {"next_url": next, "username": username, "error": "账号或口令错误"},
            status_code=401,
        )
    if user.status != "active":
        return render(
            request,
            "login.html",
            {"next_url": next, "username": username, "error": "账号已停用"},
            status_code=403,
        )
    # 为两个入口复用同一个令牌：页面写 Cookie，API 走 Bearer
    from app.core.security import create_access_token

    token = create_access_token(user_id=user.user_id, username=user.username, role=user.role)
    resp = RedirectResponse(next or "/shop", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 2,
        path="/",
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/shop", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ------------------------------------------------------------------ 商品 UC-06/07
@router.get("/shop")
def shop_list(
    request: Request,
    keyword: str = "",
    category: str = "",
    price_min: str = "",
    price_max: str = "",
    sort: str = "new",
    certified_only: str = "",
    page: int = 1,
    db: Session = Depends(get_session),
):
    user = _user_from_cookie(request, db)
    request.state.web_user = user

    def _yuan(v: str) -> float | None:
        try:
            return round(float(v), 2) if v.strip() else None
        except ValueError:
            return None

    data = ProductService.list_products(
        db,
        keyword=keyword,
        category=category,
        price_min=_yuan(price_min),
        price_max=_yuan(price_max),
        certified_only=bool(certified_only),
        sort=sort,
        page_no=page,
        size=12,
    )
    size = data["size"]
    pages = max(1, (data["total"] + size - 1) // size)
    base = {
        "keyword": keyword,
        "category": category,
        "price_min": price_min,
        "price_max": price_max,
        "sort": sort,
        "certified_only": certified_only,
    }
    ctx = {
        "items": data["items"],
        "total": data["total"],
        "page": data["page"],
        "pages": pages,
        "categories": ProductService.categories(db),
        "query": base,
        "prev_query": urlencode({**base, "page": max(1, data["page"] - 1)}),
        "next_query": urlencode({**base, "page": min(pages, data["page"] + 1)}),
        **_flash(request),
    }
    return render(request, "shop_list.html", ctx)


@router.get("/shop/{product_id}")
def shop_detail(
    request: Request, product_id: str, db: Session = Depends(get_session)
):
    request.state.web_user = _user_from_cookie(request, db)
    try:
        product = ProductService.detail(db, product_id)
    except BizError as exc:
        return render(
            request, "error.html", {"message": exc.message, "code": exc.code}, status_code=404
        )
    # 详情页额外带一个链路状态，供「产地可信」区块展示
    product["chain_ok"] = True
    try:
        from app.trace.service import TraceService

        if product.get("trace_code"):
            product["chain_ok"] = TraceService.verify_batch(db, product["batch_id"]).ok
    except BizError:
        product["chain_ok"] = False
    return render(request, "shop_detail.html", {"p": product, **_flash(request)})


# ------------------------------------------------------------------ 购物车 UC-07
@router.get("/cart")
def cart_page(request: Request, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    cart = CartService.list_cart(db, user)
    return render(
        request,
        "cart.html",
        {"cart": ns(cart), "default_name": user.display_name or "", "default_phone": "", **_flash(request)},
    )


@router.post("/shop/{product_id}/cart")
def cart_add(
    request: Request,
    product_id: str,
    quantity: int = Form(1),
    buy: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        CartService.add(db, user, CartAdd(product_id=product_id, quantity=max(1, quantity)))
    except BizError as exc:
        return _go(f"/shop/{product_id}", error=exc.message)
    # 「立即购买」与「加入购物车」共用同一条入库逻辑，只是落地页不同：
    # 直接调 OrderService 会缺收货信息，所以统一进购物车再结算，避免两套下单路径。
    if buy:
        return _go("/cart", flash="已加入购物车，请确认收货信息后提交订单")
    return _go("/cart", flash="已加入购物车")


@router.post("/cart/{cart_id}/update")
def cart_update(
    request: Request, cart_id: str, quantity: int = Form(...), db: Session = Depends(get_session)
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        CartService.update(db, user, cart_id, max(1, quantity))
    except BizError as exc:
        return _go("/cart", error=exc.message)
    return _go("/cart", flash="数量已更新")


@router.post("/cart/{cart_id}/delete")
def cart_delete(request: Request, cart_id: str, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        CartService.remove(db, user, cart_id)
    except BizError as exc:
        return _go("/cart", error=exc.message)
    return _go("/cart", flash="已移除")


# ------------------------------------------------------------------ 订单 UC-08/09/10
@router.post("/cart/checkout")
async def checkout(
    request: Request,
    receiver_name: str = Form(...),
    receiver_phone: str = Form(...),
    address: str = Form(...),
    remark: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    cart_ids = [v for v in form.getlist("cart_ids") if v]
    try:
        order = OrderService.create(
            db,
            user,
            OrderCreate(
                cart_ids=cart_ids,
                receiver_name=receiver_name,
                receiver_phone=receiver_phone,
                address=address,
                remark=remark,
            ),
        )
    except BizError as exc:
        return _go("/cart", error=exc.message)
    return _go("/orders", flash=f"下单成功：{order['order_no']}，请在 30 分钟内完成支付")


@router.get("/orders")
def order_list(
    request: Request, status: str = "", db: Session = Depends(get_session)
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = OrderService.list_orders(db, user, status=status or None)
    return render(
        request,
        "orders.html",
        {
            "orders": ns(data["items"]),
            "total": data["total"],
            "status_labels": OrderStatus.LABELS,
            "can_ship": user.role in (Role.FARMER, Role.OPERATOR),
            **_flash(request),
        },
    )


@router.post("/orders/{order_id}/pay")
def order_pay(request: Request, order_id: str, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        result = OrderService.pay(db, user, order_id, channel="mock")
    except BizError as exc:
        return _go("/orders", error=exc.message)
    if not result.get("ok", True):
        return _go("/orders", error=result.get("message", "支付失败"))
    # 已支付过的订单再点一次，返回的是幂等结果，提示要说清而不是谎报"刚支付成功"
    if not result.get("retryable", True):
        return _go("/orders", flash=result.get("message", "该订单已支付"))
    return _go("/orders", flash="支付成功，等待发货")


@router.post("/orders/{order_id}/cancel")
def order_cancel(
    request: Request,
    order_id: str,
    reason: str = Form("用户主动取消"),
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        OrderService.cancel(db, user, order_id, reason=reason or "用户主动取消")
    except BizError as exc:
        return _go("/orders", error=exc.message)
    return _go("/orders", flash="订单已取消，库存已释放")


@router.post("/orders/{order_id}/ship")
def order_ship(request: Request, order_id: str, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        OrderService.ship(db, user, order_id)
    except BizError as exc:
        return _go("/orders", error=exc.message)
    return _go("/orders", flash="已发货")


@router.post("/orders/{order_id}/receive")
def order_receive(request: Request, order_id: str, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        OrderService.receive(db, user, order_id)
    except BizError as exc:
        return _go("/orders", error=exc.message)
    return _go("/orders", flash="已确认收货，可以评价了")


@router.post("/orders/{order_id}/review")
def order_review(
    request: Request,
    order_id: str,
    product_id: str = Form(...),
    rating: int = Form(5),
    content: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        ReviewService.create(
            db, user, order_id, ReviewCreate(product_id=product_id, rating=rating, content=content)
        )
    except BizError as exc:
        return _go("/orders", error=exc.message)
    return _go("/orders", flash="评价已提交")


# ------------------------------------------------------------------ 运营控制台 UC-12~14
@router.get("/console")
def console_page(request: Request, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.role == Role.CONSUMER:
        return _go("/shop", error="消费者无权访问运营控制台")

    summary = DashboardService.summary(db, user)
    is_operator = user.role in (Role.OPERATOR, Role.REGULATOR)
    pending = CertService.list_apps(db, user, status="pending", size=10)["items"] if is_operator else []
    alerts = AlertService.list(db, size=10)["items"]
    from app.trade.service import OrderService, ProductService

    own_farm = None
    if user.role == Role.FARMER:
        own_farm = db.scalar(select(Farm.farm_id).where(Farm.owner_id == user.user_id))
    products = ProductService.list_products(
        db, include_off_shelf=True, farm_id=own_farm, size=10
    )["items"]
    orders = OrderService.list_orders(db, user, size=10)["items"]
    if user.role == Role.FARMER:
        own = set(db.scalars(select(Farm.farm_id).where(Farm.owner_id == user.user_id)).all())
        alerts = [a for a in alerts if a.get("farm_id") in own]
    return render(
        request,
        "console.html",
        {
            "summary": summary,
            "pending_certs": pending,
            "alerts": alerts,
            "is_operator": is_operator,
            # 转命名空间：订单字典里有 items 键，直接用会让 Jinja 命中 dict.items 方法
            "products": ns(products),
            "orders": ns(orders),
            "can_close_alert": user.role == Role.OPERATOR,
            **_flash(request),
        },
    )


@router.post("/console/certs/{app_id}/review")
def console_cert_review(
    request: Request,
    app_id: str,
    decision: str = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        CertService.review(db, user, app_id, CertReview(decision=decision, comment=comment))
    except BizError as exc:
        return _go("/console", error=exc.message)
    return _go("/console", flash="审核结论已提交（按 BR-08 不可修改）")


@router.post("/console/alerts/{alert_id}/handle")
def console_alert_handle(
    request: Request,
    alert_id: str,
    status: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        AlertService.handle(db, user, alert_id, status, note)
    except BizError as exc:
        return _go("/console", error=exc.message)
    return _go("/console", flash="预警单已更新")
