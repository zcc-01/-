"""pytest 公共装置。

两个要点：
1. 数据库在**导入 app 之前**用环境变量指到临时文件 —— app.db.base 在 import 时就建 engine，
   晚一步设 DATABASE_URL 就改不动了（这是最常见的踩坑点）。
2. 每个用例前 drop / create / seed，用例之间互不污染；种子数据本身是幂等的，
   所以「先 drop 再 seed」等于拿到一份干净且贴近演示环境的初始态。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="agritrace-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["JWT_SECRET"] = "test-only-secret-key-with-enough-entropy-32bytes"
os.environ["DEV_MODE"] = "true"
os.environ["ALERT_ENABLE_SCHEDULER"] = "false"
os.environ["APP_ENV"] = "test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models_registry  # noqa: E402,F401  注册全部模型
from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.db.seed import seed_if_empty  # noqa: E402
from app.main import app  # noqa: E402
from app.ops.alert_service import AlertService  # noqa: E402

DEMO_PASSWORD = "Passw0rd!"


@pytest.fixture(scope="function", autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    seed_if_empty()
    db = SessionLocal()
    try:
        AlertService.ensure_rules(db)
    finally:
        db.close()
    yield


@pytest.fixture()
def client(fresh_db):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def login(client: TestClient, username: str, password: str = DEMO_PASSWORD) -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def farmer_token(client):
    return login(client, "farmer01")


@pytest.fixture()
def operator_token(client):
    return login(client, "operator01")


@pytest.fixture()
def regulator_token(client):
    return login(client, "regulator01")


@pytest.fixture()
def consumer_token(client):
    return login(client, "consumer01")
