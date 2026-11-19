"""种子数据：给评审者一个「打开就能演示」的初始状态。

账号（口令统一 Passw0rd!）：
    farmer01   农户，拥有「青禾家庭农场」
    operator01 平台运营
    regulator01 监管人员
    consumer01  消费者

数据：1 个批次 + 5 条完整环节记录 + 10 个单品码，链校验必然通过。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.security import hash_password
from app.db.base import SessionLocal
from app.trace.chain import HashChain, canonical_detail
from app.trace.models import Batch, BatchStatus, CodeType, Stage, TraceCode, TraceEvent

DEMO_PASSWORD = "Passw0rd!"


def seed_if_empty(db: Session | None = None) -> bool:
    """空库才灌数据，重复执行安全。返回是否真的写入了。"""
    own_session = db is None
    session = db or SessionLocal()
    try:
        if session.scalar(select(User).limit(1)) is not None:
            return False

        farmer = User(
            username="farmer01",
            password_hash=hash_password(DEMO_PASSWORD),
            phone="13800000001",
            display_name="张建国",
            role=Role.FARMER,
        )
        operator = User(
            username="operator01",
            password_hash=hash_password(DEMO_PASSWORD),
            phone="13800000002",
            display_name="平台运营小李",
            role=Role.OPERATOR,
        )
        regulator = User(
            username="regulator01",
            password_hash=hash_password(DEMO_PASSWORD),
            phone="13800000003",
            display_name="监管员老王",
            role=Role.REGULATOR,
        )
        consumer = User(
            username="consumer01",
            password_hash=hash_password(DEMO_PASSWORD),
            phone="13800000004",
            display_name="李小明",
            role=Role.CONSUMER,
        )
        session.add_all([farmer, operator, regulator, consumer])
        session.flush()

        farm = Farm(
            farm_name="青禾家庭农场",
            owner_id=farmer.user_id,
            province="浙江省",
            city="杭州市",
            address="临安区清凉峰镇新峰村 3 组",
            area_mu=120,
            cert_status="approved",
        )
        session.add(farm)
        session.flush()

        batch = Batch(
            batch_no="PC-20251101-DEMO",
            farm_id=farm.farm_id,
            crop_name="临安山核桃",
            plot_no="A-03",
            plant_date=date(2026, 3, 20),
            harvest_date=date(2026, 9, 5),
            expected_yield=2600,
            status=BatchStatus.QUALIFIED,
        )
        session.add(batch)
        session.flush()

        events = [
            (
                Stage.PRODUCTION,
                "播种育苗，施用有机肥 40kg/亩",
                "生产基地 A-03 地块",
                datetime(2026, 3, 20, 8, 30),
            ),
            (Stage.PROCESSING, "自然晾晒 72 小时后低温烘干", "初加工车间", datetime(2026, 9, 7, 9, 0)),
            (
                Stage.QUALITY,
                "农残快检 32 项全部合格，报告编号 ZJ-2026-0912",
                "临安区农检中心",
                datetime(2026, 9, 12, 14, 20),
            ),
            (Stage.LOGISTICS, "冷链发运，顺丰冷运 SF7788****", "杭州中转仓", datetime(2026, 9, 14, 7, 15)),
            (Stage.SALES, "产地直供上架，批次可溯源", "青禾农场直供店", datetime(2026, 9, 15, 10, 0)),
        ]

        prev_hash = HashChain.genesis_prev_hash(batch.batch_id)
        for seq_no, (stage, detail_text, location, occurred) in enumerate(events, start=1):
            detail = canonical_detail({"note": detail_text})
            curr_hash = HashChain.compute_hash(
                prev_hash=prev_hash,
                batch_id=batch.batch_id,
                seq_no=seq_no,
                stage=stage,
                operator_id=farmer.user_id,
                occurred_at=occurred,
                detail=detail,
            )
            session.add(
                TraceEvent(
                    batch_id=batch.batch_id,
                    seq_no=seq_no,
                    stage=stage,
                    operator_id=farmer.user_id,
                    occurred_at=occurred,
                    location=location,
                    detail=detail,
                    prev_hash=prev_hash,
                    curr_hash=curr_hash,
                )
            )
            prev_hash = curr_hash

        batch.event_count = len(events)
        batch.last_event_hash = prev_hash

        for item_no in range(1, 11):
            session.add(
                TraceCode(
                    code_value=f"{batch.batch_no}-ITM{item_no:04d}",
                    code_type=CodeType.ITEM,
                    batch_id=batch.batch_id,
                    item_no=item_no,
                )
            )

        session.commit()
        return True
    finally:
        if own_session:
            session.close()


if __name__ == "__main__":  # pragma: no cover
    from app.db.base import init_db

    init_db()
    print("seeded" if seed_if_empty() else "already seeded")
