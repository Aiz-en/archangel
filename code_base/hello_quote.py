"""First market-data smoke test, via Yahoo Finance (yfinance).

Webull's OpenAPI requires a paid market-data subscription, so we're
prototyping the strategy engine on free Yahoo data for now. The data
layer is meant to be swappable — when we eventually pay for Webull
market data (or pick another provider), only this fetch changes.

Pulls 1-minute AAPL bars for the most recent trading day and prints
the tail. Success means our data layer can produce intraday OHLCV.
"""

import sys

import yfinance as yf


def main() -> int:
    ticker = yf.Ticker("AAPL")
    bars = ticker.history(period="1d", interval="1m")

    if bars.empty:
        print(
            "No bars returned. Market may be closed with no recent session, "
            "or yfinance was rate-limited. Try again or widen the period.",
            file=sys.stderr,
        )
        return 1

    print(f"Fetched {len(bars)} bars for AAPL (1m).")
    print(bars.tail())
    return 0


if __name__ == "__main__":
    sys.exit(main())
