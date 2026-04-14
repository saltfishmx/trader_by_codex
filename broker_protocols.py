from __future__ import annotations

from typing import Protocol


class SingleInstrumentBroker(Protocol):
    # 单合约策略只依赖这个最小接口，而不依赖某个具体 broker 类。
    @property
    def position(self):
        ...


class PortfolioLikeBroker(Protocol):
    # 多合约策略只依赖“能查询当前合约持仓”这个能力。
    def position_of(self, symbol: str):
        ...

