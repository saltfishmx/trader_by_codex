from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from simple_trader.brokers import BacktestBroker, BrokerAccountSnapshot
from simple_trader.execution import ExecutionReport, OrderIntent
from simple_trader.risk import FuturesRiskManager, RiskLimits
from simple_trader.sizing import PositionSizer, PositionSizingConfig
from simple_trader.stops import StopConfig, StopManager


Signal = Literal["BUY", "SELL", "SELL_SHORT", "BUY_COVER"]
Direction = Literal["LONG", "SHORT"]
FeeMode = Literal["per_contract", "notional_rate"]


@dataclass(frozen=True)
class FuturesBar:
    # 期货 K 线。和最开始的 Bar 很像，只是多了合约代码 symbol。
    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ContractSpec:
    # 合约参数是期货系统和股票系统最不一样的地方之一。
    # multiplier: 合约乘数
    # margin_rate: 保证金比例
    # fee_per_contract: 每手固定手续费
    # fee_mode: 手续费模式，可选按手数固定收费或按成交额比例收费
    # fee_rate: 按成交额比例收费时使用
    # price_tick: 最小价格跳动单位
    # slippage_ticks: 每次成交默认吃掉多少个最小跳动
    symbol: str
    multiplier: int
    margin_rate: float
    fee_per_contract: float
    fee_mode: FeeMode = "per_contract"
    fee_rate: float = 0.0
    price_tick: float = 0.2
    slippage_ticks: int = 1


@dataclass
class FuturesPosition:
    # 为了先把结构讲清楚，这里仍然只支持“单一方向的一笔持仓”。
    symbol: str
    direction: Direction
    size: int
    entry_price: float


@dataclass
class FuturesTrade:
    date: str
    symbol: str
    action: Signal
    price: float
    size: int
    fee: float
    pnl: float = 0.0

    def to_execution_report(self) -> ExecutionReport:
        # 把内部成交记录转换成统一的执行回报格式。
        return ExecutionReport(
            symbol=self.symbol,
            action=self.action,
            size=self.size,
            price=self.price,
            fee=self.fee,
            pnl=self.pnl,
        )


@dataclass
class FuturesAccount:
    # cash 表示账户可用现金 + 已实现盈亏累计后的余额。
    cash: float
    realized_pnl: float = 0.0
    total_fees: float = 0.0


class FuturesStrategy(Protocol):
    def on_bar(self, bars: list[FuturesBar], broker: "FuturesBroker") -> Signal | None:
        """Return one of BUY / SELL / SELL_SHORT / BUY_COVER / None."""


@dataclass
class FuturesBacktestResult:
    starting_cash: float
    ending_cash: float
    ending_equity: float
    realized_pnl: float
    total_fees: float
    total_return_pct: float
    trades: list[FuturesTrade]
    equity_curve: list[tuple[str, float]]


class FuturesBroker(BacktestBroker):
    # 单合约回测 broker 属于 BacktestBroker 角色：用历史 K 线回放并立即成交。
    def __init__(self, contract: ContractSpec, initial_cash: float, risk_limits: RiskLimits | None = None):
        self.contract = contract
        self.risk_limits = risk_limits or RiskLimits()
        self.risk_manager = FuturesRiskManager(self.risk_limits)
        self.account = FuturesAccount(cash=initial_cash)
        self.position: FuturesPosition | None = None
        self.last_reject_reason: str | None = None

    def _notional(self, price: float, size: int) -> float:
        # 计算这笔仓位对应的合约名义价值。
        return price * size * self.contract.multiplier

    def _margin_required(self, price: float, size: int) -> float:
        # 按名义价值和保证金比例，计算开这笔仓需要占用多少保证金。
        return self._notional(price, size) * self.contract.margin_rate

    def _fee(self, price: float, size: int) -> float:
        # 按当前手续费模型，计算这次交易需要付多少手续费。
        if self.contract.fee_mode == "notional_rate":
            return self._notional(price, size) * self.contract.fee_rate
        return size * self.contract.fee_per_contract

    def _apply_slippage(self, action: Signal, price: float) -> float:
        # 按交易方向给一个“对自己不利”的成交价，模拟真实市场滑点。
        slippage = self.contract.price_tick * self.contract.slippage_ticks
        if action in {"BUY", "BUY_COVER"}:
            return price + slippage
        return price - slippage

    def _can_open(self, bar: FuturesBar, fill_price: float, size: int, fee: float) -> tuple[bool, str | None]:
        # 把开仓前的风险检查委托给独立的风控层。
        projected_symbol_margin = self._margin_required(fill_price, size)
        current_equity = self.equity(bar)
        projected_total_margin = self.margin_in_use(bar) + projected_symbol_margin
        decision = self.risk_manager.can_open(
            cash=self.account.cash,
            current_equity=current_equity,
            projected_total_margin=projected_total_margin,
            projected_symbol_margin=projected_symbol_margin,
            projected_symbol_size=size,
            fee=fee,
        )
        return decision.allowed, decision.reason

    def available_cash(self, bar: FuturesBar) -> float:
        # 计算当前还能继续开仓的大致可用资金。
        return self.equity(bar) - self.margin_in_use(bar)

    def margin_in_use(self, bar: FuturesBar) -> float:
        # 计算当前持仓按最新价格大约占用了多少保证金。
        if self.position is None:
            return 0.0
        return self._margin_required(bar.close, self.position.size)

    def unrealized_pnl(self, bar: FuturesBar) -> float:
        # 计算当前持仓如果按最新收盘价估值时的浮动盈亏。
        if self.position is None:
            return 0.0
        direction = 1 if self.position.direction == "LONG" else -1
        price_diff = (bar.close - self.position.entry_price) * direction
        return price_diff * self.position.size * self.contract.multiplier

    def equity(self, bar: FuturesBar) -> float:
        # 期货权益 = 账户现金 + 浮动盈亏
        return self.account.cash + self.unrealized_pnl(bar)

    def account_snapshot(self, bar: FuturesBar) -> BrokerAccountSnapshot:
        # 把当前账户状态整理成统一快照，方便以后和 paper / live broker 对齐。
        equity = self.equity(bar)
        margin = self.margin_in_use(bar)
        return BrokerAccountSnapshot(
            cash=self.account.cash,
            equity=equity,
            margin=margin,
            available=equity - margin,
            realized_pnl=self.account.realized_pnl,
            total_fees=self.account.total_fees,
        )

    def open_position(self, bar: FuturesBar, direction: Direction, size: int) -> FuturesTrade | None:
        # 按当前 K 线价格开一笔新仓，支持开多或开空。
        self.last_reject_reason = None
        if size <= 0 or self.position is not None:
            self.last_reject_reason = "当前不允许开仓：数量必须大于 0，且必须先空仓。"
            return None
        action: Signal = "BUY" if direction == "LONG" else "SELL_SHORT"
        fill_price = self._apply_slippage(action, bar.close)
        fee = self._fee(fill_price, size)
        allowed, reason = self._can_open(bar, fill_price, size, fee)
        if not allowed:
            self.last_reject_reason = reason
            return None
        # 这里不真正扣掉保证金，只检查现金是否足够。
        # 这样 equity / available_cash 的概念会更容易看懂。
        self.account.cash -= fee
        self.account.total_fees += fee
        self.position = FuturesPosition(
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
        # 按当前 K 线价格把现有仓位全部平掉，并结算已实现盈亏。
        self.last_reject_reason = None
        if self.position is None:
            self.last_reject_reason = "当前没有持仓，无法平仓。"
            return None
        action: Signal = "SELL" if self.position.direction == "LONG" else "BUY_COVER"
        fill_price = self._apply_slippage(action, bar.close)
        fee = self._fee(fill_price, self.position.size)
        direction = 1 if self.position.direction == "LONG" else -1
        price_diff = (fill_price - self.position.entry_price) * direction
        pnl = price_diff * self.position.size * self.contract.multiplier
        self.account.cash += pnl
        self.account.cash -= fee
        self.account.realized_pnl += pnl
        self.account.total_fees += fee
        trade = FuturesTrade(
            date=bar.date,
            symbol=bar.symbol,
            action=action,
            price=fill_price,
            size=self.position.size,
            fee=fee,
            pnl=pnl,
        )
        self.position = None
        return trade

    def execute(self, signal: Signal, bar: FuturesBar, size: int) -> list[FuturesTrade]:
        # 把策略信号翻译成具体交易动作，并在需要时执行“平仓后反手开仓”。
        self.last_reject_reason = None
        trades: list[FuturesTrade] = []
        if signal == "BUY":
            if self.position is None:
                trade = self.open_position(bar, direction="LONG", size=size)
                if trade:
                    trades.append(trade)
                return trades
            if self.position.direction == "SHORT":
                close_trade = self.close_position(bar)
                if close_trade:
                    trades.append(close_trade)
                open_trade = self.open_position(bar, direction="LONG", size=size)
                if open_trade:
                    trades.append(open_trade)
                elif self.last_reject_reason is None:
                    self.last_reject_reason = "反手开多失败。"
            return trades

        if signal == "SELL_SHORT":
            if self.position is None:
                trade = self.open_position(bar, direction="SHORT", size=size)
                if trade:
                    trades.append(trade)
                return trades
            if self.position.direction == "LONG":
                close_trade = self.close_position(bar)
                if close_trade:
                    trades.append(close_trade)
                open_trade = self.open_position(bar, direction="SHORT", size=size)
                if open_trade:
                    trades.append(open_trade)
                elif self.last_reject_reason is None:
                    self.last_reject_reason = "反手开空失败。"
            return trades

        if signal == "SELL" and self.position and self.position.direction == "LONG":
            trade = self.close_position(bar)
            if trade:
                trades.append(trade)
            return trades

        if signal == "BUY_COVER" and self.position and self.position.direction == "SHORT":
            trade = self.close_position(bar)
            if trade:
                trades.append(trade)
            return trades

        return trades

    def submit_order(self, intent: OrderIntent, bar: FuturesBar) -> list[ExecutionReport]:
        # 统一执行入口：把“下单意图”交给 backtest broker 执行，并返回标准回报。
        trades = self.execute(intent.action, bar, size=intent.size)
        return [trade.to_execution_report() for trade in trades]


class FuturesBacktestEngine:
    def __init__(
        self,
        bars: list[FuturesBar],
        strategy: FuturesStrategy,
        contract: ContractSpec,
        initial_cash: float = 100_000,
        position_size: int = 1,
        verbose: bool = False,
        risk_limits: RiskLimits | None = None,
        position_sizing: PositionSizingConfig | None = None,
        stop_config: StopConfig | None = None,
    ):
        self.bars = bars
        self.strategy = strategy
        self.contract = contract
        self.position_size = position_size
        self.verbose = verbose
        self.broker = FuturesBroker(contract=contract, initial_cash=initial_cash, risk_limits=risk_limits)
        self.position_sizer = PositionSizer(position_sizing or PositionSizingConfig(fixed_size=position_size))
        self.stop_manager = StopManager(stop_config or StopConfig())
        self.initial_cash = initial_cash
        self.trades: list[FuturesTrade] = []
        self.equity_curve: list[tuple[str, float]] = []

    def _position_text(self) -> str:
        # 把当前持仓转成一行容易阅读的文字。
        if self.broker.position is None:
            return "空仓"
        pos = self.broker.position
        return f"{pos.direction} {pos.symbol} x {pos.size} @ {pos.entry_price:.2f}"

    @staticmethod
    def _fmt_number(value: float | None) -> str:
        # 把数字格式化成更适合教学输出的样子。
        if value is None:
            return "-"
        return f"{value:.2f}"

    def _log_day(
        self,
        bar: FuturesBar,
        signal: Signal | None,
        trades: list[FuturesTrade],
        strategy_info: dict[str, float | bool | None] | None,
        reject_reason: str | None,
        requested_size: int,
        signal_source: str,
    ):
        # 打印“今天系统看到了什么、想了什么、做了什么”。
        short_ma = None
        long_ma = None
        crossed_up = False
        crossed_down = False
        if strategy_info is not None:
            short_ma = strategy_info.get("short_ma")  # type: ignore[assignment]
            long_ma = strategy_info.get("long_ma")  # type: ignore[assignment]
            crossed_up = bool(strategy_info.get("crossed_up"))
            crossed_down = bool(strategy_info.get("crossed_down"))

        print(
            f"[DAY] {bar.date} close={bar.close:.2f} "
            f"short_ma={self._fmt_number(short_ma)} "
            f"long_ma={self._fmt_number(long_ma)} "
            f"crossed_up={crossed_up} crossed_down={crossed_down}"
        )
        print(
            f"      position={self._position_text()} "
            f"signal={signal or 'NONE'} source={signal_source} requested_size={requested_size}"
        )
        if not trades:
            print("      trades=NONE")
        else:
            for trade in trades:
                trade_line = (
                    f"      trade={trade.action} {trade.symbol} "
                    f"size={trade.size} price={trade.price:.2f} fee={trade.fee:.2f}"
                )
                if trade.action in {"SELL", "BUY_COVER"}:
                    trade_line += f" pnl={trade.pnl:.2f}"
                print(trade_line)
        if reject_reason:
            print(f"      reject={reject_reason}")
        print(
            f"      cash={self.broker.account.cash:.2f} "
            f"equity={self.broker.equity(bar):.2f} "
            f"margin={self.broker.margin_in_use(bar):.2f} "
            f"available={self.broker.available_cash(bar):.2f}"
        )

    def run(self) -> FuturesBacktestResult:
        # 一根一根回放历史 K 线，让策略产生信号，再交给 broker 执行。
        seen_bars: list[FuturesBar] = []
        for bar in self.bars:
            self.broker.last_reject_reason = None
            seen_bars.append(bar)
            strategy_info = None
            if hasattr(self.strategy, "describe_bar"):
                strategy_info = self.strategy.describe_bar(seen_bars)  # type: ignore[assignment]
            signal = None
            signal_source = "NONE"
            if self.broker.position is not None:
                stop_decision = self.stop_manager.check(
                    direction=self.broker.position.direction,
                    entry_price=self.broker.position.entry_price,
                    current_price=bar.close,
                )
                if stop_decision.triggered:
                    signal = stop_decision.signal
                    self.broker.last_reject_reason = stop_decision.reason
                    signal_source = "STOP"
            if signal is None:
                signal = self.strategy.on_bar(seen_bars, self.broker)
                signal_source = "STRATEGY" if signal is not None else "NONE"
            trades: list[FuturesTrade] = []
            requested_size = self.position_size
            if signal is not None:
                if signal in {"BUY", "SELL_SHORT"} and self.broker.position is None:
                    fill_action = signal
                    fill_price = self.broker._apply_slippage(fill_action, bar.close)
                    margin_per_contract = self.broker._margin_required(fill_price, 1)
                    requested_size = self.position_sizer.size_from_margin_budget(
                        equity=self.broker.equity(bar),
                        margin_per_contract=margin_per_contract,
                        max_position_size=self.broker.risk_limits.max_position_size_per_symbol,
                    )
                    if requested_size == 0:
                        self.broker.last_reject_reason = "按当前风险预算计算，允许开仓手数为 0。"
                if signal in {"BUY", "SELL_SHORT"} and self.broker.position is not None:
                    requested_size = self.position_sizer.fixed_size()
                if signal in {"SELL", "BUY_COVER"}:
                    requested_size = self.broker.position.size if self.broker.position else 0
                if requested_size > 0:
                    reports = self.broker.submit_order(
                        OrderIntent(symbol=bar.symbol, action=signal, size=requested_size, source=signal_source),
                        bar,
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
                self.trades.extend(trades)
            self.equity_curve.append((bar.date, self.broker.equity(bar)))
            if self.verbose:
                self._log_day(
                    bar, signal, trades, strategy_info, self.broker.last_reject_reason, requested_size, signal_source
                )

        if self.bars and self.broker.position is not None:
            # 回测结束时把最后一笔仓位平掉，方便看最终已实现结果。
            final_trade = self.broker.close_position(self.bars[-1])
            if final_trade:
                self.trades.append(final_trade)
                self.equity_curve[-1] = (self.bars[-1].date, self.broker.equity(self.bars[-1]))

        ending_equity = self.equity_curve[-1][1] if self.equity_curve else self.initial_cash
        total_return_pct = (ending_equity - self.initial_cash) / self.initial_cash * 100
        return FuturesBacktestResult(
            starting_cash=self.initial_cash,
            ending_cash=self.broker.account.cash,
            ending_equity=ending_equity,
            realized_pnl=self.broker.account.realized_pnl,
            total_fees=self.broker.account.total_fees,
            total_return_pct=total_return_pct,
            trades=self.trades,
            equity_curve=self.equity_curve,
        )
