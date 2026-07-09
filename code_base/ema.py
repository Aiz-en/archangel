"""EMA helpers for the strategy engine.

The bull-flag strategy uses 9 EMA and 12 EMA on the close price (see
docs/trading_strategy_baseline.md). This module wraps yfinance + pandas
to fetch OHLCV bars and tack on EMA columns, so strategy code can stay
focused on signal logic.

We use `adjust=False` on `.ewm()` — that gives the recursive form
EMA_t = alpha * close_t + (1 - alpha) * EMA_{t-1}, which is what
TradingView, ThinkOrSwim, and most traders' eyes are calibrated to.
"""

import sys
from typing import Iterable

import pandas as pd
import yfinance as yf


def add_ema(bars: pd.DataFrame, windows: Iterable[int], price_col: str = "Close") -> pd.DataFrame:
    """Return a copy of `bars` with an `EMA_<n>` column for each window."""
    out = bars.copy()
    for n in windows:
        out[f"EMA_{n}"] = out[price_col].ewm(span=n, adjust=False).mean()
    return out


def fetch_with_ema(
    symbol: str,
    interval: str = "5m",
    period: str = "1d",
    ema_windows: Iterable[int] = (9, 12),
) -> pd.DataFrame:
    """Fetch OHLCV bars for `symbol` and append EMA columns."""
    bars = yf.Ticker(symbol).history(period=period, interval=interval)
    if bars.empty:
        return bars
    return add_ema(bars, ema_windows)


def main() -> int:
    symbol = "AAPL"
    bars = fetch_with_ema(symbol, interval="5m", period="1d", ema_windows=(9, 12))

    if bars.empty:
        print(
            f"No bars returned for {symbol}. Market may be closed with no "
            "recent session, or yfinance was rate-limited.",
            file=sys.stderr,
        )
        return 1

    cols = ["Open", "High", "Low", "Close", "EMA_9", "EMA_12"]
    print(f"Fetched {len(bars)} 5m bars for {symbol}. Last 10:")
    print(bars[cols].tail(10).round(2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
