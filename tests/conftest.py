"""pytest 公共夹具：内存库 + 最小种子数据 + 三种角色的令牌。

测试不触碰 data/app.db：通过依赖覆盖把 get_session 换成内存 SQLite，
并且不使用 TestClient 的上下文管理器，避免触发 lifespan 里的真实建库与种子。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models_registry  # noqa: F401  注册所有表
from app.common.models import Farm, Role, User
from app.core.deps import get_session
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.main import app as fastapi_app

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def _fresh_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def seed(db):
    farmer = User(
        username="farmer01",
        password_hash=hash_password("Passw0rd!"),
        display_name="张建国",
        role=Role.FARMER,
    )
    operator = User(
        username="operator01",
        password_hash=hash_password("Passw0rd!"),
        display_name="平台运营",
        role=Role.OPERATOR,
    )
    consumer = User(
        username="consumer01",
        password_hash=hash_password("Passw0rd!"),
        display_name="李小明",
        role=Role.CONSUMER,
    )
    db.add_all([farmer, operator, consumer])
    db.flush()

    farm = Farm(
        farm_name="青禾家庭农场",
        owner_id=farmer.user_id,
        province="浙江省",
        city="杭州市",
        address="临安区新峰村",
        cert_status="approved",
    )
    db.add(farm)
    db.commit()

    def headers(user: User) -> dict[str, str]:
        token = create_access_token(user_id=user.user_id, username=user.username, role=user.role)
        return {"Authorization": f"Bearer {token}"}

    return SimpleNamespace(
        farmer=farmer,
        operator=operator,
        consumer=consumer,
        farm=farm,
        farmer_headers=headers(farmer),
        operator_headers=headers(operator),
        consumer_headers=headers(consumer),
    )


@pytest.fixture()
def client(seed):
    def _override():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_session] = _override
    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def batch_payload(seed):
    return {
        "farm_id": seed.farm.farm_id,
        "crop_name": "临安山核桃",
        "plot_no": "A-03",
        "plant_date": "2026-03-20",
        "harvest_date": "2026-09-05",
        "expected_yield": 2600,
    }


def create_batch(db, seed, *, status: str | None = None, plot_no: str = "A-03",
                 crop_name: str = "临安山核桃"):
    """直接走 Service 建档，必要时把状态推到指定值（绕过接口只为测试准备数据）。"""
    from app.trace.schemas import BatchCreate
    from app.trace.service import TraceService

    payload = BatchCreate(
        farm_id=seed.farm.farm_id,
        crop_name=crop_name,
        plot_no=plot_no,
        plant_date="2026-03-20",
        harvest_date="2026-09-05",
        expected_yield=2600,
    )
    batch = TraceService.create_batch(db, payload, seed.farmer)
    if status and status != "draft":
        batch.status = status
        db.commit()
        db.refresh(batch)
    return batch
