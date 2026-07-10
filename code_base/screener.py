"""Live multi-criteria screener for the bull-flag strategy.

`scanner.py` is the thin raw feed: it returns whatever Webull's "top
gainers" list currently shows. This module is the *screener* — it applies
the full set of strategy entry criteria (see
docs/trading_strategy_baseline.md, "Stock Selection") and maintains a
live watchlist the trading algorithm can read.

Two-stage design (cheap pass first, expensive pass only on survivors):

  1. Coarse pass — `WebullScanner.get_top_gainers()`. One request returns
     the day's gainers with %change, last price, and today's volume. We
     filter %change / price / absolute-volume here (all fields are already
     in the feed, so it costs nothing extra).

  2. Fine pass — per-ticker enrichment via yfinance `.info`, which carries
     `floatShares`, `averageVolume`, and `marketCap`. From these we get the
     two criteria the gainer feed can't give us:
        - float <= max_float
        - RVOL  >= min_rvol, where RVOL = today's volume / average volume
     Float and average volume are daily-stable, so we cache them per symbol
     per day. Enrichment of first-sight symbols runs in a small thread pool
     (the calls are network-bound); subsequent refreshes are cache hits.

Swap boundary: the trading algorithm consumes `list[Candidate]` via
`LiveScreener.watchlist`. When we move to a paid data source, only the
coarse/fine fetch internals change — `Candidate` and `watchlist` stay put.

RVOL caveat: we compare today's *cumulative* volume against the *full-day*
average. Early in the session this understates RVOL (a stock that will do
10x by close looks like 2x at 10:00am). A proper intraday RVOL compares
volume-so-far to the average volume-by-this-time-of-day; deferred. For
+70% parabolic movers the cumulative number usually clears the threshold
by mid-morning anyway.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as time_of_day
from threading import Event, Lock, Thread
from typing import Optional
from zoneinfo import ZoneInfo

from scanner import Mover, WebullScanner

_ET = ZoneInfo("America/New_York")


@dataclass
class ScreenCriteria:
    """The strategy's stock-selection filters, all in one place.

    Defaults encode docs/trading_strategy_baseline.md plus the low-float /
    relative-volume momentum filters. price_min/price_max and min_volume are
    sensible starting values for low-priced pumpers — tune to taste.
    """

    min_pct_change: float = 70.0          # +70% on the day (the core rule)
    price_min: float = 1.0                 # avoid sub-$1 illiquid junk
    price_max: float = 20.0                # low-priced parabolic range
    min_volume: int = 1_000_000            # absolute liquidity floor (today)
    max_float: float = 20_000_000          # low float <= 20M shares
    min_rvol: float = 5.0                  # today's vol >= 5x average vol
    rank_type: str = "1d"                  # Webull ranking window
    # If a symbol's float / avg-vol can't be fetched, a float<=20M or
    # rvol>=5x rule can't be evaluated. Drop it rather than let it through.
    drop_on_missing_data: bool = True

    def describe(self) -> str:
        return (
            f"+{self.min_pct_change:g}% | ${self.price_min:g}-{self.price_max:g} | "
            f"vol>={self.min_volume / 1e6:g}M | float<={self.max_float / 1e6:g}M | "
            f"rvol>={self.min_rvol:g}x"
        )


@dataclass
class Candidate:
    """An enriched, fully-screened watchlist entry the algorithm consumes."""

    symbol: str
    pct_change: float
    last_price: float
    volume: int                     # today's volume (RVOL numerator)
    float_shares: Optional[float]
    avg_volume: Optional[float]     # ~3-month average daily volume
    rvol: Optional[float]           # volume / avg_volume
    market_cap: Optional[float]


@dataclass
class ScreenResult:
    """One screen pass: the survivors plus counters for the console footer."""

    candidates: list[Candidate]
    gainers_scanned: int = 0
    enriched: int = 0
    dropped: int = 0          # failed the float/RVOL criteria
    dropped_missing: int = 0  # couldn't be evaluated (enrichment data missing)


# --- per-day enrichment cache --------------------------------------------
# float + average volume don't move intraday, so fetch them once per symbol
# per trading day. Keyed by symbol -> (date, float_shares, avg_volume, mktcap).
_enrich_cache: dict[str, tuple[date, Optional[float], Optional[float], Optional[float]]] = {}
_cache_lock = Lock()


def _fetch_fundamentals(symbol: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(float_shares, avg_volume, market_cap) from yfinance, or Nones on failure."""
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info
    except Exception:
        return (None, None, None)

    def _num(key: str) -> Optional[float]:
        val = info.get(key)
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    return (_num("floatShares"), _num("averageVolume"), _num("marketCap"))


def enrich(symbol: str, today: Optional[date] = None) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Cached fundamentals lookup; one yfinance call per symbol per day."""
    today = today or datetime.now(_ET).date()
    with _cache_lock:
        cached = _enrich_cache.get(symbol)
        if cached is not None and cached[0] == today:
            return cached[1], cached[2], cached[3]

    float_shares, avg_volume, market_cap = _fetch_fundamentals(symbol)

    # A total miss is a transient failure (rate limit, outage), not a fact about
    # the symbol — caching it would blacklist the symbol until tomorrow. Leave
    # it uncached so the next refresh retries.
    if not (float_shares is None and avg_volume is None and market_cap is None):
        with _cache_lock:
            _enrich_cache[symbol] = (today, float_shares, avg_volume, market_cap)
    return float_shares, avg_volume, market_cap


def is_market_open(now: Optional[datetime] = None) -> bool:
    """US regular trading hours, Mon-Fri 9:30-16:00 ET. Ignores holidays (v1)."""
    now = now or datetime.now(_ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return time_of_day(9, 30) <= now.time() <= time_of_day(16, 0)


def screen_once(
    criteria: ScreenCriteria,
    scanner: Optional[WebullScanner] = None,
    max_results: int = 50,
) -> ScreenResult:
    """Run both passes and return the survivors. The on-demand entry point."""
    scanner = scanner or WebullScanner()
    today = datetime.now(_ET).date()

    # Coarse pass: gainers >= threshold, then cheap price/volume filters.
    movers: list[Mover] = scanner.get_top_gainers(
        min_pct_change=criteria.min_pct_change,
        max_results=max_results,
        rank_type=criteria.rank_type,
    )
    coarse: list[Mover] = [
        m for m in movers
        if criteria.price_min <= m.last_price <= criteria.price_max
        and m.volume >= criteria.min_volume
    ]

    # Fine pass: enrich survivors in parallel (each call is network-bound and
    # releases the GIL), then compute RVOL and apply the float / rvol filters.
    # After the first refresh of the day these are all cache hits.
    fundamentals: dict[str, tuple[Optional[float], Optional[float], Optional[float]]] = {}
    if coarse:
        symbols = [m.symbol for m in coarse]
        # Modest concurrency: a burst of 8 first-sight .info calls at the open
        # is exactly how we trip Yahoo rate limits.
        with ThreadPoolExecutor(max_workers=min(4, len(symbols))) as pool:
            results = pool.map(lambda s: enrich(s, today=today), symbols)
        fundamentals = dict(zip(symbols, results))

    candidates: list[Candidate] = []
    dropped = 0
    dropped_missing = 0
    for m in coarse:
        float_shares, avg_volume, market_cap = fundamentals[m.symbol]
        rvol = m.volume / avg_volume if avg_volume else None

        if float_shares is None or rvol is None:
            if criteria.drop_on_missing_data:
                dropped_missing += 1
                continue
        if float_shares is not None and float_shares > criteria.max_float:
            dropped += 1
            continue
        if rvol is not None and rvol < criteria.min_rvol:
            dropped += 1
            continue

        candidates.append(
            Candidate(
                symbol=m.symbol,
                pct_change=m.pct_change,
                last_price=m.last_price,
                volume=m.volume,
                float_shares=float_shares,
                avg_volume=avg_volume,
                rvol=rvol,
                market_cap=market_cap,
            )
        )

    # Strongest momentum first.
    candidates.sort(key=lambda c: c.pct_change, reverse=True)
    return ScreenResult(
        candidates=candidates,
        gainers_scanned=len(movers),
        enriched=len(coarse),
        dropped=dropped,
        dropped_missing=dropped_missing,
    )


def render_table(result: ScreenResult, criteria: ScreenCriteria, clear: bool = True) -> str:
    """Build the live console dashboard for one screen pass."""
    lines: list[str] = []
    now = datetime.now(_ET)
    status = "OPEN" if is_market_open(now) else "CLOSED"
    width = 72

    lines.append("=" * width)
    lines.append(f" ARCHANGEL LIVE SCREENER   {now:%Y-%m-%d %H:%M:%S} ET   [{status}]")
    lines.append(f" {criteria.describe()}")
    lines.append("-" * width)
    lines.append(
        f" {'SYMBOL':<7} {'%CHG':>8} {'PRICE':>8} {'VOLUME':>14} "
        f"{'FLOAT(M)':>9} {'RVOL':>9} {'MKTCAP':>9}"
    )
    if not result.candidates:
        lines.append(f" {'— no candidates meet criteria —':^{width - 2}}")
    for c in result.candidates:
        float_m = f"{c.float_shares / 1e6:.1f}" if c.float_shares is not None else "?"
        rvol = f"{c.rvol:.1f}x" if c.rvol is not None else "?"
        mktcap = f"{c.market_cap / 1e6:.0f}M" if c.market_cap is not None else "?"
        lines.append(
            f" {c.symbol:<7} {c.pct_change:>+7.1f}% {c.last_price:>8.2f} "
            f"{c.volume:>14,} {float_m:>9} {rvol:>9} {mktcap:>9}"
        )
    lines.append("-" * width)
    lines.append(
        f" {len(result.candidates)} candidate(s)  |  "
        f"{result.gainers_scanned} gainers scanned, "
        f"{result.enriched} priced-in, {result.dropped} dropped"
        + (f", {result.dropped_missing} missing data" if result.dropped_missing else "")
    )
    lines.append("=" * width)

    out = "\n".join(lines)
    if clear:
        out = "\033[2J\033[H" + out  # ANSI clear-screen + home for a live feel
    return out


class LiveScreener:
    """Polls on an interval and maintains a live, in-memory watchlist.

    Use it two ways:
      * standalone dashboard — `LiveScreener(...).run()` (blocking loop), or
      * embedded component  — `s = LiveScreener(...); s.start()` spawns a
        daemon thread, then the trading algorithm reads `s.watchlist` for the
        current candidates. The watchlist is swapped atomically each refresh,
        so a reader always sees a consistent snapshot.
    """

    def __init__(
        self,
        criteria: Optional[ScreenCriteria] = None,
        refresh_seconds: float = 30.0,
        respect_market_hours: bool = True,
        scanner: Optional[WebullScanner] = None,
    ) -> None:
        self.criteria = criteria or ScreenCriteria()
        self.refresh_seconds = refresh_seconds
        self.respect_market_hours = respect_market_hours
        self._scanner = scanner or WebullScanner()
        self._watchlist: list[Candidate] = []
        self._thread: Optional[Thread] = None
        self._stop = Event()  # set() interrupts the inter-refresh wait at once

    @property
    def watchlist(self) -> list[Candidate]:
        """Current candidates. Safe to read from another thread."""
        return self._watchlist

    def refresh(self) -> ScreenResult:
        """Run one screen, update the watchlist, return the result."""
        result = screen_once(self.criteria, scanner=self._scanner)
        self._watchlist = result.candidates  # atomic rebind
        return result

    def _refresh_safely(self) -> Optional[ScreenResult]:
        """refresh(), but a transient failure logs and returns None instead of
        killing a session-long loop. Both run() and start() poll through this
        so their error behavior can't drift apart. The previous watchlist is
        kept on failure — briefly-stale candidates beat an empty list."""
        try:
            return self.refresh()
        except Exception as exc:
            print(f"[screener] refresh error: {exc}", file=sys.stderr, flush=True)
            return None

    def run(self, render: bool = True) -> None:
        """Blocking poll loop until interrupted (Ctrl-C) or `stop()`."""
        self._stop.clear()
        try:
            while not self._stop.is_set():
                if self.respect_market_hours and not is_market_open():
                    print(
                        f"[{datetime.now(_ET):%H:%M:%S} ET] Market closed — "
                        f"idling {self.refresh_seconds:g}s. "
                        f"(pass --ignore-hours to screen anyway)",
                        flush=True,
                    )
                    self._stop.wait(self.refresh_seconds)
                    continue
                result = self._refresh_safely()
                if result is not None and render:
                    print(render_table(result, self.criteria), flush=True)
                self._stop.wait(self.refresh_seconds)
        except KeyboardInterrupt:
            print("\nScreener stopped.", flush=True)

    def start(self) -> None:
        """Run the poll loop in a background daemon thread (non-rendering)."""
        if self._thread and self._thread.is_alive():
            return

        def _loop() -> None:
            while not self._stop.is_set():
                if not self.respect_market_hours or is_market_open():
                    self._refresh_safely()
                self._stop.wait(self.refresh_seconds)

        self._stop.clear()
        self._thread = Thread(target=_loop, name="LiveScreener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _smoke_test() -> int:
    # 1. Enrichment path works regardless of market state (uses a liquid name).
    f_shares, avg_vol, mcap = enrich("AAPL")
    print("Enrichment sanity (AAPL):")
    print(f"  float={f_shares}, avg_vol={avg_vol}, market_cap={mcap}")
    if f_shares is None or avg_vol is None:
        print("\nSmoke test failed: yfinance enrichment returned nothing "
              "(rate-limited?).", file=sys.stderr)
        return 1

    # 2. Full strict screen against live gainers.
    strict = ScreenCriteria()
    print(f"\nStrict screen [{strict.describe()}]:")
    result = screen_once(strict)
    print(render_table(result, strict, clear=False))

    # 3. Relaxed screen so the table populates even on a quiet/closed day —
    #    proves the coarse->fine->render pipeline end to end.
    relaxed = ScreenCriteria(
        min_pct_change=10.0, price_max=1_000.0, min_volume=100_000,
        max_float=5e12, min_rvol=0.0, drop_on_missing_data=False,
    )
    print(f"\nRelaxed screen [{relaxed.describe()}]:")
    result = screen_once(relaxed, max_results=10)  # cap enrichment for a fast smoke test
    print(render_table(result, relaxed, clear=False))

    print("\nScreener pipeline ran cleanly.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: live dashboard by default; --once for a single look, --smoke to verify."""
    d = ScreenCriteria()  # source of truth for the default thresholds
    p = argparse.ArgumentParser(
        description="Archangel live stock screener — watch tickers that meet the strategy criteria.",
    )
    p.add_argument("--once", action="store_true",
                   help="Print a single screen and exit (no live loop).")
    p.add_argument("--smoke", action="store_true",
                   help="Run the verification smoke test and exit.")
    p.add_argument("--ignore-hours", action="store_true",
                   help="Run the live loop even when the US market is closed (after-hours testing).")
    p.add_argument("--refresh", type=float, default=30.0, metavar="SECONDS",
                   help="Seconds between refreshes in live mode (default: 30).")
    p.add_argument("--min-change", type=float, default=d.min_pct_change, metavar="PCT",
                   help=f"Minimum percent change on the day (default: {d.min_pct_change:g}).")
    p.add_argument("--price-min", type=float, default=d.price_min, metavar="USD",
                   help=f"Minimum price (default: {d.price_min:g}).")
    p.add_argument("--price-max", type=float, default=d.price_max, metavar="USD",
                   help=f"Maximum price (default: {d.price_max:g}).")
    p.add_argument("--min-volume", type=int, default=d.min_volume, metavar="SHARES",
                   help=f"Minimum volume today (default: {d.min_volume:,}).")
    p.add_argument("--max-float", type=float, default=d.max_float, metavar="SHARES",
                   help=f"Maximum float (default: {d.max_float:,.0f}).")
    p.add_argument("--min-rvol", type=float, default=d.min_rvol, metavar="X",
                   help=f"Minimum relative volume (default: {d.min_rvol:g}).")
    p.add_argument("--rank-type", default=d.rank_type,
                   help="Webull ranking window: 1d, 5d, 1m, 3m, 52w (default: 1d).")
    args = p.parse_args(argv)

    if args.smoke:
        return _smoke_test()

    criteria = ScreenCriteria(
        min_pct_change=args.min_change,
        price_min=args.price_min,
        price_max=args.price_max,
        min_volume=args.min_volume,
        max_float=args.max_float,
        min_rvol=args.min_rvol,
        rank_type=args.rank_type,
    )

    if args.once:
        result = screen_once(criteria)
        print(render_table(result, criteria, clear=False))
        return 0

    print(f"Starting live screener [{criteria.describe()}], refresh {args.refresh:g}s. Ctrl-C to stop.")
    LiveScreener(
        criteria=criteria,
        refresh_seconds=args.refresh,
        respect_market_hours=not args.ignore_hours,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
