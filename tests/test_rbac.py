"""RBAC 权限矩阵测试（整合 `feat/identity-ops` 的 `app/core/rbac.py`）。

矩阵本身是纯函数，不需要数据库；这里只锁住三条最容易回归的性质：
最小权限、权限点可复用、越权消息可定位。
"""

from __future__ import annotations

import pytest

from app.common.models import Role
from app.core.errors import Forbidden
from app.core.rbac import PERMISSIONS, ROLE_LABELS, can, ensure, permissions_of


def test_every_permission_has_known_roles():
    """矩阵里不允许出现未定义角色，否则等于把人关在门外。"""
    for permission, roles in PERMISSIONS.items():
        assert roles, f"{permission} 没有授予任何角色"
        assert set(roles) <= set(Role.ALL), f"{permission} 含未知角色：{set(roles) - set(Role.ALL)}"


def test_consumer_has_minimal_permissions():
    """最小权限：消费者只能读自己的信息，不能审核、不能看预警。"""
    granted = permissions_of(Role.CONSUMER)
    assert "user:self" in granted
    assert "cert:review" not in granted
    assert "alert:dispose" not in granted
    assert "dashboard:platform" not in granted


def test_review_only_for_operator():
    assert can(Role.OPERATOR, "cert:review") is True
    assert can(Role.FARMER, "cert:review") is False
    assert can(Role.REGULATOR, "cert:review") is False


def test_permission_point_is_reusable():
    """同一权限点可同时授予多个角色（cert:read:all 给运营与监管）。"""
    assert PERMISSIONS["cert:read:all"] == frozenset({Role.OPERATOR, Role.REGULATOR})


def test_ensure_raises_forbidden_with_hint():
    with pytest.raises(Forbidden) as exc:
        ensure(Role.FARMER, "alert:dispose")
    assert exc.value.data["required_permission"] == "alert:dispose"
    assert Role.FARMER in exc.value.message or "农户" in exc.value.message


def test_role_labels_cover_all_roles():
    assert set(ROLE_LABELS) == set(Role.ALL)
