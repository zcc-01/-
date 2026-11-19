"""品类域数据模型（商品分类目录）。

设计取舍：
1. 两级分类（大类 / 子类）而不是无限层级：
   农产品目录天然只有「水果 → 柑橘类」这个深度，多余层级只会让筛选面板和
   运营维护变复杂；`level` 显式落库，避免每次递归算层级。
2. `code` 是稳定对外标识（URL、接口参数都用它），`category_id` 是内部主键。
   运营可以改分类名，但商品与链接靠 code 关联，改名不影响历史链接。
3. 停用只影响前台可见性，不删除数据；分类下还有商品或有子类时禁止删除，
   这是「数据维护」最容易踩的坑——删掉分类会让商品变成孤儿。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


class Category(Base, TimestampMixin):
    """商品分类：level=1 为大类（水果/蔬菜…），level=2 为子类（柑橘类/叶菜…）。"""

    __tablename__ = "product_category"
    __table_args__ = (
        # 同一父类下不允许重名，避免运营建出两个「柑橘类」导致筛选面板出现重复项
        UniqueConstraint("parent_id", "name", name="uq_category_name_per_parent"),
    )

    category_id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("CAT")
    )
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("product_category.category_id"), default=None, index=True
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    icon: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    image: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    children: Mapped[list[Category]] = relationship(
        back_populates="parent",
        cascade="save-update",
        lazy="selectin",
        order_by="Category.sort_no",
    )
    parent: Mapped[Category | None] = relationship(
        back_populates="children", remote_side="Category.category_id"
    )
