"""Backtest harness: run the strategy across multiple symbols and report stats.

Candidates come from the SAME full screen the live runner trades
(screener.screen_once: %change, $1-20 price band, >=1M volume, float
<=20M, RVOL >=5x) — not just the raw %change feed. If the backtest
selected symbols the live bot would never trade, its stats couldn't
validate live behavior. Each sweep then runs the strategy across the
configured period (default 5d, the yfinance 1m-bar limit) and reports
aggregate stats from the SQLite trade log.

Important caveat — sequential vs time-interleaved:
This harness walks symbols *one after another* with a shared portfolio.
The max-concurrent-positions rule is therefore enforced across the
whole list (correct), but a position opened on the FIRST symbol holds
its slot through that symbol's entire bar history before the SECOND
symbol gets a chance to enter. A true time-interleaved walker would be
more realistic; punted to a later iteration. For a first read on
"does this strategy print money on +70% movers?", the sequential
version is honest enough.

Also note: yfinance's 1m bars only go back ~7 days, so we can only
backtest the current candidates over recent history. A historical
backtest across e.g. last quarter's +70% days needs a different data
source for both the candidate list and the price bars.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from paper_engine import Portfolio
from runner import fetch_and_run
from screener import ScreenCriteria, screen_once
from storage import TradeLog


def backtest(
    symbols: list[str],
    portfolio: Portfolio,
    period: str = "5d",
    trade_log: Optional[TradeLog] = None,
) -> dict[str, Any]:
    per_symbol: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        # Snapshot trade count so we can compute the delta this symbol produced.
        # The runner's own return values are cumulative across the shared
        # portfolio, which is misleading when reading per-symbol results.
        trades_before = len(portfolio.closed_trades)
        try:
            full = fetch_and_run(sym, portfolio, period=period, trade_log=trade_log)
        except Exception as exc:
            per_symbol[sym] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        new_trades = portfolio.closed_trades[trades_before:]
        per_symbol[sym] = {
            "bars_processed": full.get("bars_processed", 0),
            "trades": len(new_trades),
            "wins": sum(1 for t in new_trades if t.pnl > 0),
            "losses": sum(1 for t in new_trades if t.pnl < 0),
            "pnl": sum(t.pnl for t in new_trades),
            "error": full.get("error"),
        }
    return {"per_symbol": per_symbol}


def summarize_history(trade_log: TradeLog, starting_equity: float) -> dict[str, Any]:
    """Compute aggregate stats from the persisted trade and snapshot tables."""
    with sqlite3.connect(trade_log.db_path) as conn:
        rows = conn.execute(
            "SELECT pnl, exit_reason FROM closed_trades"
        ).fetchall()
        equity_curve = [
            r[0] for r in conn.execute(
                "SELECT total_equity FROM equity_snapshots ORDER BY id"
            ).fetchall()
        ]

    if not rows:
        return {"trades": 0, "note": "no trades on record"}

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(pnls)
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # Max drawdown: largest peak-to-trough drop in the equity curve.
    max_dd_pct = 0.0
    if equity_curve:
        peak = equity_curve[0]
        for v in equity_curve:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd_pct:
                max_dd_pct = dd

    final_eq = equity_curve[-1] if equity_curve else starting_equity
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate * 100,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "total_pnl": sum(pnls),
        "max_drawdown_pct": max_dd_pct * 100,
        "final_equity": final_eq,
        "total_return_pct": (final_eq - starting_equity) / starting_equity * 100,
    }


def _run_one_sweep(label: str, rank_type: str, threshold: float, max_results: int) -> None:
    starting_equity = 5_000.0
    pf = Portfolio(cash=starting_equity)
    db_path = Path(f"archangel_backtest_{rank_type}.db")
    db_path.unlink(missing_ok=True)
    log = TradeLog(db_path=db_path)

    print(f"\n========== Sweep: {label} (rank_type={rank_type}, >= +{threshold}%) ==========")
    # Full live screen, not just the %change feed — see module docstring.
    if rank_type == "1d":
        criteria = ScreenCriteria(min_pct_change=threshold, rank_type=rank_type)
    else:
        # Volume and RVOL are anchored to TODAY's session. A stock that pumped
        # three days ago and is quiet today fails them — yet its pump-day bars
        # are exactly what this sweep walks. Keep the day-stable filters
        # (price band, float); drop the today-anchored ones. Faithful
        # as-of-pump-day screening needs historical volume data
        # (future find_historical_movers).
        criteria = ScreenCriteria(
            min_pct_change=threshold, rank_type=rank_type,
            min_volume=0, min_rvol=0.0,
        )
    result = screen_once(criteria, max_results=max_results)
    coarse_dropped = result.gainers_scanned - result.enriched
    print(
        f"{result.gainers_scanned} gainers above +{threshold:g}% -> "
        f"{len(result.candidates)} passed the full screen "
        f"({coarse_dropped} price/volume-filtered, {result.dropped} float/RVOL-filtered, "
        f"{result.dropped_missing} missing data)"
    )
    if not result.candidates:
        print(f"No candidates passed the full screen on {rank_type} ranking.")
        return

    symbols = [c.symbol for c in result.candidates]
    print(f"Backtesting {len(symbols)} symbols: {symbols}\n")

    result = backtest(symbols, pf, period="5d", trade_log=log)
    for sym, summary in result["per_symbol"].items():
        if summary.get("error"):
            print(f"  {sym}: ERROR — {summary['error']}")
            continue
        print(
            f"  {sym}: bars={summary['bars_processed']}, "
            f"trades={summary['trades']} "
            f"({summary['wins']}W/{summary['losses']}L), "
            f"PnL ${summary['pnl']:+.2f}"
        )

    print(f"\n--- Aggregate ({label}) ---")
    stats = summarize_history(log, starting_equity=starting_equity)
    if "note" in stats:
        print(stats["note"])
        return
    print(f"Trades:           {stats['trades']} ({stats['wins']}W / {stats['losses']}L)")
    print(f"Win rate:         {stats['win_rate_pct']:.1f}%")
    print(f"Avg win:          ${stats['avg_win']:+.2f}")
    print(f"Avg loss:         ${stats['avg_loss']:+.2f}")
    print(f"Expectancy/trade: ${stats['expectancy']:+.2f}")
    print(f"Total PnL:        ${stats['total_pnl']:+.2f}")
    print(f"Max drawdown:     {stats['max_drawdown_pct']:.2f}%")
    print(f"Final equity:     ${stats['final_equity']:.2f}")
    print(f"Total return:     {stats['total_return_pct']:+.2f}%")


def _offline_test() -> int:
    """No-network check of backtest() accounting and summarize_history math."""
    import tempfile
    from datetime import datetime, timedelta

    from paper_engine import ClosedTrade

    failures = 0
    t0 = datetime(2026, 7, 6, 10, 0)

    def fake_fetch_and_run(sym, portfolio, period="5d", trade_log=None, **kw):
        if sym == "ERR":
            raise RuntimeError("boom")
        pnl = 100.0 if sym == "WIN" else -50.0
        trade = ClosedTrade(
            symbol=sym, quantity=10, entry_price=10.0, exit_price=10.0 + pnl / 10,
            entry_time=t0, exit_time=t0 + timedelta(minutes=30),
            pnl=pnl, exit_reason="take_profit" if pnl > 0 else "stop_loss",
        )
        portfolio.closed_trades.append(trade)
        portfolio.cash += pnl
        if trade_log is not None:
            trade_log.record_trade(trade)
            trade_log.record_equity(trade.exit_time, portfolio, marks={})
        return {"bars_processed": 10}

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    original = globals()["fetch_and_run"]
    globals()["fetch_and_run"] = fake_fetch_and_run
    try:
        pf = Portfolio(cash=5_000.0)
        log = TradeLog(db_path=db_path)
        result = backtest(["WIN", "ERR", "LOSE"], pf, trade_log=log)
        per = result["per_symbol"]
        ok = (
            per["WIN"]["trades"] == 1 and per["WIN"]["wins"] == 1
            and per["WIN"]["pnl"] == 100.0
            and "error" in per["ERR"] and "boom" in per["ERR"]["error"]
            and per["LOSE"]["losses"] == 1 and per["LOSE"]["pnl"] == -50.0
        )
        print(f"{'PASS' if ok else 'FAIL'} offline backtest accounting: {per}")
        failures += 0 if ok else 1

        stats = summarize_history(log, starting_equity=5_000.0)
        ok = (
            stats["trades"] == 2 and stats["wins"] == 1
            and abs(stats["win_rate_pct"] - 50.0) < 1e-9
            and abs(stats["expectancy"] - 25.0) < 1e-9
            and abs(stats["total_pnl"] - 50.0) < 1e-9
            and abs(stats["final_equity"] - 5_050.0) < 1e-9
            # equity peaked at 5100 after WIN, troughed at 5050 after LOSE
            and abs(stats["max_drawdown_pct"] - (50.0 / 5_100.0 * 100)) < 1e-6
        )
        print(f"{'PASS' if ok else 'FAIL'} offline summarize_history: "
              f"win_rate={stats['win_rate_pct']:.0f}% expectancy=${stats['expectancy']:.2f} "
              f"dd={stats['max_drawdown_pct']:.2f}%")
        failures += 0 if ok else 1
    finally:
        globals()["fetch_and_run"] = original
        Path(db_path).unlink(missing_ok=True)
    return failures


def _smoke_test() -> int:
    offline_failures = _offline_test()
    if offline_failures:
        print(f"\n{offline_failures} offline failure(s)", file=sys.stderr)
        return 1
    print()

    # Sweep 1: today's parabolic movers (the strategy's design target).
    _run_one_sweep("today's +70% intraday movers", rank_type="1d", threshold=70.0, max_results=20)
    # Sweep 2: stocks up >= 100% over the past 5 days. Most of these will
    # have had at least one big single-day move in the window — a proxy
    # for "had a +70% day recently we can backtest with 1m bars."
    _run_one_sweep("recent 5-day +100% movers", rank_type="5d", threshold=100.0, max_results=30)
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
