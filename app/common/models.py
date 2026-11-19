"""公共基础实体（三人共用的最小契约）。

按报告 4.5 的 13 张表划分，sys_user / farm 属于「用户与运营域」，
但其主键与外键被溯源、交易两个域引用，因此由组长先在公共基础里定下最小字段，
王×× 在自己的分支上扩展（认证材料、审核状态机等），不修改这里的既有字段。
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin


class Role:
    """角色常量：与报告 2.1 的四类用户对应。"""

    FARMER = "farmer"
    CONSUMER = "consumer"
    OPERATOR = "operator"
    REGULATOR = "regulator"

    ALL = (FARMER, CONSUMER, OPERATOR, REGULATOR)


class User(Base, TimestampMixin):
    __tablename__ = "sys_user"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("USR"))
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class Farm(Base, TimestampMixin):
    __tablename__ = "farm"

    farm_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("FRM"))
    farm_name: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    province: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    area_mu: Mapped[float | None] = mapped_column(default=None)
    # pending / approved / rejected，审核流程由用户与运营域实现（UC-12）
    cert_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
