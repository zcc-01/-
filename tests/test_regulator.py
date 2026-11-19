"""UC-15 监管回溯：权限、降级、跨域表就绪后的回溯与导出。"""

from __future__ import annotations

from sqlalchemy import text

from app.db.base import engine
from tests.conftest import auth


def _drop(*names: str) -> None:
    with engine.begin() as conn:
        for name in names:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


def _create_trace_tables() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS batch (
                    batch_id TEXT PRIMARY KEY,
                    batch_no TEXT,
                    crop_name TEXT,
                    harvest_date TEXT,
                    status TEXT,
                    farm_id TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS trace_event (
                    event_id TEXT PRIMARY KEY,
                    batch_id TEXT,
                    seq_no INTEGER,
                    stage TEXT,
                    operator_id TEXT,
                    occurred_at TEXT,
                    location TEXT,
                    prev_hash TEXT,
                    curr_hash TEXT
                )
                """
            )
        )


def test_consumer_cannot_traceback(client, consumer_token):
    assert client.get("/api/regulator/traceback", headers=auth(consumer_token)).status_code == 403


def test_traceback_unavailable_without_trace_domain(client, regulator_token):
    resp = client.get("/api/regulator/traceback", headers=auth(regulator_token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["available"] is False
    assert "未合入" in data["note"]

    export = client.get("/api/regulator/traceback/export", headers=auth(regulator_token))
    assert export.status_code == 422


def test_traceback_with_trace_tables(client, regulator_token):
    _create_trace_tables()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO batch (batch_id, batch_no, crop_name, harvest_date, status, farm_id) VALUES "
                    "('B1','PC-20261101-AAAA','红富士苹果','2026-11-01','qualified','FRM1'),"
                    "('B2','PC-20261020-BBBB','武夷岩茶','2026-10-20','harvested','FRM2')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO trace_event (event_id, batch_id, seq_no, stage, operator_id, "
                    "occurred_at, location, prev_hash, curr_hash) VALUES "
                    "('E1','B1',1,'production','U1','2026-11-01 08:00:00','洛川','GENESIS','a'*64),"
                    "('E2','B1',2,'quality','U1','2026-11-03 09:00:00','洛川','a'*64,'b'*64)"
                )
            )

        resp = client.get("/api/regulator/traceback?batch_no=PC-2026", headers=auth(regulator_token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["available"] is True
        assert data["total"] == 2
        first = next(item for item in data["items"] if item["batch_id"] == "B1")
        assert first["event_count"] == 2
        # 溯源域 TraceService 未合入时链校验结论为「无法判定」而不是误报断链
        assert first["chain_ok"] is None

        filtered = client.get("/api/regulator/traceback?batch_no=AAAA", headers=auth(regulator_token))
        assert filtered.json()["data"]["total"] == 1

        detail = client.get("/api/regulator/traceback/B1", headers=auth(regulator_token))
        assert detail.status_code == 200
        assert len(detail.json()["data"]["events"]) == 2

        exported = client.get(
            "/api/regulator/traceback/export?batch_no=PC-2026", headers=auth(regulator_token)
        )
        assert exported.status_code == 200
        assert "attachment" in exported.headers["content-disposition"]
        body = exported.content.decode("utf-8-sig")
        assert "批次号" in body
        assert "PC-20261101-AAAA" in body
    finally:
        _drop("trace_event", "batch")


def test_traceback_date_filter(client, regulator_token):
    _create_trace_tables()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO batch (batch_id, batch_no, crop_name, harvest_date, status, farm_id) VALUES "
                    "('B9','PC-20260901-CCCC','稻花香','2026-09-01','closed','FRM3')"
                )
            )
        narrow = client.get(
            "/api/regulator/traceback?date_from=2026-10-01&date_to=2026-12-31", headers=auth(regulator_token)
        )
        assert narrow.json()["data"]["total"] == 0
        wide = client.get("/api/regulator/traceback?date_from=2026-08-01", headers=auth(regulator_token))
        assert wide.json()["data"]["total"] == 1
    finally:
        _drop("trace_event", "batch")
