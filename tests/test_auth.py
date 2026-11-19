"""UC-11 注册登录与鉴权：主流程 + 口令强度 + 锁定 + 验证码登录。"""

from __future__ import annotations

from tests.conftest import auth, login


def test_register_and_me(client):
    resp = client.post(
        "/api/auth/register",
        json={
            "username": "newfarmer",
            "password": "Farm2026abc",
            "phone": "13812345678",
            "display_name": "新农户",
            "role": "farmer",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["access_token"]
    assert data["user"]["role"] == "farmer"
    assert "cert:submit" in data["user"]["permissions"]

    me = client.get("/api/auth/me", headers=auth(data["access_token"]))
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "newfarmer"
    # 手机号脱敏（报告 3.4 合规性）
    assert me.json()["data"]["phone"] == "138****5678"


def test_register_weak_password_rejected(client):
    resp = client.post(
        "/api/auth/register",
        json={"username": "weakpwd", "password": "12345678", "role": "consumer"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 42201
    assert any("弱口令" in p or "字母" in p for p in body["data"]["problems"])


def test_register_duplicate_username(client):
    resp = client.post(
        "/api/auth/register",
        json={"username": "farmer01", "password": "Farm2026abc", "role": "farmer"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40901


def test_register_operator_forbidden(client):
    """运营/监管账号不允许自助注册（避免越权注册）。"""
    resp = client.post(
        "/api/auth/register",
        json={"username": "fakeops", "password": "Farm2026abc", "role": "operator"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 40001


def test_login_wrong_password(client):
    resp = client.post("/api/auth/login", json={"username": "farmer01", "password": "WrongPwd123"})
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_login_locked_after_failures(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "consumer01", "password": "WrongPwd123"})
    resp = client.post("/api/auth/login", json={"username": "consumer01", "password": "WrongPwd123"})
    assert resp.status_code == 423
    assert resp.json()["code"] == 42301


def test_sms_code_login(client):
    sent = client.post("/api/auth/sms/send", json={"phone": "13800000002"})
    assert sent.status_code == 200
    code = sent.json()["data"]["debug_code"]

    bad = client.post("/api/auth/sms/login", json={"phone": "13800000002", "code": "000000"})
    assert bad.status_code == 401

    ok = client.post("/api/auth/sms/login", json={"phone": "13800000002", "code": code})
    assert ok.status_code == 200
    assert ok.json()["data"]["user"]["username"] == "farmer02"

    # 验证码一次性：同一个码不能二次登录
    again = client.post("/api/auth/sms/login", json={"phone": "13800000002", "code": code})
    assert again.status_code == 422


def test_change_password(client, farmer_token):
    resp = client.post(
        "/api/auth/password",
        headers=auth(farmer_token),
        json={"old_password": "Passw0rd!", "new_password": "NewPass2026"},
    )
    assert resp.status_code == 200
    assert login(client, "farmer01", "NewPass2026")

    weak = client.post(
        "/api/auth/password",
        headers=auth(farmer_token),
        json={"old_password": "NewPass2026", "new_password": "abcdefgh"},
    )
    assert weak.status_code == 422
