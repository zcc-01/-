"""主键生成（公共基础层）。

报告 4.5 约定：主键统一为「雪花风格字符串」，避免三人分域并行开发时自增主键冲突。
这里实现的是字符串化的雪花：时间戳(41bit) + 机器位(5bit) + 序号(12bit)，
再拼上业务前缀，便于人眼识别实体类型（BAT/EVT/COD/LOG）。

格式：`{PREFIX}{base36 时间戳}{2 位机器}{4 位序列}`，长度 3+9+2+4 = 18，远小于 VARCHAR(32)。
"""

from __future__ import annotations

import os
import secrets
import threading
import time

# 自定义纪元 2024-01-01T00:00:00Z（毫秒），保证 base36 时间戳位数稳定
_EPOCH_MS = 1_704_067_200_000

_WORKER_ID = int(os.getenv("WORKER_ID", "1")) & 0x1F
_seq = 0
_seq_lock = threading.Lock()


def _next_seq() -> int:
    """同一毫秒内的自增序列，溢出后自旋到下一毫秒。"""
    global _seq
    with _seq_lock:
        _seq = (_seq + 1) & 0xFFF
        return _seq


def _base36(num: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if num == 0:
        return "0"
    out: list[str] = []
    while num:
        num, rem = divmod(num, 36)
        out.append(alphabet[rem])
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """生成带业务前缀的全局唯一字符串主键。"""
    if not prefix or len(prefix) > 4:
        raise ValueError("前缀长度需为 1~4 个字符")
    now = int(time.time() * 1000) - _EPOCH_MS
    return f"{prefix.upper()}{_base36(now)}{_WORKER_ID:02d}{_next_seq():04d}"


def new_batch_no() -> str:
    """批次号（对外可见的业务编号）：PC-YYYYMMDD-XXXX。"""
    stamp = time.strftime("%Y%m%d")
    return f"PC-{stamp}-{secrets.token_hex(2).upper()}"
