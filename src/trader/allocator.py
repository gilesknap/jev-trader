"""Book-level admission (#74): aggregate planned stop risk and overlapping exposure.

Hard guardrails like those in guardrails.py: constants, not configuration, so changing them
needs a human-merged PR to main. `allocate` only ever reduces an entry's size, never raises
it; a trim below `trim_floor` skips the entry rather than placing dust; and
guardrails.check_entry still runs afterwards. Each Book is allocated on its own: a sim
account, paper and live never share these limits.

A planned stop loss is an estimate, not a bound: gaps, stale quotes and failed exits can lose
more. The buckets are conservative overlap proxies, not correlation estimates.
"""

from __future__ import annotations

from dataclasses import dataclass

# Planned stop loss of everything held or pending, plus today's realised loss, stays within 3%
# of the smaller of current and day-start equity: two points under the -5% kill switch, as a
# buffer for gaps and slippage. Realised gains never enlarge it.
OPEN_RISK_FRACTION = 0.03
EQUITY_CAP = 0.75  # all stock and equity-ETF notional together
BUCKET_CAP = 0.50  # each bucket of overlapping symbols below

BROAD = {"SPY", "DIA", "IWM"}  # hold most of every bucket (IWM for market beta): count against all
NON_EQUITY = {"TLT", "GLD"}  # treasuries and gold: outside the equity cap and every bucket
BUCKETS = {
    # QQQ, XLK, SMH and XLY are dominated by these mega-caps: XLK by AAPL/MSFT/NVDA/AVGO (and
    # PLTR), SMH by NVDA/AMD/AVGO, XLY by AMZN/TSLA, QQQ by all of them plus GOOGL/META/NFLX.
    "growth": {"QQQ", "XLK", "SMH", "XLY", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
               "TSLA", "AMD", "AVGO", "NFLX", "PLTR"},
    "financials": {"XLF", "JPM", "BAC"},  # JPM and BAC are among XLF's largest holdings
    "health": {"XLV", "LLY", "UNH"},  # LLY and UNH are among XLV's largest holdings
    "energy": {"XLE", "XOM"},  # XOM is XLE's largest holding
}
# No other universe symbol is a large part of these: XLI's top holdings aren't in the universe,
# and COST is a consumer-staples name (a few % of QQQ). The 25% and 75% caps still bind them.
UNBUCKETED = {"XLI", "COST"}

# A trimmed entry is placed only if it keeps at least this much, else it is skipped (#118). A
# $1-$10 remnant would still count as a closed trade for the go-live gate and promotion while its
# P&L is mostly rounding and slippage. $10 is 4% of a ~$250 account; a quarter of the request
# still tests the signal at a meaningful size. Untrimmed entries are never affected.
MIN_TRIM_USD = 10.0
MIN_TRIM_FRACTION = 0.25


@dataclass(frozen=True)
class Exposure:
    symbol: str
    notional: float  # USD at cost
    stop_loss: float  # USD lost if the stop fills at its price


def allocate(symbol: str, desired: float, stop_fraction: float, equity: float, day_start: float,
             realised: float, exposures: list[Exposure]) -> tuple[float, str]:
    """Return (allowed USD, binding constraint); "requested" when nothing binds."""
    base = min(equity, day_start) if day_start > 0 else equity
    budget = base * OPEN_RISK_FRACTION - max(0.0, -realised)
    available = budget - sum(max(0.0, x.stop_loss) for x in exposures)
    constraints = {"requested": desired, "aggregate stop risk": available / max(stop_fraction, 1e-6)}
    if symbol not in NON_EQUITY:
        used = sum(x.notional for x in exposures if x.symbol not in NON_EQUITY)
        constraints["equity exposure"] = EQUITY_CAP * equity - used
    for name, members in BUCKETS.items():
        if symbol in members or symbol in BROAD:
            used = sum(x.notional for x in exposures if x.symbol in members or x.symbol in BROAD)
            constraints[f"{name} bucket"] = BUCKET_CAP * equity - used
    binding = min(constraints, key=constraints.get)
    return max(0.0, min(desired, constraints[binding])), binding


def trim_floor(desired: float) -> float:
    """Smallest allowance worth placing when a limit binds; below it the entry is skipped."""
    return max(MIN_TRIM_USD, MIN_TRIM_FRACTION * desired)
