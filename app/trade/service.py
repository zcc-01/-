"""交易域业务逻辑（控制类，UC-06～UC-10）。

事务边界全部在 Service 层：路由只做鉴权与参数校验，网关只负责收钱。
所有涉及库存的写操作一律用「条件更新 + 检查 rowcount」，不做「先查再改」——
后者在并发下必然超卖，这是报告 5.2.3 记录的人工修改点。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.models import Category
from app.catalog.service import CategoryService
from app.common.models import Farm, Role, User
from app.core.config import settings
from app.core.errors import Conflict, Forbidden, NotFound, RuleViolation
from app.core.ids import new_biz_no
from app.trace.models import Batch, BatchStatus, TraceCode
from app.trade.models import (
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PayStatus,
    Product,
    ProductStatus,
    Review,
)
from app.trade.payment_gateway import ChargeResult, get_gateway
from app.trade.schemas import (
    CartAdd,
    OrderCreate,
    ProductCreate,
    ProductUpdate,
    ReviewCreate,
)

ORDER_EXPIRE_MINUTES = 30
MAX_PAY_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _to_cents(yuan: float) -> int:
    """元转分：四舍五入到整数分，避免 19.99 变成 1998。"""
    return int(round(yuan * 100))


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone


def _clean_image(url: str | None) -> str:
    """只接受本地静态路径或 http(s) 外链。

    直接落库任意字符串会有两个坑：一是 `javascript:` 之类会被原样塞进 <img src>，
    二是运营误填相对路径会 404。这里做一次白名单收敛，非法值降级为空，
    前台自动回落到分类色块（不会出现裂图）。
    """
    value = (url or "").strip()
    if value.startswith("/static/") or value.startswith("http://") or value.startswith("https://"):
        return value[:200]
    return ""


def _normalize_tags(raw: str | None) -> str:
    """标签统一成「逗号分隔、去空、去重、最多 6 个」，避免后台随便填导致展示错位。"""
    if not raw:
        return ""
    seen: list[str] = []
    for part in raw.replace("，", ",").replace("、", ",").split(","):
        tag = part.strip()
        if tag and tag not in seen:
            seen.append(tag[:12])
        if len(seen) >= 6:
            break
    return ",".join(seen)[:128]


def _category_index(db: Session) -> dict[str, dict[str, str]]:
    """一次查出全部分类，避免列表页逐条商品查分类（N+1）。"""
    from app.catalog.models import Category

    rows = db.execute(select(Category.category_id, Category.code, Category.name, Category.parent_id)).all()
    names = {r.category_id: r.name for r in rows}
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        parent_name = names.get(row.parent_id or "", "")
        index[row.category_id] = {
            "code": row.code,
            "name": row.name,
            "path": f"{parent_name} / {row.name}" if parent_name else row.name,
        }
    return index


def _product_out(product: Product, categories: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    category = (categories or {}).get(product.category_id or "")
    return {
        "product_id": product.product_id,
        "batch_id": product.batch_id,
        "farm_id": product.farm_id,
        "name": product.name,
        "category": category["name"] if category else product.category,
        "category_id": product.category_id,
        "category_code": category["code"] if category else "",
        "category_path": category["path"] if category else product.category,
        "price_cents": product.price_cents,
        "price": product.price_yuan,
        "stock": product.stock,
        "unit": product.unit,
        "status": product.status,
        "status_label": ProductStatus.LABELS.get(product.status, product.status),
        "certified": product.certified,
        "description": product.description,
        "sales_count": product.sales_count,
        "spec": product.spec,
        "grade": product.grade,
        "origin": product.origin,
        "shelf_life": product.shelf_life,
        "storage": product.storage,
        "image_url": product.image_url,
        "tags": product.tag_list,
        "is_recommended": bool(product.is_recommended),
        "created_at": product.created_at,
    }


def _order_out(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "order_no": order.order_no,
        "user_id": order.user_id,
        "farmer_id": order.farmer_id,
        "total_cents": order.total_cents,
        "total": round(order.total_cents / 100, 2),
        "status": order.status,
        "status_label": OrderStatus.LABELS.get(order.status, order.status),
        "receiver_name": order.receiver_name,
        "receiver_phone": order.receiver_phone,
        "receiver_phone_masked": _mask_phone(order.receiver_phone),
        "address": order.address,
        "remark": order.remark,
        "expire_at": order.expire_at,
        "paid_at": order.paid_at,
        "shipped_at": order.shipped_at,
        "received_at": order.received_at,
        "cancelled_at": order.cancelled_at,
        "cancel_reason": order.cancel_reason,
        "pay_attempts": order.pay_attempts,
        "last_fail_reason": order.last_fail_reason,
        "created_at": order.created_at,
        "items": [
            {
                "item_id": it.item_id,
                "product_id": it.product_id,
                "product_name": it.product_name,
                "price_cents": it.price_cents,
                "price": round(it.price_cents / 100, 2),
                "quantity": it.quantity,
                "subtotal_cents": it.subtotal_cents,
                "subtotal": round(it.subtotal_cents / 100, 2),
            }
            for it in order.items
        ],
    }


def _payment_out(pay: Payment) -> dict[str, Any]:
    return {
        "payment_id": pay.payment_id,
        "order_id": pay.order_id,
        "amount_cents": pay.amount_cents,
        "amount": round(pay.amount_cents / 100, 2),
        "channel": pay.channel,
        "status": pay.status,
        "transaction_no": pay.transaction_no,
        "fail_reason": pay.fail_reason,
        "paid_at": pay.paid_at,
    }


# ================================================================== 商品 UC-06
class ProductService:
    @staticmethod
    def _assert_sellable_batch(db: Session, batch_id: str) -> Batch:
        """上架前置校验：批次必须存在且已进入可销售阶段。

        这是交易域对溯源域唯一的方向性依赖——商品不能凭空存在，
        必须挂在一条已采收的溯源链上，否则「产地直供」就失去了数据依据。
        """
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise NotFound("关联的溯源批次不存在")
        if batch.status in (BatchStatus.DRAFT, BatchStatus.GROWING):
            raise RuleViolation("该批次尚未采收，不能上架销售")
        if batch.status == BatchStatus.CLOSED:
            raise RuleViolation("该批次已结批，不能上架销售")
        return batch

    @staticmethod
    def _resolve_category(db: Session, category_id: str | None, fallback: str) -> tuple[str | None, str]:
        """把前端传来的分类主键换成「主键 + 分类名」一起落库。

        商品表同时存 category_id 与 category 名，是有意为之的冗余：
        列表页、订单快照、导出报表都直接读名字，不必每次都 join 分类表；
        代价是改名时要同步，所以只在 ProductService 这一处写，不散落在路由里。
        """
        category = CategoryService.ensure_visible(db, category_id)
        if category is None:
            return None, fallback.strip() or "其他"
        return category.category_id, category.name

    @staticmethod
    def create(db: Session, user: User, payload: ProductCreate) -> dict[str, Any]:
        batch = ProductService._assert_sellable_batch(db, payload.batch_id)
        farm = db.get(Farm, batch.farm_id)

        # 农户只能上架自家农场的批次；运营可代为上架
        if user.role == Role.FARMER and (farm is None or farm.owner_id != user.user_id):
            raise Forbidden("只能上架本农场的批次商品")

        category_id, category_name = ProductService._resolve_category(
            db, payload.category_id, payload.category
        )
        product = Product(
            batch_id=batch.batch_id,
            farm_id=batch.farm_id,
            name=payload.name.strip(),
            category=category_name,
            category_id=category_id,
            price_cents=_to_cents(payload.price_yuan),
            stock=payload.stock,
            unit=payload.unit,
            description=payload.description,
            status=ProductStatus.ON_SALE,
            # 批次所属农场已认证则在架即带认证标识（UC-12 步骤 7 的联动结果）
            certified=1 if farm is not None and farm.cert_status == "approved" else 0,
            spec=payload.spec.strip(),
            grade=payload.grade.strip(),
            origin=payload.origin.strip(),
            shelf_life=payload.shelf_life.strip(),
            storage=payload.storage.strip(),
            image_url=_clean_image(payload.image_url),
            tags=_normalize_tags(payload.tags),
            is_recommended=1 if payload.is_recommended else 0,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return _product_out(product, _category_index(db))

    @staticmethod
    def update(db: Session, user: User, product_id: str, payload: ProductUpdate) -> dict[str, Any]:
        product = db.get(Product, product_id)
        if product is None:
            raise NotFound("商品不存在")
        if user.role == Role.FARMER:
            farm = db.get(Farm, product.farm_id)
            if farm is None or farm.owner_id != user.user_id:
                raise Forbidden("只能修改本农场的商品")
        elif user.role not in (Role.OPERATOR,):
            raise Forbidden("该操作需要农户或平台运营角色")

        data = payload.model_dump(exclude_unset=True)

        # 分类换绑要同时改名字，否则列表页会显示旧分类
        if "category_id" in data:
            category_id, category_name = ProductService._resolve_category(
                db, data.pop("category_id"), data.pop("category", "") or product.category
            )
            product.category_id = category_id
            product.category = category_name
        elif data.get("category") is not None:
            product.category = str(data.pop("category")).strip() or product.category

        # 改价与改库存是高风险操作，单独留痕（报告表 3-6 安全要求）
        if data.get("price_yuan") is not None:
            product.price_cents = _to_cents(data.pop("price_yuan"))
        else:
            data.pop("price_yuan", None)
        if data.get("stock") is not None:
            product.stock = int(data.pop("stock"))
        else:
            data.pop("stock", None)

        if "is_recommended" in data and data["is_recommended"] is not None:
            product.is_recommended = 1 if data.pop("is_recommended") else 0
        if "image_url" in data:
            product.image_url = _clean_image(data.pop("image_url"))
        if "tags" in data:
            product.tags = _normalize_tags(data.pop("tags"))

        text_fields = (
            "name",
            "unit",
            "description",
            "status",
            "spec",
            "grade",
            "origin",
            "shelf_life",
            "storage",
        )
        for key in text_fields:
            if key in data and data[key] is not None:
                value = data.pop(key)
                setattr(product, key, value.strip() if isinstance(value, str) else value)

        db.commit()
        db.refresh(product)
        return _product_out(product, _category_index(db))

    @staticmethod
    def set_status(db: Session, user: User, product_id: str, status: str) -> dict[str, Any]:
        """上下架：单独开一个动作，是因为它在后台是高频操作，不该走整表单提交。"""
        if status not in ProductStatus.ALL:
            raise RuleViolation("商品状态只能是 on_sale 或 off_shelf")
        return ProductService.update(db, user, product_id, ProductUpdate(status=status))

    @staticmethod
    def list_products(
        db: Session,
        *,
        keyword: str | None = None,
        category: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        origin: str | None = None,
        certified_only: bool = False,
        in_stock_only: bool = False,
        recommended_only: bool = False,
        include_off_shelf: bool = False,
        sort: str = "new",
        page_no: int = 1,
        size: int = 20,
        farm_id: str | None = None,
        farm_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """UC-07 检索与筛选：关键词 + 分类树 + 价格区间 + 产地 + 认证 + 有货，支持排序分页。

        `category` 既接受品类域的 code（fruit / fruit-citrus），也接受分类名，
        后者是为了兼容早期只存分类文本、没有 category_id 的商品数据。
        """
        conds = []
        if not include_off_shelf:
            conds.append(Product.status == ProductStatus.ON_SALE)
        if keyword and keyword.strip():
            like = f"%{keyword.strip()}%"
            conds.append(
                or_(
                    Product.name.like(like),
                    Product.description.like(like),
                    Product.tags.like(like),
                    Product.origin.like(like),
                )
            )
        if category:
            category_ids, category_names = CategoryService.resolve_filter(db, category)
            if category_ids:
                conds.append(
                    or_(
                        Product.category_id.in_(category_ids),
                        # 冗余快照名也参与匹配，兜住尚未回填 category_id 的老数据
                        Product.category.in_(category_names),
                    )
                )
            elif category_names:
                conds.append(Product.category == category_names[0])
        if price_min is not None:
            conds.append(Product.price_cents >= _to_cents(price_min))
        if price_max is not None:
            conds.append(Product.price_cents <= _to_cents(price_max))
        if origin and origin.strip():
            like = f"%{origin.strip()}%"
            conds.append(
                or_(
                    Product.origin.like(like),
                    Product.farm_id.in_(
                        select(Farm.farm_id).where(
                            or_(Farm.province.like(like), Farm.city.like(like), Farm.farm_name.like(like))
                        )
                    ),
                )
            )
        if certified_only:
            conds.append(Product.certified == 1)
        if in_stock_only:
            conds.append(Product.stock > 0)
        if recommended_only:
            conds.append(Product.is_recommended == 1)
        if farm_id:
            conds.append(Product.farm_id == farm_id)
        if farm_ids is not None:
            # 农户可能拥有多个产地（合作社），后台商品维护要一并看到
            conds.append(Product.farm_id.in_(farm_ids or ["__none__"]))

        where = and_(*conds) if conds else None
        count_stmt = select(func.count()).select_from(Product)
        if where is not None:
            count_stmt = count_stmt.where(where)
        total = db.scalar(count_stmt) or 0

        order_by = {
            "price_asc": Product.price_cents.asc(),
            "price_desc": Product.price_cents.desc(),
            "sales": Product.sales_count.desc(),
            "stock": Product.stock.desc(),
            "new": Product.created_at.desc(),
        }.get(sort, Product.created_at.desc())

        size = max(1, min(size, settings.max_page_size))
        page_no = max(1, page_no)
        stmt = select(Product)
        if where is not None:
            stmt = stmt.where(where)
        rows = db.scalars(stmt.order_by(order_by).offset((page_no - 1) * size).limit(size)).all()
        categories = _category_index(db)

        return {
            "items": [_product_out(p, categories) for p in rows],
            "total": total,
            "page": page_no,
            "size": size,
        }

    @staticmethod
    def detail(db: Session, product_id: str) -> dict[str, Any]:
        """商品详情：把溯源侧信息（农场、批次、可扫码值）与同类推荐一并带上。"""
        product = db.get(Product, product_id)
        if product is None:
            raise NotFound("商品不存在")
        categories = _category_index(db)
        data = _product_out(product, categories)

        farm = db.get(Farm, product.farm_id)
        batch = db.get(Batch, product.batch_id)
        data["farm_name"] = farm.farm_name if farm else ""
        data["farm_province"] = farm.province if farm else ""
        data["farm_city"] = farm.city if farm else ""
        data["farm_address"] = (
            f"{farm.province}{farm.city}{farm.address}" if farm else ""
        )
        data["cert_status"] = farm.cert_status if farm else "pending"
        # 商品级产地优先；没填就用农场所在地兜底，保证详情页「产地」一栏永远有值
        if not data["origin"]:
            data["origin"] = f"{farm.province}{farm.city}" if farm else ""
        data["batch_no"] = batch.batch_no if batch else ""
        data["crop_name"] = batch.crop_name if batch else ""
        data["batch_status"] = batch.status if batch else ""
        data["event_count"] = batch.event_count if batch else 0
        data["plant_date"] = batch.plant_date.isoformat() if batch and batch.plant_date else ""
        data["harvest_date"] = batch.harvest_date.isoformat() if batch and batch.harvest_date else ""

        code = db.scalar(
            select(TraceCode).where(TraceCode.batch_id == product.batch_id).limit(1)
        )
        data["trace_code"] = code.code_value if code else None

        data["reviews"] = [
            {
                "review_id": r.review_id,
                "order_id": r.order_id,
                "product_id": r.product_id,
                "user_id": r.user_id,
                "rating": r.rating,
                "content": r.content,
                "display_name": r.display_name,
                "created_at": r.created_at,
            }
            for r in db.scalars(
                select(Review)
                .where(Review.product_id == product_id)
                .order_by(Review.created_at.desc())
                .limit(20)
            ).all()
        ]
        data["rating"] = (
            round(sum(r["rating"] for r in data["reviews"]) / len(data["reviews"]), 1)
            if data["reviews"]
            else 0.0
        )

        # 同类推荐：先按二级分类找，不够再按一级分类兜（单品分类下常常只有 1 件商品）
        related: list[Product] = []
        if product.category_id:
            related = list(
                db.scalars(
                    select(Product)
                    .where(
                        Product.category_id == product.category_id,
                        Product.product_id != product.product_id,
                        Product.status == ProductStatus.ON_SALE,
                    )
                    .order_by(Product.sales_count.desc())
                    .limit(4)
                ).all()
            )
            if len(related) < 4:
                category = db.get(Category, product.category_id)
                if category is not None and category.parent_id is None:
                    sibling_ids = [c.category_id for c in category.children]
                elif category is not None:
                    sibling_ids = [category.parent_id] if category.parent_id else []
                else:
                    sibling_ids = []
                if sibling_ids:
                    more = db.scalars(
                        select(Product)
                        .where(
                            Product.category_id.in_(sibling_ids),
                            Product.product_id != product.product_id,
                            Product.status == ProductStatus.ON_SALE,
                        )
                        .order_by(Product.sales_count.desc())
                        .limit(4 - len(related))
                    ).all()
                    related.extend(more)
        data["related"] = [_product_out(p, categories) for p in related]
        return data

    @staticmethod
    def categories(db: Session) -> list[str]:
        """兼容旧接口：只返回出现过的分类名（新代码请用 /api/categories 的两级树）。"""
        rows = db.scalars(select(Product.category).distinct()).all()
        return sorted({r for r in rows if r})

    @staticmethod
    def stats(
        db: Session, *, farm_id: str | None = None, farm_ids: list[str] | None = None
    ) -> dict[str, int]:
        """商品维护页的顶部指标：总量 / 在售 / 下架 / 未归类 / 推荐位。"""
        base = select(func.count()).select_from(Product)
        if farm_id:
            base = base.where(Product.farm_id == farm_id)
        if farm_ids is not None:
            base = base.where(Product.farm_id.in_(farm_ids or ["__none__"]))
        total = db.scalar(base) or 0
        on_sale = db.scalar(base.where(Product.status == ProductStatus.ON_SALE)) or 0
        recommended = db.scalar(base.where(Product.is_recommended == 1)) or 0
        uncategorized = db.scalar(base.where(Product.category_id.is_(None))) or 0
        return {
            "total": total,
            "on_sale": on_sale,
            "off_shelf": total - on_sale,
            "recommended": recommended,
            "uncategorized": uncategorized,
        }


# ================================================================== 购物车
class CartService:
    @staticmethod
    def add(db: Session, user: User, payload: CartAdd) -> dict[str, Any]:
        product = db.get(Product, payload.product_id)
        if product is None:
            raise NotFound("商品不存在")
        if product.status != ProductStatus.ON_SALE:
            raise RuleViolation("商品已下架，无法加入购物车")

        item = db.scalar(
            select(CartItem).where(
                CartItem.user_id == user.user_id, CartItem.product_id == product.product_id
            )
        )
        target_qty = (item.quantity if item else 0) + payload.quantity
        if target_qty > product.stock:
            raise RuleViolation(
                f"库存不足，{product.name} 当前仅剩 {product.stock} {product.unit}",
                data={"product_id": product.product_id, "stock": product.stock},
            )
        if item is None:
            item = CartItem(
                user_id=user.user_id, product_id=product.product_id, quantity=payload.quantity
            )
            db.add(item)
        else:
            item.quantity = target_qty
        db.commit()
        return CartService.list_cart(db, user)

    @staticmethod
    def update(db: Session, user: User, cart_id: str, quantity: int) -> dict[str, Any]:
        item = db.get(CartItem, cart_id)
        if item is None or item.user_id != user.user_id:
            raise NotFound("购物车条目不存在")
        product = db.get(Product, item.product_id)
        if product is None:
            raise NotFound("商品不存在")
        if quantity > product.stock:
            raise RuleViolation(f"库存不足，当前仅剩 {product.stock} {product.unit}")
        item.quantity = quantity
        db.commit()
        return CartService.list_cart(db, user)

    @staticmethod
    def remove(db: Session, user: User, cart_id: str) -> dict[str, Any]:
        item = db.get(CartItem, cart_id)
        if item is None or item.user_id != user.user_id:
            raise NotFound("购物车条目不存在")
        db.delete(item)
        db.commit()
        return CartService.list_cart(db, user)

    @staticmethod
    def clear(db: Session, user: User) -> dict[str, Any]:
        for item in db.scalars(select(CartItem).where(CartItem.user_id == user.user_id)).all():
            db.delete(item)
        db.commit()
        return {"items": [], "total_cents": 0, "total": 0.0}

    @staticmethod
    def list_cart(db: Session, user: User) -> dict[str, Any]:
        items = db.scalars(
            select(CartItem)
            .where(CartItem.user_id == user.user_id)
            .order_by(CartItem.created_at.desc())
        ).all()
        out: list[dict[str, Any]] = []
        total = 0
        for it in items:
            product = db.get(Product, it.product_id)
            if product is None:
                continue
            subtotal = product.price_cents * it.quantity
            total += subtotal
            out.append(
                {
                    "cart_id": it.cart_id,
                    "product_id": product.product_id,
                    "name": product.name,
                    "price_cents": product.price_cents,
                    "price": product.price_yuan,
                    "quantity": it.quantity,
                    "stock": product.stock,
                    "status": product.status,
                    "unit": product.unit,
                    "subtotal_cents": subtotal,
                    "subtotal": round(subtotal / 100, 2),
                    "over_stock": it.quantity > product.stock,
                }
            )
        return {"items": out, "total_cents": total, "total": round(total / 100, 2)}


# ================================================================== 订单 UC-08
class OrderService:
    @staticmethod
    def _deduct_stock(db: Session, product_id: str, quantity: int) -> None:
        """BR-05 库存条件更新：把「够不够」的判断交给数据库，一次原子完成。

        rowcount != 1 说明库存已被别人买走或商品被下架，此时抛异常，
        整个下单事务回滚，不会出现「订单建成但库存没扣」的中间态。
        """
        result = db.execute(
            update(Product)
            .where(
                Product.product_id == product_id,
                Product.stock >= quantity,
                Product.status == ProductStatus.ON_SALE,
            )
            .values(stock=Product.stock - quantity)
        )
        if result.rowcount != 1:
            product = db.get(Product, product_id)
            if product is None:
                raise NotFound("商品不存在")
            if product.status != ProductStatus.ON_SALE:
                raise RuleViolation(f"商品「{product.name}」已下架，请从购物车移除")
            raise RuleViolation(
                f"商品「{product.name}」库存不足，当前仅剩 {product.stock} {product.unit}",
                data={"product_id": product_id, "stock": product.stock},
            )

    @staticmethod
    def _restore_stock(db: Session, product_id: str, quantity: int) -> None:
        """BR-06 取消后库存原路释放：加库存方向永远安全，无需条件判断。"""
        db.execute(
            update(Product)
            .where(Product.product_id == product_id)
            .values(stock=Product.stock + quantity)
        )

    @staticmethod
    def create(db: Session, user: User, payload: OrderCreate) -> dict[str, Any]:
        """下单：校验 → 算钱 → 建单快照 → 原子扣库存 → 清购物车，全在一个事务里。"""
        lines: list[tuple[Product, int, str | None]] = []

        if payload.cart_ids:
            for cid in payload.cart_ids:
                cart = db.get(CartItem, cid)
                if cart is None or cart.user_id != user.user_id:
                    raise NotFound(f"购物车条目不存在：{cid}")
                product = db.get(Product, cart.product_id)
                if product is None:
                    raise NotFound("商品不存在")
                lines.append((product, cart.quantity, cart.cart_id))
        else:
            if not payload.product_id:
                raise RuleViolation("请选择要购买的商品")
            product = db.get(Product, payload.product_id)
            if product is None:
                raise NotFound("商品不存在")
            lines.append((product, payload.quantity, None))

        if not lines:
            raise RuleViolation("下单商品不能为空")

        # 跨域校验：批次必须仍处于可销售状态。
        # 上架时校验过一次，但批次可能在售中被监管封存或平台结批，
        # 所以下单这一刻要重新确认，否则会卖出已封存的货。
        batch_ids = {p.batch_id for p, _, _ in lines}
        batches = {
            b.batch_id: b
            for b in db.scalars(select(Batch).where(Batch.batch_id.in_(list(batch_ids)))).all()
        }
        for product, _, _ in lines:
            batch = batches.get(product.batch_id)
            if batch is None:
                raise RuleViolation(f"商品「{product.name}」关联的溯源批次不存在")
            if batch.status in (BatchStatus.DRAFT, BatchStatus.GROWING):
                raise RuleViolation(f"批次「{batch.batch_no}」尚未采收，暂不可售")
            if batch.status == BatchStatus.CLOSED:
                raise RuleViolation(f"批次「{batch.batch_no}」已结批或已被封存，暂不可售")

        # 金额完全由服务端按商品当前价重算，忽略任何客户端传来的金额
        total_cents = 0
        for product, qty, _ in lines:
            if product.status != ProductStatus.ON_SALE:
                raise RuleViolation(f"商品「{product.name}」已下架")
            if qty <= 0:
                raise RuleViolation("购买数量必须大于 0")
            total_cents += product.price_cents * qty

        farmer_id = lines[0][0].farm_id
        order = Order(
            order_no=new_biz_no("SO"),
            user_id=user.user_id,
            farmer_id=farmer_id,
            total_cents=total_cents,
            status=OrderStatus.PENDING,
            receiver_name=payload.receiver_name,
            receiver_phone=payload.receiver_phone,
            address=payload.address,
            remark=payload.remark,
            expire_at=_now() + timedelta(minutes=ORDER_EXPIRE_MINUTES),
        )
        db.add(order)
        db.flush()  # 拿到 order_id 后再插明细

        for product, qty, _ in lines:
            db.add(
                OrderItem(
                    order_id=order.order_id,
                    product_id=product.product_id,
                    product_name=product.name,
                    price_cents=product.price_cents,
                    quantity=qty,
                    subtotal_cents=product.price_cents * qty,
                )
            )
            OrderService._deduct_stock(db, product.product_id, qty)

        for _, _, cart_id in lines:
            if cart_id:
                cart = db.get(CartItem, cart_id)
                if cart is not None:
                    db.delete(cart)

        db.commit()
        db.refresh(order)
        return _order_out(order)

    @staticmethod
    def get(db: Session, user: User, order_id: str) -> dict[str, Any]:
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        if order.user_id != user.user_id and user.role not in (Role.OPERATOR, Role.REGULATOR):
            raise Forbidden("无权查看他人订单")
        return _order_out(order)

    @staticmethod
    def list_orders(
        db: Session,
        user: User,
        *,
        status: str | None = None,
        page_no: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        OrderService.sweep_expired(db, user_id=user.user_id)
        conds = []
        if user.role == Role.CONSUMER:
            conds.append(Order.user_id == user.user_id)
        elif user.role == Role.FARMER:
            farm_ids = db.scalars(select(Farm.farm_id).where(Farm.owner_id == user.user_id)).all()
            conds.append(Order.farmer_id.in_(list(farm_ids) or ["__none__"]))
        # 平台运营与监管可查看全部订单，便于 UC-13 看板与 UC-15 回溯
        if status:
            conds.append(Order.status == status)

        where = and_(*conds) if conds else None
        count_stmt = select(func.count()).select_from(Order)
        if where is not None:
            count_stmt = count_stmt.where(where)
        total = db.scalar(count_stmt) or 0

        size = max(1, min(size, settings.max_page_size))
        stmt = select(Order)
        if where is not None:
            stmt = stmt.where(where)
        rows = db.scalars(
            stmt.order_by(Order.created_at.desc()).offset((max(1, page_no) - 1) * size).limit(size)
        ).all()
        return {
            "items": [_order_out(o) for o in rows],
            "total": total,
            "page": max(1, page_no),
            "size": size,
        }

    @staticmethod
    def pay(db: Session, user: User, order_id: str, channel: str, force_fail: bool = False) -> dict:
        """UC-08 支付。

        BR-04 的两次防护：
        1. 先查是否已有成功流水——重复点击支付直接返回原结果（幂等）
        2. 部分唯一索引兜底——并发下第二次 insert 被数据库拒绝
        """
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        if order.user_id != user.user_id:
            raise Forbidden("无权支付他人订单")

        existing = db.scalar(
            select(Payment).where(
                Payment.order_id == order_id, Payment.status == PayStatus.SUCCESS
            )
        )
        if existing is not None:
            return {
                "order": _order_out(order),
                "payment": _payment_out(existing),
                "retryable": False,
                "message": "该订单已支付，无需重复支付",
            }

        if order.status != OrderStatus.PENDING:
            raise RuleViolation(
                f"当前订单状态为「{OrderStatus.LABELS.get(order.status, order.status)}」，不可支付"
            )
        if order.expire_at and order.expire_at < _now():
            OrderService._cancel_inner(db, order, "订单超时未支付，自动取消")
            db.commit()
            raise RuleViolation("订单已超时取消，请重新下单")
        if order.pay_attempts >= MAX_PAY_ATTEMPTS:
            raise RuleViolation(
                f"支付失败已达 {MAX_PAY_ATTEMPTS} 次，请取消订单后重新下单",
                data={"pay_attempts": order.pay_attempts},
            )

        result: ChargeResult = get_gateway(force_fail=force_fail).charge(
            order.order_no, order.total_cents, channel
        )

        if not result.ok:
            order.pay_attempts += 1
            order.last_fail_reason = result.fail_reason
            db.commit()
            return {
                "order": _order_out(order),
                "payment": None,
                "retryable": order.pay_attempts < MAX_PAY_ATTEMPTS,
                "message": f"支付失败：{result.fail_reason}（第 {order.pay_attempts} 次）",
            }

        payment = Payment(
            order_id=order.order_id,
            user_id=user.user_id,
            amount_cents=order.total_cents,
            channel=channel,
            status=PayStatus.SUCCESS,
            transaction_no=result.transaction_no,
            paid_at=_now(),
        )
        db.add(payment)
        order.status = OrderStatus.PAID
        order.paid_at = _now()

        # 支付成功后累加销量，供看板与排序使用
        for it in order.items:
            db.execute(
                update(Product)
                .where(Product.product_id == it.product_id)
                .values(sales_count=Product.sales_count + it.quantity)
            )

        try:
            db.commit()
        except IntegrityError as exc:  # 并发重复支付：唯一索引拦下
            db.rollback()
            raise Conflict("该订单已存在成功支付流水，请勿重复支付") from exc

        db.refresh(order)
        db.refresh(payment)
        return {
            "order": _order_out(order),
            "payment": _payment_out(payment),
            "retryable": False,
            "message": "支付成功",
        }

    @staticmethod
    def _cancel_inner(db: Session, order: Order, reason: str) -> None:
        """取消订单并归还库存（内部方法，调用方负责 commit）。"""
        if order.status == OrderStatus.CANCELLED:
            return
        for it in order.items:
            OrderService._restore_stock(db, it.product_id, it.quantity)
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = _now()
        order.cancel_reason = reason
        if order.paid_at is not None:
            # 已支付订单取消需要退款：课程作业记录退款意向，不接真实退款接口
            order.cancel_reason = f"{reason}（已支付，需退款 {order.total_cents / 100:.2f} 元）"

    @staticmethod
    def cancel(db: Session, user: User, order_id: str, reason: str = "用户主动取消") -> dict:
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        if order.user_id != user.user_id and user.role not in (Role.OPERATOR,):
            raise Forbidden("无权取消该订单")
        if order.status not in (OrderStatus.PENDING, OrderStatus.PAID):
            raise RuleViolation(
                f"当前状态「{OrderStatus.LABELS.get(order.status, order.status)}」不可取消"
            )
        OrderService._cancel_inner(db, order, reason)
        db.commit()
        db.refresh(order)
        return _order_out(order)

    @staticmethod
    def sweep_expired(db: Session, user_id: str | None = None) -> int:
        """惰性清理超时未支付订单（替代定时任务，单机演示场景够用）。

        真实部署应换成 APScheduler / Celery beat，这里放在查询前触发，
        保证「30 分钟后自动取消并释放库存」这条规则在没有调度器时也成立。
        """
        stmt = select(Order).where(
            Order.status == OrderStatus.PENDING,
            Order.expire_at.is_not(None),
            Order.expire_at < _now(),
        )
        if user_id:
            stmt = stmt.where(Order.user_id == user_id)
        rows = db.scalars(stmt).all()
        if not rows:
            return 0
        for order in rows:
            OrderService._cancel_inner(db, order, "订单超时未支付，自动取消")
        db.commit()
        return len(rows)

    @staticmethod
    def ship(db: Session, user: User, order_id: str, tracking_no: str = "") -> dict[str, Any]:
        """UC-09 发货：农户只能发自己农场的订单，运营可代为操作。"""
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        if user.role == Role.FARMER:
            farm_ids = set(
                db.scalars(select(Farm.farm_id).where(Farm.owner_id == user.user_id)).all()
            )
            if order.farmer_id not in farm_ids:
                raise Forbidden("只能对自家农场的订单发货")
        elif user.role not in (Role.OPERATOR,):
            raise Forbidden("该操作需要农户或平台运营角色")

        if order.status != OrderStatus.PAID:
            raise RuleViolation(
                f"当前状态「{OrderStatus.LABELS.get(order.status, order.status)}」不可发货"
            )
        order.status = OrderStatus.SHIPPED
        order.shipped_at = _now()
        if tracking_no:
            order.remark = f"{order.remark} 运单号：{tracking_no}".strip()
        db.commit()
        db.refresh(order)
        return _order_out(order)

    @staticmethod
    def receive(db: Session, user: User, order_id: str) -> dict[str, Any]:
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        if order.user_id != user.user_id:
            raise Forbidden("只能确认自己的订单")
        if order.status != OrderStatus.SHIPPED:
            raise RuleViolation(
                f"当前状态「{OrderStatus.LABELS.get(order.status, order.status)}」不可确认收货"
            )
        order.status = OrderStatus.RECEIVED
        order.received_at = _now()
        db.commit()
        db.refresh(order)
        return _order_out(order)


# ================================================================== 评价 UC-10
class ReviewService:
    @staticmethod
    def create(db: Session, user: User, order_id: str, payload: ReviewCreate) -> dict[str, Any]:
        """UC-10：仅收货后可评价，同一订单同一商品只能评一次（扩展 UC-09）。"""
        order = db.get(Order, order_id)
        if order is None:
            raise NotFound("订单不存在")
        if order.user_id != user.user_id:
            raise Forbidden("只能评价自己的订单")
        if order.status != OrderStatus.RECEIVED:
            raise RuleViolation("确认收货后才能评价")

        bought = any(it.product_id == payload.product_id for it in order.items)
        if not bought:
            raise RuleViolation("该订单不包含此商品")

        exists = db.scalar(
            select(Review).where(
                Review.order_id == order_id, Review.product_id == payload.product_id
            )
        )
        if exists is not None:
            raise Conflict("该商品已评价过，不能重复评价")

        review = Review(
            order_id=order_id,
            product_id=payload.product_id,
            user_id=user.user_id,
            rating=payload.rating,
            content=payload.content,
            # 合规：不展示真实姓名，统一取昵称首字 + 星号
            display_name=(user.display_name[:1] + "**") if user.display_name else "匿名用户",
        )
        db.add(review)
        db.commit()
        db.refresh(review)
        return {
            "review_id": review.review_id,
            "order_id": review.order_id,
            "product_id": review.product_id,
            "user_id": review.user_id,
            "rating": review.rating,
            "content": review.content,
            "display_name": review.display_name,
            "created_at": review.created_at,
        }

    @staticmethod
    def list_by_product(db: Session, product_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = db.scalars(
            select(Review)
            .where(Review.product_id == product_id)
            .order_by(Review.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "review_id": r.review_id,
                "order_id": r.order_id,
                "product_id": r.product_id,
                "user_id": r.user_id,
                "rating": r.rating,
                "content": r.content,
                "display_name": r.display_name,
                "created_at": r.created_at,
            }
            for r in rows
        ]
