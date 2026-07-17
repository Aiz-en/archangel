"""Strategy runner: walks bars and ties detection to the paper engine.

The 5m timeframe is used for setup detection; the 1m timeframe is used
for entry timing. We walk 1m bars in chronological order and, for each
bar, only consider 5m bars that have *closed* before this 1m bar starts —
that's how a live bot would see the data.

Entry rule: when no position or pending order exists for the symbol and
the portfolio has room (< max_concurrent positions), check for a
bull-flag setup on the closed 5m bars. If one exists and this 1m bar's
wick crosses the 1m 9 EMA, submit a limit order at the 9 EMA value with
a -5% stop and +10% take-profit.

Detection logic is unit-tested in strategy.py; this module is the glue.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import pandas as pd

from ema import fetch_with_ema
from paper_engine import Bar, Portfolio
from storage import TradeLog
from strategy import detect_setup, is_9_ema_touch


def run_strategy(
    symbol: str,
    bars_5m: pd.DataFrame,
    bars_1m: pd.DataFrame,
    portfolio: Portfolio,
    position_size_usd: float = 1_000.0,
    max_concurrent: int = 3,
    stop_pct: float = 0.05,
    tp_pct: float = 0.10,
    entry_mode: str = "strict",
    trade_log: Optional[TradeLog] = None,
) -> dict[str, Any]:
    if bars_5m.empty or bars_1m.empty:
        return {"symbol": symbol, "trades": 0, "error": "no_data"}

    five_min = pd.Timedelta(minutes=5)
    closed_count_before_bar = len(portfolio.closed_trades)

    for ts, row in bars_1m.iterrows():
        # 5m bars whose close (index + 5min) has already happened by `ts`.
        completed_5m = bars_5m[bars_5m.index <= ts - five_min]

        no_position = symbol not in portfolio.positions
        no_pending = not any(o.symbol == symbol for o in portfolio.pending_orders)
        room = len(portfolio.positions) < max_concurrent

        if no_position and no_pending and room and len(completed_5m) >= 5:
            ema_9 = row.get("EMA_9")
            if ema_9 is not None and pd.notna(ema_9):
                setup = detect_setup(completed_5m, mode=entry_mode)
                if setup is not None and is_9_ema_touch(
                    bar_low=float(row["Low"]),
                    bar_high=float(row["High"]),
                    ema_9=float(ema_9),
                ):
                    # Market entry on trigger: fill at the trigger bar's
                    # close plus slippage, as many WHOLE shares as the
                    # budget allows. Bracket off the actual fill.
                    entry_price = float(row["Close"])
                    quantity = int(position_size_usd // entry_price)
                    if quantity >= 1:
                        order = portfolio.submit_market_buy(
                            symbol=symbol,
                            quantity=quantity,
                            price=entry_price,
                            submitted_at=ts.to_pydatetime(),
                        )
                        if order.fill_price is not None:
                            position = portfolio.positions[symbol]
                            position.stop_loss = order.fill_price * (1 - stop_pct)
                            position.take_profit = order.fill_price * (1 + tp_pct)

        portfolio.process_bar(
            symbol,
            Bar(
                timestamp=ts.to_pydatetime(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
            ),
        )

        if trade_log is not None and len(portfolio.closed_trades) > closed_count_before_bar:
            for new_trade in portfolio.closed_trades[closed_count_before_bar:]:
                trade_log.record_trade(new_trade)
            trade_log.record_equity(
                ts.to_pydatetime(), portfolio, marks={symbol: float(row["Close"])}
            )
            closed_count_before_bar = len(portfolio.closed_trades)

    last_price = float(bars_1m["Close"].iloc[-1])
    if trade_log is not None:
        trade_log.record_equity(
            bars_1m.index[-1].to_pydatetime(), portfolio, marks={symbol: last_price}
        )
    return {
        "symbol": symbol,
        "bars_processed": len(bars_1m),
        "trades": len(portfolio.closed_trades),
        "wins": sum(1 for t in portfolio.closed_trades if t.pnl > 0),
        "losses": sum(1 for t in portfolio.closed_trades if t.pnl < 0),
        "total_pnl": sum(t.pnl for t in portfolio.closed_trades),
        "final_cash": portfolio.cash,
        "open_positions": len(portfolio.positions),
        "final_equity": portfolio.equity({symbol: last_price}),
    }


def fetch_and_run(
    symbol: str,
    portfolio: Portfolio,
    period: str = "5d",
    **kwargs: Any,
) -> dict[str, Any]:
    bars_5m = fetch_with_ema(symbol, interval="5m", period=period, ema_windows=(9, 12))
    bars_1m = fetch_with_ema(symbol, interval="1m", period=period, ema_windows=(9, 12))
    return run_strategy(symbol, bars_5m, bars_1m, portfolio, **kwargs)


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"Symbol:          {summary['symbol']}")
    if "error" in summary:
        print(f"Error:           {summary['error']}")
        return
    print(f"Bars processed:  {summary['bars_processed']}")
    print(f"Trades:          {summary['trades']} ({summary['wins']}W / {summary['losses']}L)")
    print(f"Total PnL:       ${summary['total_pnl']:+.2f}")
    print(f"Final cash:      ${summary['final_cash']:.2f}")
    print(f"Open positions:  {summary['open_positions']}")
    print(f"Final equity:    ${summary['final_equity']:.2f}")


def _smoke_test() -> int:
    pf = Portfolio(cash=5_000.0)
    log = TradeLog(db_path="archangel.db")
    trades_before = log.trade_count()
    snaps_before = log.snapshot_count()

    summary = fetch_and_run("AAPL", pf, period="5d", trade_log=log)
    _print_summary(summary)
    if "error" in summary:
        print("\nSmoke test failed: data fetch error", file=sys.stderr)
        return 1

    print(f"\nDB: {log.trade_count() - trades_before} new trade(s), "
          f"{log.snapshot_count() - snaps_before} new snapshot(s) recorded.")
    print("Runner executed cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
