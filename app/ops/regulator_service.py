"""监管回溯与处置（UC-15）。

监管侧的查询与消费者扫码不同：它要「按条件筛、按批次回溯、可导出」。
所以这里的两个入口是：
- traceback(query)：按产地 / 批次号 / 时间范围筛出批次清单，附链校验结论；
- export_csv(query)：把同一套查询结果导出成 CSV，供监管留档（列表 + 导出同源，避免两处逻辑不一致）。

同样采用「表未就绪即降级」：溯源域未合入时返回 available=false 与原因，
而不是抛 500 —— 监管账号登录后看到「该能力依赖溯源域」比看到报错更有用。
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.errors import RuleViolation
from app.db.base import engine, has_table

logger = logging.getLogger("agritrace.regulator")

_REQUIRED_TRACE_TABLES = ("batch", "trace_event")


def _has_trace() -> bool:
    return all(has_table(name) for name in _REQUIRED_TRACE_TABLES)


def _batch_columns() -> set[str]:
    try:
        return {c["name"] for c in inspect(engine).get_columns("batch")}
    except Exception:  # pragma: no cover
        return set()


class RegulatorService:
    @staticmethod
    def traceback(
        db: Session,
        *,
        farm_id: str | None = None,
        batch_no: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> dict:
        if not _has_trace():
            return {
                "available": False,
                "note": "溯源链域尚未合入当前分支，回溯能力暂不可用",
                "items": [],
                "total": 0,
            }

        cols = _batch_columns()
        where = ["1 = 1"]
        params: dict = {"limit": limit}
        if farm_id:
            where.append("b.farm_id = :farm_id")
            params["farm_id"] = farm_id
        if batch_no:
            where.append("b.batch_no LIKE :batch_no")
            params["batch_no"] = f"%{batch_no}%"
        if date_from:
            where.append("b.harvest_date >= :date_from")
            params["date_from"] = date_from
        if date_to:
            where.append("b.harvest_date <= :date_to")
            params["date_to"] = date_to

        harvested = "b.harvest_date" if "harvest_date" in cols else "NULL"
        crop = "b.crop_name" if "crop_name" in cols else "''"
        status = "b.status" if "status" in cols else "''"

        sql = f"""
            SELECT b.batch_id, b.batch_no, {crop} AS crop_name, {harvested} AS harvest_date,
                   {status} AS status, b.farm_id,
                   (SELECT COUNT(*) FROM trace_event e WHERE e.batch_id = b.batch_id) AS event_count
              FROM batch b
             WHERE {' AND '.join(where)}
             ORDER BY {harvested} DESC
             LIMIT :limit
        """
        rows = db.execute(text(sql), params).mappings().all()

        items = []
        for row in rows:
            verify = RegulatorService._verify(db, row["batch_id"])
            items.append(
                {
                    "batch_id": row["batch_id"],
                    "batch_no": row["batch_no"],
                    "crop_name": row["crop_name"],
                    "harvest_date": str(row["harvest_date"]) if row["harvest_date"] else None,
                    "status": row["status"],
                    "farm_id": row["farm_id"],
                    "event_count": int(row["event_count"] or 0),
                    "chain_ok": verify.get("ok"),
                    "broken_at": verify.get("broken_at"),
                }
            )
        return {"available": True, "note": "", "items": items, "total": len(items)}

    @staticmethod
    def _verify(db: Session, batch_id: str) -> dict:
        """链校验：优先调用溯源域 TraceService，未合入时退化为「无法判定」。"""
        try:  # pragma: no cover - 依赖溯源域
            from app.trace.service import TraceService

            result = TraceService.verify(db, batch_id)
            if isinstance(result, dict):
                return {"ok": bool(result.get("ok", True)), "broken_at": result.get("broken_at")}
            return {
                "ok": bool(getattr(result, "ok", True)),
                "broken_at": getattr(result, "broken_at", None),
            }
        except ModuleNotFoundError:
            return {"ok": None, "broken_at": None}
        except Exception as exc:  # pragma: no cover
            logger.warning("批次 %s 校验失败：%s", batch_id, exc)
            return {"ok": None, "broken_at": None}

    @staticmethod
    def batch_detail(db: Session, batch_id: str) -> dict:
        if not _has_trace():
            return {"available": False, "note": "溯源链域尚未合入当前分支", "events": []}
        rows = db.execute(
            text(
                """
                SELECT seq_no, stage, operator_id, occurred_at, location, curr_hash, prev_hash
                  FROM trace_event WHERE batch_id = :bid ORDER BY seq_no ASC
                """
            ),
            {"bid": batch_id},
        ).mappings().all()
        events = [
            {
                "seq_no": int(r["seq_no"]),
                "stage": r["stage"],
                "operator_id": r["operator_id"],
                "occurred_at": str(r["occurred_at"]),
                "location": r["location"],
                # 链上摘要只展示前后 8 位，避免页面被 64 位哈希撑爆
                "curr_hash_short": f"{r['curr_hash'][:8]}…{r['curr_hash'][-4:]}",
            }
            for r in rows
        ]
        verify = RegulatorService._verify(db, batch_id)
        return {
            "available": True,
            "batch_id": batch_id,
            "chain_ok": verify.get("ok"),
            "broken_at": verify.get("broken_at"),
            "events": events,
        }

    @staticmethod
    def export_csv(db: Session, **query) -> tuple[str, str]:
        """返回 (filename, csv_text)。导出内容与列表同源。"""
        data = RegulatorService.traceback(db, **query)
        if not data.get("available"):
            raise RuleViolation(data.get("note") or "回溯能力不可用")

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["批次ID", "批次号", "品种", "采收日期", "批次状态", "产地ID", "环节数", "链校验", "断链位置"]
        )
        for item in data["items"]:
            writer.writerow(
                [
                    item["batch_id"],
                    item["batch_no"],
                    item["crop_name"],
                    item["harvest_date"] or "",
                    item["status"],
                    item["farm_id"],
                    item["event_count"],
                    {True: "通过", False: "断链", None: "无法判定"}[item["chain_ok"]],
                    item["broken_at"] if item["broken_at"] is not None else "",
                ]
            )
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"regulator-traceback-{stamp}.csv", buffer.getvalue()
