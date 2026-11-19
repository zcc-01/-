"""模拟支付网关（外部系统替身，UC-08 的辅助参与者）。

为什么单独抽一个模块而不是在 Service 里写个 if：
1. 报告 4.3 的顺序图把「支付网关」画成独立参与者，代码结构要和设计图对得上；
2. 支付调用必须在数据库事务**之外**执行（5.2.3 的人工修改点），
   把它隔在一个无数据库依赖的类里，事务边界才不容易被后人写回去；
3. 课程演示要能稳定复现「支付失败」分支，所以网关行为是确定性的，
   不做随机失败率——随机失败会让测试变成偶发红。

失败分支的两种触发方式都写在这里，评审时可现场演示：
- 金额超过单笔限额（默认 20000 元）→ 网关拒付；
- 设置环境变量 ``MOCKPAY_FORCE_FAIL=1`` → 所有支付请求返回失败。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PayResult:
    """网关返回：ok 为真时 trade_no 必有值，失败时 message 记录拒付原因。"""

    ok: bool
    trade_no: str | None
    message: str


class MockPayGateway:
    """模拟网关：不做任何网络 IO，但保留真实网关的「不可靠外部依赖」语义。"""

    name = "mockpay"
    SINGLE_LIMIT = 20_000.00

    def __init__(self, single_limit: float | None = None) -> None:
        self.single_limit = single_limit if single_limit is not None else self.SINGLE_LIMIT

    def pay(self, order_no: str, amount: float, channel: str = "mockpay") -> PayResult:
        if amount <= 0:
            return PayResult(False, None, "支付金额必须大于 0")
        if amount > self.single_limit:
            return PayResult(
                False, None, f"超出模拟网关单笔限额 {self.single_limit:.2f} 元，请分单支付"
            )
        if os.getenv("MOCKPAY_FORCE_FAIL", "").strip().lower() in {"1", "true", "yes"}:
            return PayResult(False, None, "模拟网关维护中（MOCKPAY_FORCE_FAIL=1），请稍后重试")

        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        trade_no = f"MP{stamp}{secrets.token_hex(3).upper()}"
        return PayResult(True, trade_no, f"{channel} 支付成功")


gateway = MockPayGateway()
