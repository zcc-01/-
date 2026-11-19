"""UC-03 赋码与作废的接口测试。"""

from __future__ import annotations

import io
import zipfile

from tests.conftest import create_batch


def _harvested_batch(client, seed, db):
    batch = create_batch(db, seed)
    client.patch(
        f"/api/batches/{batch.batch_id}/status",
        json={"status": "harvested"},
        headers=seed.farmer_headers,
    )
    db.refresh(batch)
    return batch


def test_generate_item_codes(client, seed, db):
    batch = _harvested_batch(client, seed, db)
    resp = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 5},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 5
    assert items[0]["code_value"] == f"{batch.batch_no}-ITM0001"
    assert len({item["code_value"] for item in items}) == 5


def test_generate_codes_is_idempotent(client, seed, db):
    """重复提交不产生重复码值（报告 5.2.1 指出并修复的缺陷）。"""
    batch = _harvested_batch(client, seed, db)
    url = f"/api/batches/{batch.batch_id}/codes"
    body = {"code_type": "item", "count": 3}
    first = client.post(url, json=body, headers=seed.farmer_headers).json()["data"]["items"]
    second = client.post(url, json=body, headers=seed.farmer_headers).json()["data"]["items"]
    assert [c["code_value"] for c in first] == [c["code_value"] for c in second]

    listing = client.get(url, headers=seed.farmer_headers).json()["data"]
    assert listing["total"] == 3


def test_code_generation_requires_harvested_status(client, seed, db):
    batch = create_batch(db, seed)  # draft
    resp = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 1},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 42201


def test_code_count_limit(client, seed, db):
    batch = _harvested_batch(client, seed, db)
    resp = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 5001},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 422


def test_box_code_prefix(client, seed, db):
    batch = _harvested_batch(client, seed, db)
    resp = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "box", "count": 2},
        headers=seed.farmer_headers,
    )
    items = resp.json()["data"]["items"]
    assert all(code["code_value"].endswith(("BOX0001", "BOX0002")) for code in items)


def test_revoked_code_cannot_be_scanned(client, seed, db):
    batch = _harvested_batch(client, seed, db)
    code = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 1},
        headers=seed.farmer_headers,
    ).json()["data"]["items"][0]["code_value"]

    assert client.get(f"/api/trace/{code}").status_code == 200
    revoked = client.post(
        f"/api/codes/{code}/revoke", json={"reason": "包装破损"}, headers=seed.farmer_headers
    )
    assert revoked.status_code == 200

    resp = client.get(f"/api/trace/{code}")
    assert resp.status_code == 410
    assert resp.json()["code"] == 42202


def test_qrcode_zip_export(client, seed, db):
    batch = _harvested_batch(client, seed, db)
    client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 3},
        headers=seed.farmer_headers,
    )
    resp = client.get(f"/api/batches/{batch.batch_id}/qrcodes.zip", headers=seed.farmer_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    names = archive.namelist()
    assert "manifest.csv" in names
    assert len([n for n in names if n.endswith(".png")]) == 3
