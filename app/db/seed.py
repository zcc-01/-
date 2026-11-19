"""演示种子数据（幂等）。

为什么种子数据要写在代码里而不是 SQL 文件：
`bootstrap.sh` 与 CI 都要能用同一份数据，SQL 文件在 SQLite / MySQL 间的日期函数、
自增语法并不通用；用 ORM + 幂等判断，两边都能跑。

种子内容对应报告里要截图的两个页面：
- 认证审核工作台：1 条待审（含一份即将过期的检测报告）、1 条已通过、1 条已驳回；
- 预警列表：三种级别 + 三种状态各有样本，便于演示排序与处置流转。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import Farm, Role, User
from app.core.ids import new_id
from app.core.security import hash_password
from app.db.base import SessionLocal
from app.ops.models import (
    Alert,
    AlertLevel,
    AlertStatus,
    CertApplication,
    CertStatus,
)

logger = logging.getLogger("agritrace.seed")

DEMO_PASSWORD = "Passw0rd!"

DEMO_USERS = (
    ("farmer01", "绿野家庭农场", Role.FARMER, "13800000001"),
    ("farmer02", "青山合作社", Role.FARMER, "13800000002"),
    ("consumer01", "体验买家", Role.CONSUMER, "13900000001"),
    ("operator01", "平台运营小李", Role.OPERATOR, "13700000001"),
    ("regulator01", "市场监管局", Role.REGULATOR, "13600000001"),
)


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC).replace(tzinfo=None)


def seed_if_empty() -> bool:
    """已有用户则视为已初始化，直接返回 False（保证幂等）。"""
    db: Session = SessionLocal()
    try:
        if db.scalar(select(User).limit(1)) is not None:
            return False

        users: dict[str, User] = {}
        for username, display_name, role, phone in DEMO_USERS:
            user = User(
                user_id=new_id("USR"),
                username=username,
                password_hash=hash_password(DEMO_PASSWORD),
                phone=phone,
                display_name=display_name,
                role=role,
                status="active",
            )
            db.add(user)
            users[username] = user
        db.flush()

        farms = _seed_farms(db, users)
        _seed_certs(db, users, farms)
        _seed_alerts(db)

        db.commit()
        logger.info("种子数据写入完成：%s 个账号，%s 个产地", len(users), len(farms))
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _seed_farms(db: Session, users: dict[str, User]) -> dict[str, Farm]:
    farms = {
        "apple": Farm(
            farm_id=new_id("FRM"),
            farm_name="洛川红富士苹果园",
            owner_id=users["farmer01"].user_id,
            province="陕西省",
            city="延安市",
            address="洛川县凤栖镇",
            area_mu=120.0,
            cert_status="approved",
        ),
        "tea": Farm(
            farm_id=new_id("FRM"),
            farm_name="武夷山生态茶园",
            owner_id=users["farmer01"].user_id,
            province="福建省",
            city="南平市",
            address="武夷山市星村镇",
            area_mu=68.5,
            cert_status="pending",
        ),
        "rice": Farm(
            farm_id=new_id("FRM"),
            farm_name="五常稻花香基地",
            owner_id=users["farmer02"].user_id,
            province="黑龙江省",
            city="哈尔滨市",
            address="五常市民乐乡",
            area_mu=300.0,
            cert_status="pending",
        ),
    }
    for farm in farms.values():
        db.add(farm)
    db.flush()
    return farms


def _seed_certs(db: Session, users: dict[str, User], farms: dict[str, Farm]) -> None:
    today = date.today()

    # 1) 已通过：有效期一年，用于演示「到期前 30 天提醒」与认证标识
    approved = CertApplication(
        cert_id=new_id("CER"),
        cert_no=f"CERT-{today:%Y%m%d}-AP0001",
        farm_id=farms["apple"].farm_id,
        applicant_id=users["farmer01"].user_id,
        crop_scope="红富士苹果",
        status=CertStatus.APPROVED,
        land_proof_no="陕农地证（2021）第 0731 号",
        land_proof_url="/uploads/land/apple.pdf",
        test_report_no="SX-JC-2026-0412",
        test_report_at=today - timedelta(days=40),
        test_report_url="/uploads/report/apple.pdf",
        commitment_signed=True,
        submitted_at=_now() - timedelta(days=12),
        reviewer_id=users["operator01"].user_id,
        reviewer_name=users["operator01"].display_name,
        review_opinion="材料齐全，现场核查一致，同意授予产地认证",
        reviewed_at=_now() - timedelta(days=10),
        valid_from=today - timedelta(days=10),
        valid_to=today + timedelta(days=355),
        remind_at=today + timedelta(days=325),
    )

    # 2) 待审：检测报告已过 11 个月，逼近 12 个月有效期，演示审核页的「报告过期」标红
    pending = CertApplication(
        cert_id=new_id("CER"),
        cert_no=f"CERT-{today:%Y%m%d}-PN0001",
        farm_id=farms["tea"].farm_id,
        applicant_id=users["farmer01"].user_id,
        crop_scope="武夷岩茶（水仙、肉桂）",
        status=CertStatus.SUBMITTED,
        land_proof_no="闽农地证（2022）第 1188 号",
        land_proof_url="/uploads/land/tea.pdf",
        test_report_no="FJ-JC-2025-1103",
        test_report_at=today - timedelta(days=300),
        test_report_url="/uploads/report/tea.pdf",
        commitment_signed=True,
        submitted_at=_now() - timedelta(days=4),
    )

    # 3) 已驳回：演示结论不可修改
    rejected = CertApplication(
        cert_id=new_id("CER"),
        cert_no=f"CERT-{today:%Y%m%d}-RJ0001",
        farm_id=farms["rice"].farm_id,
        applicant_id=users["farmer02"].user_id,
        crop_scope="稻花香二号",
        status=CertStatus.REJECTED,
        land_proof_no="黑农地证（2020）第 0456 号",
        test_report_no="HLJ-JC-2024-0917",
        test_report_at=today - timedelta(days=500),
        commitment_signed=False,
        submitted_at=_now() - timedelta(days=20),
        reviewer_id=users["operator01"].user_id,
        reviewer_name=users["operator01"].display_name,
        review_opinion="检测报告超出有效期，且未签署质量安全承诺书，材料不完整",
        reject_reason="检测报告超出有效期",
        reviewed_at=_now() - timedelta(days=18),
    )

    for cert in (approved, pending, rejected):
        db.add(cert)


def _seed_alerts(db: Session) -> None:
    now = _now()
    samples = (
        (
            "R-03",
            AlertLevel.CRITICAL,
            AlertStatus.OPEN,
            "batch",
            "BAT-DEMO-0001",
            "批次 PC-20261101-8F2A 溯源链在第 3 条记录处断裂",
            {"broken_at": 3, "batch_no": "PC-20261101-8F2A", "simulated": True},
            None,
            None,
            None,
        ),
        (
            "R-01",
            AlertLevel.WARNING,
            AlertStatus.ACKNOWLEDGED,
            "batch",
            "BAT-DEMO-0002",
            "批次 PC-20261020-1C7B 采收后 7 天无物流记录",
            {"days": 7, "batch_no": "PC-20261020-1C7B", "simulated": True},
            "operator01",
            "平台运营小李",
            "已于 11 月 12 日电话联系农户补录物流信息",
        ),
        (
            "R-02",
            AlertLevel.INFO,
            AlertStatus.CLOSED,
            "batch",
            "BAT-DEMO-0003",
            "批次 PC-20261015-5D9E 在 24 小时内于 3 个省份被扫码",
            {"provinces": ["广东省", "湖南省", "江西省"], "simulated": True},
            "operator01",
            "平台运营小李",
            "核实为同一台设备随货车跨省运输，非窜货，工单关闭",
        ),
    )
    for (
        code,
        level,
        status,
        target_type,
        target_id,
        title,
        detail,
        handler,
        handler_name,
        resolution,
    ) in samples:
        db.add(
            Alert(
                alert_id=new_id("ALT"),
                rule_code=code,
                level=level,
                status=status,
                target_type=target_type,
                target_id=target_id,
                title=title,
                detail=detail,
                dedupe_key=f"{code}|{target_type}|{target_id}|seed",
                handled_by=handler,
                handled_by_name=handler_name,
                handled_at=now - timedelta(days=1) if handler else None,
                resolution=resolution,
            )
        )
