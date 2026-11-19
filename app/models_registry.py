"""模型注册表：保证 Base.metadata 在 create_all 前收齐所有表。

新增业务域时在这里补一行 import，避免出现「表没建出来」的低级问题。
三域整合后，这里是唯一的集中式模型注册点。
"""

from app.catalog.models import Category  # noqa: F401
from app.common.models import Farm, User  # noqa: F401
from app.ops.models import Alert, AuditLog, CertApplication, ReviewLog  # noqa: F401
from app.trace.models import Batch, ScanLog, TraceCode, TraceEvent  # noqa: F401
from app.trade.models import (  # noqa: F401
    CartItem,
    Order,
    OrderItem,
    Payment,
    Product,
    Review,
)
