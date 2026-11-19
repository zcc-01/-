"""UC-04 扫码溯源查询的接口测试（含匿名访问与日志回流）。"""

from __future__ import annotations

from sqlalchemy import select

from app.trace.models import ScanLog
from tests.conftest import create_batch


def _prepare(client, seed, db, count=3):
    batch = create_batch(db, seed)
    client.patch(
        f"/api/batches/{batch.batch_id}/status",
        json={"status": "harvested"},
        headers=seed.farmer_headers,
    )
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={
            "stage": "quality",
            "occurred_at": "2026-09-12T14:20:00",
            "location": "临安区农检中心",
            "detail": {"report_no": "ZJ-2026-0912", "conclusion": "合格"},
        },
        headers=seed.farmer_headers,
    )
    codes = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": count},
        headers=seed.farmer_headers,
    ).json()["data"]["items"]
    db.refresh(batch)
    return batch, codes[0]["code_value"]


def test_anonymous_scan_returns_timeline(client, seed, db):
    batch, code = _prepare(client, seed, db)
    resp = client.get(f"/api/trace/{code}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["code_value"] == code
    assert data["batch"]["batch_no"] == batch.batch_no
    assert data["farm"]["farm_name"] == "青禾家庭农场"
    assert data["chain"]["ok"] is True
    assert [e["stage"] for e in data["events"]] == ["production", "quality"]
    assert data["events"][1]["detail"]["report_no"] == "ZJ-2026-0912"
    assert data["events"][0]["operator_name"].endswith("*")  # 操作人脱敏


def test_scan_writes_scan_log(client, seed, db):
    _, code = _prepare(client, seed, db, count=1)
    client.get(f"/api/trace/{code}?province=浙江省&city=杭州市", headers={"user-agent": "Mozilla/5.0 Mobile"})

    db.expire_all()
    logs = db.scalars(select(ScanLog).where(ScanLog.code_value == code)).all()
    assert len(logs) == 1
    assert logs[0].province == "浙江省"
    assert logs[0].device == "mobile"


def test_unknown_code_returns_404(client):
    resp = client.get("/api/trace/NOT-EXIST-CODE")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40401
    assert "未查询到" in resp.json()["message"]


def test_scan_page_renders_timeline(client, seed, db):
    _, code = _prepare(client, seed, db, count=1)
    resp = client.get(f"/trace/{code}")
    assert resp.status_code == 200
    assert "生产履历" in resp.text
    assert "链校验" in resp.text


def test_scan_page_shows_broken_chain_warning(client, seed, db):
    """断链时仍展示记录，同时在页面顶部给告警条（报告 5.2.2）。"""
    from sqlalchemy import text

    batch, code = _prepare(client, seed, db, count=1)
    db.execute(
        text("UPDATE trace_event SET detail = :d WHERE batch_id = :b AND seq_no = 2"),
        {"d": '{"report_no":"伪造"}', "b": batch.batch_id},
    )
    db.commit()
    db.expire_all()

    resp = client.get(f"/trace/{code}")
    assert resp.status_code == 200
    assert "存在异常" in resp.text
    assert "质检" in resp.text  # 记录仍然展示


def test_scan_page_handles_missing_code(client):
    resp = client.get("/trace/NOT-EXIST-CODE")
    assert resp.status_code == 404
    assert "未查询到该产品信息" in resp.text
