"""SQLite persistence for trade history and equity snapshots.

Default DB lives at `archangel.db` at the project root (gitignored via
`*.db`). Runs accumulate into the same file so a real history builds up
over time.

Schema is intentionally minimal — closed trades and equity snapshots
only. Pending/rejected orders aren't logged because the closed trade is
the post-mortem unit; we can add more tables later if analysis needs them.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from paper_engine import ClosedTrade, Portfolio


SCHEMA = """
CREATE TABLE IF NOT EXISTS closed_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    quantity     REAL    NOT NULL,
    entry_price  REAL    NOT NULL,
    exit_price   REAL    NOT NULL,
    entry_time   TEXT    NOT NULL,
    exit_time    TEXT    NOT NULL,
    pnl          REAL    NOT NULL,
    exit_reason  TEXT    NOT NULL,
    recorded_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,
    cash                  REAL    NOT NULL,
    total_equity          REAL    NOT NULL,
    open_positions_count  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON closed_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time  ON closed_trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts      ON equity_snapshots(timestamp);
"""


class TradeLog:
    def __init__(self, db_path: str | Path = "archangel.db") -> None:
        self.db_path = str(db_path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_trade(self, trade: ClosedTrade) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO closed_trades
                (symbol, quantity, entry_price, exit_price,
                 entry_time, exit_time, pnl, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.symbol,
                    trade.quantity,
                    trade.entry_price,
                    trade.exit_price,
                    trade.entry_time.isoformat(),
                    trade.exit_time.isoformat(),
                    trade.pnl,
                    trade.exit_reason,
                ),
            )

    def record_equity(
        self,
        timestamp: datetime,
        portfolio: Portfolio,
        marks: dict[str, float],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO equity_snapshots
                (timestamp, cash, total_equity, open_positions_count)
                VALUES (?, ?, ?, ?)""",
                (
                    timestamp.isoformat(),
                    portfolio.cash,
                    portfolio.equity(marks),
                    len(portfolio.positions),
                ),
            )

    def trade_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]

    def snapshot_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]

    def recent_trades(self, limit: int = 10) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT symbol, entry_price, exit_price, pnl, exit_reason, exit_time
                FROM closed_trades ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()


def _smoke_test() -> int:
    failures = 0
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        log = TradeLog(db_path=db_path)

        if log.trade_count() != 0 or log.snapshot_count() != 0:
            print("FAIL: fresh DB not empty", file=sys.stderr)
            failures += 1

        t0 = datetime(2026, 5, 1, 9, 30)
        log.record_trade(
            ClosedTrade(
                symbol="AIOS",
                quantity=10,
                entry_price=20.0,
                exit_price=22.0,
                entry_time=t0,
                exit_time=t0 + timedelta(minutes=5),
                pnl=20.0,
                exit_reason="take_profit",
            )
        )
        log.record_trade(
            ClosedTrade(
                symbol="CUE",
                quantity=20,
                entry_price=30.0,
                exit_price=28.5,
                entry_time=t0,
                exit_time=t0 + timedelta(minutes=10),
                pnl=-30.0,
                exit_reason="stop_loss",
            )
        )

        pf = Portfolio(cash=4_990.0)
        log.record_equity(t0 + timedelta(minutes=10), pf, marks={})

        if log.trade_count() != 2:
            print(f"FAIL: expected 2 trades, got {log.trade_count()}", file=sys.stderr)
            failures += 1
        if log.snapshot_count() != 1:
            print(f"FAIL: expected 1 snapshot, got {log.snapshot_count()}", file=sys.stderr)
            failures += 1

        rows = log.recent_trades()
        if len(rows) != 2:
            print(f"FAIL: recent_trades returned {len(rows)}", file=sys.stderr)
            failures += 1
        else:
            print("Recent trades (most recent first):")
            for sym, ep, xp, pnl, reason, ts in rows:
                print(f"  {sym:<6} ${ep:.2f} -> ${xp:.2f}  PnL ${pnl:+.2f}  {reason}  @ {ts}")

        # Reopen the DB to confirm state persists across connections.
        log2 = TradeLog(db_path=db_path)
        if log2.trade_count() != 2:
            print("FAIL: state did not persist", file=sys.stderr)
            failures += 1
        else:
            print("\nPersistence across reopen: OK")

    finally:
        Path(db_path).unlink(missing_ok=True)

    if failures:
        print(f"\n{failures} failure(s)", file=sys.stderr)
        return 1
    print("All cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
