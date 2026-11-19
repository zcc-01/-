"""RBAC 权限矩阵（用户与运营域核心设计，报告 4.x / UC-11）。

设计要点：
1. 权限授予「角色」而非「用户」，接口只声明所需权限，不关心谁来调用；
2. 矩阵集中在这一个文件里，评审时可以一眼看完四类角色的边界（最小权限原则）；
3. 授权判定与审计留痕分离：本模块只回答「能不能」，越权留痕由 deps.require_permission 写。
"""

from __future__ import annotations

from app.common.models import Role
from app.core.errors import Forbidden

# 权限点 → 允许的角色集合
PERMISSIONS: dict[str, frozenset[str]] = {
    # --- 账号 ---
    "user:self": frozenset(Role.ALL),
    "user:manage": frozenset({Role.OPERATOR}),
    # --- 产地（UC-12 入户与认证）---
    "farm:apply": frozenset({Role.FARMER}),
    "farm:read:self": frozenset(Role.ALL),
    "farm:read:all": frozenset({Role.OPERATOR, Role.REGULATOR}),
    "cert:submit": frozenset({Role.FARMER}),
    "cert:read:self": frozenset(Role.ALL),
    "cert:review": frozenset({Role.OPERATOR}),
    "cert:read:all": frozenset({Role.OPERATOR, Role.REGULATOR}),
    # --- 看板（UC-13）---
    "dashboard:own": frozenset({Role.FARMER}),
    "dashboard:platform": frozenset({Role.OPERATOR}),
    # --- 预警（UC-14）---
    "alert:read": frozenset({Role.OPERATOR, Role.REGULATOR}),
    "alert:dispose": frozenset({Role.OPERATOR}),
    "alert:scan": frozenset({Role.OPERATOR}),
    # --- 监管回溯（UC-15）---
    "regulator:traceback": frozenset({Role.OPERATOR, Role.REGULATOR}),
    "regulator:export": frozenset({Role.REGULATOR, Role.OPERATOR}),
}

# 角色中文名，用于审计日志与页面展示
ROLE_LABELS: dict[str, str] = {
    Role.FARMER: "农户",
    Role.CONSUMER: "消费者",
    Role.OPERATOR: "平台运营",
    Role.REGULATOR: "监管人员",
}


def permissions_of(role: str) -> list[str]:
    """某角色持有的全部权限点，供 /api/auth/me 返回给前端做菜单控制。"""
    return sorted(perm for perm, roles in PERMISSIONS.items() if role in roles)


def can(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, frozenset())


def ensure(role: str, permission: str) -> None:
    """不满足即抛 403；消息里带上角色，便于排查越权。"""
    if not can(role, permission):
        raise Forbidden(
            f"角色「{ROLE_LABELS.get(role, role)}」无权执行该操作（需要权限：{permission}）",
            data={"required_permission": permission, "role": role},
        )
