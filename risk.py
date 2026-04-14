from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    # 最基础的风控参数：
    # 1. 总保证金占权益不能太高
    # 2. 单品种保证金占权益不能太高
    # 3. 单品种最多持有多少手
    max_margin_ratio: float = 0.5
    max_symbol_margin_ratio: float = 0.35
    max_position_size_per_symbol: int = 1


@dataclass(frozen=True)
class RiskDecision:
    # 风控层的输出：允许 or 拒绝，以及拒绝原因。
    allowed: bool
    reason: str | None = None


class FuturesRiskManager:
    # 风控管理器负责回答一句话：这笔新仓现在能不能开？
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def can_open(
        self,
        *,
        cash: float,
        current_equity: float,
        projected_total_margin: float,
        projected_symbol_margin: float,
        projected_symbol_size: int,
        fee: float,
    ) -> RiskDecision:
        # 在真正开仓前，检查总保证金、单品种保证金和单品种手数是否满足风控要求。
        projected_total_ratio = projected_total_margin / current_equity if current_equity > 0 else float("inf")
        projected_symbol_ratio = projected_symbol_margin / current_equity if current_equity > 0 else float("inf")
        if projected_symbol_size > self.limits.max_position_size_per_symbol:
            return RiskDecision(
                allowed=False,
                reason=(
                    "单品种手数超限，"
                    f"预计持有 {projected_symbol_size} 手，"
                    f"上限为 {self.limits.max_position_size_per_symbol} 手"
                ),
            )
        if cash < projected_total_margin + fee:
            return RiskDecision(
                allowed=False,
                reason=(
                    "现金不足，"
                    f"开仓后总保证金需要 {projected_total_margin:.2f} + 手续费 {fee:.2f}，"
                    f"当前现金只有 {cash:.2f}"
                ),
            )
        if projected_symbol_ratio > self.limits.max_symbol_margin_ratio:
            return RiskDecision(
                allowed=False,
                reason=(
                    "单品种保证金占比超限，"
                    f"预计占用 {projected_symbol_ratio:.2%}，"
                    f"上限为 {self.limits.max_symbol_margin_ratio:.2%}"
                ),
            )
        if projected_total_ratio > self.limits.max_margin_ratio:
            return RiskDecision(
                allowed=False,
                reason=(
                    "组合保证金占比超限，"
                    f"预计占用 {projected_total_ratio:.2%}，"
                    f"上限为 {self.limits.max_margin_ratio:.2%}"
                ),
            )
        return RiskDecision(allowed=True)
