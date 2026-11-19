"""交易域自动化测试（UC-06～UC-10）。

重点覆盖报告 5.3.2 与 3.3 里点名的四类风险，而不是只测「接口能返回 200」：
1. 并发下单不超卖（条件更新 + 受影响行数判断）；
2. 重复支付不产生第二条成功流水（payment.order_id 唯一索引兜底）；
3. 订单金额由服务端重算，前端传值无效；
4. 订单状态机单向流转，未支付不能发货、未收货不能评价。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import app.models_registry  # noqa: F401
from app.common.models import Farm, Role, User
from app.db.base import Base
from app.trade.models import Order, OrderStatus, Payment, PaymentStatus, Product, ProductStatus, TradeRule
from app.trade.schemas import AddressIn, CartItemIn, OrderCreate
from app.trade.service import OrderService

ADDRESS = {"receiver": "李小明", "phone": "13800000004", "detail": "浙江省杭州市临安区清凉峰镇 1 号"}


# --------------------------------------------------------------------- 夹具
def _create_product(client, seed, **overrides) -> dict:
    payload = {
        "farm_id": seed.farm.farm_id,
        "title": "临安山核桃 手剥原味 500g",
        "crop_name": "临安山核桃",
        "spec": "500g/袋",
        "unit": "袋",
        "price": 68.0,
        "stock": 10,
        "cert_badge": "产地认证",
        "description": "产地直供",
    }
    payload.update(overrides)
    resp = client.post("/api/products", json=payload, headers=seed.farmer_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _place_order(client, seed, product_id: str, quantity: int = 1) -> dict:
    resp = client.post(
        "/api/orders",
        json={"items": [{"product_id": product_id, "quantity": quantity}], "address": ADDRESS},
        headers=seed.consumer_headers,
    )
    return resp


def _stock(client, product_id: str) -> int:
    resp = client.get(f"/api/products/{product_id}")
    return resp.json()["data"]["stock"]


# --------------------------------------------------------------------- UC-06
def test_create_product_success_and_permission(client, seed):
    product = _create_product(client, seed)
    assert product["status"] == ProductStatus.ON_SHELF
    assert product["farm_name"] == "青禾家庭农场"
    assert product["price"] == 68.0

    # 消费者无权上架
    denied = client.post(
        "/api/products",
        json={
            "farm_id": seed.farm.farm_id,
            "title": "越权商品",
            "crop_name": "苹果",
            "price": 10,
            "stock": 1,
        },
        headers=seed.consumer_headers,
    )
    assert denied.status_code == 403


def test_create_product_rejects_foreign_farm(client, seed, db):
    other = User(username="farmer02", password_hash="x", display_name="别家农户", role=Role.FARMER)
    db.add(other)
    db.flush()
    foreign_farm = Farm(farm_name="别人家农场", owner_id=other.user_id, province="山东省", city="烟台市")
    db.add(foreign_farm)
    db.commit()

    resp = client.post(
        "/api/products",
        json={
            "farm_id": foreign_farm.farm_id,
            "title": "烟台苹果 5kg",
            "crop_name": "苹果",
            "price": 45,
            "stock": 10,
        },
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 403
    assert "本产地" in resp.json()["message"]


def test_duplicate_product_conflict(client, seed):
    _create_product(client, seed)
    again = client.post(
        "/api/products",
        json={
            "farm_id": seed.farm.farm_id,
            "title": "临安山核桃 手剥原味 500g",
            "crop_name": "临安山核桃",
            "spec": "500g/袋",
            "price": 70,
            "stock": 5,
        },
        headers=seed.farmer_headers,
    )
    assert again.status_code == 409


def test_off_shelf_product_hidden_from_listing(client, seed):
    product = _create_product(client, seed)
    resp = client.patch(
        f"/api/products/{product['product_id']}/status",
        json={"status": "off_shelf"},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200

    listing = client.get("/api/products").json()["data"]
    assert product["product_id"] not in [item["product_id"] for item in listing["items"]]

    own = client.get("/api/products?mine=true", headers=seed.farmer_headers).json()["data"]
    assert product["product_id"] in [item["product_id"] for item in own["items"]]


# --------------------------------------------------------------------- UC-07
def test_list_products_filters_and_sort(client, seed):
    _create_product(client, seed, title="临安山核桃 500g", spec="500g", price=68, stock=20)
    _create_product(client, seed, title="临安山核桃 1kg 礼盒", spec="1kg", price=128, stock=20)
    _create_product(
        client, seed, title="云南蓝莓 4 盒装", crop_name="蓝莓", spec="4 盒", price=59.9, stock=20
    )

    by_keyword = client.get("/api/products?keyword=蓝莓").json()["data"]
    assert by_keyword["total"] == 1
    assert by_keyword["items"][0]["crop_name"] == "蓝莓"

    price_asc = client.get("/api/products?sort=price_asc").json()["data"]["items"]
    prices = [item["price"] for item in price_asc]
    assert prices == sorted(prices)

    in_range = client.get("/api/products?min_price=60&max_price=100").json()["data"]
    assert all(60 <= item["price"] <= 100 for item in in_range["items"])

    cert_only = client.get("/api/products?cert_only=true").json()["data"]
    assert all(item["cert_badge"] for item in cert_only["items"])


def test_product_detail_contains_batch_info(client, seed, db):
    from app.trace.schemas import BatchCreate
    from app.trace.service import TraceService

    batch = TraceService.create_batch(
        db,
        BatchCreate(
            farm_id=seed.farm.farm_id,
            crop_name="临安山核桃",
            plot_no="A-03",
            plant_date="2026-03-20",
            harvest_date="2026-09-05",
        ),
        seed.farmer,
    )
    product = _create_product(client, seed, batch_id=batch.batch_id)

    detail = client.get(f"/api/products/{product['product_id']}").json()["data"]
    assert detail["batch"]["batch_no"] == batch.batch_no
    assert detail["batch"]["event_count"] == 1
    assert detail["can_buy"] is True


# --------------------------------------------------------------------- UC-08
def test_quote_freight_rule(client, seed):
    cheap = _create_product(client, seed, title="小份装 250g", spec="250g", price=32, stock=10)
    quote = client.post(
        "/api/cart/quote", json={"items": [{"product_id": cheap["product_id"], "quantity": 1}]}
    ).json()["data"]
    assert quote["groups"][0]["goods_amount"] == 32.0
    assert quote["groups"][0]["freight"] == float(TradeRule.FREIGHT_FLAT)

    enough = client.post(
        "/api/cart/quote", json={"items": [{"product_id": cheap["product_id"], "quantity": 4}]}
    ).json()["data"]
    assert enough["groups"][0]["goods_amount"] == 128.0
    assert enough["groups"][0]["freight"] == 0.0


def test_create_order_amount_is_server_side(client, seed):
    product = _create_product(client, seed, price=68, stock=10)
    resp = _place_order(client, seed, product["product_id"], quantity=2)
    assert resp.status_code == 200, resp.text
    order = resp.json()["data"]["orders"][0]

    # 服务端按库中价格重算：2 × 68 = 136，满 99 免运费
    assert order["total_amount"] == 136.0
    assert order["freight"] == 0.0
    assert order["pay_amount"] == 136.0
    # 前端即便多传 price / total 也不会生效（Pydantic 只认 items + address）
    tampered = client.post(
        "/api/orders",
        json={
            "items": [{"product_id": product["product_id"], "quantity": 1, "price": 0.01}],
            "address": ADDRESS,
            "total_amount": 0.01,
            "pay_amount": 0.01,
        },
        headers=seed.consumer_headers,
    )
    assert tampered.status_code == 200
    # 68 元未达包邮门槛，实付 = 68 + 8 元运费；前端伪造的 0.01 元被完全忽略
    assert tampered.json()["data"]["orders"][0]["pay_amount"] == 76.0


def test_order_splits_by_farm(client, seed, db):
    other_farm = Farm(
        farm_name="云岭果蔬合作社", owner_id=seed.farmer.user_id, province="云南省", city="昆明市"
    )
    db.add(other_farm)
    db.commit()

    local = _create_product(client, seed, title="浙江山核桃", price=30, stock=10)
    remote = _create_product(
        client, seed, farm_id=other_farm.farm_id, title="云南蓝莓", crop_name="蓝莓", price=59.9, stock=10
    )

    resp = client.post(
        "/api/orders",
        json={
            "items": [
                {"product_id": local["product_id"], "quantity": 1},
                {"product_id": remote["product_id"], "quantity": 1},
            ],
            "address": ADDRESS,
        },
        headers=seed.consumer_headers,
    )
    assert resp.status_code == 200
    orders = resp.json()["data"]["orders"]
    assert len(orders) == 2
    assert {order["farm_id"] for order in orders} == {seed.farm.farm_id, other_farm.farm_id}


def test_insufficient_stock_rolls_back_whole_transaction(client, seed):
    first = _create_product(client, seed, title="库存充足商品", spec="A", price=20, stock=10)
    second = _create_product(client, seed, title="库存不足商品", spec="B", price=20, stock=1)

    resp = client.post(
        "/api/orders",
        json={
            "items": [
                {"product_id": first["product_id"], "quantity": 2},
                {"product_id": second["product_id"], "quantity": 3},
            ],
            "address": ADDRESS,
        },
        headers=seed.consumer_headers,
    )
    assert resp.status_code == 422
    assert "库存不足" in resp.json()["message"]

    # 整笔事务回滚：第一件商品的库存也不能被扣掉，订单一条都不能留
    assert _stock(client, first["product_id"]) == 10
    assert _stock(client, second["product_id"]) == 1
    listing = client.get("/api/orders", headers=seed.consumer_headers).json()["data"]
    assert listing["total"] == 0


def test_pay_success_and_idempotent(client, seed):
    product = _create_product(client, seed, price=68, stock=10)
    order = _place_order(client, seed, product["product_id"], 1).json()["data"]["orders"][0]

    first = client.post(
        f"/api/orders/{order['order_id']}/pay", json={"channel": "mockpay"}, headers=seed.consumer_headers
    )
    assert first.status_code == 200, first.text
    paid = first.json()["data"]
    assert paid["status"] == OrderStatus.PAID
    assert paid["trade_no"]

    # 重复支付：幂等返回同一笔流水，不新增流水、不重复扣款
    second = client.post(
        f"/api/orders/{order['order_id']}/pay", json={"channel": "mockpay"}, headers=seed.consumer_headers
    )
    assert second.status_code == 200
    assert second.json()["data"]["payment_no"] == paid["payment_no"]

    detail = client.get(f"/api/orders/{order['order_id']}", headers=seed.consumer_headers).json()["data"]
    assert detail["status"] == OrderStatus.PAID
    # 支付成功后累加销量
    assert client.get(f"/api/products/{product['product_id']}").json()["data"]["sales"] == 1


def test_payment_failures_cancel_order_and_release_stock(client, seed, monkeypatch):
    from app.trade.gateway import MockPayGateway, PayResult

    class AlwaysFailGateway(MockPayGateway):
        def pay(self, order_no, amount, channel="mockpay"):
            return PayResult(False, None, "模拟网关拒付")

    monkeypatch.setattr("app.trade.gateway.gateway", AlwaysFailGateway())

    product = _create_product(client, seed, price=50, stock=5)
    order = _place_order(client, seed, product["product_id"], 2).json()["data"]["orders"][0]
    assert _stock(client, product["product_id"]) == 3

    for _ in range(TradeRule.MAX_PAY_ATTEMPTS):
        resp = client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)
        assert resp.status_code == 422
        assert "支付失败" in resp.json()["message"]

    detail = client.get(f"/api/orders/{order['order_id']}", headers=seed.consumer_headers).json()["data"]
    assert detail["status"] == OrderStatus.CANCELLED
    # 库存原路释放（BR-06），不允许人工改数
    assert _stock(client, product["product_id"]) == 5


def test_only_one_success_payment_row(client, seed, db):
    product = _create_product(client, seed, price=68, stock=10)
    order = _place_order(client, seed, product["product_id"], 1).json()["data"]["orders"][0]
    client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)
    client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)

    rows = db.scalars(select(Payment).where(Payment.order_id == order["order_id"])).all()
    assert len(rows) == 1
    assert rows[0].status == PaymentStatus.SUCCESS


def test_expired_order_auto_cancelled_on_pay(client, seed, db):
    product = _create_product(client, seed, price=40, stock=3)
    order = _place_order(client, seed, product["product_id"], 2).json()["data"]["orders"][0]

    row = db.scalar(select(Order).where(Order.order_id == order["order_id"]))
    row.created_at = datetime.utcnow() - timedelta(minutes=TradeRule.PAY_TIMEOUT_MINUTES + 5)
    db.commit()

    resp = client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)
    assert resp.status_code == 422
    assert "自动取消" in resp.json()["message"]
    assert _stock(client, product["product_id"]) == 3


def test_cancel_order_restores_stock(client, seed):
    product = _create_product(client, seed, price=40, stock=4)
    order = _place_order(client, seed, product["product_id"], 3).json()["data"]["orders"][0]
    assert _stock(client, product["product_id"]) == 1

    resp = client.post(
        f"/api/orders/{order['order_id']}/cancel",
        json={"reason": "改主意了"},
        headers=seed.consumer_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == OrderStatus.CANCELLED
    assert _stock(client, product["product_id"]) == 4

    # 已取消的订单不能再支付
    again = client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)
    assert again.status_code == 422


# --------------------------------------------------------------------- UC-09
def test_ship_requires_paid_status_and_farm_owner(client, seed):
    product = _create_product(client, seed, price=68, stock=10)
    order = _place_order(client, seed, product["product_id"], 1).json()["data"]["orders"][0]

    # 未支付不能发货
    early = client.post(
        f"/api/orders/{order['order_id']}/ship",
        json={"express_no": "SF7788123456"},
        headers=seed.farmer_headers,
    )
    assert early.status_code == 422

    client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)

    # 消费者不能发货
    denied = client.post(
        f"/api/orders/{order['order_id']}/ship",
        json={"express_no": "SF7788123456"},
        headers=seed.consumer_headers,
    )
    assert denied.status_code == 403

    shipped = client.post(
        f"/api/orders/{order['order_id']}/ship",
        json={"express_no": "SF7788123456"},
        headers=seed.farmer_headers,
    )
    assert shipped.status_code == 200
    assert shipped.json()["data"]["status"] == OrderStatus.SHIPPED

    received = client.post(f"/api/orders/{order['order_id']}/receive", headers=seed.consumer_headers)
    assert received.status_code == 200
    assert received.json()["data"]["status"] == OrderStatus.RECEIVED


def test_receive_requires_shipped(client, seed):
    product = _create_product(client, seed, price=68, stock=10)
    order = _place_order(client, seed, product["product_id"], 1).json()["data"]["orders"][0]
    client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)

    resp = client.post(f"/api/orders/{order['order_id']}/receive", headers=seed.consumer_headers)
    assert resp.status_code == 422


# --------------------------------------------------------------------- UC-10
def test_review_requires_received_and_unique(client, seed):
    product = _create_product(client, seed, price=68, stock=10)
    order = _place_order(client, seed, product["product_id"], 1).json()["data"]["orders"][0]

    payload = {"product_id": product["product_id"], "rating": 5, "content": "个头饱满，扫码能查到批次"}

    # 未收货不能评价（UC-10 扩展 UC-09）
    blocking = client.post(
        f"/api/orders/{order['order_id']}/reviews", json=payload, headers=seed.consumer_headers
    )
    assert blocking.status_code == 422

    client.post(f"/api/orders/{order['order_id']}/pay", headers=seed.consumer_headers)
    client.post(
        f"/api/orders/{order['order_id']}/ship",
        json={"express_no": "SF7788123456"},
        headers=seed.farmer_headers,
    )
    client.post(f"/api/orders/{order['order_id']}/receive", headers=seed.consumer_headers)

    ok = client.post(f"/api/orders/{order['order_id']}/reviews", json=payload, headers=seed.consumer_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["user_display"] == "李**"

    duplicated = client.post(
        f"/api/orders/{order['order_id']}/reviews", json=payload, headers=seed.consumer_headers
    )
    assert duplicated.status_code == 409

    reviews = client.get(f"/api/products/{product['product_id']}/reviews").json()["data"]
    assert reviews["total"] == 1
    assert reviews["items"][0]["rating"] == 5


# ----------------------------------------------------------------- 并发：不超卖
def test_no_oversell_under_concurrency(tmp_path):
    """20 个线程抢 5 件库存：卖出数量必须恰好等于扣减总量，库存不能为负。

    对应报告 5.3.2 的第 3 轮修改：条件更新 + 受影响行数判断。
    用文件型 SQLite（非内存）才能真正模拟多连接写竞争。
    """
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'trade.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    setup = Session()
    farmer = User(username="farmer01", password_hash="x", display_name="张建国", role=Role.FARMER)
    buyer = User(username="consumer01", password_hash="x", display_name="李小明", role=Role.CONSUMER)
    setup.add_all([farmer, buyer])
    setup.flush()
    farm = Farm(farm_name="并发测试农场", owner_id=farmer.user_id, province="浙江省", city="杭州市")
    setup.add(farm)
    setup.flush()
    product = Product(
        product_no="SP-CONCURRENT-0001",
        farm_id=farm.farm_id,
        title="并发测试山核桃",
        crop_name="山核桃",
        spec="500g",
        unit="袋",
        price=Decimal("10.00"),
        stock=5,
        status=ProductStatus.ON_SHELF,
    )
    setup.add(product)
    setup.commit()
    product_id = product.product_id
    buyer_id = buyer.user_id
    setup.close()

    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        session = Session()
        try:
            user = session.get(User, buyer_id)
            payload = OrderCreate(
                items=[CartItemIn(product_id=product_id, quantity=1)],
                address=AddressIn(receiver="李小明", phone="13800000004", detail="浙江省杭州市临安区 1 号"),
            )
            for attempt in range(3):
                try:
                    OrderService.create_orders(session, payload, user)
                    with lock:
                        results.append(True)
                    return
                except OperationalError:
                    # SQLite 写锁竞争属于环境噪声，重试一次即可；业务性失败（库存不足）不重试
                    session.rollback()
                    if attempt == 2:
                        with lock:
                            results.append(False)
                except Exception:
                    session.rollback()
                    with lock:
                        results.append(False)
                    return
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    session = Session()
    try:
        final_stock = session.get(Product, product_id).stock
        order_count = len(
            session.scalars(select(Order).where(Order.status == OrderStatus.PENDING)).all()
        )
        sold = sum(
            item.quantity
            for order in session.scalars(select(Order)).all()
            for item in order.items
        )
    finally:
        session.close()

    succeeded = sum(1 for ok in results if ok)
    assert succeeded <= 5, f"卖出 {succeeded} 单，超过库存 5 件，出现超卖"
    assert final_stock == 5 - sold, f"库存 {final_stock} 与售出 {sold} 不一致，扣减与订单不同步"
    assert final_stock >= 0
    assert order_count == succeeded


@pytest.mark.parametrize("quantity", [1, 2, 3])
def test_quantity_limit(client, seed, quantity):
    product = _create_product(client, seed, price=10, stock=10)
    resp = _place_order(client, seed, product["product_id"], quantity)
    assert resp.status_code == 200
    assert resp.json()["data"]["orders"][0]["pay_amount"] == float(quantity * 10 + 8)


def test_quantity_over_limit_rejected(client, seed):
    product = _create_product(client, seed, stock=1000)
    resp = _place_order(client, seed, product["product_id"], TradeRule.MAX_QTY_PER_ITEM + 1)
    assert resp.status_code == 422


# ------------------------------------------------------- 跨域契约：溯源可用性
def test_order_blocked_when_all_codes_revoked(client, seed, db):
    """跨域契约：批次溯源码全部作废时，该批次商品必须停售（交易域只读 trace_code）。"""
    from app.trace.schemas import BatchCreate, CodeGenerate
    from app.trace.service import TraceService

    batch = TraceService.create_batch(
        db,
        BatchCreate(
            farm_id=seed.farm.farm_id,
            crop_name="临安山核桃",
            plot_no="A-09",
            plant_date="2026-03-20",
            harvest_date="2026-09-05",
        ),
        seed.farmer,
    )
    # 赋码前置：批次必须已采收（溯源域规则），这里直接把状态推到质检合格
    batch.status = "qualified"
    db.commit()
    codes = TraceService.generate_codes(
        db, batch.batch_id, CodeGenerate(code_type="item", count=2), seed.farmer
    )
    product = _create_product(client, seed, batch_id=batch.batch_id, stock=10)

    # 尚未作废时可以正常下单
    assert _place_order(client, seed, product["product_id"], 1).status_code == 200

    for code in codes:
        TraceService.revoke_code(db, code.code_value, "农残复检不合格", seed.farmer)

    blocked = _place_order(client, seed, product["product_id"], 1)
    assert blocked.status_code == 422
    assert "溯源码已全部作废" in blocked.json()["message"]
