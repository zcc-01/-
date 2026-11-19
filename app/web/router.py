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
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Category
from app.catalog.schemas import CategoryCreate, CategoryUpdate
from app.catalog.service import CategoryService
from app.common.models import Farm, Role, User
from app.core.deps import get_session
from app.core.errors import BizError, Unauthenticated
from app.core.security import decode_access_token, verify_password
from app.ops.schemas import CertReview
from app.ops.service import AlertService, CertService, DashboardService
from app.trade.models import OrderStatus, Product
from app.trade.schemas import CartAdd, OrderCreate, ProductUpdate, ReviewCreate
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
    origin: str = "",
    price_min: str = "",
    price_max: str = "",
    sort: str = "new",
    certified_only: str = "",
    in_stock: str = "",
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
        origin=origin,
        price_min=_yuan(price_min),
        price_max=_yuan(price_max),
        certified_only=bool(certified_only),
        in_stock_only=bool(in_stock),
        sort=sort,
        page_no=page,
        size=12,
    )
    size = data["size"]
    pages = max(1, (data["total"] + size - 1) // size)

    base = {
        "keyword": keyword,
        "category": category,
        "origin": origin,
        "price_min": price_min,
        "price_max": price_max,
        "sort": sort,
        "certified_only": "1" if certified_only else "",
        "in_stock": "1" if in_stock else "",
    }

    def href(**over: object) -> str:
        """当前筛选条件 + 覆盖项 → 链接，模板里不用再手拼查询串。"""
        params = {**base, **{k: ("" if v is None else v) for k, v in over.items()}}
        params = {k: str(v) for k, v in params.items() if str(v) != ""}
        return "/shop?" + urlencode(params) if params else "/shop"

    catalog = CategoryService.tree(db)
    selected = CategoryService.get_by_code(db, category) if category else None
    current_parent = selected
    if selected is not None and selected.parent_id:
        current_parent = db.get(Category, selected.parent_id)

    tabs = [
        {"code": "", "name": "全部", "icon": "🧺", "count": sum(c["product_count"] for c in catalog),
         "href": href(category=""), "active": not category}
    ]
    for node in catalog:
        tabs.append(
            {
                "code": node["code"],
                "name": node["name"],
                "icon": node["icon"],
                "count": node["product_count"],
                "href": href(category=node["code"]),
                "active": current_parent is not None and current_parent.code == node["code"],
            }
        )

    chips = []
    if current_parent is not None:
        chips.append(
            {
                "code": current_parent.code,
                "name": "全部" + current_parent.name,
                "href": href(category=current_parent.code),
                "active": selected is not None and not selected.parent_id,
            }
        )
        for child in current_parent.children:
            chips.append(
                {
                    "code": child.code,
                    "name": child.name,
                    "href": href(category=child.code),
                    "active": selected is not None and selected.code == child.code,
                }
            )

    sorts = [
        {"key": "new", "label": "最新上架", "href": href(sort="new")},
        {"key": "price_asc", "label": "价格 ↑", "href": href(sort="price_asc")},
        {"key": "price_desc", "label": "价格 ↓", "href": href(sort="price_desc")},
        {"key": "sales", "label": "销量优先", "href": href(sort="sales")},
    ]

    ctx = {
        "items": data["items"],
        "total": data["total"],
        "page": data["page"],
        "pages": pages,
        "tabs": tabs,
        "chips": chips,
        "sorts": sorts,
        "sort": sort,
        "query": base,
        "current_name": current_parent.name if current_parent else "",
        "cert_href": href(certified_only="" if certified_only else "1"),
        "stock_href": href(in_stock="" if in_stock else "1"),
        "reset_href": "/shop",
        "prev_href": href(page=max(1, data["page"] - 1)),
        "next_href": href(page=min(pages, data["page"] + 1)),
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


# ------------------------------------------------------- 品类与商品维护 UC-06/07
def _require_staff(
    request: Request, db: Session, allow: tuple[str, ...] = (Role.FARMER, Role.OPERATOR)
) -> User | RedirectResponse:
    """运营后台的统一入口守卫：消费者与监管人员只能看，不能改。"""
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.role not in allow:
        return _go("/shop", error="该页面仅对农户与平台运营开放")
    return user


def _own_farm_ids(db: Session, user: User) -> list[str]:
    """农户看自己的全部产地（一个人可能经营多个合作社）；运营看全平台。"""
    if user.role != Role.FARMER:
        return []
    return list(db.scalars(select(Farm.farm_id).where(Farm.owner_id == user.user_id)).all()) or ["__none__"]


@router.get("/console/catalog")
def console_catalog(
    request: Request,
    keyword: str = "",
    category: str = "",
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db)
    if isinstance(user, RedirectResponse):
        return user

    farm_ids = _own_farm_ids(db, user)
    stats = ProductService.stats(db, farm_ids=farm_ids or None)
    data = ProductService.list_products(
        db,
        keyword=keyword,
        category=category,
        include_off_shelf=True,
        farm_ids=farm_ids or None,
        sort="new",
        size=50,
    )
    return render(
        request,
        "console_catalog.html",
        {
            "stats": stats,
            "products": data["items"],
            "total": data["total"],
            "keyword": keyword,
            "category": category,
            "categories": CategoryService.flat(db),
            "tree": CategoryService.tree(db, include_disabled=True, on_sale_only=False),
            "is_operator": user.role == Role.OPERATOR,
            **_flash(request),
        },
    )


@router.post("/console/categories")
def console_category_create(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    parent_id: str = Form(""),
    icon: str = Form(""),
    image: str = Form(""),
    sort_no: int = Form(0),
    description: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db, allow=(Role.OPERATOR,))
    if isinstance(user, RedirectResponse):
        return user
    try:
        CategoryService.create(
            db,
            CategoryCreate(
                code=code,
                name=name,
                parent_id=parent_id or None,
                icon=icon,
                image=image,
                sort_no=sort_no,
                description=description,
            ),
        )
    except (BizError, ValidationError) as exc:
        message = getattr(exc, "message", "分类编码需为 2~32 位小写字母/数字/连字符")
        return _go("/console/catalog", error=message)
    return _go("/console/catalog", flash=f"分类「{name}」已创建")


@router.post("/console/categories/{category_id}/update")
def console_category_update(
    request: Request,
    category_id: str,
    code: str = Form(...),
    name: str = Form(...),
    parent_id: str = Form(""),
    icon: str = Form(""),
    image: str = Form(""),
    sort_no: int = Form(0),
    description: str = Form(""),
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db, allow=(Role.OPERATOR,))
    if isinstance(user, RedirectResponse):
        return user
    try:
        CategoryService.update(
            db,
            category_id,
            CategoryUpdate(
                code=code,
                name=name,
                parent_id=parent_id or None,
                icon=icon,
                image=image,
                sort_no=sort_no,
                description=description,
            ),
        )
    except (BizError, ValidationError) as exc:
        return _go("/console/catalog", error=getattr(exc, "message", "分类保存失败，请检查编码与上级分类"))
    return _go("/console/catalog", flash=f"分类「{name}」已更新")


@router.post("/console/categories/{category_id}/toggle")
def console_category_toggle(
    request: Request,
    category_id: str,
    enabled: int = Form(...),
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db, allow=(Role.OPERATOR,))
    if isinstance(user, RedirectResponse):
        return user
    try:
        CategoryService.toggle(db, category_id, bool(enabled))
    except BizError as exc:
        return _go("/console/catalog", error=exc.message)
    return _go("/console/catalog", flash="分类状态已更新（停用父类会级联停用子类）")


@router.post("/console/categories/{category_id}/delete")
def console_category_delete(
    request: Request,
    category_id: str,
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db, allow=(Role.OPERATOR,))
    if isinstance(user, RedirectResponse):
        return user
    try:
        CategoryService.delete(db, category_id)
    except BizError as exc:
        return _go("/console/catalog", error=exc.message)
    return _go("/console/catalog", flash="分类已删除")


@router.get("/console/products/{product_id}/edit")
def console_product_edit(
    request: Request,
    product_id: str,
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db)
    if isinstance(user, RedirectResponse):
        return user
    product = db.get(Product, product_id)
    if product is None:
        return _go("/console/catalog", error="商品不存在")
    farm = db.get(Farm, product.farm_id)
    if user.role == Role.FARMER and (farm is None or farm.owner_id != user.user_id):
        return _go("/console/catalog", error="只能维护本农场的商品")
    return render(
        request,
        "console_product_edit.html",
        {
            "p": ProductService.detail(db, product_id),
            "categories": CategoryService.flat(db),
            **_flash(request),
        },
    )


@router.post("/console/products/{product_id}/update")
async def console_product_update(
    request: Request,
    product_id: str,
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()

    def _num(key: str) -> float | None:
        raw = str(form.get(key, "")).strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    payload = ProductUpdate(
        name=str(form.get("name", "")).strip() or None,
        category_id=str(form.get("category_id", "")).strip() or None,
        price_yuan=_num("price_yuan"),
        stock=int(_num("stock")) if _num("stock") is not None else None,
        unit=str(form.get("unit", "")).strip() or None,
        spec=str(form.get("spec", "")).strip(),
        grade=str(form.get("grade", "")).strip(),
        origin=str(form.get("origin", "")).strip(),
        shelf_life=str(form.get("shelf_life", "")).strip(),
        storage=str(form.get("storage", "")).strip(),
        image_url=str(form.get("image_url", "")).strip(),
        tags=str(form.get("tags", "")).strip(),
        description=str(form.get("description", "")).strip(),
        status=str(form.get("status", "")).strip() or None,
        is_recommended=form.get("is_recommended") is not None,
    )
    try:
        ProductService.update(db, user, product_id, payload)
    except (BizError, ValidationError) as exc:
        message = getattr(exc, "message", "保存失败，请检查填写内容")
        return _go(f"/console/products/{product_id}/edit", error=message)
    return _go(f"/console/products/{product_id}/edit", flash="商品信息已保存")


@router.post("/console/products/{product_id}/status")
def console_product_status(
    request: Request,
    product_id: str,
    status: str = Form(...),
    db: Session = Depends(get_session),
):
    user = _require_staff(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        ProductService.set_status(db, user, product_id, status)
    except BizError as exc:
        return _go("/console/catalog", error=exc.message)
    return _go("/console/catalog", flash="商品已" + ("上架" if status == "on_sale" else "下架"))


@router.post("/console/products/{product_id}/quick")
def console_product_quick_edit(
    request: Request,
    product_id: str,
    price_yuan: str = Form(""),
    stock: str = Form(""),
    db: Session = Depends(get_session),
):
    """列表页行内改价/改库存：运营最常用的两个动作，不必进编辑页。"""
    user = _require_staff(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        payload = ProductUpdate(
            price_yuan=float(price_yuan) if price_yuan.strip() else None,
            stock=int(stock) if stock.strip() else None,
        )
        ProductService.update(db, user, product_id, payload)
    except (BizError, ValidationError, ValueError) as exc:
        return _go("/console/catalog", error=getattr(exc, "message", "价格或库存填写有误"))
    return _go("/console/catalog", flash="价格/库存已更新")
