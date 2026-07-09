"""Paper trading engine: in-memory portfolio with simulated fills.

Consumes real market bars (e.g., from yfinance) and simulates order fills
locally. Fill model is limit-style: a pending order at price X fills on a
bar if `bar.low <= X <= bar.high`. Stop-loss and take-profit on open
positions are checked against the same OHLC range.

The Portfolio is intentionally strategy-agnostic. It owns cash, positions,
and order state; it does NOT enforce strategy rules like max-concurrent
positions or sizing — those live in the strategy layer that calls
`submit_order`. The one invariant Portfolio does enforce is "one position
per ticker," because stacking complicates entry-price tracking and
`Position` would need to become more complex to support averaging in.

When we eventually wire up live trading, only `submit_order` swaps to a
Webull SDK call — everything else (strategy, EMA helpers, bar processing)
stays the same.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: float
    limit_price: float
    submitted_at: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[float] = None
    filled_at: Optional[datetime] = None
    reject_reason: Optional[str] = None


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity


@dataclass
class ClosedTrade:
    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    exit_reason: str


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: list[Order] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    def submit_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        limit_price: float,
        submitted_at: datetime,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Order:
        order = Order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            submitted_at=submitted_at,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        if side is Side.BUY:
            if symbol in self.positions:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "position_already_open"
                return order
            cost = quantity * limit_price
            if cost > self.cash:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "insufficient_cash"
                return order
        else:
            if symbol not in self.positions:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "no_position_to_sell"
                return order

        self.pending_orders.append(order)
        return order

    def process_bar(self, symbol: str, bar: Bar) -> None:
        """Apply one bar of market data to all state for `symbol`."""
        # Exits first: if a stop and TP are both inside the bar's range, OHLC
        # alone can't tell us which printed first. Assume stop fills first —
        # the conservative (worse-for-PnL) choice, matches typical backtest
        # convention.
        if symbol in self.positions:
            pos = self.positions[symbol]
            if pos.stop_loss is not None and bar.low <= pos.stop_loss:
                self._close_position(symbol, pos.stop_loss, bar.timestamp, "stop_loss")
            elif pos.take_profit is not None and bar.high >= pos.take_profit:
                self._close_position(symbol, pos.take_profit, bar.timestamp, "take_profit")

        still_pending: list[Order] = []
        for order in self.pending_orders:
            if order.symbol != symbol or order.status is not OrderStatus.PENDING:
                still_pending.append(order)
                continue
            if bar.low <= order.limit_price <= bar.high:
                self._fill_order(order, order.limit_price, bar.timestamp)
            else:
                still_pending.append(order)
        self.pending_orders = still_pending

    def equity(self, marks: dict[str, float]) -> float:
        total = self.cash
        for symbol, pos in self.positions.items():
            total += pos.quantity * marks.get(symbol, pos.entry_price)
        return total

    def _fill_order(self, order: Order, fill_price: float, ts: datetime) -> None:
        order.status = OrderStatus.FILLED
        order.fill_price = fill_price
        order.filled_at = ts
        if order.side is Side.BUY:
            self.cash -= fill_price * order.quantity
            self.positions[order.symbol] = Position(
                symbol=order.symbol,
                quantity=order.quantity,
                entry_price=fill_price,
                entry_time=ts,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
            )
        else:
            self._close_position(order.symbol, fill_price, ts, "manual")

    def _close_position(
        self, symbol: str, exit_price: float, ts: datetime, reason: str
    ) -> None:
        pos = self.positions.pop(symbol)
        self.cash += exit_price * pos.quantity
        self.closed_trades.append(
            ClosedTrade(
                symbol=symbol,
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_time=pos.entry_time,
                exit_time=ts,
                pnl=(exit_price - pos.entry_price) * pos.quantity,
                exit_reason=reason,
            )
        )


def _smoke_test() -> int:
    """Walk through a small scenario end-to-end so we can eyeball the engine."""
    pf = Portfolio(cash=5_000.0)
    print(f"Start: cash=${pf.cash:.2f}\n")

    t0 = datetime(2026, 4, 28, 9, 30)
    entry = 100.0
    qty = 10  # $1,000 position
    stop = entry * 0.95
    take = entry * 1.10

    pf.submit_order("FAKE", Side.BUY, qty, entry, t0, stop_loss=stop, take_profit=take)
    print(f"Submitted BUY 10 FAKE @ ${entry} (stop ${stop}, TP ${take})")
    print(f"Pending: {len(pf.pending_orders)}, positions: {len(pf.positions)}\n")

    bar1 = Bar(t0, open=99.5, high=100.5, low=99.0, close=100.2)
    pf.process_bar("FAKE", bar1)
    print(f"After bar1 (range 99.0–100.5): positions={len(pf.positions)}, cash=${pf.cash:.2f}")

    bar2 = Bar(datetime(2026, 4, 28, 9, 35), open=100.2, high=110.5, low=100.0, close=110.0)
    pf.process_bar("FAKE", bar2)
    print(f"After bar2 (range 100.0–110.5, hits TP @ 110): positions={len(pf.positions)}, cash=${pf.cash:.2f}")
    print(f"Closed trades: {len(pf.closed_trades)}")
    if pf.closed_trades:
        t = pf.closed_trades[0]
        print(f"  {t.symbol}: entry ${t.entry_price} -> exit ${t.exit_price} ({t.exit_reason}), PnL ${t.pnl:.2f}")

    print(f"\nFinal equity: ${pf.equity({}):.2f}")
    expected = 5_000.0 + (110.0 - 100.0) * 10
    if abs(pf.equity({}) - expected) > 1e-6:
        print(f"FAIL: expected ${expected:.2f}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
