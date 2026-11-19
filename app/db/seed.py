"""种子数据：给评审者一个「打开就能演示」的初始状态。

账号（口令统一 Passw0rd!）：
    farmer01    农户，拥有「青禾家庭农场」（已认证）与「云岭生态茶园」（待认证）
    operator01  平台运营
    regulator01 监管人员
    consumer01  消费者

三域数据一次灌齐，保证 clone 后能演示完整闭环：
- 溯源域：2 个批次、8 条环节记录、13 个溯源码，链校验必然通过
- 交易域：3 个商品（含已认证/未认证对比）、购物车、待支付订单、已收货订单、1 条评价
- 运营域：认证申请（1 通过 / 1 待审）、1 张时间倒挂预警单、审核轨迹

设计取舍：刻意让「云岭生态茶园」处于待认证、其商品未带认证标识，
这样检索页的「只看已认证」筛选、商品详情的认证徽标、认证审核流程
都能用真实数据演示，而不是靠临时造数据。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.security import hash_password
from app.db.base import SessionLocal
from app.ops.models import (
    Alert,
    AlertLevel,
    AlertStatus,
    AlertType,
    CertApplication,
    CertStatus,
    ReviewLog,
)
from app.trace.chain import HashChain, canonical_detail
from app.trace.models import Batch, BatchStatus, CodeType, Stage, TraceCode, TraceEvent
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

DEMO_PASSWORD = "Passw0rd!"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _append_chain(
    session: Session,
    batch: Batch,
    events: list[tuple[str, str, str, datetime]],
    operator_id: str,
) -> None:
    """按顺序把环节记录串进哈希链，并同步批次链尾锚点。"""
    prev_hash = HashChain.genesis_prev_hash(batch.batch_id)
    for seq_no, (stage, detail_text, location, occurred) in enumerate(events, start=1):
        detail = canonical_detail({"note": detail_text})
        curr_hash = HashChain.compute_hash(
            prev_hash=prev_hash,
            batch_id=batch.batch_id,
            seq_no=seq_no,
            stage=stage,
            operator_id=operator_id,
            occurred_at=occurred,
            detail=detail,
        )
        session.add(
            TraceEvent(
                batch_id=batch.batch_id,
                seq_no=seq_no,
                stage=stage,
                operator_id=operator_id,
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


def seed_if_empty(db: Session | None = None) -> bool:
    """空库才灌数据，重复执行安全。返回是否真的写入了。"""
    own_session = db is None
    session = db or SessionLocal()
    try:
        if session.scalar(select(User).limit(1)) is not None:
            return False

        # ============================================================ 用户
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

        # ============================================================ 产地
        farm_ok = Farm(
            farm_name="青禾家庭农场",
            owner_id=farmer.user_id,
            province="浙江省",
            city="杭州市",
            address="临安区清凉峰镇新峰村 3 组",
            area_mu=120,
            cert_status="approved",
        )
        farm_pending = Farm(
            farm_name="云岭生态茶园",
            owner_id=farmer.user_id,
            province="浙江省",
            city="杭州市",
            address="余杭区径山镇长乐村 12 号",
            area_mu=68,
            cert_status="pending",
        )
        session.add_all([farm_ok, farm_pending])
        session.flush()

        # ============================================================ 批次
        batch_main = Batch(
            batch_no="PC-20251101-DEMO",
            farm_id=farm_ok.farm_id,
            crop_name="临安山核桃",
            plot_no="A-03",
            plant_date=date(2026, 3, 20),
            harvest_date=date(2026, 9, 5),
            expected_yield=2600,
            status=BatchStatus.QUALIFIED,
        )
        batch_tea = Batch(
            batch_no="PC-20251101-TEA",
            farm_id=farm_pending.farm_id,
            crop_name="径山云雾茶",
            plot_no="B-07",
            plant_date=date(2026, 4, 2),
            harvest_date=date(2026, 9, 28),
            expected_yield=520,
            status=BatchStatus.QUALIFIED,
        )
        session.add_all([batch_main, batch_tea])
        session.flush()

        _append_chain(
            session,
            batch_main,
            [
                (
                    Stage.PRODUCTION,
                    "播种育苗，施用有机肥 40kg/亩",
                    "生产基地 A-03 地块",
                    datetime(2026, 3, 20, 8, 30),
                ),
                (
                    Stage.PROCESSING,
                    "自然晾晒 72 小时后低温烘干",
                    "初加工车间",
                    datetime(2026, 9, 7, 9, 0),
                ),
                (
                    Stage.QUALITY,
                    "农残快检 32 项全部合格，报告编号 ZJ-2026-0912",
                    "临安区农检中心",
                    datetime(2026, 9, 12, 14, 20),
                ),
                (
                    Stage.LOGISTICS,
                    "冷链发运，顺丰冷运 SF7788****",
                    "杭州中转仓",
                    datetime(2026, 9, 14, 7, 15),
                ),
                (
                    Stage.SALES,
                    "产地直供上架，批次可溯源",
                    "青禾农场直供店",
                    datetime(2026, 9, 15, 10, 0),
                ),
            ],
            farmer.user_id,
        )
        _append_chain(
            session,
            batch_tea,
            [
                (
                    Stage.PRODUCTION,
                    "明前采摘，一芽一叶手工摊青 6 小时",
                    "茶园 B-07 地块",
                    datetime(2026, 4, 2, 6, 30),
                ),
                (
                    Stage.PROCESSING,
                    "杀青、揉捻、初烘，全程不落地",
                    "云岭初制所",
                    datetime(2026, 9, 28, 11, 0),
                ),
                (
                    Stage.QUALITY,
                    "感官审评一级，水分 4.8%",
                    "余杭区茶叶质检站",
                    datetime(2026, 9, 30, 15, 40),
                ),
            ],
            farmer.user_id,
        )

        # 溯源码：主批次 10 个单品码 + 2 个箱码，茶叶批次 1 个箱码
        for item_no in range(1, 11):
            session.add(
                TraceCode(
                    code_value=f"{batch_main.batch_no}-ITM{item_no:04d}",
                    code_type=CodeType.ITEM,
                    batch_id=batch_main.batch_id,
                    item_no=item_no,
                )
            )
        for box_no in range(1, 3):
            session.add(
                TraceCode(
                    code_value=f"{batch_main.batch_no}-BOX{box_no:04d}",
                    code_type=CodeType.BOX,
                    batch_id=batch_main.batch_id,
                    item_no=box_no,
                )
            )
        session.add(
            TraceCode(
                code_value=f"{batch_tea.batch_no}-BOX0001",
                code_type=CodeType.BOX,
                batch_id=batch_tea.batch_id,
                item_no=1,
            )
        )

        # ============================================================ 商品
        p_walnut = Product(
            batch_id=batch_main.batch_id,
            farm_id=farm_ok.farm_id,
            name="临安山核桃 500g",
            category="坚果炒货",
            price_cents=5980,
            stock=200,
            unit="袋",
            status=ProductStatus.ON_SALE,
            certified=1,
            description="临安清凉峰产区，手剥小核桃，低温烘干不加油，2026 年新货。",
            sales_count=12,
        )
        p_gift = Product(
            batch_id=batch_main.batch_id,
            farm_id=farm_ok.farm_id,
            name="临安山核桃礼盒 1kg",
            category="坚果炒货",
            price_cents=11800,
            stock=80,
            unit="盒",
            status=ProductStatus.ON_SALE,
            certified=1,
            description="双罐礼盒装，含检测报告卡，适合节日送礼。",
            sales_count=5,
        )
        p_tea = Product(
            batch_id=batch_tea.batch_id,
            farm_id=farm_pending.farm_id,
            name="径山云雾茶 100g",
            category="茶叶",
            price_cents=8800,
            stock=50,
            unit="罐",
            status=ProductStatus.ON_SALE,
            certified=0,  # 产地认证审核中，故意不带认证标识
            description="明前一芽一叶，手工摊青，香气清高。产地认证审核中。",
            sales_count=3,
        )
        p_bamboo = Product(
            batch_id=batch_main.batch_id,
            farm_id=farm_ok.farm_id,
            name="临安天目笋干 250g",
            category="干货",
            price_cents=3680,
            stock=120,
            unit="袋",
            status=ProductStatus.ON_SALE,
            certified=1,
            description="天目山春笋炭火烘焙，无硫熏，泡发后炖汤鲜甜。",
            sales_count=8,
        )
        p_black_tea = Product(
            batch_id=batch_tea.batch_id,
            farm_id=farm_pending.farm_id,
            name="径山红茶 100g",
            category="茶叶",
            price_cents=9800,
            stock=40,
            unit="罐",
            status=ProductStatus.ON_SALE,
            certified=0,
            description="径山群体种一芽二叶发酵，蜜香明显，产地认证审核中。",
            sales_count=2,
        )
        session.add_all([p_walnut, p_gift, p_tea, p_bamboo, p_black_tea])
        session.flush()

        # ============================================================ 购物车
        session.add(CartItem(user_id=consumer.user_id, product_id=p_tea.product_id, quantity=2))

        # ============================================================ 订单
        # 已收货订单：用于演示「确认收货后可评价」
        order_done = Order(
            order_no="SO-20251101-DEMO1",
            user_id=consumer.user_id,
            farmer_id=farm_ok.farm_id,
            total_cents=p_walnut.price_cents * 2,
            status=OrderStatus.RECEIVED,
            receiver_name="李小明",
            receiver_phone="13800000004",
            address="浙江省杭州市西湖区文三路 100 号 3 幢 502",
            remark="请放快递柜",
            paid_at=_now() - timedelta(days=5),
            shipped_at=_now() - timedelta(days=4),
            received_at=_now() - timedelta(days=2),
        )
        # 待支付订单：用于演示支付与超时取消
        order_pending = Order(
            order_no="SO-20251101-DEMO2",
            user_id=consumer.user_id,
            farmer_id=farm_ok.farm_id,
            total_cents=p_gift.price_cents,
            status=OrderStatus.PENDING,
            receiver_name="李小明",
            receiver_phone="13800000004",
            address="浙江省杭州市西湖区文三路 100 号 3 幢 502",
            remark="",
            expire_at=_now() + timedelta(minutes=30),
        )
        session.add_all([order_done, order_pending])
        session.flush()

        session.add_all(
            [
                OrderItem(
                    order_id=order_done.order_id,
                    product_id=p_walnut.product_id,
                    product_name=p_walnut.name,
                    price_cents=p_walnut.price_cents,
                    quantity=2,
                    subtotal_cents=p_walnut.price_cents * 2,
                ),
                OrderItem(
                    order_id=order_pending.order_id,
                    product_id=p_gift.product_id,
                    product_name=p_gift.name,
                    price_cents=p_gift.price_cents,
                    quantity=1,
                    subtotal_cents=p_gift.price_cents,
                ),
            ]
        )
        session.add(
            Payment(
                order_id=order_done.order_id,
                user_id=consumer.user_id,
                amount_cents=order_done.total_cents,
                channel="mock",
                status=PayStatus.SUCCESS,
                transaction_no="MOCK20251101000001DEMO",
                paid_at=order_done.paid_at,
            )
        )
        session.add(
            Review(
                order_id=order_done.order_id,
                product_id=p_walnut.product_id,
                user_id=consumer.user_id,
                rating=5,
                content="颗粒饱满，壳薄好剥，扫码能看到整条生产记录，踏实。",
                display_name="李**",
            )
        )

        # ============================================================ 认证申请
        app_approved = CertApplication(
            app_no="CA-20251001-DEMO1",
            farm_id=farm_ok.farm_id,
            applicant_id=farmer.user_id,
            status=CertStatus.APPROVED,
            materials=json.dumps(
                {"土地流转合同": "LZ-2024-0387.pdf", "检测报告": "ZJ-2026-0912.pdf"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            land_cert_no="LZ-2024-0387",
            report_no="ZJ-2026-0912",
            report_expire_at=_now() + timedelta(days=200),
            commitment_signed=1,
            submitted_at=_now() - timedelta(days=40),
            reviewed_at=_now() - timedelta(days=38),
            reviewer_id=operator.user_id,
            review_comment="材料齐全，现场核查通过。",
            valid_until=_now() + timedelta(days=365 - 38),
        )
        app_pending = CertApplication(
            app_no="CA-20251115-DEMO2",
            farm_id=farm_pending.farm_id,
            applicant_id=farmer.user_id,
            status=CertStatus.PENDING,
            materials=json.dumps(
                {"土地流转合同": "LZ-2025-1102.pdf", "检测报告": "YH-2026-0930.pdf"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            land_cert_no="LZ-2025-1102",
            report_no="YH-2026-0930",
            report_expire_at=_now() + timedelta(days=300),
            commitment_signed=1,
            submitted_at=_now() - timedelta(days=3),
        )
        session.add_all([app_approved, app_pending])
        session.flush()

        session.add_all(
            [
                ReviewLog(
                    app_id=app_approved.app_id,
                    reviewer_id=farmer.user_id,
                    action="submit",
                    from_status="",
                    to_status=CertStatus.PENDING,
                    comment="",
                ),
                ReviewLog(
                    app_id=app_approved.app_id,
                    reviewer_id=operator.user_id,
                    action="approve",
                    from_status=CertStatus.PENDING,
                    to_status=CertStatus.APPROVED,
                    comment="材料齐全，现场核查通过。",
                ),
                ReviewLog(
                    app_id=app_pending.app_id,
                    reviewer_id=farmer.user_id,
                    action="submit",
                    from_status="",
                    to_status=CertStatus.PENDING,
                    comment="",
                ),
            ]
        )

        # ============================================================ 预警单
        # 一张时间倒挂预警：演示运营侧的处置流程，且不会干扰主批次扫码的正常展示
        session.add(
            Alert(
                alert_no="AL-20251116-DEMO1",
                alert_type=AlertType.TIME_INVERSION,
                level=AlertLevel.WARN,
                status=AlertStatus.OPEN,
                batch_id=batch_tea.batch_id,
                farm_id=farm_pending.farm_id,
                title=f"批次 {batch_tea.batch_no} 环节时间倒挂",
                detail=json.dumps(
                    {
                        "batch_id": batch_tea.batch_id,
                        "stage": Stage.SALES,
                        "occurred_at": "2026-09-20 09:00:00",
                        "prev_stage": Stage.QUALITY,
                        "prev_time": "2026-09-30 15:40:00",
                        "hint": "销售环节时间早于质检环节，请运营核实是否为补录",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                dedup_key=f"{AlertType.TIME_INVERSION}:{batch_tea.batch_id}",
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
