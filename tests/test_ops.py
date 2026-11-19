"""用户与运营域（UC-11～UC-15）测试。

跨域联动是重点：
- 断链扫码自动生成预警单（溯源域 → 运营域）
- 时间倒挂自动生成预警单（溯源域 → 运营域）
- 认证通过后该产地商品自动打认证标识（运营域 → 交易域）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.conftest import create_batch


def _harvested(client, seed, db, *, plot_no="A-03"):
    """赋码要求批次已采收，测试里统一走这个助手。"""
    batch = create_batch(db, seed, plot_no=plot_no)
    client.patch(
        f"/api/batches/{batch.batch_id}/status",
        json={"status": "harvested"},
        headers=seed.farmer_headers,
    )
    db.refresh(batch)
    return batch


def _regulator(db):
    """监管员账号：conftest 只建了农户/运营/消费者。"""
    from app.common.models import Role, User
    from app.core.security import create_access_token, hash_password

    user = User(
        username="regulator01",
        password_hash=hash_password("Passw0rd!"),
        display_name="市场监管员",
        role=Role.REGULATOR,
    )
    db.add(user)
    db.commit()
    token = create_access_token(user_id=user.user_id, username=user.username, role=user.role)
    return user, {"Authorization": f"Bearer {token}"}


def _cert_body(seed, **over):
    body = {
        "farm_id": seed.farm.farm_id,
        "land_cert_no": "ZJ-LD-2026-0088",
        "report_no": "ZJ-JC-2026-0912",
        "report_issue_date": "2026-09-01",
        "report_expire_at": (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=180)).isoformat(),
        "commitment_signed": True,
        "materials": {"land_photo": "a.jpg", "report_scan": "b.pdf"},
        "submit": True,
    }
    body.update(over)
    return body


# ------------------------------------------------------------------ UC-12 提交
def test_submit_cert_missing_material_goes_draft(client, seed):
    """异常流 2a：材料缺失只存草稿，不进待审队列。"""
    resp = client.post(
        "/api/certs",
        json=_cert_body(seed, land_cert_no="", report_no=""),
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "draft"
    assert set(data["missing"]) == {"土地证明编号", "检测报告编号"}


def test_duplicate_active_application_is_conflict(client, seed):
    """BR-07：同一产地在途申请只能有一份。"""
    assert client.post("/api/certs", json=_cert_body(seed), headers=seed.farmer_headers).status_code == 200
    second = client.post("/api/certs", json=_cert_body(seed), headers=seed.farmer_headers)
    assert second.status_code == 409
    assert "在途申请" in second.json()["message"]


def test_farmer_cannot_apply_for_others_farm(client, seed, db):
    from app.common.models import Farm, Role, User
    from app.core.security import create_access_token, hash_password

    other = User(
        username="farmer03",
        password_hash=hash_password("Passw0rd!"),
        role=Role.FARMER,
    )
    db.add(other)
    db.flush()
    db.add(Farm(farm_name="他人农场", owner_id=other.user_id))
    db.commit()
    token = create_access_token(user_id=other.user_id, username=other.username, role=other.role)
    resp = client.post(
        "/api/certs",
        json=_cert_body(seed),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ UC-12 审核
def test_approve_marks_farm_and_products(client, seed, db):
    """UC-12 步骤 7：认证通过 → 产地 approved，且该产地商品自动打认证标识。"""
    from app.common.models import Farm

    batch = create_batch(db, seed, status="harvested", plot_no="O-01")
    product = client.post(
        "/api/products",
        json={"batch_id": batch.batch_id, "name": "待认证茶叶", "price_yuan": 88, "stock": 10},
        headers=seed.farmer_headers,
    ).json()["data"]

    # 先让农场退回未认证，验证联动确实由审核动作触发
    farm = db.get(Farm, seed.farm.farm_id)
    farm.cert_status = "pending"
    db.commit()
    db.execute(text("UPDATE product SET certified = 0 WHERE product_id = :p"), {"p": product["product_id"]})
    db.commit()

    app = client.post("/api/certs", json=_cert_body(seed), headers=seed.farmer_headers).json()["data"]
    reviewed = client.post(
        f"/api/certs/{app['app_id']}/review",
        json={"decision": "approve", "comment": "材料齐全，现场核查通过"},
        headers=seed.operator_headers,
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["status"] == "approved"

    db.expire_all()
    assert db.get(Farm, seed.farm.farm_id).cert_status == "approved"
    detail = client.get(f"/api/products/{product['product_id']}").json()["data"]
    assert detail["certified"] == 1


def test_review_conclusion_is_immutable(client, seed):
    """BR-08：结论提交后不可改，再次审核返回 409。"""
    app = client.post("/api/certs", json=_cert_body(seed), headers=seed.farmer_headers).json()["data"]
    first = client.post(
        f"/api/certs/{app['app_id']}/review",
        json={"decision": "reject", "comment": "报告编号与出具机构不一致"},
        headers=seed.operator_headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/certs/{app['app_id']}/review",
        json={"decision": "approve", "comment": "改判"},
        headers=seed.operator_headers,
    )
    assert second.status_code == 409
    assert "不可修改" in second.json()["message"]


def test_expired_report_cannot_be_approved(client, seed):
    """异常流 4a：检测报告过期只能驳回。"""
    expired = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10)).isoformat()
    app = client.post(
        "/api/certs", json=_cert_body(seed, report_expire_at=expired), headers=seed.farmer_headers
    ).json()["data"]

    resp = client.post(
        f"/api/certs/{app['app_id']}/review",
        json={"decision": "approve", "comment": "通过"},
        headers=seed.operator_headers,
    )
    assert resp.status_code == 422
    assert "有效期" in resp.json()["message"]


def test_reject_requires_comment(client, seed):
    app = client.post("/api/certs", json=_cert_body(seed), headers=seed.farmer_headers).json()["data"]
    resp = client.post(
        f"/api/certs/{app['app_id']}/review",
        json={"decision": "reject", "comment": ""},
        headers=seed.operator_headers,
    )
    assert resp.status_code == 422


def test_farmer_cannot_review(client, seed):
    app = client.post("/api/certs", json=_cert_body(seed), headers=seed.farmer_headers).json()["data"]
    resp = client.post(
        f"/api/certs/{app['app_id']}/review",
        json={"decision": "approve", "comment": "自己批自己"},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ UC-13 看板
def test_dashboard_scope_farmer_vs_operator(client, seed, db):
    """口径写死在服务端：农户只看自己，运营看全平台。"""
    from app.common.models import Farm, Role, User
    from app.core.security import hash_password
    from app.trace.schemas import BatchCreate
    from app.trace.service import TraceService

    create_batch(db, seed, status="harvested", plot_no="D-01")

    other = User(username="farmer04", password_hash=hash_password("Passw0rd!"), role=Role.FARMER)
    db.add(other)
    db.flush()
    other_farm = Farm(farm_name="另一家农场", owner_id=other.user_id)
    db.add(other_farm)
    db.commit()
    TraceService.create_batch(
        db,
        BatchCreate(
            farm_id=other_farm.farm_id,
            crop_name="龙井",
            plot_no="E-01",
            plant_date="2026-03-01",
            harvest_date="2026-09-20",
            expected_yield=500,
        ),
        other,
    )

    farmer_view = client.get("/api/dashboard", headers=seed.farmer_headers).json()["data"]
    operator_view = client.get("/api/dashboard", headers=seed.operator_headers).json()["data"]

    assert farmer_view["scope"] == "farm"
    assert farmer_view["batch_total"] == 1
    assert operator_view["scope"] == "platform"
    assert operator_view["batch_total"] == 2


def test_dashboard_order_metrics_match_orders(client, seed, db):
    """看板成交额必须与已支付订单一致，避免前后端各算一套。"""
    batch = create_batch(db, seed, status="harvested", plot_no="D-02")
    product = client.post(
        "/api/products",
        json={"batch_id": batch.batch_id, "name": "看板测试茶", "price_yuan": 100, "stock": 10},
        headers=seed.farmer_headers,
    ).json()["data"]
    order = client.post(
        "/api/orders",
        json={
            "product_id": product["product_id"],
            "quantity": 2,
            "receiver_name": "张三",
            "receiver_phone": "13800138000",
            "address": "浙江省杭州市西湖区文三路 100 号",
        },
        headers=seed.consumer_headers,
    ).json()["data"]
    client.post(f"/api/orders/{order['order_id']}/pay", json={}, headers=seed.consumer_headers)

    view = client.get("/api/dashboard", headers=seed.operator_headers).json()["data"]
    assert view["order_paid"] == 1
    assert view["gmv_cents"] == 20000
    assert view["gmv"] == 200.0


# ------------------------------------------------------------------ UC-14 预警
def test_chain_broken_creates_alert_on_scan(client, seed, db):
    """跨域联动：扫码发现断链 → 自动开预警单（幂等，重复扫码不重复建单）。"""
    batch = _harvested(client, seed, db)
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "quality", "detail": {"report_no": "ZJ-001"}},
        headers=seed.farmer_headers,
    )
    code = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 1},
        headers=seed.farmer_headers,
    ).json()["data"]["items"][0]["code_value"]

    # 篡改第 2 条记录，制造断链
    db.execute(
        text("UPDATE trace_event SET detail = :d WHERE batch_id = :b AND seq_no = 2"),
        {"d": '{"report_no":"伪造"}', "b": batch.batch_id},
    )
    db.commit()
    db.expire_all()

    assert client.get(f"/api/trace/{code}").status_code == 200
    alerts = client.get(
        "/api/alerts?alert_type=chain_broken", headers=seed.operator_headers
    ).json()["data"]["items"]
    assert len(alerts) == 1
    assert alerts[0]["status"] == "open"
    assert alerts[0]["batch_id"] == batch.batch_id

    # 再扫一次不应重复建单
    client.get(f"/api/trace/{code}")
    again = client.get(
        "/api/alerts?alert_type=chain_broken", headers=seed.operator_headers
    ).json()["data"]["items"]
    assert len(again) == 1


def test_time_inversion_creates_alert(client, seed, db):
    """录入时间倒挂：不阻断录入，但生成预警单供运营复核。"""
    batch = create_batch(db, seed)
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "quality", "occurred_at": "2026-09-10T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    resp = client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "logistics", "occurred_at": "2026-09-01T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["warning"]

    alerts = client.get(
        "/api/alerts?alert_type=time_inversion", headers=seed.operator_headers
    ).json()["data"]["items"]
    assert len(alerts) == 1


def test_alert_handle_flow(client, seed, db):
    """预警单处置：运营可改为处理中/关闭，关闭后不再计入未关闭数。"""
    batch = create_batch(db, seed)
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "quality", "occurred_at": "2026-09-10T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "logistics", "occurred_at": "2026-09-01T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    alert = client.get("/api/alerts", headers=seed.operator_headers).json()["data"]["items"][0]

    resp = client.post(
        f"/api/alerts/{alert['alert_id']}/handle",
        json={"status": "closed", "note": "已核对原始记录，时间录入有误已更正"},
        headers=seed.operator_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "closed"

    stats = client.get("/api/alerts/stats", headers=seed.operator_headers).json()["data"]
    assert stats["open"] == 0


def test_farmer_cannot_handle_alert(client, seed, db):
    batch = create_batch(db, seed)
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "quality", "occurred_at": "2026-09-10T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    client.post(
        f"/api/batches/{batch.batch_id}/events",
        json={"stage": "logistics", "occurred_at": "2026-09-01T09:00:00", "detail": {}},
        headers=seed.farmer_headers,
    )
    alert = client.get("/api/alerts", headers=seed.operator_headers).json()["data"]["items"][0]
    resp = client.post(
        f"/api/alerts/{alert['alert_id']}/handle",
        json={"status": "closed", "note": "农户自己关自己的单"},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ UC-15 监管
def test_regulator_trace_back(client, seed, db):
    batch = _harvested(client, seed, db)
    code = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 1},
        headers=seed.farmer_headers,
    ).json()["data"]["items"][0]["code_value"]

    _, headers = _regulator(db)
    resp = client.get(f"/api/regulator/trace/{code}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["batch"]["batch_id"] == batch.batch_id
    assert data["events"]
    ctx = data["regulator_context"]
    assert ctx["batch_id"] == batch.batch_id
    assert ctx["alert_count"] == 0
    assert ctx["suggestion"] == "未发现异常预警"


def test_regulator_dispose_records_action(client, seed, db):
    batch = _harvested(client, seed, db)
    code = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 1},
        headers=seed.farmer_headers,
    ).json()["data"]["items"][0]["code_value"]

    _, headers = _regulator(db)
    resp = client.post(
        f"/api/regulator/trace/{code}/dispose",
        json={"action": "freeze_batch", "note": "抽检不合格，先行冻结"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["action"] == "freeze_batch"

    # 处置动作要留痕
    from app.ops.models import AuditLog

    rows = db.query(AuditLog).filter(AuditLog.action == "regulator.freeze_batch").all()
    assert len(rows) == 1


def test_farmer_cannot_use_regulator_api(client, seed, db):
    batch = _harvested(client, seed, db)
    code = client.post(
        f"/api/batches/{batch.batch_id}/codes",
        json={"code_type": "item", "count": 1},
        headers=seed.farmer_headers,
    ).json()["data"]["items"][0]["code_value"]
    assert client.get(f"/api/regulator/trace/{code}", headers=seed.farmer_headers).status_code == 403
