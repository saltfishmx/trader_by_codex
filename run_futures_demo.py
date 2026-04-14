from pathlib import Path

from simple_trader.data import load_futures_bars_from_csv
from simple_trader.futures import ContractSpec, FuturesBacktestEngine
from simple_trader.futures_strategy import FuturesMovingAverageStrategy
from simple_trader.risk import RiskLimits
from simple_trader.sizing import PositionSizingConfig
from simple_trader.stops import StopConfig


def main():
    csv_path = Path(__file__).resolve().parent / "sample_data" / "IF8888_daily.csv"
    bars = load_futures_bars_from_csv(csv_path)

    # 这里用的是“教学用”的简化合约参数，不是交易所精确参数。
    contract = ContractSpec(
        symbol="IF8888",
        multiplier=300,
        margin_rate=0.12,
        fee_per_contract=3.0,
        fee_mode="notional_rate",
        fee_rate=0.000005,
        price_tick=0.2,
        slippage_ticks=1,
    )
    # 这里给一个比较严格的保证金上限，方便演示风险控制会如何阻止继续开仓。
    risk_limits = RiskLimits(
        max_margin_ratio=0.49,
        max_symbol_margin_ratio=0.49,
        max_position_size_per_symbol=3,
    )
    position_sizing = PositionSizingConfig(
        fixed_size=1,
        max_margin_fraction_per_trade=0.20,
    )
    stop_config = StopConfig(stop_loss_pct=0.015)
    strategy = FuturesMovingAverageStrategy(short_window=3, long_window=5)
    engine = FuturesBacktestEngine(
        bars=bars,
        strategy=strategy,
        contract=contract,
        initial_cash=300_000,
        position_size=1,
        verbose=True,
        risk_limits=risk_limits,
        position_sizing=position_sizing,
        stop_config=stop_config,
    )
    result = engine.run()

    print("=== Futures Backtest Summary ===")
    print(f"symbol:         {contract.symbol}")
    print(f"fee_mode:       {contract.fee_mode}")
    print(f"max_margin:     {risk_limits.max_margin_ratio:.2%}")
    print(f"max_symbol:     {risk_limits.max_symbol_margin_ratio:.2%}")
    print(f"max_size:       {risk_limits.max_position_size_per_symbol}")
    print(f"risk_budget:    {position_sizing.max_margin_fraction_per_trade:.2%}")
    print(f"stop_loss:      {stop_config.stop_loss_pct:.2%}")
    print(f"starting_cash:  {result.starting_cash:.2f}")
    print(f"ending_cash:    {result.ending_cash:.2f}")
    print(f"ending_equity:  {result.ending_equity:.2f}")
    print(f"realized_pnl:   {result.realized_pnl:.2f}")
    print(f"total_fees:     {result.total_fees:.2f}")
    print(f"return_pct:     {result.total_return_pct:.2f}%")
    print()
    print("=== Trades ===")
    for trade in result.trades:
        line = (
            f"{trade.date} {trade.action} "
            f"size={trade.size} price={trade.price:.2f} fee={trade.fee:.2f}"
        )
        if trade.action in {"SELL", "BUY_COVER"}:
            line += f" pnl={trade.pnl:.2f}"
        print(line)


if __name__ == "__main__":
    main()
