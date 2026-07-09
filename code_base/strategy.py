"""Bull-flag strategy detection.

Pure functions over pandas DataFrames so they're easy to test and reuse
between live trading and backtests. The actual runner that ties yfinance,
strategy, and Portfolio together lives elsewhere.

Pattern (see docs/trading_strategy_baseline.md):
- 5m setup: 3+ consecutive green candles (pole), then 2–3 red candles
  (pullback). All bars in the window must stay above the 12 EMA.
- 1m trigger: a bar's wick crosses through the 9 EMA value
  (`low <= EMA_9 <= high`). Entry price is the 9 EMA value.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ema import add_ema


@dataclass
class BullFlagSetup:
    pole_bars: int
    pullback_bars: int
    window_start: pd.Timestamp
    window_end: pd.Timestamp


def detect_bull_flag_setup(
    bars_5m: pd.DataFrame,
    min_pole: int = 3,
    min_pullback: int = 2,
    max_pullback: int = 3,
    ema_col: str = "EMA_12",
) -> Optional[BullFlagSetup]:
    """Return a setup if the last bars match the bull-flag pattern, else None.

    `bars_5m` must have Open/High/Low/Close columns and an `ema_col` column
    (typically EMA_12), with rows in chronological order.
    """
    if ema_col not in bars_5m.columns:
        raise ValueError(f"bars_5m missing required column {ema_col!r}")
    if len(bars_5m) < min_pole + min_pullback:
        return None

    is_green = bars_5m["Close"] > bars_5m["Open"]
    is_red = bars_5m["Close"] < bars_5m["Open"]
    above_ema = bars_5m["Low"] >= bars_5m[ema_col]

    # Try larger pullbacks first — more selective when both fit.
    for pullback_n in range(max_pullback, min_pullback - 1, -1):
        window_size = pullback_n + min_pole
        if len(bars_5m) < window_size:
            continue

        pole_slice = is_green.iloc[-window_size:-pullback_n]
        pullback_slice = is_red.iloc[-pullback_n:]
        ema_slice = above_ema.iloc[-window_size:]

        if not pole_slice.all():
            continue
        if not pullback_slice.all():
            continue
        if not ema_slice.all():
            continue

        window = bars_5m.iloc[-window_size:]
        return BullFlagSetup(
            pole_bars=min_pole,
            pullback_bars=pullback_n,
            window_start=window.index[0],
            window_end=window.index[-1],
        )
    return None


def is_9_ema_touch(bar_low: float, bar_high: float, ema_9: float) -> bool:
    """The 1m entry trigger: wick crosses through the 9 EMA value."""
    return bar_low <= ema_9 <= bar_high


def _make_bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Helper for the smoke test — builds an OHLC DataFrame from tuples."""
    idx = pd.date_range("2026-04-28 09:30", periods=len(rows), freq="5min")
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


# Sideways runway at $95 so EMA_12 settles below the bull-flag prices that follow.
# Real setups always have history; without it, the EMA is seeded from the first
# close and ends up above the bars' lows, breaking the "above EMA" check.
_RUNWAY = [(95.0, 95.3, 94.7, 95.2)] * 10


def _smoke_test() -> int:
    failures = 0

    # Case 1: clean bull flag (3 green pole + 2 red pullback, all above EMA_12)
    bars = _make_bars(_RUNWAY + [
        (100.0, 101.0, 99.5, 100.8),
        (100.8, 102.0, 100.5, 101.7),
        (101.7, 103.0, 101.5, 102.8),
        (102.8, 103.5, 102.0, 102.2),
        (102.2, 102.5, 101.5, 101.7),
    ])
    bars = add_ema(bars, [9, 12])
    setup = detect_bull_flag_setup(bars)
    if setup is None:
        print("FAIL case 1: clean bull flag was not detected", file=sys.stderr)
        failures += 1
    else:
        print(f"PASS case 1: setup detected ({setup.pole_bars}g + {setup.pullback_bars}r)")

    # Case 2: pullback breaks below 12 EMA → reject
    bars = _make_bars(_RUNWAY + [
        (100.0, 101.0, 99.5, 100.8),
        (100.8, 102.0, 100.5, 101.7),
        (101.7, 103.0, 101.5, 102.8),
        (102.8, 103.5, 95.0, 96.0),
        (96.0, 97.0, 94.0, 94.5),
    ])
    bars = add_ema(bars, [9, 12])
    if detect_bull_flag_setup(bars) is None:
        print("PASS case 2: pullback below 12 EMA correctly rejected")
    else:
        print("FAIL case 2: should have rejected pullback that broke 12 EMA", file=sys.stderr)
        failures += 1

    # Case 3: only 2 green bars before pullback → reject (1 red, 2 green, 2 red)
    bars = _make_bars(_RUNWAY + [
        (100.0, 100.5, 99.0, 99.5),   # red
        (99.5, 101.0, 99.3, 100.8),   # green
        (100.8, 102.0, 100.5, 101.7), # green
        (101.7, 102.0, 101.0, 101.2), # red
        (101.2, 101.5, 100.5, 100.8), # red
    ])
    bars = add_ema(bars, [9, 12])
    if detect_bull_flag_setup(bars) is None:
        print("PASS case 3: insufficient pole correctly rejected")
    else:
        print("FAIL case 3: should have rejected — only 2 green bars", file=sys.stderr)
        failures += 1

    # Case 4: 9 EMA touch trigger
    if is_9_ema_touch(bar_low=99.5, bar_high=100.5, ema_9=100.0):
        print("PASS case 4: 9 EMA inside wick range -> trigger fires")
    else:
        print("FAIL case 4", file=sys.stderr)
        failures += 1

    if is_9_ema_touch(bar_low=100.5, bar_high=101.0, ema_9=100.0):
        print("FAIL case 5: 9 EMA below the bar should NOT trigger", file=sys.stderr)
        failures += 1
    else:
        print("PASS case 5: 9 EMA outside wick range -> no trigger")

    if failures:
        print(f"\n{failures} failure(s)", file=sys.stderr)
        return 1
    print("\nAll cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
