from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from simple_trader.execution import ExecutionReport, OrderIntent, Signal
from simple_trader.risk import FuturesRiskManager, RiskLimits


BrokerMode = Literal["BACKTEST", "PAPER", "LIVE"]
Direction = Literal["LONG", "SHORT"]
FeeMode = Literal["per_contract", "notional_rate"]


class ContractLike(Protocol):
    # ContractLike 约定了 paper broker 最少需要知道哪些合约参数。
    symbol: str
    multiplier: int
    margin_rate: float
    fee_per_contract: float
    fee_mode: FeeMode
    fee_rate: float
    price_tick: float
    slippage_ticks: int


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    # 统一账户快照：不管是回测、仿真还是实盘，都尽量对外暴露同一种账户视角。
    cash: float
    equity: float
    margin: float
    available: float
    realized_pnl: float
    total_fees: float


class BacktestBroker:
    # Backtest broker 代表“用历史数据回放并立即成交”的执行角色。
    broker_mode: BrokerMode = "BACKTEST"
    is_live: bool = False


@dataclass
class PaperPosition:
    # Paper 持仓和回测持仓很像，只是去掉了回测里专用的日期语义。
    symbol: str
    direction: Direction
    size: int
    entry_price: float


@dataclass
class PaperBroker:
    # Paper broker 负责“拿最新行情做本地模拟撮合”，但不会真的把订单发到柜台。
    contracts: dict[str, ContractLike]
    initial_cash: float
    risk_limits: RiskLimits = field(default_factory=RiskLimits)
    cash: float = field(init=False)
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    last_price_by_symbol: dict[str, float] = field(default_factory=dict)
    fills: list[ExecutionReport] = field(default_factory=list)
    last_reject_reason: str | None = None
    broker_mode: BrokerMode = field(init=False, default="PAPER")
    is_live: bool = field(init=False, default=False)

    def __post_init__(self):
        # Paper broker 启动时先初始化资金和风控管理器。
        self.cash = self.initial_cash
        self.risk_manager = FuturesRiskManager(self.risk_limits)

    def _contract(self, symbol: str) -> ContractLike:
        # 取出某个合约自己的交易规则。
        return self.contracts[symbol]

    def _notional(self, symbol: str, price: float, size: int) -> float:
        # 计算这笔仓位在该合约下对应的名义价值。
        contract = self._contract(symbol)
        return price * size * contract.multiplier

    def _margin_required(self, symbol: str, price: float, size: int) -> float:
        # 按最新价格估算这笔仓位会占用多少保证金。
        contract = self._contract(symbol)
        return self._notional(symbol, price, size) * contract.margin_rate

    def _fee(self, symbol: str, price: float, size: int) -> float:
        # 按该合约自己的手续费模式，估算这次成交成本。
        contract = self._contract(symbol)
        if contract.fee_mode == "notional_rate":
            return self._notional(symbol, price, size) * contract.fee_rate
        return size * contract.fee_per_contract

    def _apply_slippage(self, symbol: str, action: Signal, price: float) -> float:
        # 按交易方向给一个更保守的成交价，模拟 paper matching 的滑点。
        contract = self._contract(symbol)
        slippage = contract.price_tick * contract.slippage_ticks
        if action in {"BUY", "BUY_COVER"}:
            return price + slippage
        return price - slippage

    def update_market_price(self, symbol: str, price: float):
        # 记录最新行情，paper matching 会用它作为当前可成交价格。
        self.last_price_by_symbol[symbol] = price

    def position_of(self, symbol: str) -> PaperPosition | None:
        # 查询某个合约当前是否持仓，方便策略读取状态。
        return self.positions.get(symbol)

    def margin_in_use(self) -> float:
        # 计算整个 paper 账户按最新价格估值后占用了多少保证金。
        total = 0.0
        for symbol, position in self.positions.items():
            price = self.last_price_by_symbol.get(symbol)
            if price is None:
                continue
            total += self._margin_required(symbol, price, position.size)
        return total

    def unrealized_pnl(self) -> float:
        # 计算所有持仓按最新价格估值时的总浮动盈亏。
        total = 0.0
        for symbol, position in self.positions.items():
            price = self.last_price_by_symbol.get(symbol)
            if price is None:
                continue
            direction = 1 if position.direction == "LONG" else -1
            price_diff = (price - position.entry_price) * direction
            total += price_diff * position.size * self._contract(symbol).multiplier
        return total

    def account_snapshot(self) -> BrokerAccountSnapshot:
        # 把 paper 账户当前状态整理成统一快照。
        margin = self.margin_in_use()
        equity = self.cash + self.unrealized_pnl()
        return BrokerAccountSnapshot(
            cash=self.cash,
            equity=equity,
            margin=margin,
            available=equity - margin,
            realized_pnl=self.realized_pnl,
            total_fees=self.total_fees,
        )

    def _can_open(self, symbol: str, fill_price: float, size: int, fee: float) -> tuple[bool, str | None]:
        # 开新仓前，先按组合口径检查总保证金和单品种风险是否超限。
        required_margin = self._margin_required(symbol, fill_price, size)
        snapshot = self.account_snapshot()
        projected_total_margin = snapshot.margin + required_margin

        current_symbol_position = self.positions.get(symbol)
        current_symbol_margin = 0.0
        current_symbol_size = 0
        if current_symbol_position is not None:
            current_price = self.last_price_by_symbol.get(symbol)
            if current_price is not None:
                current_symbol_margin = self._margin_required(symbol, current_price, current_symbol_position.size)
            current_symbol_size = current_symbol_position.size

        decision = self.risk_manager.can_open(
            cash=self.cash,
            current_equity=snapshot.equity,
            projected_total_margin=projected_total_margin,
            projected_symbol_margin=current_symbol_margin + required_margin,
            projected_symbol_size=current_symbol_size + size,
            fee=fee,
        )
        return decision.allowed, decision.reason

    def _open_position(self, symbol: str, direction: Direction, size: int) -> ExecutionReport | None:
        # 按当前最新价格在 paper 账户里开一笔新仓。
        self.last_reject_reason = None
        if size <= 0 or symbol in self.positions:
            self.last_reject_reason = "当前不允许开仓：数量必须大于 0，且该合约必须先空仓。"
            return None
        price = self.last_price_by_symbol.get(symbol)
        if price is None:
            self.last_reject_reason = f"{symbol} 还没有最新价格，无法模拟成交。"
            return None
        action: Signal = "BUY" if direction == "LONG" else "SELL_SHORT"
        fill_price = self._apply_slippage(symbol, action, price)
        fee = self._fee(symbol, fill_price, size)
        allowed, reason = self._can_open(symbol, fill_price, size, fee)
        if not allowed:
            self.last_reject_reason = reason
            return None
        self.cash -= fee
        self.total_fees += fee
        self.positions[symbol] = PaperPosition(symbol=symbol, direction=direction, size=size, entry_price=fill_price)
        report = ExecutionReport(symbol=symbol, action=action, size=size, price=fill_price, fee=fee, pnl=0.0)
        self.fills.append(report)
        return report

    def _close_position(self, symbol: str) -> ExecutionReport | None:
        # 按当前最新价格把 paper 账户里这个合约的仓位全部平掉。
        self.last_reject_reason = None
        position = self.positions.get(symbol)
        if position is None:
            self.last_reject_reason = f"{symbol} 当前没有持仓，无法平仓。"
            return None
        price = self.last_price_by_symbol.get(symbol)
        if price is None:
            self.last_reject_reason = f"{symbol} 还没有最新价格，无法模拟成交。"
            return None
        action: Signal = "SELL" if position.direction == "LONG" else "BUY_COVER"
        fill_price = self._apply_slippage(symbol, action, price)
        fee = self._fee(symbol, fill_price, position.size)
        direction = 1 if position.direction == "LONG" else -1
        price_diff = (fill_price - position.entry_price) * direction
        pnl = price_diff * position.size * self._contract(symbol).multiplier
        self.cash += pnl
        self.cash -= fee
        self.realized_pnl += pnl
        self.total_fees += fee
        self.positions.pop(symbol, None)
        report = ExecutionReport(symbol=symbol, action=action, size=position.size, price=fill_price, fee=fee, pnl=pnl)
        self.fills.append(report)
        return report

    def submit_order(self, intent: OrderIntent) -> list[ExecutionReport]:
        # Paper matching 的统一入口：收到下单意图后，按最新价格立刻做一版本地模拟撮合。
        self.last_reject_reason = None
        symbol = intent.symbol
        size = intent.size
        position = self.positions.get(symbol)
        reports: list[ExecutionReport] = []

        if intent.action == "BUY":
            if position is None:
                report = self._open_position(symbol, direction="LONG", size=size)
                if report:
                    reports.append(report)
                return reports
            if position.direction == "SHORT":
                close_report = self._close_position(symbol)
                if close_report:
                    reports.append(close_report)
                open_report = self._open_position(symbol, direction="LONG", size=size)
                if open_report:
                    reports.append(open_report)
                return reports
            return reports

        if intent.action == "SELL_SHORT":
            if position is None:
                report = self._open_position(symbol, direction="SHORT", size=size)
                if report:
                    reports.append(report)
                return reports
            if position.direction == "LONG":
                close_report = self._close_position(symbol)
                if close_report:
                    reports.append(close_report)
                open_report = self._open_position(symbol, direction="SHORT", size=size)
                if open_report:
                    reports.append(open_report)
                return reports
            return reports

        if intent.action == "SELL" and position and position.direction == "LONG":
            report = self._close_position(symbol)
            if report:
                reports.append(report)
            return reports

        if intent.action == "BUY_COVER" and position and position.direction == "SHORT":
            report = self._close_position(symbol)
            if report:
                reports.append(report)
            return reports

        return reports


@dataclass
class LiveBroker:
    # Live broker 先定义成真实柜台角色的骨架，后面再把 CTP 或券商网关接进来。
    gateway_name: str = "ctp"
    connected: bool = False
    cash: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    available: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    broker_mode: BrokerMode = field(init=False, default="LIVE")
    is_live: bool = field(init=False, default=True)
    last_error: str | None = None

    def connect(self):
        # 先提供一个最小连接入口，后面再替换成真实网关登录流程。
        self.connected = True
        self.last_error = None

    def disconnect(self):
        # 断开真实网关连接时，把状态切回未连接。
        self.connected = False

    def submit_order(self, intent: OrderIntent):
        # 真实下单还没接网关，所以这里明确提示“骨架已在，执行未接”。
        raise NotImplementedError(f"{self.gateway_name} live broker 还没有接入真实下单网关。")

    def account_snapshot(self) -> BrokerAccountSnapshot:
        # Live broker 统一对外暴露账户快照，后续由真实柜台资金回报来填充。
        return BrokerAccountSnapshot(
            cash=self.cash,
            equity=self.equity,
            margin=self.margin,
            available=self.available,
            realized_pnl=self.realized_pnl,
            total_fees=self.total_fees,
        )
