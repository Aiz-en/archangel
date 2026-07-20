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
from typing import Iterable, Optional

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
    ema_floor: str = "low",
    floor_scope: str = "window",
    doji_tolerant_pole: bool = False,
) -> Optional[BullFlagSetup]:
    """Return a setup if the last bars match the bull-flag pattern, else None.

    `bars_5m` must have Open/High/Low/Close columns and an `ema_col` column
    (typically EMA_12), with rows in chronological order.

    The defaults are the documented baseline strategy. The relaxation knobs
    exist for the fast-mover experiments (see ENTRY_MODES):
    - ema_floor="close": bodies must hold the EMA but wicks may pierce it
      (violent low-float tapes wick through EMAs on most bars).
    - floor_scope="pullback": the EMA floor applies to the pullback bars only.
      On fast movers the pole IS what drags the EMA up, so early pole bars
      necessarily straddle the lagging EMA (SDOT 2026-07-17: all three pole
      bars' wicks below EMA_12, the first pole bar's close below it too —
      a hand-validated setup, rejected by the window-wide floor).
    - doji_tolerant_pole=True: a doji doesn't break the pole run, but the
      pole must still contain at least one true green candle.
    """
    if ema_col not in bars_5m.columns:
        raise ValueError(f"bars_5m missing required column {ema_col!r}")
    if floor_scope not in ("window", "pullback"):
        raise ValueError(f"unknown floor_scope {floor_scope!r}")
    if len(bars_5m) < min_pole + min_pullback:
        return None

    is_green = bars_5m["Close"] > bars_5m["Open"]
    is_red = bars_5m["Close"] < bars_5m["Open"]
    pole_bar_ok = ~is_red if doji_tolerant_pole else is_green
    if ema_floor == "close":
        above_ema = bars_5m["Close"] >= bars_5m[ema_col]
    else:
        above_ema = bars_5m["Low"] >= bars_5m[ema_col]

    # Try larger pullbacks first — more selective when both fit.
    for pullback_n in range(max_pullback, min_pullback - 1, -1):
        window_size = pullback_n + min_pole
        if len(bars_5m) < window_size:
            continue

        pole_slice = pole_bar_ok.iloc[-window_size:-pullback_n]
        pole_has_green = is_green.iloc[-window_size:-pullback_n].any()
        pullback_slice = is_red.iloc[-pullback_n:]
        if floor_scope == "pullback":
            ema_slice = above_ema.iloc[-pullback_n:]
        else:
            ema_slice = above_ema.iloc[-window_size:]

        if not (pole_slice.all() and pole_has_green):
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


# Named entry-rule configurations. "strict" is the documented baseline
# strategy (docs/trading_strategy_baseline.md). "relaxed" is the fast-mover
# experiment: pole >=2 with dojis tolerated, pullback 1-3 reds, EMA floor on
# closes. Measured on 6 candidate tapes over 5 days (2026-07-13): strict took
# 1 trade, relaxed took 43 at a 30% win rate (breakeven for the -5%/+10%
# bracket is 33.3%) — relaxed exists to gather evidence at volume on the
# shadow runner, NOT because it has proven edge yet.
ENTRY_MODES: dict[str, dict] = {
    "strict": {},
    "relaxed": dict(
        min_pole=2, min_pullback=1, ema_floor="close", doji_tolerant_pole=True
    ),
    # From the SDOT 2026-07-17 case study (a trader-annotated tape): baseline
    # pole/pullback grammar, but the EMA floor moves to the pullback bars only
    # (close basis) — pole wicks below the lagging EMA are expected on fast
    # movers. min_pullback=1 so the gate arms while pullback bar 2 is still
    # forming, matching how the entry is actually timed. Pair with
    # is_ema_reversal_touch() as the 1m trigger, NOT is_9_ema_touch().
    "case_study": dict(min_pullback=1, ema_floor="close", floor_scope="pullback"),
}


def detect_setup(bars_5m: pd.DataFrame, mode: str = "strict") -> Optional[BullFlagSetup]:
    """Mode-selected entry detection — the single gate runners should call."""
    try:
        params = ENTRY_MODES[mode]
    except KeyError:
        raise ValueError(f"unknown entry mode {mode!r}; choose from {sorted(ENTRY_MODES)}")
    return detect_bull_flag_setup(bars_5m, **params)


def is_9_ema_touch(bar_low: float, bar_high: float, ema_9: float) -> bool:
    """The 1m entry trigger: wick crosses through the 9 EMA value."""
    return bar_low <= ema_9 <= bar_high


def is_ema_reversal_touch(
    bar_low: float,
    bar_high: float,
    emas: Iterable[float],
    tolerance: float = 0.005,
) -> bool:
    """Case-study 1m trigger (SDOT 2026-07-17 exercise spec).

    Fires when the bar intersects — or comes within `tolerance` above — ANY of
    the given EMAs (9 and/or 12): "coming close or intersecting with one of the
    EMA lines is a strong indicator for a reversal on the pullback." A true
    touch is NOT required; tolerance is 0.5% for now, subject to change.
    Bars sitting entirely below an EMA don't fire — the pullback has to reach
    the EMA from above, not break down through it.
    """
    for ema in emas:
        if bar_low <= ema * (1 + tolerance) and bar_high >= ema:
            return True
    return False


def entry_trigger_fires(
    mode: str,
    bar_low: float,
    bar_high: float,
    ema_9: float,
    ema_12: Optional[float] = None,
) -> bool:
    """The mode-paired 1m trigger — the single trigger runners should call.

    strict/relaxed pair with the baseline is_9_ema_touch (EMA 9 wick-through);
    case_study pairs with is_ema_reversal_touch (either EMA, 0.5% proximity).
    Keeping the pairing here means live_runner and backtests can never drift
    apart on which trigger a mode uses. Callers without an EMA_12 value pass
    None; case_study then tests the 9 EMA alone.
    """
    if mode not in ENTRY_MODES:
        raise ValueError(f"unknown entry mode {mode!r}; choose from {sorted(ENTRY_MODES)}")
    if mode == "case_study":
        emas = [ema_9] if ema_12 is None else [ema_9, ema_12]
        return is_ema_reversal_touch(bar_low, bar_high, emas)
    return is_9_ema_touch(bar_low, bar_high, ema_9)


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

    # Case 6: fast-mover tape — green, doji, green pole with a 1-red rest.
    # strict must reject it (doji breaks the pole, pullback too short);
    # relaxed must accept it (doji-tolerant pole >=2, pullback >=1,
    # close-basis EMA floor).
    bars = _make_bars(_RUNWAY + [
        (95.2, 96.7, 95.1, 96.5),   # green
        (96.5, 97.0, 96.2, 96.5),   # doji
        (96.5, 98.2, 96.4, 98.0),   # green
        (98.0, 98.2, 97.2, 97.5),   # single red rest
    ])
    bars = add_ema(bars, [9, 12])
    strict_hit = detect_setup(bars, mode="strict")
    relaxed_hit = detect_setup(bars, mode="relaxed")
    if strict_hit is None and relaxed_hit is not None:
        print("PASS case 6: doji-pole tape — strict rejects, relaxed detects")
    else:
        print(f"FAIL case 6: strict={strict_hit}, relaxed={relaxed_hit}",
              file=sys.stderr)
        failures += 1

    # Case 7: SDOT-shaped fast mover — 3-green pole whose wicks dip below the
    # lagging EMA_12 (first pole bar closes below it too), then 2 reds holding
    # above on a close basis. strict must reject (window-wide wick floor);
    # case_study must accept (pullback-only close floor).
    bars = _make_bars(_RUNWAY + [
        (95.0, 96.6, 94.9, 96.4),      # green, wick below EMA_12, close below too
        (96.4, 98.4, 95.3, 98.2),      # green, wick below EMA_12
        (98.2, 101.0, 98.0, 100.8),    # green
        (100.8, 101.2, 99.6, 100.0),   # red, close above EMA_12
        (100.0, 100.4, 98.8, 99.2),    # red, close above EMA_12
    ])
    bars = add_ema(bars, [9, 12])
    strict_hit = detect_setup(bars, mode="strict")
    cs_hit = detect_setup(bars, mode="case_study")
    if strict_hit is None and cs_hit is not None:
        print("PASS case 7: pole-wicks-below-EMA tape — strict rejects, case_study detects")
    else:
        print(f"FAIL case 7: strict={strict_hit}, case_study={cs_hit}", file=sys.stderr)
        failures += 1

    # Case 8: the reversal-touch trigger — near-miss within tolerance fires,
    # beyond tolerance doesn't, entirely-below-the-EMA doesn't, second EMA counts.
    checks = [
        (is_ema_reversal_touch(100.2, 100.8, [100.0]), True, "0.2% above fires"),
        (is_ema_reversal_touch(100.6, 101.0, [100.0]), False, "0.6% above stays quiet"),
        (is_ema_reversal_touch(98.0, 99.5, [100.0]), False, "bar fully below stays quiet"),
        (is_ema_reversal_touch(99.8, 100.4, [100.0]), True, "true intersect fires"),
        (is_ema_reversal_touch(100.6, 101.0, [100.0, 100.55]), True, "second EMA catches it"),
    ]
    if all(got == want for got, want, _ in checks):
        print("PASS case 8: reversal-touch trigger tolerance behavior")
    else:
        for got, want, what in checks:
            if got != want:
                print(f"FAIL case 8: {what} (got {got})", file=sys.stderr)
        failures += 1

    # Case 9: mode->trigger pairing. The same near-miss bar (0.2% above the
    # 9 EMA, never touching it) must stay quiet under strict's wick-through
    # trigger and fire under case_study's proximity trigger; unknown modes raise.
    strict_fire = entry_trigger_fires("strict", 100.2, 100.8, ema_9=100.0)
    cs_fire = entry_trigger_fires("case_study", 100.2, 100.8, ema_9=100.0)
    try:
        entry_trigger_fires("bogus", 100.2, 100.8, ema_9=100.0)
        raised = False
    except ValueError:
        raised = True
    if (not strict_fire) and cs_fire and raised:
        print("PASS case 9: mode-paired trigger dispatch")
    else:
        print(f"FAIL case 9: strict={strict_fire} case_study={cs_fire} "
              f"raised={raised}", file=sys.stderr)
        failures += 1

    if failures:
        print(f"\n{failures} failure(s)", file=sys.stderr)
        return 1
    print("\nAll cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
