"""UC-13 看板：跨域降级策略 + 依赖域就绪后的真实聚合。"""

from __future__ import annotations

from sqlalchemy import text

from app.db.base import engine
from tests.conftest import auth


def _drop(*names: str) -> None:
    with engine.begin() as conn:
        for name in names:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


def _create_orders_table() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    farm_id TEXT,
                    total_amount REAL,
                    status TEXT,
                    created_at TEXT
                )
                """
            )
        )


def test_platform_overview_degrades_gracefully(client, operator_token):
    resp = client.get("/api/ops/dashboard", headers=auth(operator_token))
    assert resp.status_code == 200
    data = resp.json()["data"]

    # 本域指标可用
    assert data["farms"]["total"]["available"] is True
    assert data["farms"]["total"]["value"] >= 3
    assert data["certs"]["approved"] >= 1
    assert data["alerts"]["open"] >= 1

    # 跨域指标降级为 unavailable，并给出原因（而不是 0）
    assert data["sales"]["gmv"]["available"] is False
    assert data["sales"]["gmv"]["value"] is None
    assert "未合入" in data["sales"]["gmv"]["note"]
    assert data["dependencies"] == {"trade_domain": False, "trace_domain": False}


def test_platform_overview_reads_orders_when_ready(client, operator_token):
    _create_orders_table()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO orders (order_id, farm_id, total_amount, status, created_at) VALUES "
                    "('O1','FRM1',120.50,'paid','2026-11-10 10:00:00'),"
                    "('O2','FRM1',80.00,'paid','2026-11-11 11:00:00'),"
                    "('O3','FRM2',50.00,'pending','2026-11-11 12:00:00')"
                )
            )
        data = client.get("/api/ops/dashboard", headers=auth(operator_token)).json()["data"]
        assert data["sales"]["gmv"]["available"] is True
        assert data["sales"]["gmv"]["value"] == 200.5
        assert data["sales"]["orders"]["value"] == 2
        assert data["dependencies"]["trade_domain"] is True
        assert len(data["sales"]["daily"]["value"]) >= 1
    finally:
        _drop("orders")


def test_farmer_dashboard_scope(client, farmer_token):
    farms = client.get("/api/farms/mine", headers=auth(farmer_token)).json()["data"]["items"]
    farm_id = farms[0]["farm_id"]
    resp = client.get(f"/api/farms/{farm_id}/dashboard", headers=auth(farmer_token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scope"] == "farm"
    assert data["cert"] is not None
    assert data["scans"]["available"] is False


def test_farmer_cannot_view_others_dashboard(client, farmer_token, db):
    from app.common.models import Farm

    other = db.query(Farm).filter(Farm.owner_id != None).first()  # noqa: E711
    resp = client.get(f"/api/farms/{other.farm_id}/dashboard", headers=auth(farmer_token))
    mine = client.get("/api/farms/mine", headers=auth(farmer_token)).json()["data"]["items"]
    if other.farm_id not in {f["farm_id"] for f in mine}:
        assert resp.status_code == 404


def test_platform_overview_with_scan_log(client, operator_token):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS scan_log (
                    log_id TEXT PRIMARY KEY,
                    code_value TEXT,
                    batch_id TEXT,
                    scanned_at TEXT,
                    province TEXT,
                    city TEXT,
                    device TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO scan_log (log_id, code_value, batch_id, scanned_at, province, device) VALUES "
                "('L1','C1','B1','2026-11-15 09:00:00','广东省','D1'),"
                "('L2','C1','B1','2026-11-15 10:00:00','广东省','D2'),"
                "('L3','C2','B2','2026-11-15 11:00:00','浙江省','D3')"
            )
        )
    try:
        data = client.get("/api/ops/dashboard", headers=auth(operator_token)).json()["data"]
        assert data["scans"]["total"]["available"] is True
        assert data["scans"]["total"]["value"] == 3
        assert data["regions"]["available"] is True
        top = data["regions"]["items"][0]
        assert top["province"] == "广东省"
        assert top["scans"] == 2
    finally:
        _drop("scan_log")
