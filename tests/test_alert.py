"""UC-14 预警：规则配置、工单生成去重、处置流转、规则启停。"""

from __future__ import annotations

from tests.conftest import auth


def test_rules_seeded(client, operator_token):
    resp = client.get("/api/ops/alert-rules", headers=auth(operator_token))
    assert resp.status_code == 200
    codes = {item["code"] for item in resp.json()["data"]["items"]}
    assert codes == {"R-01", "R-02", "R-03"}


def test_simulate_creates_alert(client, operator_token):
    resp = client.post(
        "/api/ops/alerts/simulate",
        headers=auth(operator_token),
        json={"rule_code": "R-03", "target_id": "BAT-TEST-1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["level"] == "critical"
    assert data["status"] == "open"

    listing = client.get("/api/ops/alerts", headers=auth(operator_token))
    assert data["alert_id"] in {item["alert_id"] for item in listing.json()["data"]["items"]}


def test_simulate_is_deduped_within_same_minute(client, operator_token):
    payload = {"rule_code": "R-01", "target_id": "BAT-TEST-2"}
    first = client.post("/api/ops/alerts/simulate", headers=auth(operator_token), json=payload)
    assert first.status_code == 200
    second = client.post("/api/ops/alerts/simulate", headers=auth(operator_token), json=payload)
    assert second.status_code == 422


def test_dispose_flow(client, operator_token):
    created = client.post(
        "/api/ops/alerts/simulate",
        headers=auth(operator_token),
        json={"rule_code": "R-02", "target_id": "BAT-TEST-3"},
    ).json()["data"]
    alert_id = created["alert_id"]

    ack = client.post(
        f"/api/ops/alerts/{alert_id}/dispose",
        headers=auth(operator_token),
        json={"action": "acknowledge", "resolution": ""},
    )
    assert ack.status_code == 200
    assert ack.json()["data"]["status"] == "acknowledged"

    closed = client.post(
        f"/api/ops/alerts/{alert_id}/dispose",
        headers=auth(operator_token),
        json={"action": "close", "resolution": "核实为同一设备跨省运输，非窜货"},
    )
    assert closed.status_code == 200
    assert closed.json()["data"]["status"] == "closed"

    # 终态不可再流转
    again = client.post(
        f"/api/ops/alerts/{alert_id}/dispose",
        headers=auth(operator_token),
        json={"action": "acknowledge", "resolution": ""},
    )
    assert again.status_code == 422


def test_close_requires_resolution(client, operator_token):
    alert_id = client.post(
        "/api/ops/alerts/simulate",
        headers=auth(operator_token),
        json={"rule_code": "R-03", "target_id": "BAT-TEST-4"},
    ).json()["data"]["alert_id"]

    resp = client.post(
        f"/api/ops/alerts/{alert_id}/dispose",
        headers=auth(operator_token),
        json={"action": "close", "resolution": ""},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 42201


def test_scan_without_trace_tables_creates_nothing(client, operator_token):
    """溯源域未合入时规则求值为空集合，既不报错也不产生误报。"""
    resp = client.post("/api/ops/alerts/scan", headers=auth(operator_token))
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 0


def test_toggle_rule(client, operator_token):
    resp = client.post(
        "/api/ops/alert-rules/R-02",
        headers=auth(operator_token),
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    rules = client.get("/api/ops/alert-rules", headers=auth(operator_token)).json()["data"]["items"]
    target = next(r for r in rules if r["code"] == "R-02")
    assert target["enabled"] is False

    # 停用后不再产出该规则的工单
    scan = client.post("/api/ops/alerts/scan", headers=auth(operator_token))
    assert scan.json()["data"]["created"] == 0


def test_alert_summary_counts(client, operator_token):
    listing = client.get("/api/ops/alerts", headers=auth(operator_token))
    summary = listing.json()["data"]["summary"]
    assert set(summary) == {"open", "acknowledged", "closed", "critical"}
    assert summary["open"] >= 1
