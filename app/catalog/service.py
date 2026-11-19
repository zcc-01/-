"""品类域业务逻辑（分类目录的增删改查、层级校验、筛选解析与计数）。

三条硬规则，全部在 Service 层兜住，不依赖前端传参正确：
- R1 层级最多两级：二级分类不能再挂子类，一级分类有子类时不能自己变成子类；
- R2 停用父类级联停用子类，避免前台出现「父类已隐藏、子类还能被筛到」的孤岛；
- R3 分类下仍有商品或有子类时禁止删除，先迁移再删除，防止商品变孤儿。

计数的口径要区分清楚：前台只算在售商品（`enabled` + `on_sale`），
后台要算全部（含已下架），否则运营会以为商品丢了。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.models import Category
from app.catalog.schemas import CategoryCreate, CategoryUpdate
from app.core.errors import Conflict, NotFound, RuleViolation
from app.trade.models import Product, ProductStatus


def _out(category: Category, *, count: int = 0, parent_name: str = "") -> dict[str, Any]:
    return {
        "category_id": category.category_id,
        "code": category.code,
        "name": category.name,
        "parent_id": category.parent_id,
        "parent_name": parent_name,
        "level": category.level,
        "icon": category.icon,
        "image": category.image,
        "sort_no": category.sort_no,
        "enabled": bool(category.enabled),
        "description": category.description,
        "product_count": count,
        "children": [],
    }


class CategoryService:
    # ---------------------------------------------------------------- 查询
    @staticmethod
    def list_all(db: Session, *, include_disabled: bool = True) -> list[Category]:
        stmt = select(Category).order_by(Category.level, Category.sort_no, Category.name)
        if not include_disabled:
            stmt = stmt.where(Category.enabled == 1)
        return list(db.scalars(stmt).all())

    @staticmethod
    def counts(db: Session, *, on_sale_only: bool = False) -> dict[str, int]:
        """按分类统计商品数。

        没有挂分类的历史商品（category_id 为空）不参与统计，由商品维护页单独提示。
        """
        stmt = select(Product.category_id, func.count()).where(Product.category_id.is_not(None))
        if on_sale_only:
            stmt = stmt.where(Product.status == ProductStatus.ON_SALE)
        rows = db.execute(stmt.group_by(Product.category_id)).all()
        return {cid: int(n) for cid, n in rows if cid}

    @staticmethod
    def tree(
        db: Session,
        *,
        include_disabled: bool = False,
        on_sale_only: bool = True,
    ) -> list[dict[str, Any]]:
        """返回两级分类树，父类 product_count 含所有子类商品数（前台筛选面板用）。"""
        rows = CategoryService.list_all(db, include_disabled=include_disabled)
        counts = CategoryService.counts(db, on_sale_only=on_sale_only)

        parents = [c for c in rows if not c.parent_id]
        by_parent: dict[str, list[Category]] = {}
        for c in rows:
            if c.parent_id:
                by_parent.setdefault(c.parent_id, []).append(c)

        tree: list[dict[str, Any]] = []
        for parent in parents:
            node = _out(parent, count=counts.get(parent.category_id, 0))
            child_total = node["product_count"]
            for child in by_parent.get(parent.category_id, []):
                child_node = _out(child, count=counts.get(child.category_id, 0), parent_name=parent.name)
                child_total += child_node["product_count"]
                node["children"].append(child_node)
            node["product_count"] = child_total
            tree.append(node)
        return tree

    @staticmethod
    def flat(db: Session, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        """扁平列表（父类后紧跟其子类），给后台表格与下拉框用。

        父类的商品数按「自身 + 全部子类」汇总，与前台分类页签口径一致；
        否则运营会看到「水果 0 件、柑橘类 1 件」这种自相矛盾的表格。
        """
        rows = CategoryService.list_all(db, include_disabled=include_disabled)
        counts = CategoryService.counts(db, on_sale_only=False)
        names = {c.category_id: c.name for c in rows}
        ordered: list[dict[str, Any]] = []
        for parent in [c for c in rows if not c.parent_id]:
            kids = [c for c in rows if c.parent_id == parent.category_id]
            total = counts.get(parent.category_id, 0) + sum(counts.get(k.category_id, 0) for k in kids)
            ordered.append(_out(parent, count=total))
            for child in kids:
                ordered.append(
                    _out(child, count=counts.get(child.category_id, 0), parent_name=parent.name)
                )
        # 兜底：parent_id 指向已不存在父类的脏数据也要能显示出来
        seen = {item["category_id"] for item in ordered}
        for c in rows:
            if c.category_id not in seen:
                ordered.append(
                    _out(
                        c,
                        count=counts.get(c.category_id, 0),
                        parent_name=names.get(c.parent_id or "", ""),
                    )
                )
        return ordered

    @staticmethod
    def get(db: Session, category_id: str) -> Category:
        category = db.get(Category, category_id)
        if category is None:
            raise NotFound("商品分类不存在")
        return category

    @staticmethod
    def get_by_code(db: Session, code: str) -> Category | None:
        if not code:
            return None
        return db.scalar(select(Category).where(Category.code == code))

    @staticmethod
    def descendant_ids(db: Session, category: Category) -> list[str]:
        ids = [category.category_id]
        children = db.scalars(
            select(Category.category_id).where(Category.parent_id == category.category_id)
        ).all()
        ids.extend(children)
        return ids

    @staticmethod
    def resolve_filter(db: Session, value: str) -> tuple[list[str], list[str]]:
        """把「分类 code 或分类名」解析成筛选条件。

        返回 (category_ids, names)：
        - 命中 code：返回自身 + 所有子类的 id（选「水果」要能看到所有水果）；
        - 未命中 code：退化成按分类名精确匹配，兼容早期只存 category 文本的商品。
        """
        value = (value or "").strip()
        if not value:
            return [], []
        category = CategoryService.get_by_code(db, value)
        if category is not None:
            names = [category.name]
            names.extend(
                db.scalars(
                    select(Category.name).where(Category.parent_id == category.category_id)
                ).all()
            )
            return CategoryService.descendant_ids(db, category), names
        return [], [value]

    # ---------------------------------------------------------------- 写入
    @staticmethod
    def _next_sort_no(db: Session, parent_id: str | None) -> int:
        current = db.scalar(
            select(func.max(Category.sort_no)).where(
                Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
            )
        )
        return int(current or 0) + 10

    @staticmethod
    def _assert_name_free(
        db: Session, name: str, parent_id: str | None, exclude_id: str | None = None
    ) -> None:
        """同一层级不允许重名。

        只在库上建 UniqueConstraint 是不够的：SQLite/MySQL 都把 NULL 视为互不相等，
        一级分类的 parent_id 是 NULL，唯一约束对它们形同虚设，必须应用层再挡一次。
        """
        stmt = select(Category.category_id).where(Category.name == name)
        stmt = stmt.where(
            Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
        )
        if exclude_id:
            stmt = stmt.where(Category.category_id != exclude_id)
        if db.scalar(stmt) is not None:
            raise Conflict(f"同一层级下已存在分类「{name}」，请换一个名称")

    @staticmethod
    def create(db: Session, payload: CategoryCreate) -> dict[str, Any]:
        if CategoryService.get_by_code(db, payload.code) is not None:
            raise Conflict(f"分类编码「{payload.code}」已存在，请换一个")

        parent: Category | None = None
        if payload.parent_id:
            parent = CategoryService.get(db, payload.parent_id)
            if parent.level != 1 or parent.parent_id:
                raise RuleViolation("只支持两级分类，二级分类不能再挂子类")

        CategoryService._assert_name_free(db, payload.name, parent.category_id if parent else None)

        category = Category(
            code=payload.code,
            name=payload.name,
            parent_id=parent.category_id if parent else None,
            level=2 if parent else 1,
            icon=payload.icon,
            image=payload.image,
            sort_no=payload.sort_no or CategoryService._next_sort_no(db, payload.parent_id),
            enabled=1,
            description=payload.description,
        )
        db.add(category)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise Conflict("同一父类下不允许重名分类") from exc
        db.refresh(category)
        return _out(category, parent_name=parent.name if parent else "")

    @staticmethod
    def update(db: Session, category_id: str, payload: CategoryUpdate) -> dict[str, Any]:
        category = CategoryService.get(db, category_id)
        data = payload.model_dump(exclude_unset=True)

        if "code" in data and data["code"] and data["code"] != category.code:
            if CategoryService.get_by_code(db, data["code"]) is not None:
                raise Conflict(f"分类编码「{data['code']}」已存在，请换一个")
            category.code = data.pop("code")

        if "parent_id" in data:
            new_parent_id = data.pop("parent_id") or None
            if new_parent_id != category.parent_id:
                has_children = db.scalar(
                    select(func.count())
                    .select_from(Category)
                    .where(Category.parent_id == category.category_id)
                )
                if has_children:
                    raise RuleViolation("该分类下还有子类，不能调整层级；请先处理子类")
                if new_parent_id is not None:
                    if new_parent_id == category.category_id:
                        raise RuleViolation("分类不能挂到自己下面")
                    parent = CategoryService.get(db, new_parent_id)
                    if parent.level != 1 or parent.parent_id:
                        raise RuleViolation("只支持两级分类，请选择一级分类作为上级")
                    category.parent_id = parent.category_id
                    category.level = 2
                else:
                    category.parent_id = None
                    category.level = 1

        for key in ("name", "icon", "image", "sort_no", "description"):
            if key in data and data[key] is not None:
                setattr(category, key, data[key])
        CategoryService._assert_name_free(
            db, category.name, category.parent_id, exclude_id=category.category_id
        )
        if "enabled" in data and data["enabled"] is not None:
            CategoryService._apply_enabled(db, category, bool(data["enabled"]))

        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise Conflict("同一父类下不允许重名分类") from exc
        db.refresh(category)
        return _out(category, parent_name=category.parent.name if category.parent else "")

    @staticmethod
    def _apply_enabled(db: Session, category: Category, enabled: bool) -> None:
        category.enabled = 1 if enabled else 0
        # R2：停用父类时级联停用子类，避免「父类隐藏、子类还能被筛到」
        if not enabled and category.level == 1:
            for child in db.scalars(
                select(Category).where(Category.parent_id == category.category_id)
            ).all():
                child.enabled = 0

    @staticmethod
    def toggle(db: Session, category_id: str, enabled: bool) -> dict[str, Any]:
        category = CategoryService.get(db, category_id)
        CategoryService._apply_enabled(db, category, enabled)
        db.commit()
        db.refresh(category)
        return _out(category, count=CategoryService.counts(db).get(category_id, 0))

    @staticmethod
    def delete(db: Session, category_id: str) -> None:
        category = CategoryService.get(db, category_id)
        children = db.scalar(
            select(func.count()).select_from(Category).where(Category.parent_id == category_id)
        )
        if children:
            raise RuleViolation("该分类下还有子类，请先删除或迁移子类")
        used = db.scalar(
            select(func.count()).select_from(Product).where(Product.category_id == category_id)
        )
        if used:
            raise RuleViolation(f"该分类下还有 {used} 件商品，请先调整商品分类再删除")
        db.delete(category)
        db.commit()

    @staticmethod
    def ensure_visible(db: Session, category_id: str | None) -> Category | None:
        """校验商品要挂的分类可用：不存在或已停用都不允许挂。"""
        if not category_id:
            return None
        category = db.get(Category, category_id)
        if category is None:
            raise NotFound("所选商品分类不存在")
        if not category.enabled:
            raise RuleViolation("所选商品分类已停用，请换一个分类")
        return category
