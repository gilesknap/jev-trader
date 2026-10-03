from typing import Any

import pytest

from trader import guardrails as G


def acct(**kw):
    base: dict[str, Any] = dict(
        equity=250.0,
        cash=250.0,
        open_symbols=set(),
        universe={"SPY", "QQQ"},
        minutes_to_close=120,
        trading_blocked=False,
    )
    return G.AccountView(**(base | kw))


def order(**kw):
    base: dict[str, Any] = dict(symbol="SPY", notional=50.0, ref_price=600.0, stop_price=597.0, take_profit_price=606.0)
    return G.EntryOrder(**(base | kw))


def test_valid_entry_passes():
    G.check_entry(order(), acct())


@pytest.mark.parametrize(
    "o,a,msg",
    [
        (order(side="sell"), acct(), "long-only"),
        (order(symbol="GME"), acct(), "universe"),
        (order(), acct(open_symbols={"SPY"}), "already holding"),
        (order(), acct(minutes_to_close=15), "flatten window"),
        (order(ref_price=4.0, stop_price=3.9, take_profit_price=4.1, symbol="SPY"), acct(), "below"),
        (order(notional=62.6), acct(), "exceeds 25%"),
        (order(notional=40.0), acct(cash=30.0), "settled cash"),
        (order(stop_price=600.0), acct(), "stop must be below"),
        (order(stop_price=530.0), acct(), "further than 10%"),
        (order(take_profit_price=599.0), acct(), "take-profit"),
        (order(), acct(trading_blocked=True), "blocked"),
        (order(notional=0.5), acct(), "minimum"),
    ],
)
def test_violations(o, a, msg):
    with pytest.raises(G.GuardrailViolation, match=msg):
        G.check_entry(o, a)


def test_risk_check():
    assert G.risk_check(250, 250, 1.0, 1.0) is None
    assert G.risk_check(237.5, 250, 0.95, 1.0) == "kill"  # -5% day
    assert G.risk_check(200, 205, 0.70, 1.0) == "halt"  # -30% from HWM
    assert G.risk_check(49.9, 50, 1.0, 1.0) == "halt"  # floor
    assert G.risk_check(300, 300, 1.2, 1.6) is None  # -25% from HWM: not yet


def test_limits_are_the_agreed_numbers():
    assert (
        G.MAX_POSITION_FRACTION,
        G.DAILY_LOSS_KILL,
        G.TRAILING_HALT,
        G.EQUITY_FLOOR,
        G.FLATTEN_MINUTES_BEFORE_CLOSE,
    ) == (0.25, 0.05, 0.30, 50.0, 15)
