from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSizingConfig:
    # 仓位配置：
    # fixed_size: 固定每次开多少手
    # max_margin_fraction_per_trade: 单次新开仓最多允许拿多少权益去占保证金
    fixed_size: int = 1
    max_margin_fraction_per_trade: float = 0.2


class PositionSizer:
    # 仓位管理器负责回答一句话：这次信号最多应该下多少手？
    def __init__(self, config: PositionSizingConfig):
        self.config = config

    def fixed_size(self) -> int:
        # 返回固定手数模式下的默认开仓数量。
        return max(0, self.config.fixed_size)

    def size_from_margin_budget(
        self,
        *,
        equity: float,
        margin_per_contract: float,
        max_position_size: int,
    ) -> int:
        # 按“单次开仓最多用多少权益去占保证金”来估算可开手数。
        if equity <= 0 or margin_per_contract <= 0 or max_position_size <= 0:
            return 0
        budget = equity * self.config.max_margin_fraction_per_trade
        size = int(budget // margin_per_contract)
        if size < 1:
            return 0
        return min(size, max_position_size)

