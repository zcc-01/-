"""溯源异常预警（UC-14）。

规则引擎的三层结构：

    AlertRule（配置：code / level / condition / message / enabled）
        ↓  由调度器每 30 分钟拉取启用的规则
    规则求值器（每种 code 一个纯函数，输入事实、输出候选工单）
        ↓
    Alert（工单：级别、对象、去重键、处置记录）

三个刻意的设计选择：

1. 规则参数从 condition 里读，不写死常量 —— 运营改阈值不需要改代码（报告 5.2.4 人工修改点）。
2. 每条规则产出的工单带 dedupe_key（规则 + 对象 + 时间桶），靠唯一索引去重，
   调度器重复扫描不会刷出重复工单。
3. 跨省规则按「设备」聚合：同一设备在多省扫码视为正常出差，其记录不计入异常，
   把误报从 41 条压到 6 条（报告 BUG-15）。这是规则里最容易漏掉的一步。

事实来源全部走「表存在才查询」的降级策略：溯源链域未合入时，规则求值返回空集合，
既不报错也不产生误报。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.common.models import User
from app.core.audit import write_audit
from app.core.config import settings
from app.core.errors import NotFound, RuleViolation
from app.core.ids import new_id
from app.db.base import has_table
from app.ops.models import (
    Alert,
    AlertLevel,
    AlertRule,
    AlertStatus,
    RuleCode,
)

logger = logging.getLogger("agritrace.alert")


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


DEFAULT_RULES: tuple[dict, ...] = (
    {
        "code": RuleCode.NO_LOGISTICS,
        "name": "采收后长期无物流环节",
        "level": AlertLevel.WARNING,
        "condition": {"days": settings.alert_no_logistics_days},
        "message": "批次已采收 {days} 天仍无物流环节记录，存在漏录或流转停滞风险",
        "priority": 10,
    },
    {
        "code": RuleCode.CROSS_PROVINCE,
        "name": "同批次短时跨多省扫码",
        "level": AlertLevel.INFO,
        "condition": {
            "threshold": settings.alert_cross_province_threshold,
            "hours": settings.alert_cross_province_hours,
        },
        "message": "同一批次 {hours} 小时内在 {provinces} 个省份被扫码，疑似跨区窜货",
        "priority": 20,
    },
    {
        "code": RuleCode.CHAIN_BROKEN,
        "name": "溯源哈希链校验失败",
        "level": AlertLevel.CRITICAL,
        "condition": {"include_missing_seq": True},
        "message": "批次溯源链在第 {broken_at} 条记录处断裂，数据疑被篡改或缺失",
        "priority": 1,
    },
)


def _to_dict(alert: Alert) -> dict:
    return {
        "alert_id": alert.alert_id,
        "rule_code": alert.rule_code,
        "level": alert.level,
        "level_label": AlertLevel.LABELS.get(alert.level, alert.level),
        "status": alert.status,
        "status_label": AlertStatus.LABELS.get(alert.status, alert.status),
        "target_type": alert.target_type,
        "target_id": alert.target_id,
        "title": alert.title,
        "detail": alert.detail or {},
        "created_at": alert.created_at.isoformat(sep=" ", timespec="seconds")
        if alert.created_at
        else None,
        "handled_by_name": alert.handled_by_name,
        "handled_at": alert.handled_at.isoformat(sep=" ", timespec="seconds")
        if alert.handled_at
        else None,
        "resolution": alert.resolution,
    }


class AlertService:
    # ------------------------------------------------------------ 规则配置
    @staticmethod
    def ensure_rules(db: Session) -> int:
        """幂等初始化内置规则（种子与启动各调一次都安全）。"""
        created = 0
        for item in DEFAULT_RULES:
            exists = db.scalar(select(AlertRule).where(AlertRule.code == item["code"]))
            if exists is not None:
                continue
            db.add(
                AlertRule(
                    rule_id=new_id("RUL"),
                    code=item["code"],
                    name=item["name"],
                    level=item["level"],
                    enabled=True,
                    condition=item["condition"],
                    message=item["message"],
                    priority=item["priority"],
                )
            )
            created += 1
        if created:
            db.commit()
        return created

    @staticmethod
    def list_rules(db: Session) -> list[dict]:
        rows = list(db.scalars(select(AlertRule).order_by(AlertRule.priority.asc())))
        return [
            {
                "rule_id": r.rule_id,
                "code": r.code,
                "name": r.name,
                "level": r.level,
                "level_label": AlertLevel.LABELS.get(r.level, r.level),
                "enabled": r.enabled,
                "condition": r.condition or {},
                "message": r.message,
            }
            for r in rows
        ]

    @staticmethod
    def toggle_rule(db: Session, operator: User, rule_code: str, enabled: bool) -> dict:
        rule = db.scalar(select(AlertRule).where(AlertRule.code == rule_code))
        if rule is None:
            raise NotFound("规则不存在")
        rule.enabled = enabled
        db.commit()
        write_audit(
            db,
            action="alert.rule.toggle",
            user_id=operator.user_id,
            username=operator.username,
            role=operator.role,
            resource=f"alert_rule:{rule.code}",
            detail={"enabled": enabled},
        )
        return {"code": rule.code, "name": rule.name, "enabled": rule.enabled, "level": rule.level}

    # ------------------------------------------------------------ 规则求值
    @staticmethod
    def run_rules(db: Session, *, only: str | None = None) -> dict:
        """执行一轮扫描，返回本轮新建工单数。"""
        stmt = select(AlertRule).where(AlertRule.enabled.is_(True)).order_by(AlertRule.priority)
        rules = list(db.scalars(stmt))
        created: list[dict] = []
        skipped: list[str] = []
        for rule in rules:
            if only and rule.code != only:
                continue
            evaluator = _EVALUATORS.get(rule.code)
            if evaluator is None:
                skipped.append(rule.code)
                continue
            for candidate in evaluator(db, rule):
                alert = AlertService._create_if_absent(db, rule, candidate)
                if alert is not None:
                    created.append(_to_dict(alert))
        return {"created": len(created), "alerts": created, "skipped": skipped}

    @staticmethod
    def _create_if_absent(db: Session, rule: AlertRule, candidate: dict) -> Alert | None:
        dedupe_key = candidate["dedupe_key"]
        exists = db.scalar(select(Alert).where(Alert.dedupe_key == dedupe_key))
        if exists is not None:
            return None
        alert = Alert(
            alert_id=new_id("ALT"),
            rule_code=rule.code,
            level=candidate.get("level") or rule.level,
            status=AlertStatus.OPEN,
            target_type=candidate.get("target_type", "batch"),
            target_id=candidate["target_id"],
            title=candidate["title"],
            detail=candidate.get("detail", {}),
            dedupe_key=dedupe_key,
        )
        db.add(alert)
        try:
            db.commit()
        except Exception:  # pragma: no cover - 并发下唯一索引冲突即视为已存在
            db.rollback()
            return None
        db.refresh(alert)
        logger.info("生成预警工单 %s %s %s", alert.alert_id, rule.code, alert.target_id)
        return alert

    # ------------------------------------------------------------ 查询与处置
    @staticmethod
    def list_alerts(
        db: Session,
        *,
        status: str | None = None,
        level: str | None = None,
        rule_code: str | None = None,
        page_no: int = 1,
        size: int = 20,
    ) -> dict:
        stmt = select(Alert)
        if status:
            stmt = stmt.where(Alert.status == status)
        if level:
            stmt = stmt.where(Alert.level == level)
        if rule_code:
            stmt = stmt.where(Alert.rule_code == rule_code)
        rows = list(db.scalars(stmt.order_by(Alert.created_at.desc())))
        # 严重级排前面：工单列表的第一屏应该是「必须先看的」
        rows.sort(key=lambda a: (AlertLevel.ORDER.get(a.level, 9), a.created_at or datetime.min))
        total = len(rows)
        page_rows = rows[(page_no - 1) * size : (page_no - 1) * size + size]
        return {
            "items": [_to_dict(a) for a in page_rows],
            "total": total,
            "page": page_no,
            "size": size,
            "summary": AlertService.summary(db),
        }

    @staticmethod
    def summary(db: Session) -> dict:
        rows = list(db.scalars(select(Alert)))
        return {
            "open": sum(1 for a in rows if a.status == AlertStatus.OPEN),
            "acknowledged": sum(1 for a in rows if a.status == AlertStatus.ACKNOWLEDGED),
            "closed": sum(1 for a in rows if a.status == AlertStatus.CLOSED),
            "critical": sum(
                1 for a in rows if a.level == AlertLevel.CRITICAL and a.status != AlertStatus.CLOSED
            ),
        }

    @staticmethod
    def dispose(db: Session, operator: User, alert_id: str, *, action: str, resolution: str = "") -> dict:
        alert = db.get(Alert, alert_id)
        if alert is None:
            raise NotFound("预警工单不存在")

        target = {
            "acknowledge": AlertStatus.ACKNOWLEDGED,
            "close": AlertStatus.CLOSED,
            "reopen": AlertStatus.OPEN,
        }.get(action)
        if target is None:
            raise RuleViolation("处置动作只能是 acknowledge / close / reopen")

        allowed = AlertStatus.TRANSITIONS.get(alert.status, ())
        if target not in allowed:
            raise RuleViolation(
                f"工单当前为「{AlertStatus.LABELS.get(alert.status, alert.status)}」，不允许流转到"
                f"「{AlertStatus.LABELS.get(target, target)}」",
                data={"from": alert.status, "to": target},
            )
        if target == AlertStatus.CLOSED and len(resolution.strip()) < 2:
            raise RuleViolation("关闭工单必须填写处置结论")

        alert.status = target
        alert.handled_by = operator.user_id
        alert.handled_by_name = operator.display_name or operator.username
        alert.handled_at = now()
        if resolution:
            alert.resolution = resolution.strip()[:256]
        db.commit()
        db.refresh(alert)

        write_audit(
            db,
            action=f"alert.{action}",
            user_id=operator.user_id,
            username=operator.username,
            role=operator.role,
            resource=f"alert:{alert.alert_id}",
            detail={"from": alert.status, "resolution": alert.resolution},
        )
        return _to_dict(alert)

    # ------------------------------------------------------------ 演示辅助
    @staticmethod
    def simulate(db: Session, operator: User, *, rule_code: str, target_id: str) -> dict:
        """仅开发环境：按指定规则造一张工单，用于演示与截图（不影响真实数据）。"""
        rule = db.scalar(select(AlertRule).where(AlertRule.code == rule_code))
        if rule is None:
            raise NotFound("规则不存在")
        candidate = {
            "target_type": "batch",
            "target_id": target_id,
            "title": f"【演示】{rule.name}",
            "detail": {"simulated": True, "rule": rule.code},
            "dedupe_key": f"{rule.code}|batch|{target_id}|sim-{now().strftime('%Y%m%d%H%M')}",
        }
        alert = AlertService._create_if_absent(db, rule, candidate)
        if alert is None:
            raise RuleViolation("同一分钟内已生成过该演示工单，请稍后再试")
        write_audit(
            db,
            action="alert.simulate",
            user_id=operator.user_id,
            username=operator.username,
            role=operator.role,
            resource=f"alert:{alert.alert_id}",
        )
        return _to_dict(alert)


# ------------------------------------------------------------------ 规则求值器
def _eval_no_logistics(db: Session, rule: AlertRule) -> list[dict]:
    """R-01：采收后 N 天仍无物流环节。"""
    if not (has_table("batch") and has_table("trace_event")):
        return []
    days = int((rule.condition or {}).get("days", settings.alert_no_logistics_days))
    deadline = (now() - timedelta(days=days)).date().isoformat()
    rows = db.execute(
        text(
            """
            SELECT b.batch_id, b.batch_no, b.harvest_date, b.farm_id
              FROM batch b
             WHERE b.harvest_date <= :deadline
               AND b.status <> 'closed'
               AND NOT EXISTS (
                     SELECT 1 FROM trace_event e
                      WHERE e.batch_id = b.batch_id AND e.stage = 'logistics'
               )
             LIMIT 200
            """
        ),
        {"deadline": deadline},
    ).mappings().all()

    bucket = now().strftime("%Y%m%d")
    out = []
    for row in rows:
        out.append(
            {
                "target_type": "batch",
                "target_id": row["batch_id"],
                "title": f"批次 {row['batch_no']} 采收后 {days} 天无物流记录",
                "detail": {
                    "batch_no": row["batch_no"],
                    "harvest_date": str(row["harvest_date"]),
                    "farm_id": row["farm_id"],
                    "days": days,
                },
                "dedupe_key": f"{rule.code}|batch|{row['batch_id']}|{bucket}",
            }
        )
    return out


def _eval_cross_province(db: Session, rule: AlertRule) -> list[dict]:
    """R-02：24 小时内同批次跨省扫码。

    按设备聚合：某设备自身出现在多个省份，说明是同一个人的跨省移动（出差、物流跟车），
    不构成窜货证据，该设备的扫码记录整体剔除后再统计省份数。
    """
    if not has_table("scan_log"):
        return []
    params = rule.condition or {}
    threshold = int(params.get("threshold", settings.alert_cross_province_threshold))
    hours = int(params.get("hours", settings.alert_cross_province_hours))
    since = now() - timedelta(hours=hours)

    rows = db.execute(
        text(
            """
            SELECT batch_id, province, device, COUNT(*) AS cnt
              FROM scan_log
             WHERE scanned_at >= :since
               AND batch_id IS NOT NULL
               AND province IS NOT NULL
             GROUP BY batch_id, province, device
            """
        ),
        {"since": since},
    ).mappings().all()

    by_batch: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        devices = by_batch.setdefault(row["batch_id"], {})
        devices.setdefault(row["device"] or "unknown", set()).add(row["province"])

    bucket = now().strftime("%Y%m%d%H")
    out = []
    for batch_id, devices in by_batch.items():
        single_region_provinces: set[str] = set()
        roamer_provinces: set[str] = set()
        for provinces in devices.values():
            if len(provinces) > 1:
                # 同一设备跨省：视为正常移动，整体剔除
                roamer_provinces |= provinces
            else:
                single_region_provinces |= provinces
        effective = single_region_provinces - roamer_provinces
        if len(effective) < threshold:
            continue
        out.append(
            {
                "target_type": "batch",
                "target_id": batch_id,
                "title": f"批次 {batch_id} 在 {hours} 小时内于 {len(effective)} 个省份被扫码",
                "detail": {
                    "provinces": sorted(effective),
                    "window_hours": hours,
                    "excluded_roaming_provinces": sorted(roamer_provinces),
                },
                "dedupe_key": f"{rule.code}|batch|{batch_id}|{bucket}",
            }
        )
    return out


def _eval_chain_broken(db: Session, rule: AlertRule) -> list[dict]:
    """R-03：哈希链校验失败。

    校验能力由溯源链域的 TraceService 提供；未合入时跳过（不猜测实现、不产生误报）。
    """
    try:  # pragma: no cover - 合入溯源链域分支后才会走到
        from app.trace.service import TraceService
    except ModuleNotFoundError:
        return []
    if not has_table("batch"):
        return []

    batch_ids = [
        row[0]
        for row in db.execute(
            text("SELECT batch_id FROM batch WHERE event_count > 0 LIMIT 200")
        ).all()
    ]
    out = []
    for batch_id in batch_ids:
        try:
            result = TraceService.verify(db, batch_id)
        except Exception:  # pragma: no cover - 校验异常不应打断扫描
            logger.warning("批次 %s 链校验异常，跳过", batch_id)
            continue
        if isinstance(result, dict):
            broken_at, kept = result.get("broken_at"), result.get("ok", True)
        else:
            broken_at, kept = getattr(result, "broken_at", None), getattr(result, "ok", True)
        if broken_at is None and kept:
            continue
        out.append(
            {
                "target_type": "batch",
                "target_id": batch_id,
                "title": f"批次 {batch_id} 溯源链校验失败",
                "detail": {"broken_at": broken_at},
                "dedupe_key": f"{rule.code}|batch|{batch_id}|static",
            }
        )
    return out


_EVALUATORS = {
    RuleCode.NO_LOGISTICS: _eval_no_logistics,
    RuleCode.CROSS_PROVINCE: _eval_cross_province,
    RuleCode.CHAIN_BROKEN: _eval_chain_broken,
}
