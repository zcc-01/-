"""品类域接口层：分类目录的查询与维护。

读接口对匿名开放（前台筛选面板要用），写接口一律收口到平台运营——
分类是平台级目录，农户只能给自己的商品选分类，不能新建分类，
否则运营侧的分类树很快会被各种同义名撑爆。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.catalog.schemas import CategoryCreate, CategoryUpdate
from app.catalog.service import CategoryService
from app.common.models import Role, User
from app.core.deps import get_session, require_roles
from app.core.response import ok

router = APIRouter(prefix="/api", tags=["品类域"])


@router.get("/categories", summary="商品分类树 / 扁平列表")
def list_categories(
    include_disabled: bool = Query(default=False, description="是否包含已停用分类（后台用）"),
    flat: bool = Query(default=False, description="true 返回扁平列表，false 返回两级树"),
    db: Session = Depends(get_session),
):
    if flat:
        return ok(CategoryService.flat(db, include_disabled=include_disabled))
    return ok(CategoryService.tree(db, include_disabled=include_disabled, on_sale_only=False))


@router.get("/categories/{category_id}", summary="分类详情")
def get_category(category_id: str, db: Session = Depends(get_session)):
    from app.catalog.service import _out

    return ok(_out(CategoryService.get(db, category_id)))


@router.post("/categories", summary="新建商品分类（运营）")
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    return ok(CategoryService.create(db, payload), message="分类已创建")


@router.patch("/categories/{category_id}", summary="修改商品分类（运营）")
def update_category(
    category_id: str,
    payload: CategoryUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    return ok(CategoryService.update(db, category_id, payload), message="分类已更新")


@router.post("/categories/{category_id}/toggle", summary="启用 / 停用分类（运营）")
def toggle_category(
    category_id: str,
    enabled: bool = Query(..., description="true 启用，false 停用（停用父类会级联停用子类）"),
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    return ok(CategoryService.toggle(db, category_id, enabled), message="分类状态已更新")


@router.delete("/categories/{category_id}", summary="删除商品分类（运营）")
def delete_category(
    category_id: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_roles(Role.OPERATOR)),
):
    CategoryService.delete(db, category_id)
    return ok({"category_id": category_id}, message="分类已删除")
