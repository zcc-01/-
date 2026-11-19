"""模型注册表：保证 Base.metadata 在 create_all 前收齐所有表。

新增业务域时在这里补一行 import，避免出现「表没建出来」的低级问题。
溯源链域（app.trace）与交易域（app.trade）由其他成员的分支提供，
这里用 suppress 容忍它们尚未合入 —— 三个分支单独 checkout 都能启动。
"""

from contextlib import suppress

from app.common.models import Farm, User  # noqa: F401
from app.ops.models import (  # noqa: F401
    Alert,
    AlertRule,
    AuditLog,
    CertApplication,
    LoginCode,
)

with suppress(ModuleNotFoundError):  # pragma: no cover - 合入溯源链域分支后生效
    from app.trace.models import Batch, ScanLog, TraceCode, TraceEvent  # noqa: F401

with suppress(ModuleNotFoundError):  # pragma: no cover - 合入交易域分支后生效
    from app.trade.models import Order, OrderItem, Product, Review  # noqa: F401
