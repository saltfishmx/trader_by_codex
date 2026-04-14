from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Signal = Literal["BUY", "SELL", "SELL_SHORT", "BUY_COVER"]


@dataclass(frozen=True)
class OrderIntent:
    # 下单意图：策略 / 风控层只负责提出“我想下什么单”。
    symbol: str
    action: Signal
    size: int
    source: str = "STRATEGY"


@dataclass(frozen=True)
class ExecutionReport:
    # 执行回报：broker 执行完后，统一返回这次到底成交了什么。
    symbol: str
    action: Signal
    size: int
    price: float
    fee: float
    pnl: float = 0.0

