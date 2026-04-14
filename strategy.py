from __future__ import annotations

from dataclasses import dataclass

from simple_trader.engine import Bar, Broker


def sma(values: list[float], window: int) -> float | None:
    # 简单移动平均线：取最近 window 个值做平均。
    if len(values) < window:
        return None
    subset = values[-window:]
    return sum(subset) / window


@dataclass
class MovingAverageCrossStrategy:
    # 一个最经典、也最容易理解的量化策略：
    # 短均线上穿长均线时买入，下穿时卖出。
    short_window: int = 3
    long_window: int = 5

    def on_bar(self, bars: list[Bar], broker: Broker) -> str | None:
        closes = [bar.close for bar in bars]
        short_ma = sma(closes, self.short_window)
        long_ma = sma(closes, self.long_window)
        if short_ma is None or long_ma is None:
            return None

        if len(closes) < self.long_window + 1:
            return None

        prev_closes = closes[:-1]
        prev_short_ma = sma(prev_closes, self.short_window)
        prev_long_ma = sma(prev_closes, self.long_window)
        if prev_short_ma is None or prev_long_ma is None:
            return None

        # 不只是看“短均线是否大于长均线”，
        # 而是看这一根是否发生了真正的穿越。
        crossed_up = prev_short_ma <= prev_long_ma and short_ma > long_ma
        crossed_down = prev_short_ma >= prev_long_ma and short_ma < long_ma

        if crossed_up and broker.position.size == 0:
            return "BUY"
        if crossed_down and broker.position.size > 0:
            return "SELL"
        return None
