"""Live polling runner: trade the strategy in real time during market hours.

This is the live-mode counterpart of runner.py (which walks a fixed span of
historical bars). Every poll cycle it:

  1. refreshes the screener watchlist (the full strategy selection criteria
     from screener.py — %change, price band, volume, float, RVOL),
  2. fetches fresh 1m/5m bars for watchlist symbols plus any symbol that
     still has an open position or pending order,
  3. feeds newly *closed* 1m bars through the paper engine (entry fills,
     stop-loss / take-profit exits),
  4. checks flat symbols for a bull-flag setup + 9 EMA touch and submits
     entry orders — same rules and same code path as the backtest
     (strategy.detect_bull_flag_setup / is_9_ema_touch),
  5. logs closed trades and equity snapshots to SQLite.

Execution is the LOCAL paper engine: simulated fills against real market
bars. Swapping in the Webull paper account later means routing the same
submit/close calls through webull_paper.py — the loop doesn't change.

Live-data discipline (the parts that differ from historical walking):

- yfinance's newest intraday bar is usually still forming. A bar is only
  processed once its full window has elapsed on the wall clock
  (bar_start + interval <= now), mirroring runner.py's closed-bar gating.
- Each symbol remembers the timestamp of its last processed 1m bar, so no
  bar is ever processed twice and quiet cycles are no-ops.
- On first sight of a symbol the runner *skips* that day's earlier bars —
  it only trades forward from the moment the symbol enters the watchlist.
  Pass replay_today=True (CLI: --replay-today) to process the whole
  session instead, which is how you test against a finished day.
- A data failure on one symbol logs and skips that symbol for the cycle.
  A screener failure keeps the previous watchlist. Nothing short of
  Ctrl-C stops the loop.
- Entry limit orders that sit unfilled for entry_ttl_minutes are canceled:
  in live mode an old limit at a stale 9 EMA value is no longer the setup
  we priced.
- End of day: at eod_flatten (default 15:55 ET) open positions are closed
  at the last seen price and pending orders canceled — this bot does not
  hold low-float movers overnight. Disable with --no-eod-flatten.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as time_of_day
from threading import Event
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from ema import fetch_with_ema
from paper_engine import Bar, OrderStatus, Portfolio, Side
from screener import ScreenCriteria, ScreenResult, is_market_open, screen_once
from storage import TradeLog
from strategy import detect_bull_flag_setup, is_9_ema_touch

_ET = ZoneInfo("America/New_York")

# (symbol, interval) -> DataFrame of recent bars with EMA_9/EMA_12 columns.
BarFetcher = Callable[[str, str], pd.DataFrame]


def _default_bar_fetcher(symbol: str, interval: str) -> pd.DataFrame:
    # period="5d" matches runner.py/backtest exactly: the recursive EMA
    # (adjust=False) depends on its seed, so a 1-day window would give the
    # live bot different EMA values — and different entries — than the
    # backtest until the seed decays (~2h on the 5m frame).
    return fetch_with_ema(symbol, interval=interval, period="5d", ema_windows=(9, 12))


def closed_bars(
    bars: pd.DataFrame,
    interval_minutes: int,
    now: datetime,
    finality_lag_seconds: float = 0.0,
) -> pd.DataFrame:
    """Only bars whose full window has elapsed — the forming bar is dropped.

    `finality_lag_seconds` holds bars back a little longer: yfinance is a
    delayed consolidated feed, so the just-closed minute can still mutate as
    late prints arrive. Costing ~90s of latency buys bars that stay frozen.
    """
    if bars.empty:
        return bars
    idx = bars.index
    if idx.tz is None:  # yfinance intraday is tz-aware; synthetic test data may not be
        idx = idx.tz_localize(_ET)
        bars = bars.set_axis(idx)
    cutoff = now - timedelta(minutes=interval_minutes, seconds=finality_lag_seconds)
    return bars[idx <= cutoff]


@dataclass
class CycleReport:
    """What one poll cycle did — printed as the live status line."""

    at: datetime
    watchlist: list[str] = field(default_factory=list)
    bars_processed: int = 0
    entries_submitted: int = 0
    orders_canceled: int = 0
    trades_closed: int = 0
    errors: list[str] = field(default_factory=list)
    equity: float = 0.0


class LiveRunner:
    def __init__(
        self,
        portfolio: Portfolio,
        criteria: Optional[ScreenCriteria] = None,
        trade_log: Optional[TradeLog] = None,
        refresh_seconds: float = 30.0,
        position_size_usd: float = 1_000.0,
        max_concurrent: int = 3,
        stop_pct: float = 0.05,
        tp_pct: float = 0.10,
        entry_ttl_minutes: float = 10.0,
        eod_flatten: Optional[time_of_day] = time_of_day(15, 55),
        respect_market_hours: bool = True,
        replay_today: bool = False,
        finality_lag_seconds: float = 90.0,
        bar_fetcher: Optional[BarFetcher] = None,
        screen: Optional[Callable[[ScreenCriteria], ScreenResult]] = None,
    ) -> None:
        self.portfolio = portfolio
        self.criteria = criteria or ScreenCriteria()
        self.trade_log = trade_log
        self.refresh_seconds = refresh_seconds
        self.position_size_usd = position_size_usd
        self.max_concurrent = max_concurrent
        self.stop_pct = stop_pct
        self.tp_pct = tp_pct
        self.entry_ttl_minutes = entry_ttl_minutes
        self.eod_flatten = eod_flatten
        self.respect_market_hours = respect_market_hours
        self.replay_today = replay_today
        self.finality_lag_seconds = finality_lag_seconds
        # Entries only fire on bars this fresh. A symbol can drop off the
        # watchlist for a few cycles (screener blip) and come back — its gap
        # bars are still *processed* (exits stay correct) but never *traded*:
        # entering on a minutes-old bar books a fictitious fill at a past price.
        self._entry_staleness = timedelta(
            seconds=max(180.0, 2 * refresh_seconds + finality_lag_seconds)
        )
        self._fetch = bar_fetcher or _default_bar_fetcher
        if screen is None:
            from scanner import WebullScanner

            shared_scanner = WebullScanner()  # one session across all cycles
            screen = lambda c: screen_once(c, scanner=shared_scanner)
        self._screen = screen
        self._last_processed: dict[str, pd.Timestamp] = {}
        self._last_close: dict[str, float] = {}
        self._watchlist_symbols: list[str] = []
        self._trades_logged = 0
        self._stop = Event()

    # -- one poll cycle ----------------------------------------------------

    def cycle(self, now: Optional[datetime] = None) -> CycleReport:
        # Normalize to aware-ET once; a naive `now` would make every tz-aware
        # bar comparison raise, which the per-symbol guard would then eat —
        # a runner that silently trades nothing.
        if now is None:
            now = datetime.now(_ET)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=_ET)
        else:
            now = now.astimezone(_ET)
        report = CycleReport(at=now)

        # 1. Refresh the watchlist; keep the previous one on failure.
        try:
            self._watchlist_symbols = [
                c.symbol for c in self._screen(self.criteria).candidates
            ]
        except Exception as exc:
            report.errors.append(f"screener: {exc}")
        report.watchlist = list(self._watchlist_symbols)

        # 2. Expire stale entry orders before processing new bars.
        ttl = timedelta(minutes=self.entry_ttl_minutes)
        stale = [
            o for o in self.portfolio.pending_orders
            if _as_et(o.submitted_at) + ttl <= now
        ]
        for order in stale:
            order.status = OrderStatus.CANCELED
            self.portfolio.pending_orders.remove(order)
            report.orders_canceled += 1

        # No fresh entries once we're inside the flatten window — otherwise
        # the loop buys at 15:56 only to force-close the same position at
        # 15:57, churning spread until the bell.
        allow_entries = (
            self.eod_flatten is None or now.time() < self.eod_flatten
        )

        # 3. Walk every symbol we owe attention: watchlist + open/pending.
        symbols = list(dict.fromkeys(  # ordered de-dupe
            self._watchlist_symbols
            + list(self.portfolio.positions)
            + [o.symbol for o in self.portfolio.pending_orders]
        ))
        trades_before = len(self.portfolio.closed_trades)
        for symbol in symbols:
            try:
                bars_n, entries_n = self._process_symbol(symbol, now, allow_entries)
                report.bars_processed += bars_n
                report.entries_submitted += entries_n
            except Exception as exc:
                report.errors.append(f"{symbol}: {type(exc).__name__}: {exc}")

        # 4. End-of-day flatten.
        if not allow_entries:
            report.orders_canceled += len(self.portfolio.cancel_pending())
            for symbol in list(self.portfolio.positions):
                price = self._last_close.get(
                    symbol, self.portfolio.positions[symbol].entry_price
                )
                self.portfolio.close_position_at(symbol, price, now, "eod_flatten")

        # 5. Log anything not yet persisted (guarded: a failed DB write must
        # not kill the session — the persistent counter retries next cycle).
        report.trades_closed = len(self.portfolio.closed_trades) - trades_before
        report.equity = self.portfolio.equity(self._last_close)
        try:
            self._flush_trade_log(now)
        except Exception as exc:
            report.errors.append(f"trade-log: {type(exc).__name__}: {exc}")
        return report

    def _flush_trade_log(self, now: datetime) -> None:
        """Persist closed trades past the high-water mark, plus a snapshot."""
        if self.trade_log is None:
            return
        unlogged = self.portfolio.closed_trades[self._trades_logged:]
        if not unlogged:
            return
        for trade in unlogged:
            self.trade_log.record_trade(trade)
            self._trades_logged += 1
        self.trade_log.record_equity(now, self.portfolio, marks=self._last_close)

    def _process_symbol(
        self, symbol: str, now: datetime, allow_entries: bool
    ) -> tuple[int, int]:
        """Process new closed 1m bars for one symbol.

        Returns (bars_processed, entries_submitted)."""
        last_seen = self._last_processed.get(symbol)
        # No new 1m bar can exist yet: the next bar closes at last_seen + 2min
        # (start of next window + its 1 minute) plus the finality lag. Skipping
        # the fetch here halves yfinance traffic at a 30s refresh.
        if last_seen is not None and now < (
            last_seen.to_pydatetime()
            + timedelta(minutes=2, seconds=self.finality_lag_seconds)
        ):
            return 0, 0

        bars_1m = closed_bars(self._fetch(symbol, "1m"), 1, now, self.finality_lag_seconds)
        if bars_1m.empty:
            return 0, 0

        if last_seen is None and not self.replay_today:
            # First sight: don't trade the morning we weren't watching.
            self._last_processed[symbol] = bars_1m.index[-1]
            self._last_close[symbol] = float(bars_1m["Close"].iloc[-1])
            return 0, 0
        new_bars = bars_1m if last_seen is None else bars_1m[bars_1m.index > last_seen]
        if new_bars.empty:
            return 0, 0

        # The 5m frame is only needed for entry evaluation — skip the second
        # network call when this symbol can't enter anyway.
        can_enter = (
            allow_entries
            and symbol in self._watchlist_symbols
            and symbol not in self.portfolio.positions
            and not any(o.symbol == symbol for o in self.portfolio.pending_orders)
        )
        bars_5m = (
            closed_bars(self._fetch(symbol, "5m"), 5, now, self.finality_lag_seconds)
            if can_enter else pd.DataFrame()
        )

        processed = 0
        entries = 0
        for ts, row in new_bars.iterrows():
            if can_enter and not bars_5m.empty:
                entries += self._maybe_enter(symbol, ts, row, bars_5m, now)
            self.portfolio.process_bar(
                symbol,
                Bar(
                    timestamp=ts.to_pydatetime(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                ),
            )
            self._last_processed[symbol] = ts
            self._last_close[symbol] = float(row["Close"])
            processed += 1
        return processed, entries

    def _maybe_enter(
        self, symbol: str, ts: pd.Timestamp, row: Any, bars_5m: pd.DataFrame, now: datetime
    ) -> int:
        """Same entry gate as runner.py, evaluated on one live 1m bar.

        Returns 1 if an entry order was submitted, else 0."""
        # Never enter on a bar minutes in the past (gap bars after a watchlist
        # flicker or an error streak) — that fill price no longer exists.
        # replay_today is the explicit testing exemption.
        if not self.replay_today and ts.to_pydatetime() < now - self._entry_staleness:
            return 0
        no_position = symbol not in self.portfolio.positions
        no_pending = not any(o.symbol == symbol for o in self.portfolio.pending_orders)
        # Pending entries count toward the cap: several symbols can trigger in
        # the same cycle, and unfilled orders may all fill later.
        room = (
            len(self.portfolio.positions) + len(self.portfolio.pending_orders)
            < self.max_concurrent
        )
        if not (no_position and no_pending and room):
            return 0

        completed_5m = bars_5m[bars_5m.index <= ts - pd.Timedelta(minutes=5)]
        if len(completed_5m) < 5:
            return 0
        ema_9 = row.get("EMA_9")
        if ema_9 is None or pd.isna(ema_9):
            return 0
        if detect_bull_flag_setup(completed_5m) is None:
            return 0
        if not is_9_ema_touch(
            bar_low=float(row["Low"]), bar_high=float(row["High"]), ema_9=float(ema_9)
        ):
            return 0

        entry_price = float(ema_9)
        self.portfolio.submit_order(
            symbol=symbol,
            side=Side.BUY,
            quantity=self.position_size_usd / entry_price,
            limit_price=entry_price,
            submitted_at=ts.to_pydatetime(),
            stop_loss=entry_price * (1 - self.stop_pct),
            take_profit=entry_price * (1 + self.tp_pct),
        )
        return 1

    # -- the loop ----------------------------------------------------------

    def run(self) -> None:
        """Blocking poll loop until Ctrl-C or stop()."""
        self._stop.clear()
        print(
            f"Live runner up [{self.criteria.describe()}] — "
            f"${self.position_size_usd:g}/position, max {self.max_concurrent}, "
            f"stop -{self.stop_pct:.0%} / TP +{self.tp_pct:.0%}, "
            f"refresh {self.refresh_seconds:g}s. Ctrl-C to stop.",
            flush=True,
        )
        try:
            while not self._stop.is_set():
                if self.respect_market_hours and not is_market_open():
                    print(
                        f"[{datetime.now(_ET):%H:%M:%S} ET] Market closed — idling. "
                        f"(--ignore-hours to run anyway)",
                        flush=True,
                    )
                    self._stop.wait(self.refresh_seconds)
                    continue
                try:
                    report = self.cycle()
                    self._print_report(report)
                except Exception as exc:
                    # Same lesson as screener._refresh_safely: nothing short of
                    # Ctrl-C may kill a session-long loop.
                    print(f"[runner] cycle error: {type(exc).__name__}: {exc}",
                          file=sys.stderr, flush=True)
                self._stop.wait(self.refresh_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        # Ctrl-C can land mid-cycle, after a trade closed but before step 5
        # persisted it — flush the high-water mark so the DB never loses one.
        try:
            self._flush_trade_log(datetime.now(_ET))
        except Exception as exc:
            print(f"[runner] final trade-log flush failed: {exc}",
                  file=sys.stderr, flush=True)
        open_syms = list(self.portfolio.positions)
        print(
            f"\nLive runner stopped. Equity ${self.portfolio.equity(self._last_close):.2f}, "
            f"{len(self.portfolio.closed_trades)} trade(s) this run"
            + (f", still open (NOT flattened): {', '.join(open_syms)}" if open_syms else ""),
            flush=True,
        )

    def _print_report(self, r: CycleReport) -> None:
        positions = ", ".join(
            f"{s}@{p.entry_price:.2f}" for s, p in self.portfolio.positions.items()
        ) or "flat"
        line = (
            f"[{r.at:%H:%M:%S} ET] watch={r.watchlist or '—'} pos=[{positions}] "
            f"bars={r.bars_processed} entries={r.entries_submitted or 0} "
            f"closed={r.trades_closed} eq=${r.equity:.2f}"
        )
        if r.errors:
            line += f"  ERR: {'; '.join(r.errors)}"
        print(line, flush=True)


def _as_et(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=_ET)


# -- offline smoke test ----------------------------------------------------


def _smoke_test() -> int:
    """Deterministic, no-network run: fake screener + fake bars drive a full
    entry -> fill -> take-profit -> EOD-flatten lifecycle."""
    from ema import add_ema

    failures = 0
    day = datetime(2026, 7, 9, tzinfo=_ET)

    def bars_1m_raw() -> pd.DataFrame:
        # 09:30-09:44: runway at ~95 while the 5m flag is still forming (no
        # setup visible -> the trivial runway EMA hugs can't enter). The 5m
        # flag completes at 09:45; the 1m pole bars keep their lows ABOVE the
        # rising 9 EMA (no touch), then the 09:48 pullback bar wicks through
        # it (~96.6) — that is the designed entry. It fills same-bar (touch
        # range contains the EMA by definition — backtest parity), rides
        # through 09:49, and the 09:50 bar crosses the +10% TP (~106.3).
        rows = [(95.0, 95.3, 94.7, 95.2)] * 15               # runway 9:30-9:44
        rows += [(95.2, 96.6, 95.6, 96.5), (96.5, 98.1, 96.4, 98.0),
                 (98.0, 99.6, 97.9, 99.5)]                   # 9:45-9:47 pole, no touch
        rows += [(99.5, 99.6, 96.2, 96.4)]                   # 9:48 pullback: wick to EMA
        rows += [(96.4, 105.0, 96.3, 104.5)]                 # 9:49 rip, TP not yet
        rows += [(104.5, 112.5, 104.0, 112.0)]               # 9:50 crosses TP
        idx = pd.date_range(day.replace(hour=9, minute=30), periods=len(rows),
                            freq="1min", tz=_ET)
        return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])

    def bars_5m_raw() -> pd.DataFrame:
        # Flag completes at 09:45 (pullback bar 09:40 closes then), so 1m bars
        # before 09:45 see no setup. Pole/pullback lows stay above the settling
        # 12 EMA (~95.4-96.9) — the strategy requires every bar above the EMA.
        rows = [(95.0, 95.3, 94.7, 95.2)] * 13               # runway 8:15-9:15
        rows += [(95.2, 96.6, 95.6, 96.5), (96.5, 98.1, 96.4, 98.0),
                 (98.0, 99.6, 97.9, 99.5)]                   # 9:20-9:30 pole (3 green)
        rows += [(99.5, 99.6, 98.4, 98.6), (98.6, 98.9, 97.9, 98.1)]  # 9:35-9:40 (2 red)
        idx = pd.date_range(day.replace(hour=8, minute=15), periods=len(rows),
                            freq="5min", tz=_ET)
        return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])

    fake_bars = {"1m": add_ema(bars_1m_raw(), (9, 12)), "5m": add_ema(bars_5m_raw(), (9, 12))}

    def fetcher(symbol: str, interval: str) -> pd.DataFrame:
        return fake_bars[interval]

    fake_screen = lambda criteria: ScreenResult(candidates=[
        type("C", (), {"symbol": "FAKE"})()
    ])

    pf = Portfolio(cash=5_000.0)
    runner = LiveRunner(
        portfolio=pf, bar_fetcher=fetcher, screen=fake_screen,
        replay_today=True, respect_market_hours=False, eod_flatten=None,
    )

    now = day.replace(hour=10, minute=0)
    report = runner.cycle(now=now)
    if (
        pf.closed_trades
        and pf.closed_trades[0].exit_reason == "take_profit"
        and 96.0 <= pf.closed_trades[0].entry_price <= 97.5  # the 9:48 pullback EMA,
    ):                                                       # NOT a runway-bar touch
        t = pf.closed_trades[0]
        print(f"PASS entry+TP: {t.symbol} {t.entry_price:.2f} -> {t.exit_price:.2f} "
              f"(+${t.pnl:.2f}), {report.bars_processed} bars")
    else:
        print(f"FAIL: expected a take-profit close entered at the ~96.6 pullback touch, "
              f"got {pf.closed_trades} (pending={pf.pending_orders}, "
              f"positions={pf.positions})", file=sys.stderr)
        failures += 1

    # Idempotency: a second cycle with no new bars must do nothing.
    report2 = runner.cycle(now=now)
    if report2.bars_processed == 0 and len(pf.closed_trades) == 1:
        print("PASS idempotent: second cycle processed 0 bars")
    else:
        print(f"FAIL: second cycle reprocessed bars ({report2.bars_processed})",
              file=sys.stderr)
        failures += 1

    # First-sight skip: a fresh runner without replay must not trade history.
    pf2 = Portfolio(cash=5_000.0)
    runner2 = LiveRunner(portfolio=pf2, bar_fetcher=fetcher, screen=fake_screen,
                         respect_market_hours=False, eod_flatten=None)
    r = runner2.cycle(now=now)
    if r.bars_processed == 0 and not pf2.closed_trades and not pf2.pending_orders:
        print("PASS first-sight: history skipped when replay_today=False")
    else:
        print("FAIL: first-sight cycle traded history", file=sys.stderr)
        failures += 1

    # EOD flatten: open a position mid-day (unreachable TP keeps it open),
    # then a later cycle inside the flatten window must force-close it —
    # and must NOT submit fresh entries (allow_entries gate).
    pf3 = Portfolio(cash=5_000.0)
    runner3 = LiveRunner(portfolio=pf3, bar_fetcher=fetcher, screen=fake_screen,
                         replay_today=True, respect_market_hours=False,
                         tp_pct=10.0,  # unreachable TP so the position stays open
                         eod_flatten=time_of_day(15, 55))
    runner3.cycle(now=day.replace(hour=10, minute=0))
    opened = len(pf3.positions) == 1
    late = runner3.cycle(now=day.replace(hour=15, minute=56))
    if (opened and not pf3.positions and late.entries_submitted == 0
            and pf3.closed_trades and pf3.closed_trades[-1].exit_reason == "eod_flatten"):
        print("PASS eod-flatten: position force-closed after 15:55, no new entries")
    elif not opened:
        print("FAIL: eod case never opened a position", file=sys.stderr)
        failures += 1
    else:
        print(f"FAIL: expected eod_flatten close + entry lockout, got "
              f"{pf3.closed_trades} entries={late.entries_submitted}", file=sys.stderr)
        failures += 1

    if failures:
        print(f"\n{failures} failure(s)", file=sys.stderr)
        return 1
    print("\nAll live-runner cases passed.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    d = ScreenCriteria()
    p = argparse.ArgumentParser(
        description="Archangel live runner — trade the strategy in real time (paper fills).",
    )
    p.add_argument("--smoke", action="store_true", help="Run the offline smoke test and exit.")
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    p.add_argument("--ignore-hours", action="store_true",
                   help="Run even when the US market is closed (testing).")
    p.add_argument("--replay-today", action="store_true",
                   help="On first sight of a symbol, process today's whole session "
                        "instead of only trading forward (testing against a finished day).")
    p.add_argument("--refresh", type=float, default=30.0, metavar="SECONDS",
                   help="Seconds between cycles (default: 30).")
    p.add_argument("--cash", type=float, default=5_000.0, metavar="USD",
                   help="Starting paper cash (default: 5000).")
    p.add_argument("--db", default="archangel_live.db", metavar="PATH",
                   help="SQLite trade log (default: archangel_live.db).")
    p.add_argument("--min-change", type=float, default=d.min_pct_change, metavar="PCT",
                   help=f"Screener: min %% change (default: {d.min_pct_change:g}).")
    p.add_argument("--min-rvol", type=float, default=d.min_rvol, metavar="X",
                   help=f"Screener: min relative volume (default: {d.min_rvol:g}).")
    p.add_argument("--max-float", type=float, default=d.max_float, metavar="SHARES",
                   help=f"Screener: max float (default: {d.max_float:,.0f}).")
    p.add_argument("--no-eod-flatten", action="store_true",
                   help="Do not force-close positions at 15:55 ET.")
    args = p.parse_args(argv)

    if args.smoke:
        return _smoke_test()

    if (args.replay_today or args.ignore_hours) and args.db == "archangel_live.db":
        print(
            "NOTE: test-mode flags with the default DB — replayed/off-hours trades "
            "will mix into archangel_live.db. Pass --db <path> to keep tests separate.",
            file=sys.stderr, flush=True,
        )

    criteria = ScreenCriteria(
        min_pct_change=args.min_change, min_rvol=args.min_rvol, max_float=args.max_float,
    )
    runner = LiveRunner(
        portfolio=Portfolio(cash=args.cash),
        criteria=criteria,
        trade_log=TradeLog(db_path=args.db),
        refresh_seconds=args.refresh,
        respect_market_hours=not args.ignore_hours,
        replay_today=args.replay_today,
        eod_flatten=None if args.no_eod_flatten else time_of_day(15, 55),
    )
    if args.once:
        runner._print_report(runner.cycle())
        return 0
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
