"""应用入口（公共基础层）。

启动即完成建表 + 种子数据，评审者 clone 后 `bash scripts/bootstrap.sh` 一条命令即可演示。
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.account.router import router as account_router
from app.core.config import settings
from app.core.deps import get_session
from app.core.errors import BizError, ErrorCode
from app.core.response import fail, ok
from app.db.base import init_db
from app.db.seed import seed_if_empty
from app.ops.router import router as ops_router
from app.trace.models import Stage
from app.trace.router import router as trace_router
from app.trace.service import TraceService
from app.trade.router import router as trade_router
from app.web.context import WEB_DIR, render
from app.web.router import router as web_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("agritrace")

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    try:
        if seed_if_empty():
            logger.info("已写入演示种子数据（farmer01 / operator01 / consumer01，口令 Passw0rd!）")
    except Exception as exc:  # 种子失败不应阻断启动
        logger.warning("种子数据写入失败：%s", exc)
    yield


class RequestContextMiddleware(BaseHTTPMiddleware):
    """给每个请求打上 X-Request-ID，并记录耗时，便于排查慢查询。"""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or secrets.token_hex(8)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api"):
            logger.info(
                "%s %s -> %s %.1fms rid=%s",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                request_id,
            )
        return response


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "高级软件工程课程项目 · 农产品溯源与产地直供电商平台。\n\n"
        "三个业务域已整合为单一应用：\n"
        "- 溯源链域：批次建档、环节上链、赋码、扫码、完整性校验（UC-01～UC-05）\n"
        "- 交易域：商品上架、检索筛选、购物车、下单支付、发货收货、评价（UC-06～UC-10）\n"
        "- 用户与运营域：注册鉴权、产地认证审核、经营看板、异常预警、监管回溯（UC-11～UC-15）"
    ),
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
# 路由挂载顺序即 OpenAPI 文档中的分组顺序：公共 → 溯源 → 交易 → 运营
app.include_router(account_router)
app.include_router(trace_router)
app.include_router(trade_router)
app.include_router(ops_router)
# 页面路由（服务端渲染），与 /api 同进程、同 service 层
app.include_router(web_router)


# ------------------------------------------------------------ 异常统一处理
@app.exception_handler(BizError)
async def biz_error_handler(_: Request, exc: BizError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=fail(exc.code, exc.message, exc.data))


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"field": ".".join(str(x) for x in err.get("loc", [])[1:]), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=fail(ErrorCode.PARAM_INVALID, "参数校验失败", details),
    )


@app.exception_handler(Exception)
async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content=fail(ErrorCode.INTERNAL, "服务内部错误，请稍后重试"),
    )


# ------------------------------------------------------------------ 基础路由
@app.get("/api/health", tags=["公共"], summary="健康检查")
def health() -> dict:
    return ok(
        {"status": "up", "env": settings.env, "db": "sqlite" if settings.is_sqlite else "mysql"}
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, db: Session = Depends(get_session)):
    """统一入口：首页承担「推荐 + 导航」，商品取自交易域，链路环节取自溯源域。"""
    from app.trade.service import ProductService

    latest = ProductService.list_products(db, sort="new", size=8)
    certified = ProductService.list_products(db, sort="sales", certified_only=True, size=4)
    return render(
        request,
        "index.html",
        {
            "stage_labels": Stage.LABELS,
            "featured": latest["items"],
            "certified": certified["items"],
            "categories": ProductService.categories(db),
            "product_total": latest["total"],
        },
    )


@app.get("/trace/{code_value}", response_class=HTMLResponse, include_in_schema=False)
def trace_page(request: Request, code_value: str, db: Session = Depends(get_session)):
    """扫码落地页：时间轴 + 链校验结论 + 断链告警条（报告 5.2.2 的界面要求）。"""
    try:
        data = TraceService.scan(
            db,
            code_value,
            province=request.query_params.get("province"),
            city=request.query_params.get("city"),
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except BizError as exc:
        status = exc.http_status if exc.http_status < 500 else 400
        return render(
            request, "error.html", {"message": exc.message, "code": exc.code}, status_code=status
        )
    return render(request, "scan.html", {"data": data, "stage_labels": Stage.LABELS})
