"""UC-02 录入溯源环节的接口测试。"""

from __future__ import annotations

from sqlalchemy import select

from app.trace.models import TraceEvent
from tests.conftest import create_batch


def test_append_event_links_chain(client, seed, db):
    batch = create_batch(db, seed)
    resp = client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={
            "stage": "processing",
            "occurred_at": "2026-11-20T09:00:00",
            "location": "初加工车间",
            "detail": {"note": "自然晾晒 72 小时", "temperature": 26},
        },
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["event"]["seq_no"] == 2
    assert data["warning"] is None

    events = db.scalars(
        select(TraceEvent).where(TraceEvent.batch_id == batch.batch_id).order_by(TraceEvent.seq_no)
    ).all()
    assert len(events) == 2
    assert events[1].prev_hash == events[0].curr_hash
    assert events[1].curr_hash != events[0].curr_hash


def test_inverted_time_returns_warning_but_still_records(client, seed, db):
    """UC-14 扩展：时间倒挂不阻断录入，但返回预警提示。"""
    batch = create_batch(db, seed)
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "processing", "occurred_at": "2026-11-01T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    resp = client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "quality", "occurred_at": "2026-10-01T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200
    assert "早于上一条记录" in resp.json()["data"]["warning"]


def test_closed_batch_rejects_new_event(client, seed, db):
    batch = create_batch(db, seed, status="qualified")
    client.patch(
        f"/api/batches/{batch.batch_id}/status",
        json={"status": "closed"},
        headers=seed.farmer_headers,
    )
    resp = client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "logistics", "detail": {}},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 42201


def test_invalid_stage_rejected(client, seed, db):
    batch = create_batch(db, seed)
    resp = client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "unknown_stage", "detail": {}},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 422


def test_event_list_is_ordered(client, seed, db):
    batch = create_batch(db, seed)
    for stage in ("processing", "quality", "logistics"):
        client.post(
            f"/api/batches/{batch.batch_id}/events",
            json={"stage": stage, "detail": {}},
            headers=seed.farmer_headers,
        )
    resp = client.get(f"/api/batches/{batch.batch_id}/events", headers=seed.operator_headers)
    seqs = [item["seq_no"] for item in resp.json()["data"]]
    assert seqs == [1, 2, 3, 4]
