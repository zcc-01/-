"""交易域（UC-06～UC-10）接口与业务规则测试。

覆盖报告里明确写出的硬约束：
- BR-04 同一订单仅一条成功支付流水（幂等）
- BR-05 库存条件更新，扣不动就整单回滚（不超卖）
- BR-06 取消后库存原路释放
- 金额由服务端按商品快照重算，不信任客户端
"""

from __future__ import annotations

import itertools

import pytest

from tests.conftest import create_batch

ORDER_BODY = {
    "receiver_name": "张三",
    "receiver_phone": "13800138000",
    "address": "浙江省杭州市西湖区文三路 100 号",
    "remark": "测试订单",
}


_PLOT_SEQ = itertools.count(1)


def make_product(client, seed, db, *, price=59.8, stock=100, batch_status="harvested"):
    """建批次 → 上架商品，返回商品 dict。

    批次有 (farm_id, plot_no, crop_name, harvest_date) 唯一约束，
    所以每次用一个新地块号，否则同一测试里建第二个批次会撞车。
    """
    batch = create_batch(
        db, seed, status=batch_status, plot_no=f"T-{next(_PLOT_SEQ):03d}"
    )
    resp = client.post(
        "/api/products",
        json={
            "batch_id": batch.batch_id,
            "name": f"临安山核桃 {price}",
            "category": "坚果",
            "price_yuan": price,
            "stock": stock,
            "unit": "袋",
            "description": "产地直供，壳薄仁香",
        },
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def place_order(client, seed, product, quantity=2, **extra):
    body = {**ORDER_BODY, "product_id": product["product_id"], "quantity": quantity, **extra}
    return client.post("/api/orders", json=body, headers=seed.consumer_headers)


# ------------------------------------------------------------------ UC-06 上架
def test_product_create_requires_harvested_batch(client, seed, db):
    """生长中的批次不能上架——交易域对溯源域的方向性依赖。"""
    batch = create_batch(db, seed, status="growing")
    resp = client.post(
        "/api/products",
        json={"batch_id": batch.batch_id, "name": "早采山核桃", "price_yuan": 30, "stock": 10},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 422
    assert "采收" in resp.json()["message"]


def test_product_certified_flag_follows_farm(client, seed, db):
    """农场已认证（seed 里 cert_status=approved）时，上架即带认证标识。"""
    product = make_product(client, seed, db)
    assert product["certified"] == 1


def test_other_farmer_cannot_shelve_my_batch(client, seed, db):
    """越权：非本农场批次上架返回 403。"""
    from app.common.models import Farm, Role, User
    from app.core.security import create_access_token, hash_password

    other = User(
        username="farmer02",
        password_hash=hash_password("Passw0rd!"),
        display_name="王二",
        role=Role.FARMER,
    )
    db.add(other)
    db.flush()
    db.add(Farm(farm_name="别家农场", owner_id=other.user_id, cert_status="pending"))
    db.commit()

    batch = create_batch(db, seed, status="harvested")
    token = create_access_token(user_id=other.user_id, username=other.username, role=other.role)
    resp = client.post(
        "/api/products",
        json={"batch_id": batch.batch_id, "name": "蹭批次", "price_yuan": 10, "stock": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ UC-07 检索
def test_search_and_filter(client, seed, db):
    make_product(client, seed, db, price=20, stock=10)
    make_product(client, seed, db, price=90, stock=10)

    all_items = client.get("/api/products").json()["data"]["items"]
    assert len(all_items) == 2

    cheap = client.get("/api/products?price_max=50").json()["data"]["items"]
    assert [p["price"] for p in cheap] == [20.0]

    none_match = client.get("/api/products?keyword=不存在的商品").json()["data"]["items"]
    assert none_match == []

    desc = client.get("/api/products?sort=price_desc").json()["data"]["items"]
    assert desc[0]["price"] == 90.0


# ------------------------------------------------------------------ UC-07 购物车
def test_cart_add_update_remove(client, seed, db):
    product = make_product(client, seed, db, stock=10)
    pid = product["product_id"]

    data = client.post(
        "/api/cart", json={"product_id": pid, "quantity": 3}, headers=seed.consumer_headers
    ).json()["data"]
    assert data["items"][0]["quantity"] == 3

    cart_id = data["items"][0]["cart_id"]
    data = client.patch(
        f"/api/cart/{cart_id}", json={"quantity": 5}, headers=seed.consumer_headers
    ).json()["data"]
    assert data["items"][0]["quantity"] == 5
    assert data["total_cents"] == product["price_cents"] * 5

    data = client.delete(f"/api/cart/{cart_id}", headers=seed.consumer_headers).json()["data"]
    assert data["items"] == []


def test_cart_rejects_over_stock(client, seed, db):
    product = make_product(client, seed, db, stock=2)
    resp = client.post(
        "/api/cart",
        json={"product_id": product["product_id"], "quantity": 5},
        headers=seed.consumer_headers,
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------ UC-08 下单
def test_order_amount_recomputed_server_side(client, seed, db):
    """客户端即便伪造金额也无效：OrderCreate 里根本没有金额字段。"""
    product = make_product(client, seed, db, price=59.8, stock=10)
    resp = place_order(client, seed, product, quantity=3, total_cents=1)
    assert resp.status_code == 200
    order = resp.json()["data"]
    assert order["total_cents"] == 5980 * 3
    assert order["status"] == "pending"


def test_order_deducts_stock(client, seed, db):
    product = make_product(client, seed, db, stock=10)
    place_order(client, seed, product, quantity=4)

    latest = client.get(f"/api/products/{product['product_id']}").json()["data"]
    assert latest["stock"] == 6


def test_oversell_is_blocked(client, seed, db):
    """BR-05：库存不足时整单回滚，库存不会被扣成负数。"""
    product = make_product(client, seed, db, stock=2)
    resp = place_order(client, seed, product, quantity=3)
    assert resp.status_code == 422
    assert "库存" in resp.json()["message"]

    latest = client.get(f"/api/products/{product['product_id']}").json()["data"]
    assert latest["stock"] == 2


def test_order_blocked_when_batch_closed(client, seed, db):
    """跨域联动：批次结批/被封存后，即使商品仍在架也不能下单。"""
    product = make_product(client, seed, db)
    batch_id = product["batch_id"]

    from app.trace.models import Batch, BatchStatus

    batch = db.get(Batch, batch_id)
    batch.status = BatchStatus.CLOSED
    db.commit()

    resp = place_order(client, seed, product, quantity=1)
    assert resp.status_code == 422
    assert "结批" in resp.json()["message"] or "封存" in resp.json()["message"]


# ------------------------------------------------------------------ UC-08 支付
def test_pay_is_idempotent(client, seed, db):
    """BR-04：连续两次支付只产生一条成功流水，第二次返回原结果。"""
    from app.trade.models import Payment, PayStatus

    product = make_product(client, seed, db)
    order = place_order(client, seed, product, quantity=1).json()["data"]
    oid = order["order_id"]

    first = client.post(f"/api/orders/{oid}/pay", json={}, headers=seed.consumer_headers)
    assert first.status_code == 200
    assert first.json()["data"]["retryable"] is False

    second = client.post(f"/api/orders/{oid}/pay", json={}, headers=seed.consumer_headers)
    assert second.status_code == 200
    assert second.json()["data"]["message"].startswith("该订单已支付")

    from sqlalchemy import select

    rows = db.scalars(
        select(Payment).where(Payment.order_id == oid, Payment.status == PayStatus.SUCCESS)
    ).all()
    assert len(rows) == 1


def test_pay_failure_is_retryable_then_locked(client, seed, db):
    """失败可重试，但达到上限后必须换单——避免无限重试刷网关。"""
    product = make_product(client, seed, db)
    order = place_order(client, seed, product, quantity=1).json()["data"]
    oid = order["order_id"]

    for i in range(1, 4):
        resp = client.post(
            f"/api/orders/{oid}/pay", json={"force_fail": True}, headers=seed.consumer_headers
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["payment"] is None
        assert data["retryable"] is (i < 3)

    resp = client.post(
        f"/api/orders/{oid}/pay", json={"force_fail": True}, headers=seed.consumer_headers
    )
    assert resp.status_code == 422
    assert "重新下单" in resp.json()["message"]


def test_cancel_restores_stock(client, seed, db):
    """BR-06：取消后库存原路释放。"""
    product = make_product(client, seed, db, stock=10)
    order = place_order(client, seed, product, quantity=4).json()["data"]
    assert client.get(f"/api/products/{product['product_id']}").json()["data"]["stock"] == 6

    resp = client.post(
        f"/api/orders/{order['order_id']}/cancel", headers=seed.consumer_headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"
    assert client.get(f"/api/products/{product['product_id']}").json()["data"]["stock"] == 10


# ------------------------------------------------------------------ UC-09/10
def test_ship_receive_review_flow(client, seed, db):
    product = make_product(client, seed, db)
    order = place_order(client, seed, product, quantity=1).json()["data"]
    oid = order["order_id"]
    client.post(f"/api/orders/{oid}/pay", json={}, headers=seed.consumer_headers)

    # 消费者不能自己发货
    assert (
        client.post(f"/api/orders/{oid}/ship", headers=seed.consumer_headers).status_code == 403
    )
    shipped = client.post(f"/api/orders/{oid}/ship", headers=seed.farmer_headers)
    assert shipped.status_code == 200
    assert shipped.json()["data"]["status"] == "shipped"

    received = client.post(f"/api/orders/{oid}/receive", headers=seed.consumer_headers)
    assert received.json()["data"]["status"] == "received"

    review = client.post(
        f"/api/orders/{oid}/reviews",
        json={"product_id": product["product_id"], "rating": 5, "content": "颗粒饱满，回甘"},
        headers=seed.consumer_headers,
    )
    assert review.status_code == 200

    detail = client.get(f"/api/products/{product['product_id']}").json()["data"]
    assert len(detail["reviews"]) == 1
    assert detail["reviews"][0]["rating"] == 5


def test_review_requires_received(client, seed, db):
    """UC-10 前置条件：只有已收货的订单能评价。"""
    product = make_product(client, seed, db)
    order = place_order(client, seed, product, quantity=1).json()["data"]
    resp = client.post(
        f"/api/orders/{order['order_id']}/reviews",
        json={"product_id": product["product_id"], "rating": 4, "content": "还没收到"},
        headers=seed.consumer_headers,
    )
    assert resp.status_code == 422


def test_cannot_pay_others_order(client, seed, db):
    product = make_product(client, seed, db)
    order = place_order(client, seed, product, quantity=1).json()["data"]
    resp = client.post(
        f"/api/orders/{order['order_id']}/pay", json={}, headers=seed.farmer_headers
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_quantity_rejected(client, seed, db, quantity):
    product = make_product(client, seed, db)
    resp = place_order(client, seed, product, quantity=quantity)
    assert resp.status_code == 422
