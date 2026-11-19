"""UC-05 溯源链完整性校验的接口测试。"""

from __future__ import annotations

from sqlalchemy import text

from tests.conftest import create_batch


def test_operator_can_verify_chain(client, seed, db):
    batch = create_batch(db, seed)
    resp = client.get(f"/api/batches/{batch.batch_id}/verify", headers=seed.operator_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True
    assert data["total"] == 1
    assert data["reason"] is None


def test_farmer_cannot_call_verify(client, seed, db):
    """UC-05 的主要参与者是平台运营，农户越权返回 403。"""
    batch = create_batch(db, seed)
    resp = client.get(f"/api/batches/{batch.batch_id}/verify", headers=seed.farmer_headers)
    assert resp.status_code == 403


def test_verify_locates_tampered_record(client, seed, db):
    batch = create_batch(db, seed)
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "quality", "detail": {"report_no": "ZJ-2026-0912"}},
        headers=seed.farmer_headers,
    )
    db.execute(
        text(
            "UPDATE trace_event SET detail = :d WHERE batch_id = :b AND seq_no = 2"
        ),
        {"d": '{"report_no":"伪造报告"}', "b": batch.batch_id},
    )
    db.commit()
    db.expire_all()

    data = client.get(f"/api/batches/{batch.batch_id}/verify", headers=seed.operator_headers).json()["data"]
    assert data["ok"] is False
    assert data["broken_at"] == 2
    assert data["reason"] in {"content_tampered", "prev_hash_mismatch"}
    assert data["broken_event_id"]


def test_regulator_can_verify(client, seed, db):
    from app.common.models import User
    from app.core.security import create_access_token, hash_password

    regulator = User(
        username="regulator01",
        password_hash=hash_password("Passw0rd!"),
        display_name="监管员",
        role="regulator",
    )
    db.add(regulator)
    db.commit()
    token = create_access_token(
        user_id=regulator.user_id, username=regulator.username, role=regulator.role
    )
    batch = create_batch(db, seed)
    resp = client.get(
        f"/api/batches/{batch.batch_id}/verify", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is True
