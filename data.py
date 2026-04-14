from __future__ import annotations

import csv
from pathlib import Path

from simple_trader.futures import FuturesBar


def load_futures_bars_from_csv(path: str | Path) -> list[FuturesBar]:
    # 用标准库 csv 读取，尽量保持代码朴素、容易看懂。
    csv_path = Path(path)
    bars: list[FuturesBar] = []
    with csv_path.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for row in reader:
            bars.append(
                FuturesBar(
                    symbol=row["symbol"],
                    date=row["date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    return bars

