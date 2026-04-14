from __future__ import annotations

from dataclasses import dataclass

from simple_trader.brokers import PaperBroker
from simple_trader.execution import ExecutionReport, OrderIntent
from simple_trader.futures import ContractSpec, FuturesBar, Signal
from simple_trader.risk import RiskLimits
from simple_trader.sizing import PositionSizer, PositionSizingConfig
from simple_trader.stops import StopConfig, StopManager


@dataclass
class PaperFill:
    # Paper 成交记录会保留日期，方便教学输出和后续对账。
    date: str
    source: str
    report: ExecutionReport


@dataclass
class PaperTradingResult:
    starting_cash: float
    ending_cash: float
    ending_equity: float
    realized_pnl: float
    total_fees: float
    fills: list[PaperFill]
    equity_curve: list[tuple[str, float]]


class PaperTradingEngine:
    # Paper 引擎负责“用一条条新行情驱动策略和 paper matching”，模拟更接近实盘的节奏。
    def __init__(
        self,
        bars_by_symbol: dict[str, list[FuturesBar]],
        strategies: dict[str, object],
        contracts: dict[str, ContractSpec],
        initial_cash: float = 300_000,
        verbose: bool = False,
        risk_limits: RiskLimits | None = None,
        position_sizing: PositionSizingConfig | None = None,
        stop_config: StopConfig | None = None,
    ):
        self.bars_by_symbol = bars_by_symbol
        self.strategies = strategies
        self.contracts = contracts
        self.verbose = verbose
        self.broker = PaperBroker(contracts=contracts, initial_cash=initial_cash, risk_limits=risk_limits or RiskLimits())
        self.position_sizer = PositionSizer(position_sizing or PositionSizingConfig())
        self.stop_manager = StopManager(stop_config or StopConfig())
        self.initial_cash = initial_cash
        self.fills: list[PaperFill] = []
        self.equity_curve: list[tuple[str, float]] = []

    def _all_dates(self) -> list[str]:
        # 收集所有合约出现过的交易日，并按时间排序。
        all_dates = {bar.date for bars in self.bars_by_symbol.values() for bar in bars}
        return sorted(all_dates)

    def _fmt(self, value: float | None) -> str:
        # 把数字格式化成统一的教学输出样式。
        if value is None:
            return "-"
        return f"{value:.2f}"

    def _position_text(self, symbol: str) -> str:
        # 把当前这个合约的持仓转成一行更容易读的文字。
        position = self.broker.position_of(symbol)
        if position is None:
            return "空仓"
        return f"{position.direction} {position.symbol} x {position.size} @ {position.entry_price:.2f}"

    def _requested_size(self, symbol: str, signal: Signal | None) -> int:
        # 先按当前价格和风险预算估算“这笔单最多该下几手”。
        position = self.broker.position_of(symbol)
        if signal in {"SELL", "BUY_COVER"}:
            return position.size if position is not None else self.position_sizer.fixed_size()
        if signal not in {"BUY", "SELL_SHORT"}:
            return self.position_sizer.fixed_size()
        price = self.broker.last_price_by_symbol.get(symbol)
        if price is None:
            return 0
        contract = self.contracts[symbol]
        fill_price = price + contract.price_tick * contract.slippage_ticks
        margin_per_contract = fill_price * contract.multiplier * contract.margin_rate
        snapshot = self.broker.account_snapshot()
        max_size = self.broker.risk_limits.max_position_size_per_symbol
        return self.position_sizer.size_from_margin_budget(
            equity=snapshot.equity,
            margin_per_contract=margin_per_contract,
            max_position_size=max_size,
        )

    def _log_symbol_day(
        self,
        bar: FuturesBar,
        info: dict[str, float | bool | None] | None,
        signal: Signal | None,
        source: str,
        requested_size: int,
        reports: list[ExecutionReport],
        reject_reason: str | None,
    ):
        # 打印 paper 版本当天这只合约看到的行情、信号和模拟成交。
        short_ma = info.get("short_ma") if info else None  # type: ignore[assignment]
        long_ma = info.get("long_ma") if info else None  # type: ignore[assignment]
        crossed_up = bool(info.get("crossed_up")) if info else False
        crossed_down = bool(info.get("crossed_down")) if info else False
        snapshot = self.broker.account_snapshot()
        print(
            f"[PAPER] {bar.date} {bar.symbol} close={bar.close:.2f} "
            f"short_ma={self._fmt(short_ma)} long_ma={self._fmt(long_ma)} "
            f"crossed_up={crossed_up} crossed_down={crossed_down}"
        )
        print(
            f"        position={self._position_text(bar.symbol)} "
            f"signal={signal or 'NONE'} source={source} requested_size={requested_size}"
        )
        if not reports:
            print("        fills=NONE")
        else:
            for report in reports:
                line = (
                    f"        fill={report.action} {report.symbol} size={report.size} "
                    f"price={report.price:.2f} fee={report.fee:.2f}"
                )
                if report.action in {"SELL", "BUY_COVER"}:
                    line += f" pnl={report.pnl:.2f}"
                print(line)
        if reject_reason:
            print(f"        reject={reject_reason}")
        print(
            f"        cash={snapshot.cash:.2f} equity={snapshot.equity:.2f} "
            f"margin={snapshot.margin:.2f} available={snapshot.available:.2f}"
        )

    def _log_portfolio_summary(self, date: str):
        # 每天结束后打印一次整个 paper 账户的汇总状态。
        snapshot = self.broker.account_snapshot()
        print(f"[PAPER-PORTFOLIO] {date}")
        print(
            f"  cash={snapshot.cash:.2f} equity={snapshot.equity:.2f} "
            f"margin={snapshot.margin:.2f} available={snapshot.available:.2f}"
        )
        if not self.broker.positions:
            print("  positions=空仓")
            return
        print("  positions:")
        for symbol in sorted(self.broker.positions):
            print(f"  - {self._position_text(symbol)}")

    def run(self) -> PaperTradingResult:
        # 把历史 bar 当成“实时到来的行情”，一天一天推进 paper trading。
        histories: dict[str, list[FuturesBar]] = {symbol: [] for symbol in self.bars_by_symbol}

        for date in self._all_dates():
            for symbol, bars in self.bars_by_symbol.items():
                bar = next((item for item in bars if item.date == date), None)
                if bar is None:
                    continue

                histories[symbol].append(bar)
                self.broker.update_market_price(symbol, bar.close)
                strategy = self.strategies[symbol]
                info = strategy.describe_bar(histories[symbol]) if hasattr(strategy, "describe_bar") else None

                signal: Signal | None = None
                source = "NONE"
                position = self.broker.position_of(symbol)
                if position is not None:
                    stop_decision = self.stop_manager.check(
                        direction=position.direction,
                        entry_price=position.entry_price,
                        current_price=bar.close,
                    )
                    if stop_decision.triggered:
                        signal = stop_decision.signal
                        source = "STOP"

                if signal is None:
                    signal = strategy.on_bar(histories[symbol], self.broker)
                    if signal is not None:
                        source = "STRATEGY"

                requested_size = self._requested_size(symbol, signal)
                reports: list[ExecutionReport] = []
                reject_reason: str | None = None
                if signal is not None:
                    if signal in {"BUY", "SELL_SHORT"} and requested_size == 0:
                        reject_reason = "按当前风险预算计算，允许开仓手数为 0。"
                    else:
                        reports = self.broker.submit_order(
                            OrderIntent(symbol=symbol, action=signal, size=requested_size, source=source)
                        )
                        reject_reason = self.broker.last_reject_reason
                        for report in reports:
                            self.fills.append(PaperFill(date=date, source=source, report=report))

                if self.verbose:
                    self._log_symbol_day(
                        bar=bar,
                        info=info,
                        signal=signal,
                        source=source,
                        requested_size=requested_size,
                        reports=reports,
                        reject_reason=reject_reason,
                    )

            snapshot = self.broker.account_snapshot()
            self.equity_curve.append((date, snapshot.equity))
            if self.verbose:
                self._log_portfolio_summary(date)

        ending_snapshot = self.broker.account_snapshot()
        return PaperTradingResult(
            starting_cash=self.initial_cash,
            ending_cash=ending_snapshot.cash,
            ending_equity=ending_snapshot.equity,
            realized_pnl=ending_snapshot.realized_pnl,
            total_fees=ending_snapshot.total_fees,
            fills=self.fills,
            equity_curve=self.equity_curve,
        )
