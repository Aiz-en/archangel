"""SQLite persistence for trade history and equity snapshots.

Default DB lives at `archangel.db` at the project root (gitignored via
`*.db`). Runs accumulate into the same file so a real history builds up
over time.

Schema is intentionally minimal — closed trades, equity snapshots, and a
small live-runner state snapshot (open positions + cash) so a crashed or
restarted runner can resume the same day's session instead of forgetting
its positions. Pending/rejected orders aren't logged because the closed
trade is the post-mortem unit.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from paper_engine import ClosedTrade, Portfolio, Position


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

CREATE TABLE IF NOT EXISTS open_positions (
    symbol       TEXT    PRIMARY KEY,
    quantity     REAL    NOT NULL,
    entry_price  REAL    NOT NULL,
    entry_time   TEXT    NOT NULL,
    stop_loss    REAL,
    take_profit  REAL,
    saved_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS runner_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON closed_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time  ON closed_trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts      ON equity_snapshots(timestamp);
"""


class TradeLog:
    def __init__(
        self, db_path: str | Path = "archangel.db", exclusive: bool = False
    ) -> None:
        """`exclusive=True` takes a process-lifetime advisory lock on
        `<db>.lock` and refuses to start if another process holds it — the
        enforcement behind the one-runner-per-DB rule. Two live runners on the
        same DB would alternately clobber each other's position snapshots and
        double-log fills. Short-lived readers (backtest, analysis) don't need
        it."""
        self.db_path = str(db_path)
        self._lock_file = None
        if exclusive:
            import fcntl

            lock_path = self.db_path + ".lock"
            self._lock_file = open(lock_path, "w")
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._lock_file.close()
                self._lock_file = None
                raise RuntimeError(
                    f"{lock_path} is held by another process — one runner per "
                    f"DB. Is a live runner already using {self.db_path}?"
                )
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

    @staticmethod
    def _insert_trade(conn: sqlite3.Connection, trade: ClosedTrade) -> None:
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

    @staticmethod
    def _insert_equity(
        conn: sqlite3.Connection,
        timestamp: datetime,
        portfolio: Portfolio,
        marks: dict[str, float],
    ) -> None:
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

    @staticmethod
    def _write_state(
        conn: sqlite3.Connection, portfolio: Portfolio, now: datetime
    ) -> None:
        conn.execute("DELETE FROM open_positions")
        for pos in portfolio.positions.values():
            conn.execute(
                """INSERT INTO open_positions
                (symbol, quantity, entry_price, entry_time,
                 stop_loss, take_profit, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    pos.symbol,
                    pos.quantity,
                    pos.entry_price,
                    pos.entry_time.isoformat(),
                    pos.stop_loss,
                    pos.take_profit,
                    now.isoformat(),
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO runner_state (key, value) VALUES ('cash', ?)",
            (repr(portfolio.cash),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO runner_state (key, value) VALUES ('saved_at', ?)",
            (now.isoformat(),),
        )

    def record_trade(self, trade: ClosedTrade) -> None:
        with self._connect() as conn:
            self._insert_trade(conn, trade)

    def record_equity(
        self,
        timestamp: datetime,
        portfolio: Portfolio,
        marks: dict[str, float],
    ) -> None:
        with self._connect() as conn:
            self._insert_equity(conn, timestamp, portfolio, marks)

    def persist_cycle_state(
        self,
        trades: list[ClosedTrade],
        timestamp: datetime,
        portfolio: Portfolio,
        marks: dict[str, float],
    ) -> None:
        """New trades + equity snapshot + position/cash state in ONE
        transaction. A crash between separate writes could leave the DB
        claiming an already-recorded trade's position is still open; a single
        transaction commits all of it or none of it."""
        with self._connect() as conn:
            for trade in trades:
                self._insert_trade(conn, trade)
            if trades:
                self._insert_equity(conn, timestamp, portfolio, marks)
            self._write_state(conn, portfolio, timestamp)

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

    # -- live-runner state (crash/restart recovery) -------------------------
    # One runner per DB file: the snapshot is a singleton, overwritten each
    # save. Two runners sharing a DB would clobber each other's state.

    def save_portfolio_state(self, portfolio: Portfolio, now: datetime) -> None:
        """Overwrite the open-positions + cash snapshot for this runner."""
        with self._connect() as conn:
            self._write_state(conn, portfolio, now)

    def load_portfolio_state(
        self,
    ) -> tuple[Optional[float], list[Position], Optional[datetime]]:
        """(cash, open positions, saved_at) — (None, [], None) if never saved."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT symbol, quantity, entry_price, entry_time,
                          stop_loss, take_profit
                   FROM open_positions"""
            ).fetchall()
            state = dict(
                conn.execute("SELECT key, value FROM runner_state").fetchall()
            )
        if "saved_at" not in state:
            return None, [], None
        positions = [
            Position(
                symbol=r[0],
                quantity=r[1],
                entry_price=r[2],
                entry_time=datetime.fromisoformat(r[3]),
                stop_loss=r[4],
                take_profit=r[5],
            )
            for r in rows
        ]
        return (
            float(state["cash"]),
            positions,
            datetime.fromisoformat(state["saved_at"]),
        )


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
