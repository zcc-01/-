"""建表 + 灌种子数据（幂等）。

用法：
    python scripts/seed.py          # 空库才灌，重复执行安全
    python scripts/seed.py --reset  # 删库重建（仅开发环境）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.base import Base, engine, init_db  # noqa: E402
from app.db.seed import DEMO_PASSWORD, seed_if_empty  # noqa: E402


def main() -> None:
    if "--reset" in sys.argv:
        if not settings.is_sqlite:
            raise SystemExit("--reset 仅支持 SQLite 开发库，拒绝在 MySQL 上执行")
        Base.metadata.drop_all(bind=engine)
        print("已清空开发库")

    init_db()
    created = seed_if_empty()
    if created:
        print(f"种子数据写入完成，演示口令：{DEMO_PASSWORD}")
        print("  farmer01 / operator01 / regulator01 / consumer01")
        print("  演示溯源码：PC-20251101-DEMO-ITM0001")
    else:
        print("库中已有数据，跳过种子写入（需要重建请加 --reset）")


if __name__ == "__main__":
    main()
