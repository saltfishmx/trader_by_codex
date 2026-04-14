from simple_trader.engine import Bar, BacktestEngine
from simple_trader.strategy import MovingAverageCrossStrategy


def demo_bars() -> list[Bar]:
    # 这里手工造一段价格，让双均线能出现一次买入和一次卖出。
    closes = [10.6, 10.4, 10.2, 10.0, 10.1, 10.5, 10.9, 11.3, 11.0, 10.7, 10.3, 9.9]
    bars: list[Bar] = []
    for idx, close in enumerate(closes, start=1):
        bars.append(
            Bar(
                date=f"2026-01-{idx:02d}",
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
            )
        )
    return bars


def main():
    bars = demo_bars()
    strategy = MovingAverageCrossStrategy(short_window=3, long_window=5)
    # 引擎负责把行情逐根喂给策略，并让账户执行交易。
    engine = BacktestEngine(bars=bars, strategy=strategy, initial_cash=100_000)
    result = engine.run()

    print("=== Backtest Summary ===")
    print(f"starting_cash: {result.starting_cash:.2f}")
    print(f"ending_cash:   {result.ending_cash:.2f}")
    print(f"ending_equity: {result.ending_equity:.2f}")
    print(f"return_pct:    {result.total_return_pct:.2f}%")
    print()
    print("=== Trades ===")
    for trade in result.trades:
        line = f"{trade.date} {trade.side} size={trade.size} price={trade.price:.2f}"
        if trade.side == "SELL":
            line += f" pnl={trade.pnl:.2f}"
        print(line)


if __name__ == "__main__":
    main()
