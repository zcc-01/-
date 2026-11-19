"""UC-11 权限边界：越权 403 + 留痕审计 + 资源级归属校验。"""

from __future__ import annotations

from sqlalchemy import func, select

from app.ops.models import AuditLog
from tests.conftest import auth


def test_anonymous_rejected(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/ops/certs").status_code == 401


def test_consumer_cannot_review_cert(client, consumer_token):
    resp = client.get("/api/ops/certs", headers=auth(consumer_token))
    assert resp.status_code == 403
    assert resp.json()["code"] == 40301


def test_deny_is_audited(client, consumer_token, db):
    before = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.result == "deny"))
    client.get("/api/ops/certs", headers=auth(consumer_token))
    db.expire_all()
    after = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.result == "deny"))
    assert after == before + 1

    row = db.scalars(
        select(AuditLog).where(AuditLog.result == "deny").order_by(AuditLog.created_at.desc())
    ).first()
    assert row.action == "permission.denied"
    assert row.role == "consumer"
    assert row.path == "/api/ops/certs"


def test_farmer_cannot_review(client, farmer_token):
    resp = client.post(
        "/api/ops/certs/CER-not-exist/review",
        headers=auth(farmer_token),
        json={"decision": "approve", "opinion": "同意"},
    )
    assert resp.status_code == 403


def test_regulator_cannot_view_platform_dashboard(client, regulator_token):
    """监管人员只做回溯，不能看经营看板（最小权限）。"""
    assert client.get("/api/ops/dashboard", headers=auth(regulator_token)).status_code == 403


def test_regulator_cannot_dispose_alert(client, regulator_token):
    resp = client.post(
        "/api/ops/alerts/ALT-not-exist/dispose",
        headers=auth(regulator_token),
        json={"action": "close", "resolution": "已处理"},
    )
    assert resp.status_code == 403


def test_operator_can_read_queue(client, operator_token):
    resp = client.get("/api/ops/certs", headers=auth(operator_token))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1


def test_farmer_cannot_read_others_cert(client, farmer_token, db):
    """资源级归属校验：他人申请一律按「不存在」返回，避免探测 id。"""
    from app.ops.models import CertApplication

    other = db.scalars(
        select(CertApplication).where(CertApplication.applicant_id != "").limit(1)
    ).first()
    # 种子里 farmer02 的申请对 farmer01 不可见
    resp = client.get("/api/certs/mine", headers=auth(farmer_token))
    mine_ids = {item["cert_id"] for item in resp.json()["data"]["items"]}
    assert other is not None
    if other.cert_id not in mine_ids:
        detail = client.get(f"/api/certs/{other.cert_id}", headers=auth(farmer_token))
        assert detail.status_code == 404


def test_farmer_cannot_create_farm_as_consumer(client, consumer_token):
    resp = client.post(
        "/api/farms",
        headers=auth(consumer_token),
        json={"farm_name": "越权产地", "province": "某省", "city": "某市"},
    )
    assert resp.status_code == 403
