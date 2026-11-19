"""品类域与商品目录模块测试。

覆盖新增能力的硬约束：
- 分类树最多两级，父子关系变更不能绕成环；
- 停用父类必须级联停用子类，否则前台会出现「父类隐藏、子类还能筛到」的孤岛；
- 分类下还有商品或子类时禁止删除（数据维护最容易踩的坑）；
- 按一级分类筛选要能覆盖其全部子类商品；
- 关键词检索要命中标签与产地，而不是只匹配名称；
- 商品维护（改价/改库存/改分类/上下架）复用同一套服务与权限校验。
"""

from __future__ import annotations

import itertools

import pytest

from app.catalog.models import Category
from app.catalog.schemas import CategoryCreate, CategoryUpdate
from app.catalog.service import CategoryService
from tests.conftest import create_batch

_PLOT = itertools.count(1)


def make_category(db, code: str, name: str, parent: Category | None = None) -> dict:
    return CategoryService.create(
        db,
        CategoryCreate(
            code=code,
            name=name,
            parent_id=parent.category_id if parent else None,
            icon="🍊",
        ),
    )


def make_product(client, seed, db, *, name="赣南脐橙 5kg", category_id=None, price=69.8, stock=10, **extra):
    """建批次 → 上架商品（品类与商品测试专用，每次换一个地块号避开唯一约束）。"""
    batch = create_batch(db, seed, status="qualified", plot_no=f"C-{next(_PLOT):03d}", crop_name=name)
    body = {
        "batch_id": batch.batch_id,
        "name": name,
        "category_id": category_id,
        "price_yuan": price,
        "stock": stock,
        "unit": "箱",
        "description": "产地直供",
        **extra,
    }
    resp = client.post("/api/products", json=body, headers=seed.farmer_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ 分类树
def test_category_tree_is_two_levels(client, db, seed):
    fruit = make_category(db, "fruit", "水果")
    citrus = make_category(db, "fruit-citrus", "柑橘类", parent=CategoryService.get(db, fruit["category_id"]))

    assert citrus["level"] == 2
    tree = CategoryService.tree(db, include_disabled=True, on_sale_only=False)
    assert [n["code"] for n in tree] == ["fruit"]
    assert [c["code"] for c in tree[0]["children"]] == ["fruit-citrus"]

    # 二级分类不能再挂子类
    resp = client.post(
        "/api/categories",
        json={"code": "fruit-citrus-sweet", "name": "甜橙", "parent_id": citrus["category_id"]},
        headers=seed.operator_headers,
    )
    assert resp.status_code == 422
    assert "两级" in resp.json()["message"]


def test_category_code_and_name_uniqueness(client, db, seed):
    make_category(db, "fruit", "水果")
    dup = client.post(
        "/api/categories",
        json={"code": "fruit", "name": "水果二"},
        headers=seed.operator_headers,
    )
    assert dup.status_code == 409
    assert "编码" in dup.json()["message"]

    dup_name = client.post(
        "/api/categories",
        json={"code": "fruit2", "name": "水果"},
        headers=seed.operator_headers,
    )
    assert dup_name.status_code == 409

    bad_code = client.post(
        "/api/categories",
        json={"code": "Fruit!", "name": "水果三"},
        headers=seed.operator_headers,
    )
    assert bad_code.status_code == 422


def test_toggle_parent_cascades_to_children(client, db, seed):
    fruit = make_category(db, "fruit", "水果")
    parent = CategoryService.get(db, fruit["category_id"])
    make_category(db, "fruit-citrus", "柑橘类", parent=parent)

    resp = client.post(
        f"/api/categories/{fruit['category_id']}/toggle?enabled=false",
        headers=seed.operator_headers,
    )
    assert resp.status_code == 200

    db.expire_all()
    assert CategoryService.get(db, fruit["category_id"]).enabled == 0
    child = CategoryService.get_by_code(db, "fruit-citrus")
    assert child is not None and child.enabled == 0
    # 前台分类树默认只返回启用的分类
    assert CategoryService.tree(db) == []


def test_delete_is_blocked_when_children_or_products_exist(client, db, seed):
    fruit = make_category(db, "fruit", "水果")
    parent = CategoryService.get(db, fruit["category_id"])
    make_category(db, "fruit-citrus", "柑橘类", parent=parent)

    resp = client.delete(f"/api/categories/{fruit['category_id']}", headers=seed.operator_headers)
    assert resp.status_code == 422
    assert "子类" in resp.json()["message"]

    child = CategoryService.get_by_code(db, "fruit-citrus")
    assert child is not None
    make_product(client, seed, db, name="赣南脐橙", category_id=child.category_id)
    resp = client.delete(f"/api/categories/{child.category_id}", headers=seed.operator_headers)
    assert resp.status_code == 422
    assert "商品" in resp.json()["message"]


def test_category_write_requires_operator(client, db, seed):
    resp = client.post(
        "/api/categories",
        json={"code": "fruit", "name": "水果"},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 403


def test_moving_category_with_children_is_rejected(db):
    fruit = make_category(db, "fruit", "水果")
    vegetable = make_category(db, "vegetable", "蔬菜")
    parent = CategoryService.get(db, fruit["category_id"])
    make_category(db, "fruit-citrus", "柑橘类", parent=parent)

    with pytest.raises(Exception) as exc:
        CategoryService.update(
            db, fruit["category_id"], CategoryUpdate(parent_id=vegetable["category_id"])
        )
    assert "子类" in str(exc.value)


# ------------------------------------------------------------------ 检索筛选
def test_filter_by_parent_category_includes_children(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    parent = CategoryService.get(db, fruit["category_id"])
    citrus = make_category(db, "fruit-citrus", "柑橘类", parent=parent)
    apple = make_category(db, "fruit-pome", "苹果梨类", parent=parent)

    make_product(client, seed, db, name="赣南脐橙", category_id=citrus["category_id"])
    make_product(client, seed, db, name="烟台苹果", category_id=apple["category_id"])
    make_product(client, seed, db, name="无名商品")

    data = client.get("/api/products?category=fruit").json()["data"]
    assert data["total"] == 2
    assert {p["category_code"] for p in data["items"]} == {"fruit-citrus", "fruit-pome"}

    only_citrus = client.get("/api/products?category=fruit-citrus").json()["data"]
    assert [p["name"] for p in only_citrus["items"]] == ["赣南脐橙"]


def test_keyword_matches_tags_and_origin(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    make_product(
        client,
        seed,
        db,
        name="赣南脐橙",
        category_id=fruit["category_id"],
        tags="当季,冷链直发",
        origin="江西省赣州市信丰县",
    )
    make_product(client, seed, db, name="烟台苹果", category_id=fruit["category_id"], origin="山东省烟台市")

    assert [p["name"] for p in client.get("/api/products?keyword=冷链").json()["data"]["items"]] == ["赣南脐橙"]
    assert [p["name"] for p in client.get("/api/products?keyword=赣州").json()["data"]["items"]] == ["赣南脐橙"]
    assert [p["name"] for p in client.get("/api/products?origin=山东").json()["data"]["items"]] == ["烟台苹果"]


def test_price_and_stock_filters(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    make_product(client, seed, db, name="便宜的", category_id=fruit["category_id"], price=9.9, stock=5)
    make_product(client, seed, db, name="贵的", category_id=fruit["category_id"], price=199.0, stock=0)

    assert [p["name"] for p in client.get("/api/products?price_min=50").json()["data"]["items"]] == ["贵的"]
    assert [p["name"] for p in client.get("/api/products?in_stock_only=true").json()["data"]["items"]] == ["便宜的"]


# ------------------------------------------------------------------ 数据维护
def test_product_update_keeps_category_name_in_sync(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    vegetable = make_category(db, "vegetable", "蔬菜")
    product = make_product(client, seed, db, name="赣南脐橙", category_id=fruit["category_id"])
    assert product["category"] == "水果"

    resp = client.patch(
        f"/api/products/{product['product_id']}",
        json={
            "category_id": vegetable["category_id"],
            "spec": "5kg/箱",
            "grade": "一级果",
            "origin": "山东省潍坊市",
            "shelf_life": "冷藏 7 天",
            "storage": "0~4℃",
            "tags": "当季， 冷链 ,当季",
            "image_url": "/static/img/products/tomato.svg",
        },
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["category"] == "蔬菜"
    assert data["category_path"] == "蔬菜"
    assert data["tags"] == ["当季", "冷链"]  # 中文逗号归一 + 去重
    assert data["spec"] == "5kg/箱"


def test_invalid_image_url_is_dropped(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    product = make_product(client, seed, db, name="赣南脐橙", category_id=fruit["category_id"])
    resp = client.patch(
        f"/api/products/{product['product_id']}",
        json={"image_url": "javascript:alert(1)"},
        headers=seed.farmer_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["image_url"] == ""


def test_off_shelf_hides_from_list_and_blocks_cart(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    product = make_product(client, seed, db, name="赣南脐橙", category_id=fruit["category_id"])

    resp = client.patch(
        f"/api/products/{product['product_id']}", json={"status": "off_shelf"}, headers=seed.farmer_headers
    )
    assert resp.status_code == 200
    assert client.get("/api/products").json()["data"]["total"] == 0
    assert client.get("/api/products?include_off_shelf=true").json()["data"]["total"] == 1

    cart = client.post(
        "/api/cart",
        json={"product_id": product["product_id"], "quantity": 1},
        headers=seed.consumer_headers,
    )
    assert cart.status_code == 422
    assert "下架" in cart.json()["message"]


def test_operator_can_edit_any_product(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    product = make_product(client, seed, db, name="赣南脐橙", category_id=fruit["category_id"])
    resp = client.patch(
        f"/api/products/{product['product_id']}", json={"stock": 66}, headers=seed.operator_headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["stock"] == 66


# ------------------------------------------------------------------ 页面
def test_shop_page_renders_category_tree(client, seed, db):
    fruit = make_category(db, "fruit", "水果")
    parent = CategoryService.get(db, fruit["category_id"])
    citrus = make_category(db, "fruit-citrus", "柑橘类", parent=parent)
    make_product(client, seed, db, name="赣南脐橙", category_id=citrus["category_id"])

    page = client.get("/shop?category=fruit")
    assert page.status_code == 200
    assert "赣南脐橙" in page.text
    assert "柑橘类" in page.text

    detail = client.get(f"/shop/{client.get('/api/products').json()['data']['items'][0]['product_id']}")
    assert detail.status_code == 200
    assert "商品详情" in detail.text


def test_console_catalog_page_requires_login_and_renders(client, seed, db):
    make_category(db, "fruit", "水果")
    assert client.get("/console/catalog", follow_redirects=False).status_code == 303

    login = client.post(
        "/login",
        data={"username": "operator01", "password": "Passw0rd!", "next": "/console/catalog"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    page = client.get("/console/catalog")
    assert page.status_code == 200
    assert "分类管理" in page.text
    assert "水果" in page.text


def test_console_category_create_via_form(client, seed, db):
    login = client.post(
        "/login",
        data={"username": "operator01", "password": "Passw0rd!", "next": "/console/catalog"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    resp = client.post(
        "/console/categories",
        data={"code": "fruit", "name": "水果", "parent_id": "", "icon": "🍊", "sort_no": 10, "description": "当季鲜果"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert CategoryService.get_by_code(db, "fruit") is not None


def test_seed_catalog_is_idempotent(db):
    """ensure_catalog 可以重复执行：分类与商品都不会翻倍（老库升级靠它补齐）。"""
    from app.catalog.seed import ensure_catalog

    first = ensure_catalog(db)
    assert first["categories"] > 0
    second = ensure_catalog(db)
    assert second["categories"] == 0
    assert second["products"] == 0

    codes = {c.code for c in db.query(Category).all()}
    assert {"fruit", "vegetable", "grain", "tea", "nuts", "dried", "poultry"} <= codes
