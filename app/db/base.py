"""数据库连接与会话（公共基础层）。

开发用 SQLite（免安装），生产切 MySQL，只改 DATABASE_URL，不改业务代码。
SQLite 开启 WAL + busy_timeout，保证并发写入测试下不会大面积 database is locked。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """所有表统一带 created_at / updated_at 审计字段（报告 4.5 约定）。"""

    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        onupdate=lambda: datetime.now(UTC).replace(tzinfo=None),
    )


_connect_args: dict = {}
if settings.is_sqlite:
    _connect_args = {"check_same_thread": False, "timeout": 30}
    # 全新克隆的仓库里 data/ 目录可能不存在，SQLite 不会自动建目录，
    # 直接连会报 "unable to open database file"（CI 上就是这么挂的）。
    _sqlite_file = settings.database_url.replace("sqlite:///", "", 1).split("?", 1)[0]
    Path(_sqlite_file).expanduser().parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - 仅在 SQLite 下生效
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator:
    """FastAPI 依赖：每请求一个会话，异常回滚，结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """建表。课程作业量级用 create_all 足够；结构变更走 sql/ 下的迁移脚本。"""
    from app import models_registry  # noqa: F401  确保所有模型完成注册

    Base.metadata.create_all(bind=engine)
