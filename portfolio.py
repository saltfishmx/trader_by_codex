from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from simple_trader.brokers import BacktestBroker, BrokerAccountSnapshot
from simple_trader.execution import ExecutionReport, OrderIntent
from simple_trader.futures import (
    ContractSpec,
    Direction,
    FuturesAccount,
    FuturesBar,
    FuturesPosition,
    FuturesTrade,
    Signal,
)
from simple_trader.risk import FuturesRiskManager, RiskLimits
from simple_trader.sizing import PositionSizer, PositionSizingConfig
from simple_trader.stops import StopConfig, StopManager


class SymbolStrategy(Protocol):
    def on_bar(self, bars: list[FuturesBar], broker: "PortfolioBroker") -> Signal | None:
        """Return one of BUY / SELL / SELL_SHORT / BUY_COVER / None."""


@dataclass
class PortfolioBacktestResult:
    starting_cash: float
    ending_cash: float
    ending_equity: float
    realized_pnl: float
    total_fees: float
    total_return_pct: float
    trades: list[FuturesTrade]
    equity_curve: list[tuple[str, float]]


class PortfolioBroker(BacktestBroker):
    # 组合 broker 负责管理“一个账户里多个合约”的持仓、资金和交易。
    def __init__(self, contracts: dict[str, ContractSpec], initial_cash: float, risk_limits: RiskLimits | None = None):
        self.contracts = contracts
        self.risk_limits = risk_limits or RiskLimits()
        self.risk_manager = FuturesRiskManager(self.risk_limits)
        self.account = FuturesAccount(cash=initial_cash)
        self.positions: dict[str, FuturesPosition] = {}
        self.last_reject_reason: str | None = None

    def _contract(self, symbol: str) -> ContractSpec:
        # 根据合约代码取出它自己的交易参数。
        return self.contracts[symbol]

    def _notional(self, symbol: str, price: float, size: int) -> float:
        # 计算某个合约这笔仓位对应的名义价值。
        contract = self._contract(symbol)
        return price * size * contract.multiplier

    def _margin_required(self, symbol: str, price: float, size: int) -> float:
        # 计算某个合约这笔仓位会额外占用多少保证金。
        contract = self._contract(symbol)
        return self._notional(symbol, price, size) * contract.margin_rate

    def _fee(self, symbol: str, price: float, size: int) -> float:
        # 按该合约自己的手续费模式，计算这次交易成本。
        contract = self._contract(symbol)
        if contract.fee_mode == "notional_rate":
            return self._notional(symbol, price, size) * contract.fee_rate
        return size * contract.fee_per_contract

    def _apply_slippage(self, symbol: str, action: Signal, price: float) -> float:
        # 按该合约自己的最小跳动和滑点设置，给出更真实的成交价。
        contract = self._contract(symbol)
        slippage = contract.price_tick * contract.slippage_ticks
        if action in {"BUY", "BUY_COVER"}:
            return price + slippage
        return price - slippage

    def margin_in_use(self, latest_bars: dict[str, FuturesBar]) -> float:
        # 计算当前整个组合一共占用了多少保证金。
        total = 0.0
        for symbol, position in self.positions.items():
            bar = latest_bars.get(symbol)
            if bar is None:
                continue
            total += self._margin_required(symbol, bar.close, position.size)
        return total

    def unrealized_pnl(self, latest_bars: dict[str, FuturesBar]) -> float:
        # 计算整个组合按最新价格估值时的总浮动盈亏。
        total = 0.0
        for symbol, position in self.positions.items():
            bar = latest_bars.get(symbol)
            if bar is None:
                continue
            direction = 1 if position.direction == "LONG" else -1
            price_diff = (bar.close - position.entry_price) * direction
            total += price_diff * position.size * self._contract(symbol).multiplier
        return total

    def equity(self, latest_bars: dict[str, FuturesBar]) -> float:
        # 组合权益 = 账户现金 + 所有持仓的浮动盈亏。
        return self.account.cash + self.unrealized_pnl(latest_bars)

    def available_cash(self, latest_bars: dict[str, FuturesBar]) -> float:
        # 组合可用资金 = 组合权益 - 当前保证金占用。
        return self.equity(latest_bars) - self.margin_in_use(latest_bars)

    def account_snapshot(self, latest_bars: dict[str, FuturesBar]) -> BrokerAccountSnapshot:
        # 把组合账户整理成统一快照，方便以后和 paper / live broker 共享同一观察口径。
        equity = self.equity(latest_bars)
        margin = self.margin_in_use(latest_bars)
        return BrokerAccountSnapshot(
            cash=self.account.cash,
            equity=equity,
            margin=margin,
            available=equity - margin,
            realized_pnl=self.account.realized_pnl,
            total_fees=self.account.total_fees,
        )

    def position_of(self, symbol: str) -> FuturesPosition | None:
        # 读取某个合约当前是否有持仓。
        return self.positions.get(symbol)

    def _can_open(
        self,
        *,
        symbol: str,
        latest_bars: dict[str, FuturesBar],
        fill_price: float,
        size: int,
        fee: float,
    ) -> tuple[bool, str | None]:
        # 开新仓前，按组合口径检查这笔单是否会让总风险超标。
        required_margin = self._margin_required(symbol, fill_price, size)
        current_equity = self.equity(latest_bars)
        projected_margin = self.margin_in_use(latest_bars) + required_margin
        current_symbol_position = self.positions.get(symbol)
        current_symbol_size = current_symbol_position.size if current_symbol_position else 0
        current_symbol_bar = latest_bars.get(symbol)
        current_symbol_margin = 0.0
        if current_symbol_position is not None and current_symbol_bar is not None:
            current_symbol_margin = self._margin_required(symbol, current_symbol_bar.close, current_symbol_position.size)
        projected_symbol_margin = current_symbol_margin + required_margin
        projected_symbol_size = current_symbol_size + size
        decision = self.risk_manager.can_open(
            cash=self.account.cash,
            current_equity=current_equity,
            projected_total_margin=projected_margin,
            projected_symbol_margin=projected_symbol_margin,
            projected_symbol_size=projected_symbol_size,
            fee=fee,
        )
        return decision.allowed, decision.reason

    def open_position(self, bar: FuturesBar, direction: Direction, size: int, latest_bars: dict[str, FuturesBar]) -> FuturesTrade | None:
        # 在组合账户里为某个合约开一笔新仓。
        self.last_reject_reason = None
        if size <= 0 or bar.symbol in self.positions:
            self.last_reject_reason = "当前不允许开仓：数量必须大于 0，且该合约必须先空仓。"
            return None
        action: Signal = "BUY" if direction == "LONG" else "SELL_SHORT"
        fill_price = self._apply_slippage(bar.symbol, action, bar.close)
        fee = self._fee(bar.symbol, fill_price, size)
        allowed, reason = self._can_open(
            symbol=bar.symbol,
            latest_bars=latest_bars,
            fill_price=fill_price,
            size=size,
            fee=fee,
        )
        if not allowed:
            self.last_reject_reason = reason
            return None
        self.account.cash -= fee
        self.account.total_fees += fee
        self.positions[bar.symbol] = FuturesPosition(
            symbol=bar.symbol,
            direction=direction,
            size=size,
            entry_price=fill_price,
        )
        return FuturesTrade(
            date=bar.date,
            symbol=bar.symbol,
            action=action,
            price=fill_price,
            size=size,
            fee=fee,
        )

    def close_position(self, bar: FuturesBar) -> FuturesTrade | None:
        # 把某个合约当前的持仓全部平掉，并把盈亏记到账户里。
        self.last_reject_reason = None
        position = self.positions.get(bar.symbol)
        if position is None:
            self.last_reject_reason = "该合约当前没有持仓，无法平仓。"
            return None
        action: Signal = "SELL" if position.direction == "LONG" else "BUY_COVER"
        fill_price = self._apply_slippage(bar.symbol, action, bar.close)
        fee = self._fee(bar.symbol, fill_price, position.size)
        direction = 1 if position.direction == "LONG" else -1
        price_diff = (fill_price - position.entry_price) * direction
        pnl = price_diff * position.size * self._contract(bar.symbol).multiplier
        self.account.cash += pnl
        self.account.cash -= fee
        self.account.realized_pnl += pnl
        self.account.total_fees += fee
        trade = FuturesTrade(
            date=bar.date,
            symbol=bar.symbol,
            action=action,
            price=fill_price,
            size=position.size,
            fee=fee,
            pnl=pnl,
        )
        self.positions.pop(bar.symbol, None)
        return trade

    def execute(self, signal: Signal, bar: FuturesBar, size: int, latest_bars: dict[str, FuturesBar]) -> list[FuturesTrade]:
        # 把某个合约的策略信号翻译成组合账户里的具体交易动作。
        self.last_reject_reason = None
        position = self.positions.get(bar.symbol)
        trades: list[FuturesTrade] = []
        if signal == "BUY":
            if position is None:
                trade = self.open_position(bar, direction="LONG", size=size, latest_bars=latest_bars)
                if trade:
                    trades.append(trade)
                return trades
            if position.direction == "SHORT":
                close_trade = self.close_position(bar)
                if close_trade:
                    trades.append(close_trade)
                open_trade = self.open_position(bar, direction="LONG", size=size, latest_bars=latest_bars)
                if open_trade:
                    trades.append(open_trade)
            return trades

        if signal == "SELL_SHORT":
            if position is None:
                trade = self.open_position(bar, direction="SHORT", size=size, latest_bars=latest_bars)
                if trade:
                    trades.append(trade)
                return trades
            if position.direction == "LONG":
                close_trade = self.close_position(bar)
                if close_trade:
                    trades.append(close_trade)
                open_trade = self.open_position(bar, direction="SHORT", size=size, latest_bars=latest_bars)
                if open_trade:
                    trades.append(open_trade)
            return trades

        if signal == "SELL" and position and position.direction == "LONG":
            trade = self.close_position(bar)
            if trade:
                trades.append(trade)
            return trades

        if signal == "BUY_COVER" and position and position.direction == "SHORT":
            trade = self.close_position(bar)
            if trade:
                trades.append(trade)
            return trades

        return trades

    def submit_order(self, intent: OrderIntent, bar: FuturesBar, latest_bars: dict[str, FuturesBar]) -> list[ExecutionReport]:
        # 统一执行入口：把“下单意图”交给组合 backtest broker 执行，并返回标准回报。
        trades = self.execute(intent.action, bar, size=intent.size, latest_bars=latest_bars)
        return [trade.to_execution_report() for trade in trades]


class PortfolioBacktestEngine:
    # 组合回测引擎负责把多个合约按日期对齐后，一天一天推进。
    def __init__(
        self,
        bars_by_symbol: dict[str, list[FuturesBar]],
        strategies: dict[str, SymbolStrategy],
        contracts: dict[str, ContractSpec],
        initial_cash: float = 300_000,
        position_size: int = 1,
        verbose: bool = False,
        risk_limits: RiskLimits | None = None,
        position_sizing: PositionSizingConfig | None = None,
        stop_config: StopConfig | None = None,
    ):
        self.bars_by_symbol = bars_by_symbol
        self.strategies = strategies
        self.contracts = contracts
        self.position_size = position_size
        self.verbose = verbose
        self.broker = PortfolioBroker(contracts=contracts, initial_cash=initial_cash, risk_limits=risk_limits)
        self.position_sizer = PositionSizer(position_sizing or PositionSizingConfig(fixed_size=position_size))
        self.stop_manager = StopManager(stop_config or StopConfig())
        self.initial_cash = initial_cash
        self.trades: list[FuturesTrade] = []
        self.equity_curve: list[tuple[str, float]] = []
        self.realized_pnl_by_symbol: dict[str, float] = {symbol: 0.0 for symbol in contracts}

    def _all_dates(self) -> list[str]:
        # 收集所有合约出现过的交易日，并按时间排序。
        all_dates = {bar.date for bars in self.bars_by_symbol.values() for bar in bars}
        return sorted(all_dates)

    def _position_text(self, symbol: str) -> str:
        # 把某个合约当前持仓格式化成一行文字。
        pos = self.broker.position_of(symbol)
        if pos is None:
            return "空仓"
        return f"{pos.direction} {pos.symbol} x {pos.size} @ {pos.entry_price:.2f}"

    def _fmt_number(self, value: float | None) -> str:
        # 把数字格式化成更容易读的样子。
        if value is None:
            return "-"
        return f"{value:.2f}"

    def _log_symbol_day(
        self,
        bar: FuturesBar,
        signal: Signal | None,
        trades: list[FuturesTrade],
        info: dict[str, float | bool | None] | None,
        latest_bars: dict[str, FuturesBar],
        reject_reason: str | None,
        requested_size: int,
        signal_source: str,
    ):
        # 打印某个合约当天的思考过程和执行结果。
        short_ma = info.get("short_ma") if info else None  # type: ignore[assignment]
        long_ma = info.get("long_ma") if info else None  # type: ignore[assignment]
        crossed_up = bool(info.get("crossed_up")) if info else False
        crossed_down = bool(info.get("crossed_down")) if info else False
        print(
            f"[DAY] {bar.date} {bar.symbol} close={bar.close:.2f} "
            f"short_ma={self._fmt_number(short_ma)} long_ma={self._fmt_number(long_ma)} "
            f"crossed_up={crossed_up} crossed_down={crossed_down}"
        )
        print(
            f"      position={self._position_text(bar.symbol)} "
            f"signal={signal or 'NONE'} source={signal_source} requested_size={requested_size}"
        )
        if not trades:
            print("      trades=NONE")
        else:
            for trade in trades:
                line = f"      trade={trade.action} {trade.symbol} size={trade.size} price={trade.price:.2f} fee={trade.fee:.2f}"
                if trade.action in {"SELL", "BUY_COVER"}:
                    line += f" pnl={trade.pnl:.2f}"
                print(line)
        if reject_reason:
            print(f"      reject={reject_reason}")
        print(
            f"      portfolio_cash={self.broker.account.cash:.2f} "
            f"portfolio_equity={self.broker.equity(latest_bars):.2f} "
            f"portfolio_margin={self.broker.margin_in_use(latest_bars):.2f} "
            f"portfolio_available={self.broker.available_cash(latest_bars):.2f}"
        )

    def _portfolio_positions_text(self) -> list[str]:
        # 把当前所有持仓整理成几行，方便做组合总览。
        rows: list[str] = []
        for symbol in sorted(self.contracts.keys()):
            position = self.broker.position_of(symbol)
            if position is None:
                continue
            rows.append(f"{symbol} {position.direction} x {position.size} @ {position.entry_price:.2f}")
        return rows

    def _portfolio_pnl_rows(self, latest_bars: dict[str, FuturesBar]) -> list[str]:
        # 汇总每个合约的已实现盈亏和当前浮动盈亏。
        rows: list[str] = []
        for symbol in sorted(self.contracts.keys()):
            realized = self.realized_pnl_by_symbol.get(symbol, 0.0)
            unrealized = 0.0
            position = self.broker.position_of(symbol)
            bar = latest_bars.get(symbol)
            if position is not None and bar is not None:
                direction = 1 if position.direction == "LONG" else -1
                price_diff = (bar.close - position.entry_price) * direction
                unrealized = price_diff * position.size * self.contracts[symbol].multiplier
            rows.append(f"{symbol} realized={realized:.2f} unrealized={unrealized:.2f}")
        return rows

    def _log_portfolio_summary(self, day: str, latest_bars: dict[str, FuturesBar]):
        # 在每个交易日结束后打印一次组合级总览。
        print(f"[PORTFOLIO] {day}")
        print(
            f"  cash={self.broker.account.cash:.2f} "
            f"equity={self.broker.equity(latest_bars):.2f} "
            f"margin={self.broker.margin_in_use(latest_bars):.2f} "
            f"available={self.broker.available_cash(latest_bars):.2f}"
        )
        positions = self._portfolio_positions_text()
        if not positions:
            print("  positions=空仓")
        else:
            print("  positions:")
            for row in positions:
                print(f"  - {row}")
        print("  pnl_by_symbol:")
        for row in self._portfolio_pnl_rows(latest_bars):
            print(f"  - {row}")

    def run(self) -> PortfolioBacktestResult:
        # 把多个合约按日期拼起来，一天一天地回测整个组合。
        history_by_symbol: dict[str, list[FuturesBar]] = {symbol: [] for symbol in self.bars_by_symbol}
        latest_bars: dict[str, FuturesBar] = {}
        bars_lookup = {
            symbol: {bar.date: bar for bar in bars}
            for symbol, bars in self.bars_by_symbol.items()
        }

        for day in self._all_dates():
            for symbol in sorted(self.bars_by_symbol.keys()):
                self.broker.last_reject_reason = None
                bar = bars_lookup[symbol].get(day)
                if bar is None:
                    continue
                latest_bars[symbol] = bar
                history_by_symbol[symbol].append(bar)
                strategy = self.strategies[symbol]
                info = strategy.describe_bar(history_by_symbol[symbol]) if hasattr(strategy, "describe_bar") else None  # type: ignore[attr-defined]
                signal = None
                signal_source = "NONE"
                current_position = self.broker.position_of(symbol)
                if current_position is not None:
                    stop_decision = self.stop_manager.check(
                        direction=current_position.direction,
                        entry_price=current_position.entry_price,
                        current_price=bar.close,
                    )
                    if stop_decision.triggered:
                        signal = stop_decision.signal
                        self.broker.last_reject_reason = stop_decision.reason
                        signal_source = "STOP"
                if signal is None:
                    signal = strategy.on_bar(history_by_symbol[symbol], self.broker)
                    signal_source = "STRATEGY" if signal is not None else "NONE"
                trades: list[FuturesTrade] = []
                requested_size = self.position_size
                if signal is not None:
                    if signal in {"BUY", "SELL_SHORT"} and self.broker.position_of(symbol) is None:
                        fill_price = self.broker._apply_slippage(symbol, signal, bar.close)
                        margin_per_contract = self.broker._margin_required(symbol, fill_price, 1)
                        requested_size = self.position_sizer.size_from_margin_budget(
                            equity=self.broker.equity(latest_bars),
                            margin_per_contract=margin_per_contract,
                            max_position_size=self.broker.risk_limits.max_position_size_per_symbol,
                        )
                        if requested_size == 0:
                            self.broker.last_reject_reason = "按当前风险预算计算，允许开仓手数为 0。"
                    if signal in {"BUY", "SELL_SHORT"} and self.broker.position_of(symbol) is not None:
                        requested_size = self.position_sizer.fixed_size()
                    position = self.broker.position_of(symbol)
                    if signal in {"SELL", "BUY_COVER"}:
                        requested_size = position.size if position else 0
                    if requested_size > 0:
                        reports = self.broker.submit_order(
                            OrderIntent(symbol=symbol, action=signal, size=requested_size, source=signal_source),
                            bar,
                            latest_bars,
                        )
                        trades = [
                            FuturesTrade(
                                date=bar.date,
                                symbol=report.symbol,
                                action=report.action,
                                price=report.price,
                                size=report.size,
                                fee=report.fee,
                                pnl=report.pnl,
                            )
                            for report in reports
                        ]
                        for trade in trades:
                            if trade.action in {"SELL", "BUY_COVER"}:
                                self.realized_pnl_by_symbol[trade.symbol] += trade.pnl
                    self.trades.extend(trades)
                if self.verbose:
                    self._log_symbol_day(
                        bar,
                        signal,
                        trades,
                        info,
                        latest_bars,
                        self.broker.last_reject_reason,
                        requested_size,
                        signal_source,
                    )
            if latest_bars:
                self.equity_curve.append((day, self.broker.equity(latest_bars)))
                if self.verbose:
                    self._log_portfolio_summary(day, latest_bars)

        for symbol in sorted(list(self.broker.positions.keys())):
            bar = latest_bars.get(symbol)
            if bar is None:
                continue
            final_trade = self.broker.close_position(bar)
            if final_trade:
                self.trades.append(final_trade)
                if final_trade.action in {"SELL", "BUY_COVER"}:
                    self.realized_pnl_by_symbol[final_trade.symbol] += final_trade.pnl
        if self.equity_curve:
            last_day = self.equity_curve[-1][0]
            self.equity_curve[-1] = (last_day, self.broker.equity(latest_bars))

        ending_equity = self.equity_curve[-1][1] if self.equity_curve else self.initial_cash
        total_return_pct = (ending_equity - self.initial_cash) / self.initial_cash * 100
        return PortfolioBacktestResult(
            starting_cash=self.initial_cash,
            ending_cash=self.broker.account.cash,
            ending_equity=ending_equity,
            realized_pnl=self.broker.account.realized_pnl,
            total_fees=self.broker.account.total_fees,
            total_return_pct=total_return_pct,
            trades=self.trades,
            equity_curve=self.equity_curve,
        )
