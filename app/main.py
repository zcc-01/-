"""应用入口（公共基础层）。

启动即完成建表 + 种子数据，评审者 clone 后 `bash scripts/bootstrap.sh` 一条命令即可演示。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.account.router import router as account_router
from app.core.config import settings
from app.core.deps import get_session
from app.core.errors import BizError, ErrorCode
from app.core.response import fail, ok
from app.db.base import init_db
from app.db.seed import seed_if_empty
from app.trace.models import Stage
from app.trace.router import router as trace_router
from app.trace.service import TraceService
from app.trade.models import OrderStatus, TradeRule
from app.trade.router import router as trade_router
from app.trade.service import OrderService

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("agritrace")

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _sweep_orders_once() -> int:
    """后台清理入口：独立会话，异常不外抛（定时任务挂掉不能影响主服务）。"""
    from app.db.base import SessionLocal

    session = SessionLocal()
    try:
        return OrderService.sweep_expired(session)
    finally:
        session.close()


async def _order_sweeper(interval_seconds: int = 60) -> None:
    """每 60 秒扫一次超时未支付订单（UC-08 异常流程 6a 的定时兜底）。

    放在 lifespan 里而不是引入 APScheduler：课程项目不值得为一次定时扫描多装一个调度器，
    asyncio 任务 + to_thread 已经能覆盖单机部署，多实例部署时退化为「谁扫到谁处理」，
    依赖 _cancel 里的状态判断保证幂等。
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            cancelled = await asyncio.to_thread(_sweep_orders_once)
            if cancelled:
                logger.info("定时任务：自动取消 %s 笔超时未支付订单", cancelled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 依赖数据库可用性
            logger.warning("定时取消超时订单失败：%s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    try:
        if seed_if_empty():
            logger.info("已写入演示种子数据（farmer01 / operator01 / consumer01，口令 Passw0rd!）")
    except Exception as exc:  # 种子失败不应阻断启动
        logger.warning("种子数据写入失败：%s", exc)

    sweeper = asyncio.create_task(_order_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


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
    description="高级软件工程课程项目 · 溯源链域 + 交易域 + 平台公共基础",
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.include_router(account_router)
app.include_router(trace_router)
app.include_router(trade_router)


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
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"app_name": settings.app_name, "stage_labels": Stage.LABELS},
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
        return templates.TemplateResponse(
            request, "error.html", {"message": exc.message, "code": exc.code}, status_code=status
        )
    return templates.TemplateResponse(
        request,
        "scan.html",
        {"data": data, "stage_labels": Stage.LABELS, "app_name": settings.app_name},
    )


# ------------------------------------------------------- 交易域页面（UC-06～UC-10）
def _trade_context(request: Request, **extra) -> dict:
    """商城页面共用上下文：页面只负责渲染骨架，数据由前端调用 /api 接口拉取。"""
    return {
        "app_name": settings.app_name,
        "freight_flat": TradeRule.FREIGHT_FLAT,
        "free_freight_threshold": TradeRule.FREE_FREIGHT_THRESHOLD,
        "pay_timeout_minutes": TradeRule.PAY_TIMEOUT_MINUTES,
        "order_status_labels": OrderStatus.LABELS,
        "active": extra.pop("active", "shop"),
        **extra,
    }


@app.get("/shop", response_class=HTMLResponse, include_in_schema=False)
def shop_page(request: Request):
    return templates.TemplateResponse(request, "trade/shop.html", _trade_context(request, active="shop"))


@app.get("/shop/{product_id}", response_class=HTMLResponse, include_in_schema=False)
def product_page(request: Request, product_id: str):
    return templates.TemplateResponse(
        request, "trade/product.html", _trade_context(request, active="shop", product_id=product_id)
    )


@app.get("/cart", response_class=HTMLResponse, include_in_schema=False)
def cart_page(request: Request):
    return templates.TemplateResponse(request, "trade/cart.html", _trade_context(request, active="cart"))


@app.get("/orders", response_class=HTMLResponse, include_in_schema=False)
def orders_page(request: Request):
    return templates.TemplateResponse(request, "trade/orders.html", _trade_context(request, active="orders"))
