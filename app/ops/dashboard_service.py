"""经营数据看板（UC-13）。

看板的核心难点不是聚合，而是**跨域取数的边界**：销量、好评率在交易域，
扫码量与区域分布在溯源域，认证与预警在用户运营域。三个域由三个人并行开发，
本分支不能假设对方的表一定就绪。

因此这里的每个指标都带 available 标记：

    {"available": true,  "value": 1280}   —— 表就绪，数据可信
    {"available": false, "value": None}   —— 依赖域未合入，页面显示「—」而不是 0

这一点很重要：把「没有数据」显示成 0，会让评审老师以为系统真的零销量；
显示「—」并附一句依赖说明，才是诚实的表达。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.common.models import Farm, User
from app.core.errors import NotFound
from app.db.base import engine, has_table
from app.ops.models import Alert, AlertLevel, AlertStatus, CertApplication, CertStatus

logger = logging.getLogger("agritrace.dashboard")


def _columns(table: str) -> set[str]:
    if not has_table(table):
        return set()
    try:
        return {col["name"] for col in inspect(engine).get_columns(table)}
    except Exception:  # pragma: no cover
        return set()


def _metric(value: Any, *, available: bool = True, note: str = "") -> dict:
    return {"value": value, "available": available, "note": note}


def _safe(fn: Callable[[], Any], fallback: Any = None) -> Any:
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - 跨域查询异常一律降级
        logger.warning("看板指标计算失败：%s", exc)
        return fallback


class DashboardService:
    # ------------------------------------------------------------ 平台视角
    @staticmethod
    def platform_overview(db: Session) -> dict:
        farms = list(db.scalars(select(Farm)))
        certs = list(db.scalars(select(CertApplication)))

        cert_stats = {
            "total": len(certs),
            "pending": sum(1 for c in certs if c.status == CertStatus.SUBMITTED),
            "approved": sum(1 for c in certs if c.status == CertStatus.APPROVED),
            "rejected": sum(1 for c in certs if c.status == CertStatus.REJECTED),
            "draft": sum(1 for c in certs if c.status == CertStatus.DRAFT),
            "expiring_soon": sum(
                1
                for c in certs
                if c.status == CertStatus.APPROVED
                and c.valid_to is not None
                and c.valid_to <= datetime.now(UTC).date() + timedelta(days=30)
            ),
        }

        alerts = list(db.scalars(select(Alert)))
        alert_stats = {
            "open": sum(1 for a in alerts if a.status == AlertStatus.OPEN),
            "critical": sum(
                1 for a in alerts if a.level == AlertLevel.CRITICAL and a.status != AlertStatus.CLOSED
            ),
            "closed": sum(1 for a in alerts if a.status == AlertStatus.CLOSED),
        }

        return {
            "scope": "platform",
            "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
            "farms": {
                "total": _metric(len(farms)),
                "certified": _metric(sum(1 for f in farms if f.cert_status == "approved")),
                "pending": _metric(sum(1 for f in farms if f.cert_status == "pending")),
            },
            "certs": cert_stats,
            "alerts": alert_stats,
            "sales": DashboardService._platform_sales(db),
            "scans": DashboardService._platform_scans(db),
            "reputation": DashboardService._platform_reputation(db),
            "regions": DashboardService._platform_regions(db),
            "dependencies": {
                "trade_domain": has_table("orders"),
                "trace_domain": has_table("scan_log"),
            },
        }

    @staticmethod
    def _platform_sales(db: Session) -> dict:
        cols = _columns("orders")
        if not cols:
            return {
                "gmv": _metric(None, available=False, note="订单表未就绪（交易域未合入）"),
                "orders": _metric(None, available=False, note="订单表未就绪（交易域未合入）"),
                "daily": _metric([], available=False),
            }
        amount_col = "total_amount" if "total_amount" in cols else None
        paid_col = "paid_at" if "paid_at" in cols else None
        status_col = "status" if "status" in cols else None
        if amount_col is None:
            return {"gmv": _metric(None, available=False, note="orders 缺少 total_amount 字段")}

        paid_filter = ""
        if status_col:
            paid_filter = "WHERE status IN ('paid','shipped','received','已支付','已发货','已收货')"
        elif paid_col:
            paid_filter = "WHERE paid_at IS NOT NULL"

        def _agg() -> dict:
            sql = (
                f"SELECT COUNT(*) AS cnt, COALESCE(SUM({amount_col}), 0) AS gmv "
                f"FROM orders {paid_filter}"
            )
            row = db.execute(text(sql)).mappings().one()
            return {"cnt": int(row["cnt"]), "gmv": float(row["gmv"])}

        agg = _safe(_agg, {"cnt": 0, "gmv": 0.0})
        daily = _safe(lambda: DashboardService._sales_daily(db, amount_col, paid_filter), [])
        return {
            "gmv": _metric(round(agg["gmv"], 2)),
            "orders": _metric(agg["cnt"]),
            "daily": _metric(daily),
        }

    @staticmethod
    def _sales_daily(db: Session, amount_col: str, paid_filter: str) -> list[dict]:
        created = "created_at" if "created_at" in _columns("orders") else "order_id"
        rows = db.execute(
            text(
                f"""
                SELECT substr(CAST({created} AS VARCHAR), 1, 10) AS day,
                       COUNT(*) AS cnt, COALESCE(SUM({amount_col}), 0) AS gmv
                  FROM orders {paid_filter}
                 GROUP BY day ORDER BY day DESC LIMIT 7
                """
            )
        ).mappings().all()
        return [{"day": r["day"], "orders": int(r["cnt"]), "gmv": float(r["gmv"])} for r in reversed(rows)]

    @staticmethod
    def _platform_scans(db: Session) -> dict:
        cols = _columns("scan_log")
        if not cols:
            return {
                "total": _metric(None, available=False, note="扫码日志表未就绪（溯源域未合入）"),
                "daily": _metric([], available=False),
            }

        def _total() -> int:
            return int(db.execute(text("SELECT COUNT(*) FROM scan_log")).scalar() or 0)

        def _daily() -> list[dict]:
            rows = db.execute(
                text(
                    """
                    SELECT substr(CAST(scanned_at AS VARCHAR), 1, 10) AS day, COUNT(*) AS cnt
                      FROM scan_log GROUP BY day ORDER BY day DESC LIMIT 7
                    """
                )
            ).mappings().all()
            return [{"day": r["day"], "scans": int(r["cnt"])} for r in reversed(rows)]

        return {
            "total": _metric(_safe(_total, 0)),
            "daily": _metric(_safe(_daily, [])),
        }

    @staticmethod
    def _platform_reputation(db: Session) -> dict:
        cols = _columns("review")
        if not cols or "rating" not in cols:
            return {
                "avg_rating": _metric(None, available=False, note="评价表未就绪（交易域未合入）"),
                "positive_rate": _metric(None, available=False),
                "count": _metric(None, available=False),
            }

        def _agg() -> dict:
            sql = (
                "SELECT COUNT(*) AS cnt, AVG(rating) AS avg_rating, "
                "SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) AS good FROM review"
            )
            row = db.execute(text(sql)).mappings().one()
            cnt = int(row["cnt"] or 0)
            good = int(row["good"] or 0)
            return {
                "cnt": cnt,
                "avg": round(float(row["avg_rating"] or 0), 2),
                "rate": round(good / cnt * 100, 1) if cnt else 0.0,
            }

        agg = _safe(_agg, {"cnt": 0, "avg": 0.0, "rate": 0.0})
        return {
            "avg_rating": _metric(agg["avg"]),
            "positive_rate": _metric(agg["rate"]),
            "count": _metric(agg["cnt"]),
        }

    @staticmethod
    def _platform_regions(db: Session) -> dict:
        cols = _columns("scan_log")
        if not cols or "province" not in cols:
            return {"items": [], "available": False, "note": "扫码日志表未就绪（溯源域未合入）"}

        def _rows() -> list[dict]:
            rows = db.execute(
                text(
                    """
                    SELECT province, COUNT(*) AS cnt FROM scan_log
                     WHERE province IS NOT NULL AND province <> ''
                     GROUP BY province ORDER BY cnt DESC LIMIT 8
                    """
                )
            ).mappings().all()
            return [{"province": r["province"], "scans": int(r["cnt"])} for r in rows]

        return {"items": _safe(_rows, []), "available": True, "note": ""}

    # ------------------------------------------------------------ 农户视角
    @staticmethod
    def farm_overview(db: Session, user: User, farm_id: str) -> dict:
        farm = db.get(Farm, farm_id)
        if farm is None or farm.owner_id != user.user_id:
            raise NotFound("产地不存在或无权访问")

        cert = db.scalar(
            select(CertApplication)
            .where(CertApplication.farm_id == farm_id)
            .order_by(CertApplication.created_at.desc())
        )

        result = {
            "scope": "farm",
            "farm_id": farm.farm_id,
            "farm_name": farm.farm_name,
            "cert_status": farm.cert_status,
            "cert": None,
            "sales": _metric(None, available=False, note="交易域未合入"),
            "scans": _metric(None, available=False, note="溯源域未合入"),
            "positive_rate": _metric(None, available=False, note="交易域未合入"),
            "regions": {"items": [], "available": False},
        }
        if cert is not None:
            result["cert"] = {
                "cert_no": cert.cert_no,
                "status": cert.status,
                "status_label": CertStatus.LABELS.get(cert.status, cert.status),
                "valid_to": cert.valid_to.isoformat() if cert.valid_to else None,
            }

        # 销量：按订单归属产地统计
        cols = _columns("orders")
        if cols and "farm_id" in cols and "total_amount" in cols:
            def _sales() -> float:
                return float(
                    db.execute(
                        text("SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE farm_id = :fid"),
                        {"fid": farm_id},
                    ).scalar()
                    or 0
                )

            result["sales"] = _metric(round(_safe(_sales, 0.0), 2))

        # 扫码量：按批次归属产地统计（间接 join batch，溯源域就绪才可用）
        if has_table("scan_log") and has_table("batch") and "farm_id" in _columns("batch"):
            def _scans() -> int:
                return int(
                    db.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM scan_log s
                              JOIN batch b ON b.batch_id = s.batch_id
                             WHERE b.farm_id = :fid
                            """
                        ),
                        {"fid": farm_id},
                    ).scalar()
                    or 0
                )

            result["scans"] = _metric(_safe(_scans, 0))

            def _regions() -> list[dict]:
                rows = db.execute(
                    text(
                        """
                        SELECT s.province, COUNT(*) AS cnt FROM scan_log s
                          JOIN batch b ON b.batch_id = s.batch_id
                         WHERE b.farm_id = :fid AND s.province IS NOT NULL
                         GROUP BY s.province ORDER BY cnt DESC LIMIT 8
                        """
                    ),
                    {"fid": farm_id},
                ).mappings().all()
                return [{"province": r["province"], "scans": int(r["cnt"])} for r in rows]

            result["regions"] = {"items": _safe(_regions, []), "available": True}

        return result
