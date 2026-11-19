"""UC-01 创建生产批次的接口测试。"""

from __future__ import annotations

from app.trace.chain import HashChain
from app.trace.service import TraceService
from tests.conftest import create_batch


def test_create_batch_writes_genesis_event(client, seed, batch_payload):
    resp = client.post("/api/batches", json=batch_payload, headers=seed.farmer_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["batch_no"].startswith("PC-")
    assert data["status"] == "draft"
    assert data["event_count"] == 1

    # 创世记录必须与批次同事务落库，且链可校验
    verify = client.get(f"/api/batches/{data['batch_id']}/verify", headers=seed.operator_headers)
    assert verify.json()["data"]["ok"] is True


def test_duplicate_batch_returns_409(client, seed, batch_payload):
    first = client.post("/api/batches", json=batch_payload, headers=seed.farmer_headers)
    assert first.status_code == 200
    second = client.post("/api/batches", json=batch_payload, headers=seed.farmer_headers)
    assert second.status_code == 409
    assert second.json()["code"] == 40901


def test_harvest_before_plant_returns_422(client, seed, batch_payload):
    payload = dict(batch_payload, harvest_date="2026-01-01")
    resp = client.post("/api/batches", json=payload, headers=seed.farmer_headers)
    assert resp.status_code == 422
    assert resp.json()["code"] == 40001


def test_consumer_cannot_create_batch(client, seed, batch_payload):
    resp = client.post("/api/batches", json=batch_payload, headers=seed.consumer_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == 40301


def test_anonymous_cannot_create_batch(client, batch_payload):
    resp = client.post("/api/batches", json=batch_payload)
    assert resp.status_code == 401


def test_batch_list_only_returns_own_farm(client, seed, db, batch_payload):
    batch = create_batch(db, seed)
    resp = client.get("/api/batches", headers=seed.farmer_headers)
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["batch_id"] == batch.batch_id

    # 运营可看到全部批次
    all_batches = client.get("/api/batches", headers=seed.operator_headers)
    assert all_batches.json()["data"]["total"] == 1


def test_status_machine_blocks_backward_transition(client, seed, db):
    batch = create_batch(db, seed, status="harvested")
    resp = client.patch(
        f"/api/batches/{batch.batch_id}/status",
        json={"status": "growing"},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 42201


def test_status_machine_allows_forward_transition(client, seed, db):
    batch = create_batch(db, seed)
    for target in ("growing", "harvested", "qualified"):
        resp = client.patch(
            f"/api/batches/{batch.batch_id}/status",
            json={"status": target},
            headers=seed.farmer_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == target


def test_other_farmer_batch_is_forbidden(client, seed, db):
    """越权访问返回 403（报告 3.6 安全性要求）。"""
    from app.common.models import User
    from app.core.security import create_access_token, hash_password

    other = User(
        username="farmer02",
        password_hash=hash_password("Passw0rd!"),
        display_name="另一个农户",
        role="farmer",
    )
    db.add(other)
    db.commit()
    token = create_access_token(user_id=other.user_id, username=other.username, role=other.role)

    batch = create_batch(db, seed)
    resp = client.get(f"/api/batches/{batch.batch_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_farm_owner_guard(db, seed, batch_payload):
    from app.core.errors import Forbidden

    batch = create_batch(db, seed)
    try:
        TraceService._assert_farm_owner(db, batch, seed.consumer)
    except Forbidden:
        pass
    else:  # pragma: no cover
        raise AssertionError("非产地所有者应被拒绝")


def test_batch_genesis_hash_matches_stored(db, seed):
    batch = create_batch(db, seed)
    events = TraceService.list_events(db, batch.batch_id)
    assert events[0].prev_hash == HashChain.genesis_prev_hash(batch.batch_id)
