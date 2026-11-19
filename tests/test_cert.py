"""UC-12 产地认证：材料缺项、在途唯一、状态机、报告过期、到期处理。"""

from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import auth

COMPLETE_MATERIALS = {
    "crop_scope": "红富士苹果",
    "land_proof_no": "陕农地证（2024）第 9999 号",
    "test_report_no": "SX-JC-2026-8888",
    "test_report_at": (date.today() - timedelta(days=20)).isoformat(),
    "commitment_signed": True,
}


def _new_farm(client, token, name: str = "测试果园") -> str:
    resp = client.post(
        "/api/farms",
        headers=auth(token),
        json={"farm_name": name, "province": "陕西省", "city": "延安市", "area_mu": 30},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["farm_id"]


def _submit(client, token, farm_id: str, materials: dict | None = None):
    payload = {"farm_id": farm_id, **(materials or COMPLETE_MATERIALS)}
    return client.post("/api/certs", headers=auth(token), json=payload)


def test_farm_apply_and_list(client, farmer_token):
    farm_id = _new_farm(client, farmer_token)
    mine = client.get("/api/farms/mine", headers=auth(farmer_token))
    assert mine.status_code == 200
    assert farm_id in {item["farm_id"] for item in mine.json()["data"]["items"]}


def test_submit_missing_materials_saved_as_draft(client, farmer_token, operator_token):
    farm_id = _new_farm(client, farmer_token)
    resp = client.post(
        "/api/certs",
        headers=auth(farmer_token),
        json={"farm_id": farm_id, "land_proof_no": "陕农地证（2024）第 0001 号"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["saved_as"] == "draft"
    assert "质量检测报告" in data["missing_items"]
    assert "质量安全承诺书" in data["missing_items"]

    # 草稿不进入待审队列
    queue = client.get("/api/ops/certs", headers=auth(operator_token))
    queued = {item["cert_id"] for item in queue.json()["data"]["items"]}
    assert data["cert"]["cert_id"] not in queued


def test_submit_complete_enters_queue(client, farmer_token, operator_token):
    farm_id = _new_farm(client, farmer_token)
    data = _submit(client, farmer_token, farm_id).json()["data"]
    assert data["saved_as"] == "submitted"

    queue = client.get("/api/ops/certs", headers=auth(operator_token))
    assert data["cert"]["cert_id"] in {item["cert_id"] for item in queue.json()["data"]["items"]}


def test_duplicate_in_flight_conflict(client, farmer_token):
    farm_id = _new_farm(client, farmer_token)
    assert _submit(client, farmer_token, farm_id).status_code == 200
    second = _submit(client, farmer_token, farm_id)
    assert second.status_code == 409
    assert second.json()["code"] == 40901


def test_approve_grants_badge_and_validity(client, farmer_token, operator_token, db):
    farm_id = _new_farm(client, farmer_token)
    cert_id = _submit(client, farmer_token, farm_id).json()["data"]["cert"]["cert_id"]

    resp = client.post(
        f"/api/ops/certs/{cert_id}/review",
        headers=auth(operator_token),
        json={"decision": "approve", "opinion": "材料齐全，同意授予产地认证"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "approved"
    assert data["valid_to"] is not None
    assert data["remind_at"] is not None

    from app.common.models import Farm

    farm = db.get(Farm, farm_id)
    db.refresh(farm)
    assert farm.cert_status == "approved"


def test_review_conclusion_is_immutable(client, farmer_token, operator_token):
    farm_id = _new_farm(client, farmer_token)
    cert_id = _submit(client, farmer_token, farm_id).json()["data"]["cert"]["cert_id"]

    client.post(
        f"/api/ops/certs/{cert_id}/review",
        headers=auth(operator_token),
        json={"decision": "reject", "opinion": "材料存疑，驳回"},
    )
    again = client.post(
        f"/api/ops/certs/{cert_id}/review",
        headers=auth(operator_token),
        json={"decision": "approve", "opinion": "改为通过"},
    )
    assert again.status_code == 422
    assert again.json()["code"] == 42203


def test_approve_blocked_when_report_expired(client, farmer_token, operator_token):
    farm_id = _new_farm(client, farmer_token, "过期报告果园")
    materials = dict(COMPLETE_MATERIALS)
    materials["test_report_at"] = (date.today() - timedelta(days=400)).isoformat()
    cert_id = _submit(client, farmer_token, farm_id, materials).json()["data"]["cert"]["cert_id"]

    resp = client.post(
        f"/api/ops/certs/{cert_id}/review",
        headers=auth(operator_token),
        json={"decision": "approve", "opinion": "同意"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 42205

    reject = client.post(
        f"/api/ops/certs/{cert_id}/review",
        headers=auth(operator_token),
        json={
            "decision": "reject",
            "opinion": "检测报告超出有效期",
            "reject_reason": "检测报告超出有效期",
        },
    )
    assert reject.status_code == 200
    assert reject.json()["data"]["status"] == "rejected"


def test_queue_marks_overdue(client, farmer_token, operator_token, db):
    from app.account.service import now
    from app.ops.models import CertApplication

    farm_id = _new_farm(client, farmer_token, "超时果园")
    cert_id = _submit(client, farmer_token, farm_id).json()["data"]["cert"]["cert_id"]

    cert = db.get(CertApplication, cert_id)
    cert.submitted_at = now() - timedelta(days=5)
    db.commit()

    queue = client.get("/api/ops/certs", headers=auth(operator_token))
    item = next(x for x in queue.json()["data"]["items"] if x["cert_id"] == cert_id)
    assert item["overdue"] is True


def test_sweep_expired_marks_farm_pending(db):
    from app.common.models import Farm
    from app.ops.cert_service import CertService
    from app.ops.models import CertApplication, CertStatus

    farm = Farm(
        farm_name="到期果园",
        owner_id="USR-test",
        province="陕西省",
        city="延安市",
        cert_status="approved",
    )
    db.add(farm)
    db.flush()
    cert = CertApplication(
        cert_no="CERT-EXPIRED-0001",
        farm_id=farm.farm_id,
        applicant_id="USR-test",
        crop_scope="苹果",
        status=CertStatus.APPROVED,
        valid_from=date.today() - timedelta(days=400),
        valid_to=date.today() - timedelta(days=35),
        remind_at=date.today() - timedelta(days=65),
    )
    db.add(cert)
    db.commit()

    result = CertService.sweep_expired(db)
    assert result["expired"] >= 1
    db.refresh(cert)
    db.refresh(farm)
    assert cert.status == CertStatus.EXPIRED
    assert farm.cert_status == "pending"
