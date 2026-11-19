"""轻量结构迁移（课程作业版 Alembic）。

为什么不用 Alembic：项目用 `create_all` 建表，评审者拿到的是「clone 就能跑」的仓库，
引入迁移框架会多出 `alembic upgrade head` 这一步。但 `create_all` 只建缺失的表，
**不会给已有表补字段**——所以升级到「品类 + 商品属性」版本时，老库会缺列。

这里做最小可用版本：按声明式清单检查缺列并 `ALTER TABLE ADD COLUMN`，
只增不删、幂等、可重复执行。生产环境请换成真正的迁移工具（报错可回滚、有版本号）。
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text

logger = logging.getLogger(__name__)

# 表名 → [(列名, 列定义)]，列定义必须带默认值，SQLite/MySQL 都接受这种写法
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "product": [
        ("category_id", "VARCHAR(32)"),
        ("spec", "VARCHAR(32) NOT NULL DEFAULT ''"),
        ("grade", "VARCHAR(16) NOT NULL DEFAULT ''"),
        ("origin", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ("shelf_life", "VARCHAR(32) NOT NULL DEFAULT ''"),
        ("storage", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ("image_url", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("tags", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("is_recommended", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def ensure_columns(engine: Engine) -> list[str]:
    """补齐缺失列，返回本次实际新增的「表.列」列表（便于启动日志与测试断言）。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    applied: list[str] = []

    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            applied.append(f"{table}.{name}")
            logger.info("结构迁移：%s 新增列 %s", table, name)

    if applied and engine.dialect.name == "sqlite":
        # SQLite 建表时索引跟着表走，补列后要手工补索引；MySQL 由 sql/schema_mysql.sql 声明
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_product_category_id "
                    "ON product (category_id)"
                )
            )
    return applied
