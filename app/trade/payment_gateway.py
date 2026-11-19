"""支付网关抽象（交易域基础设施）。

报告表 3-6 要求「外部依赖降级」：支付网关不可用时下单流程可降级为货到付款。
因此这里把网关抽成 Protocol，业务层只依赖 `charge()` 这个契约，
换成真实微信/支付宝网关时只替换实现，不动 OrderService。

本地模拟网关的行为：
- 正常情况返回成功与一个模拟流水号
- force_fail=True 时返回失败（用于演示「失败可重试三次」异常流）
- channel 非法或金额异常时返回失败，而不是抛异常——
  网关故障不应把整个支付事务炸掉，由业务层决定订单状态怎么处理
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Protocol


class ChargeResult:
    """网关返回结果：成功与否、第三方流水号、失败原因。"""

    __slots__ = ("ok", "transaction_no", "fail_reason")

    def __init__(self, ok: bool, transaction_no: str | None = None, fail_reason: str | None = None):
        self.ok = ok
        self.transaction_no = transaction_no
        self.fail_reason = fail_reason


class PaymentGateway(Protocol):
    """支付网关契约。实现方不得修改订单状态，只负责「收钱」这一件事。"""

    def charge(self, order_no: str, amount_cents: int, channel: str) -> ChargeResult: ...


class MockGateway:
    """本地模拟网关：不产生真实交易，用于课程演示与自动化测试。"""

    SUPPORTED = ("mock", "wechat", "alipay")

    def __init__(self, force_fail: bool = False) -> None:
        self._force_fail = force_fail

    def charge(self, order_no: str, amount_cents: int, channel: str) -> ChargeResult:
        if channel not in self.SUPPORTED:
            return ChargeResult(False, fail_reason=f"不支持的支付渠道：{channel}")
        if amount_cents <= 0:
            return ChargeResult(False, fail_reason="支付金额必须大于 0")
        if self._force_fail:
            return ChargeResult(False, fail_reason="支付网关返回失败（演示用）")
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        return ChargeResult(
            True,
            transaction_no=f"MOCK{stamp}{secrets.token_hex(4).upper()}",
        )


def get_gateway(force_fail: bool = False) -> PaymentGateway:
    """网关工厂：真实环境在这里按 settings 选择实现，当前仅提供模拟实现。"""
    return MockGateway(force_fail=force_fail)
