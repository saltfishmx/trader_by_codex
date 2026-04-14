from __future__ import annotations

from dataclasses import dataclass

from simple_trader.broker_protocols import PortfolioLikeBroker, SingleInstrumentBroker
from simple_trader.futures import FuturesBar, Signal


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    subset = values[-window:]
    return sum(subset) / window


@dataclass
class FuturesMovingAverageStrategy:
    # 这个策略比最初版更接近期货：
    # 可以做多，也可以做空。
    short_window: int = 3
    long_window: int = 5

    @staticmethod
    def _position_for_current_symbol(broker: SingleInstrumentBroker | PortfolioLikeBroker, symbol: str):
        # 同时兼容单合约 broker 和组合 broker，取出“当前这个合约”的持仓。
        if hasattr(broker, "position_of"):
            return broker.position_of(symbol)  # type: ignore[attr-defined]
        return broker.position

    def describe_bar(self, bars: list[FuturesBar]) -> dict[str, float | bool | None]:
        # 计算当前这根 K 线对应的均线和穿越状态，方便调试和教学输出。
        closes = [bar.close for bar in bars]
        short_ma = sma(closes, self.short_window)
        long_ma = sma(closes, self.long_window)
        prev_short_ma = None
        prev_long_ma = None
        crossed_up = False
        crossed_down = False

        if len(closes) >= self.long_window + 1:
            prev_closes = closes[:-1]
            prev_short_ma = sma(prev_closes, self.short_window)
            prev_long_ma = sma(prev_closes, self.long_window)
            if prev_short_ma is not None and prev_long_ma is not None and short_ma is not None and long_ma is not None:
                crossed_up = prev_short_ma <= prev_long_ma and short_ma > long_ma
                crossed_down = prev_short_ma >= prev_long_ma and short_ma < long_ma

        return {
            "short_ma": short_ma,
            "long_ma": long_ma,
            "prev_short_ma": prev_short_ma,
            "prev_long_ma": prev_long_ma,
            "crossed_up": crossed_up,
            "crossed_down": crossed_down,
        }

    def on_bar(self, bars: list[FuturesBar], broker: SingleInstrumentBroker | PortfolioLikeBroker) -> Signal | None:
        # 看最新一根 K 线后，判断当前应该开多、平多、开空、平空，还是不动。
        current_symbol = bars[-1].symbol
        current_position = self._position_for_current_symbol(broker, current_symbol)
        info = self.describe_bar(bars)
        short_ma = info["short_ma"]
        long_ma = info["long_ma"]
        prev_short_ma = info["prev_short_ma"]
        prev_long_ma = info["prev_long_ma"]
        crossed_up = info["crossed_up"]
        crossed_down = info["crossed_down"]
        closes = [bar.close for bar in bars]
        if short_ma is None or long_ma is None or len(closes) < self.long_window + 1:
            return None
        if prev_short_ma is None or prev_long_ma is None:
            return None

        if current_position is None:
            if crossed_up:
                return "BUY"
            if crossed_down:
                return "SELL_SHORT"
            return None

        # 这里直接返回反向开仓信号，让 broker 去处理“先平后开”的反手动作。
        if current_position.direction == "LONG" and crossed_down:
            return "SELL_SHORT"
        if current_position.direction == "SHORT" and crossed_up:
            return "BUY"
        return None
