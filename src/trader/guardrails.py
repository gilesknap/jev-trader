"""Hard guardrails. Every order passes through here before reaching a broker.

These limits are deliberately constants, not configuration: the strategist cannot
change them. Changing them requires a human-merged PR to main.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_POSITION_FRACTION = 0.25  # of current equity, per position
DAILY_LOSS_KILL = 0.05  # -5% from day-start equity -> flatten, no entries until next session
TRAILING_HALT = 0.30  # -30% from unit-NAV high-water mark -> halt + revert to paper
EQUITY_FLOOR = 50.0  # USD; below this -> halt
FLATTEN_MINUTES_BEFORE_CLOSE = 15
MIN_PRICE = 5.0  # no penny stocks
MAX_STOP_DISTANCE = 0.10  # stop may be at most 10% below entry reference
MIN_NOTIONAL = 1.0  # Alpaca fractional minimum


class GuardrailViolation(Exception):
    pass


@dataclass
class EntryOrder:
    symbol: str
    notional: float  # USD
    ref_price: float  # last price used to size and validate the bracket
    stop_price: float
    take_profit_price: float
    side: str = "buy"


@dataclass
class AccountView:
    equity: float
    cash: float  # settled cash available for new buys
    open_symbols: set[str]
    universe: set[str]
    minutes_to_close: float
    trading_blocked: bool  # kill switch, halt or STOP in force


def check_entry(order: EntryOrder, acct: AccountView) -> None:
    """Raise GuardrailViolation unless the entry is allowed."""
    if acct.trading_blocked:
        raise GuardrailViolation("trading blocked (kill switch, halt or STOP)")
    if order.side != "buy":
        raise GuardrailViolation("long-only: only buy entries allowed")
    if order.symbol not in acct.universe:
        raise GuardrailViolation(f"{order.symbol} not in universe")
    if order.symbol in acct.open_symbols:
        raise GuardrailViolation(f"already holding {order.symbol}")
    if acct.minutes_to_close <= FLATTEN_MINUTES_BEFORE_CLOSE:
        raise GuardrailViolation("inside end-of-day flatten window")
    if order.ref_price < MIN_PRICE:
        raise GuardrailViolation(f"price {order.ref_price} below {MIN_PRICE}")
    if order.notional < MIN_NOTIONAL:
        raise GuardrailViolation(f"notional {order.notional:.2f} below minimum")
    if order.notional > MAX_POSITION_FRACTION * acct.equity + 1e-9:
        raise GuardrailViolation(f"notional {order.notional:.2f} exceeds {MAX_POSITION_FRACTION:.0%} of equity")
    if order.notional > acct.cash + 1e-9:
        raise GuardrailViolation(f"notional {order.notional:.2f} exceeds settled cash")
    if not order.stop_price < order.ref_price:
        raise GuardrailViolation("stop must be below entry reference")
    if order.stop_price < order.ref_price * (1 - MAX_STOP_DISTANCE):
        raise GuardrailViolation("stop further than 10% from entry")
    if not order.take_profit_price > order.ref_price:
        raise GuardrailViolation("take-profit must be above entry reference")


def risk_check(equity: float, day_start_equity: float, nav_per_unit: float, nav_hwm: float) -> str | None:
    """Return 'halt', 'kill' or None. Halt is sticky; kill lasts until next session."""
    if equity < EQUITY_FLOOR:
        return "halt"
    if nav_hwm > 0 and nav_per_unit <= nav_hwm * (1 - TRAILING_HALT):
        return "halt"
    if day_start_equity > 0 and equity <= day_start_equity * (1 - DAILY_LOSS_KILL):
        return "kill"
    return None


def flatten_due(minutes_to_close: float) -> bool:
    return minutes_to_close <= FLATTEN_MINUTES_BEFORE_CLOSE
