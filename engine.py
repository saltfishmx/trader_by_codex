from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    # 一根最基本的 K 线数据。
    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Position:
    # 当前持仓：这里只有“持有多少”和“持仓成本”两个最小字段。
    size: int = 0
    entry_price: float = 0.0


@dataclass
class Trade:
    # 一次成交记录。BUY 代表开仓，SELL 代表平仓。
    side: str
    date: str
    price: float
    size: int
    pnl: float = 0.0


@dataclass
class Broker:
    # Broker 在这个极简模型里就代表“账户 + 持仓”。
    cash: float
    position: Position

    def buy(self, bar: Bar, size: int) -> Trade | None:
        cost = bar.close * size
        # 规则尽量简单：
        # 1. 数量必须大于 0
        # 2. 账户现金要够
        # 3. 这里只允许单仓位，所以有持仓时不重复开仓
        if size <= 0 or cost > self.cash or self.position.size != 0:
            return None
        self.cash -= cost
        self.position = Position(size=size, entry_price=bar.close)
        return Trade(side="BUY", date=bar.date, price=bar.close, size=size)

    def sell_all(self, bar: Bar) -> Trade | None:
        # 这里做最朴素的“全部卖出”。
        if self.position.size == 0:
            return None
        proceeds = bar.close * self.position.size
        pnl = (bar.close - self.position.entry_price) * self.position.size
        trade = Trade(
            side="SELL",
            date=bar.date,
            price=bar.close,
            size=self.position.size,
            pnl=pnl,
        )
        self.cash += proceeds
        self.position = Position()
        return trade

    def equity(self, bar: Bar) -> float:
        # 当前权益 = 现金 + 持仓按最新收盘价计算的市值
        return self.cash + self.position.size * bar.close


class Strategy(Protocol):
    def on_bar(self, bars: list[Bar], broker: Broker) -> str | None:
        """Return 'BUY', 'SELL', or None."""


@dataclass
class BacktestResult:
    starting_cash: float
    ending_cash: float
    ending_equity: float
    total_return_pct: float
    trades: list[Trade]
    equity_curve: list[tuple[str, float]]


class BacktestEngine:
    def __init__(self, bars: list[Bar], strategy: Strategy, initial_cash: float = 100_000):
        self.bars = bars
        self.strategy = strategy
        self.broker = Broker(cash=initial_cash, position=Position())
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[str, float]] = []
        self.initial_cash = initial_cash

    def run(self) -> BacktestResult:
        # seen_bars 表示“到当前时刻为止，策略能看到的全部历史数据”
        seen_bars: list[Bar] = []
        for bar in self.bars:
            seen_bars.append(bar)
            # 每来一根新 K 线，就把历史数据交给策略判断要不要交易
            signal = self.strategy.on_bar(seen_bars, self.broker)
            if signal == "BUY":
                trade = self.broker.buy(bar, size=1)
                if trade:
                    self.trades.append(trade)
            elif signal == "SELL":
                trade = self.broker.sell_all(bar)
                if trade:
                    self.trades.append(trade)
            # 不管有没有交易，都记录一下这一天账户值多少钱
            self.equity_curve.append((bar.date, self.broker.equity(bar)))

        if self.bars and self.broker.position.size > 0:
            # 回测结束时如果还有持仓，就按最后一根 K 线强制平仓，
            # 这样最终收益不会悬在半空中。
            final_trade = self.broker.sell_all(self.bars[-1])
            if final_trade:
                self.trades.append(final_trade)
                self.equity_curve[-1] = (self.bars[-1].date, self.broker.equity(self.bars[-1]))

        ending_equity = self.equity_curve[-1][1] if self.equity_curve else self.initial_cash
        total_return_pct = (ending_equity - self.initial_cash) / self.initial_cash * 100
        return BacktestResult(
            starting_cash=self.initial_cash,
            ending_cash=self.broker.cash,
            ending_equity=ending_equity,
            total_return_pct=total_return_pct,
            trades=self.trades,
            equity_curve=self.equity_curve,
        )
