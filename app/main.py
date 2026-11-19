"""应用入口（用户与运营域 + 公共基础）。

启动即完成：建表 → 种子数据（幂等）→ 预警规则初始化 → 起一个轻量调度协程。
评审者 clone 后 `bash scripts/bootstrap.sh` 一条命令即可演示。

调度器刻意不用 APScheduler：只为一个「每 30 分钟跑一次」的任务引入调度框架，
依赖成本大于收益。这里用 asyncio 协程 + to_thread 把同步 ORM 调用挪出事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.account.router import farm_router
from app.account.router import router as account_router
from app.core.config import settings
from app.core.errors import BizError, ErrorCode
from app.core.response import fail, ok
from app.db.base import SessionLocal, init_db
from app.ops.alert_service import AlertService
from app.ops.cert_service import CertService
from app.ops.router import cert_router, farm_dash_router, ops_router, regulator_router
from app.web.router import router as web_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("agritrace")

WEB_DIR = Path(__file__).resolve().parent / "web"


def _run_maintenance() -> None:
    """一轮后台维护：规则扫描 + 认证到期处理。失败只记日志，不影响服务。"""
    db = SessionLocal()
    try:
        AlertService.ensure_rules(db)
        created = AlertService.run_rules(db)
        expired = CertService.sweep_expired(db)
        logger.info(
            "后台维护完成：新增预警 %s 条，失效认证 %s 条，待续期 %s 条",
            created["created"],
            expired["expired"],
            len(expired["reminders"]),
        )
    except Exception as exc:  # pragma: no cover - 后台任务不允许把服务带崩
        logger.warning("后台维护失败：%s", exc)
    finally:
        db.close()


def _seed() -> None:
    try:
        from app.db.seed import seed_if_empty

        if seed_if_empty():
            logger.info(
                "已写入演示种子数据（farmer01 / operator01 / regulator01 / consumer01，口令 Passw0rd!）"
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("种子数据写入失败：%s", exc)


async def _maintenance_loop() -> None:
    interval = max(1, settings.alert_scan_minutes) * 60
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(_run_maintenance)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _seed()
    _run_maintenance()

    task: asyncio.Task | None = None
    if settings.alert_enable_scheduler:
        task = asyncio.create_task(_maintenance_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


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
    description="高级软件工程课程项目 · 用户与运营域（王×× 交付部分：UC-11～UC-15）",
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

app.include_router(account_router)
app.include_router(farm_router)
app.include_router(cert_router)
app.include_router(farm_dash_router)
app.include_router(ops_router)
app.include_router(regulator_router)
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
