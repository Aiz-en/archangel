"""Top-gainers scanner.

Provides the candidate list for the bull-flag strategy: tickers up by at
least `min_pct_change` percent intraday. The strategy targets +70% movers
(see docs/trading_strategy_baseline.md).

The scanner is a separate module from the runner because the data source
will swap when we move from paper to live trading. The current
implementation uses the *unofficial* `tedchou12/webull` package, which
scrapes the same backend that powers the Webull app — so the gainers we
get here match what the app's own screener shows exactly.

When we go live (or if Webull breaks the unofficial endpoints), the swap
point is `WebullScanner` — anyone implementing `get_top_gainers` and
returning `list[Mover]` is a drop-in replacement. The runner only depends
on the `Mover` dataclass, not on which provider produced it.

Why not the official OpenAPI: confirmed via dev docs and the official SDK
that no screener / movers endpoint is exposed, even with a paid quotes
subscription. The official SDK is reserved for order execution.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class Mover:
    symbol: str
    pct_change: float
    last_price: float
    volume: int


class WebullScanner:
    """Top-gainers via the unofficial Webull web-endpoint scraper.

    Real-time-ish: returns whatever the Webull app would currently show
    in `Markets > Top Gainers > 1 Day`. No auth required for this endpoint.
    """

    def __init__(self) -> None:
        from webull import webull

        self._wb = webull()

    def get_top_gainers(
        self,
        min_pct_change: float = 70.0,
        max_results: int = 50,
        rank_type: str = "1d",
    ) -> list[Mover]:
        """`rank_type` mirrors Webull's screener: 1d, 5d, 1m, 3m, 52w (and
        intraday: preMarket, afterMarket, 5min). For non-1d ranks, the
        threshold filters cumulative % change over that period — e.g.,
        rank_type='5d' min_pct_change=70 means "up >= 70% over 5 days,"
        which is a *proxy* for "had a +70% intraday day in there."
        """
        raw = self._wb.active_gainer_loser(
            direction="gainer", rank_type=rank_type, count=max_results
        )
        movers: list[Mover] = []
        for entry in raw.get("data", []):
            ticker = entry.get("ticker", {})
            try:
                pct = float(ticker["changeRatio"]) * 100
            except (KeyError, ValueError, TypeError):
                continue
            if pct < min_pct_change:
                continue
            try:
                movers.append(
                    Mover(
                        symbol=ticker["symbol"],
                        pct_change=pct,
                        last_price=float(ticker["close"]),
                        volume=int(ticker["volume"]),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return movers


def _smoke_test() -> int:
    scanner = WebullScanner()

    # Show top movers above a few thresholds so we can see the data live.
    for threshold in (70.0, 30.0, 10.0):
        movers = scanner.get_top_gainers(min_pct_change=threshold, max_results=50)
        print(f"\nMovers >= +{threshold}%: {len(movers)} found")
        for m in movers[:10]:
            print(
                f"  {m.symbol:<8} +{m.pct_change:>6.2f}%  "
                f"${m.last_price:>8.2f}  vol {m.volume:>12,}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
