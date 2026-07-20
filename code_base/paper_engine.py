"""Paper trading engine: in-memory portfolio with simulated fills.

Consumes real market bars (e.g., from yfinance) and simulates order fills
locally. Fill model is limit-style: a pending order at price X fills on a
bar if `bar.low <= X <= bar.high`. Stop-loss and take-profit on open
positions are checked against the same OHLC range — except the bar a
position was entered on, whose low/high already happened before the entry
fill (see `process_bar`).

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
    # Slippage haircut, as a FRACTION per side (0.005 = 0.5%). Applied where
    # the strategy demands immediacy and must cross the spread: market buys
    # fill that much WORSE (higher), stop-loss and forced-close exits fill
    # that much WORSE (lower). Limit-style fills — take-profits and resting
    # limit buys — fill exactly, as real limits do. Default 0 keeps the pure
    # engine deterministic for tests; the runners opt in.
    slippage_pct: float = 0.0
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

    def submit_market_buy(
        self,
        symbol: str,
        quantity: float,
        price: float,
        submitted_at: datetime,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Order:
        """Market entry: fills IMMEDIATELY at `price` plus the slippage
        haircut (you pay the ask and may walk the book on thin names). In
        bar-based simulation `price` is the trigger bar's close — the last
        known price when the signal confirmed.
        """
        fill_price = price * (1 + self.slippage_pct)
        order = Order(
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            limit_price=price,  # recorded as the pre-slippage reference price
            submitted_at=submitted_at,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if symbol in self.positions:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "position_already_open"
            return order
        if quantity <= 0:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "zero_quantity"
            return order
        if quantity * fill_price > self.cash:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "insufficient_cash"
            return order
        self._fill_order(order, fill_price, submitted_at)
        return order

    def process_bar(self, symbol: str, bar: Bar) -> None:
        """Apply one bar of market data to all state for `symbol`."""
        # Exits first: if a stop and TP are both inside the bar's range, OHLC
        # alone can't tell us which printed first. Assume stop fills first —
        # the conservative (worse-for-PnL) choice, matches typical backtest
        # convention.
        #
        # A position is never checked against stop/tp on the SAME bar it was
        # entered on: entry fills at that bar's close, so the bar's low/high
        # already happened chronologically before the fill — a position can't
        # be stopped out by a price that printed before it existed. Caught
        # 2026-07-20 on a real live trade (ADVB): entered on a bar's close,
        # immediately "stopped out" by that same bar's own low.
        if symbol in self.positions and self.positions[symbol].entry_time != bar.timestamp:
            pos = self.positions[symbol]
            if pos.stop_loss is not None and bar.low <= pos.stop_loss:
                # A triggered stop becomes a market sell: it fills through the
                # spread, i.e. slippage-worse than the stop price.
                self._close_position(
                    symbol,
                    pos.stop_loss * (1 - self.slippage_pct),
                    bar.timestamp,
                    "stop_loss",
                )
            elif pos.take_profit is not None and bar.high >= pos.take_profit:
                # A take-profit is a resting limit sell: fills at its price.
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

    def close_position_at(
        self, symbol: str, price: float, ts: datetime, reason: str
    ) -> Optional[ClosedTrade]:
        """Force-close an open position at `price` less the slippage haircut
        (a forced close is a market sell — e.g. the end-of-day flatten).

        Returns the resulting ClosedTrade, or None if no position is open.
        """
        if symbol not in self.positions:
            return None
        self._close_position(symbol, price * (1 - self.slippage_pct), ts, reason)
        return self.closed_trades[-1]

    def cancel_pending(self, symbol: Optional[str] = None) -> list[Order]:
        """Cancel pending orders (all of them, or just `symbol`'s). Returns them."""
        canceled: list[Order] = []
        still_pending: list[Order] = []
        for order in self.pending_orders:
            if symbol is None or order.symbol == symbol:
                order.status = OrderStatus.CANCELED
                canceled.append(order)
            else:
                still_pending.append(order)
        self.pending_orders = still_pending
        return canceled


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

    # Slippage haircut: market buy pays up 1%, a triggered stop fills 1%
    # through the stop price, a take-profit (limit) fills exactly.
    pf2 = Portfolio(cash=5_000.0, slippage_pct=0.01)
    o = pf2.submit_market_buy("SLIP", 10, 100.0, t0)
    if not (abs(o.fill_price - 101.0) < 1e-9 and abs(pf2.cash - 3_990.0) < 1e-9):
        print(f"FAIL slippage buy: fill={o.fill_price}, cash={pf2.cash}", file=sys.stderr)
        return 1
    pf2.positions["SLIP"].stop_loss = 95.0
    t1 = datetime(2026, 4, 28, 9, 35)
    pf2.process_bar("SLIP", Bar(t1, open=96.0, high=96.5, low=94.0, close=94.5))
    t = pf2.closed_trades[-1]
    if not (t.exit_reason == "stop_loss" and abs(t.exit_price - 95.0 * 0.99) < 1e-9):
        print(f"FAIL slippage stop: exit={t.exit_price} ({t.exit_reason})", file=sys.stderr)
        return 1
    print(f"Slippage case: buy 100->fill {o.fill_price:.2f}, stop 95->fill {t.exit_price:.2f}: OK")

    # Same-bar entry/exit: a position must NOT be checked against its own
    # stop/tp on the bar it was entered on (that bar's low/high already
    # happened before the close it was entered at). The very next bar with
    # the same breach DOES trigger — the guard is scoped to the entry bar only.
    pf3 = Portfolio(cash=5_000.0)
    pf3.submit_market_buy("SAMEBAR", 10, 100.0, t0)
    pf3.positions["SAMEBAR"].stop_loss = 95.0
    pf3.process_bar("SAMEBAR", Bar(t0, open=99.0, high=101.0, low=90.0, close=100.0))
    if "SAMEBAR" not in pf3.positions or pf3.closed_trades:
        print(f"FAIL same-bar guard: position closed by its own entry bar's low", file=sys.stderr)
        return 1
    pf3.process_bar("SAMEBAR", Bar(t1, open=99.0, high=99.5, low=90.0, close=91.0))
    if "SAMEBAR" in pf3.positions or not pf3.closed_trades:
        print(f"FAIL same-bar guard: next bar should have triggered the stop", file=sys.stderr)
        return 1
    print(f"Same-bar guard: entry bar's low (90.0) ignored; next bar's low (90.0) triggers stop_loss: OK")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
