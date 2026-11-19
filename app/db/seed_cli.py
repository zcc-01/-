"""建库 + 种子数据的一次性入口（供 bootstrap.sh 与 CI 调用）。

单独做一个 CLI 而不是在 bootstrap.sh 里写 `python -c "..."`，
原因是 `-c` 里写多行逻辑在 Windows / Git Bash 下的引号转义极易出错。
"""

from __future__ import annotations

import logging

from app.db.base import SessionLocal, init_db
from app.db.seed import seed_if_empty
from app.ops.alert_service import AlertService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> None:
    init_db()
    created = seed_if_empty()
    db = SessionLocal()
    try:
        rules = AlertService.ensure_rules(db)
    finally:
        db.close()
    print(f"数据库初始化完成：种子数据{'已写入' if created else '已存在（跳过）'}，新增预警规则 {rules} 条")


if __name__ == "__main__":
    main()
