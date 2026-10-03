"""Entry-failure alerts are throttled per book and symbol (#50 item 2): a persistent broker
refusal at a short cadence must not become a push storm, but each symbol still alerts, and an
order whose outcome is unknown (its cash stays reserved) is always reported."""

import datetime as dt
import time

import pandas as pd
import pytest

from conftest import broker_of
from test_engine import Always, spec
from trader.broker import NotFilled, SimBroker
from trader.data import ET
from trader.engine import Book, Engine


class APIError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg or f"http {code}")
        self.status_code = code


class Refusing(SimBroker):
    """Every market entry raises `error`."""

    def __init__(self, cash, error):
        super().__init__(cash)
        self.error, self.attempts = error, []

    def buy_notional(self, symbol, notional, ref_price, now, client_id):
        self.attempts.append((symbol, now))
        raise self.error


@pytest.fixture
def clock(monkeypatch):
    """time.monotonic that the test advances: one simulated minute per tick."""
    t = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    return t


def run(tmp_path, session, error, clock, minutes, symbols=("SPY", "QQQ"), switch=None):
    """`switch`: (minute, error) from which every entry raises that error instead."""
    book = Book("paper", Refusing(1000.0, error), tmp_path / "paper")
    alerts = []
    s = spec(symbols=list(symbols), cadence_min=1, max_trades=20, after_exit="rearm")
    eng = Engine(
        [s],
        {"live": book, "shadow": book},
        Always(),
        set(symbols),
        tmp_path,
        alert=lambda level, msg: alerts.append((level, msg)),
    )
    bars = {sym: session(seed=i) for i, sym in enumerate(symbols)}
    day = bars["SPY"].index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    idx = bars["SPY"].index
    start = idx.get_loc(pd.Timestamp(dt.datetime.combine(day, dt.time(9, 35), ET)))
    for i, ts in enumerate(idx[start : start + minutes]):
        if switch and i == switch[0]:
            broker_of(book, Refusing).error = switch[1]
        clock[0] += 60
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {sym: b.loc[:ts] for sym, b in bars.items()}, (close - now).total_seconds() / 60)
    return book, alerts


def entry_alerts(alerts, sym, text="failed"):
    return [(lvl, m) for lvl, m in alerts if f"entry {sym} " in m and text in m]


@pytest.mark.parametrize(
    "error, level",
    [
        (APIError(403, "account is trading_blocked"), "urgent"),
        (NotFilled("order canceled with nothing filled"), "info"),
    ],
)
def test_repeated_entry_failures_alert_once_per_window_per_symbol(tmp_path, session, clock, error, level):
    book, alerts = run(tmp_path, session, error, clock, minutes=25)
    for sym in ("SPY", "QQQ"):
        tries = [a for a in broker_of(book, Refusing).attempts if a[0] == sym]
        assert len(tries) == 25  # retried every minute: the refusal never reserves anything
        got = entry_alerts(alerts, sym)
        # 25 one-minute failures span three 10-minute windows: three alerts, not 25
        assert len(got) == 3, got
        assert all(lvl == level for lvl, _ in got)
    assert not book.pending and book.buys_today == 0


def test_refusal_is_not_hidden_by_an_earlier_unfilled_alert(tmp_path, session, clock):
    book, alerts = run(
        tmp_path,
        session,
        NotFilled("canceled"),
        clock,
        minutes=4,
        symbols=("SPY",),
        switch=(2, APIError(403, "asset not fractionable")),
    )
    # a different condition alerts at once, at its own level, then is throttled in turn
    assert [lvl for lvl, _ in entry_alerts(alerts, "SPY")] == ["info", "urgent"]


def test_unknown_outcome_is_always_alerted(tmp_path, session, clock):
    """No clear answer: the cash and symbol stay reserved on an order that may be live, so every
    one is reported, as is its release once the broker shows it was never placed."""
    book, alerts = run(tmp_path, session, TimeoutError("read timed out"), clock, minutes=12, symbols=("SPY",))
    tries = len(broker_of(book, Refusing).attempts)
    assert tries >= 3
    assert len(entry_alerts(alerts, "SPY", "no clear answer")) == tries
    assert len(entry_alerts(alerts, "SPY", "never placed")) >= tries - 1
