from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Direction = Literal["LONG", "SHORT"]
Signal = Literal["BUY", "SELL", "SELL_SHORT", "BUY_COVER"]


@dataclass(frozen=True)
class StopConfig:
    # 最简单的一版止损参数：按持仓开仓价的固定百分比止损。
    stop_loss_pct: float = 0.0


@dataclass(frozen=True)
class StopDecision:
    # 止损层的输出：是否触发、该发什么平仓信号、以及原因说明。
    triggered: bool
    signal: Signal | None = None
    reason: str | None = None


class StopManager:
    # 止损管理器负责回答一句话：当前这笔持仓要不要立刻止损？
    def __init__(self, config: StopConfig):
        self.config = config

    def check(self, *, direction: Direction, entry_price: float, current_price: float) -> StopDecision:
        # 按固定百分比止损规则，判断这笔持仓是否已经该平掉。
        if self.config.stop_loss_pct <= 0:
            return StopDecision(triggered=False)

        if direction == "LONG":
            stop_price = entry_price * (1 - self.config.stop_loss_pct)
            if current_price <= stop_price:
                return StopDecision(
                    triggered=True,
                    signal="SELL",
                    reason=f"LONG 止损触发，current={current_price:.2f} <= stop={stop_price:.2f}",
                )
            return StopDecision(triggered=False)

        stop_price = entry_price * (1 + self.config.stop_loss_pct)
        if current_price >= stop_price:
            return StopDecision(
                triggered=True,
                signal="BUY_COVER",
                reason=f"SHORT 止损触发，current={current_price:.2f} >= stop={stop_price:.2f}",
            )
        return StopDecision(triggered=False)
