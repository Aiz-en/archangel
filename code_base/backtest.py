"""Backtest harness: run the strategy across multiple symbols and report stats.

Pulls today's +70% movers from the scanner, runs the strategy on each
across the configured period (default 5d, the yfinance 1m-bar limit),
and reports aggregate stats from the SQLite trade log.

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
from scanner import WebullScanner
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
    movers = WebullScanner().get_top_gainers(
        min_pct_change=threshold, max_results=max_results, rank_type=rank_type
    )
    if not movers:
        print(f"No movers found above +{threshold}% on {rank_type} ranking.")
        return

    symbols = [m.symbol for m in movers]
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


def _smoke_test() -> int:
    # Sweep 1: today's parabolic movers (the strategy's design target).
    _run_one_sweep("today's +70% intraday movers", rank_type="1d", threshold=70.0, max_results=20)
    # Sweep 2: stocks up >= 100% over the past 5 days. Most of these will
    # have had at least one big single-day move in the window — a proxy
    # for "had a +70% day recently we can backtest with 1m bars."
    _run_one_sweep("recent 5-day +100% movers", rank_type="5d", threshold=100.0, max_results=30)
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
