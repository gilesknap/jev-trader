"""#131: a tracked position gone from the broker whose exit fill can't be read (an API error) is
not one with no fill. It stays tracked, is never sold, and is looked up again each tick; only when
the retries run out, or at the EOD flatten, is its exit recorded at a guess."""

import datetime as dt

import pandas as pd
import pytest

from trader import runner
from trader.broker import Fill, Position, SimBroker
from trader.data import ET
from trader.engine import EXIT_LOOKUP_TRIES, Entry

from test_engine import Always, spec
from test_execution_toolkit import make, ticks
from test_orders import FakeClient, broker

DAY = dt.date(2026, 9, 21)
AT = lambda h, m: dt.datetime.combine(DAY, dt.time(h, m), ET)


class Flaky(SimBroker):
    """The position is gone; its stop's fill lookup fails `fails` times, then reports `fill`."""

    def __init__(self, fails, fill=None):
        super().__init__(250.0)
        self.fails, self.fill, self.lookups, self.sells = fails, fill, 0, []

    def stop_fill(self, stop_id, now, strict=False):
        self.lookups += 1
        if self.lookups <= self.fails:
            if strict:
                raise ConnectionError("alpaca 503")
            return None
        return self.fill

    def sell_all(self, symbol, ref_price, now, client_id, stop_id=None):
        self.sells.append(symbol)
        return super().sell_all(symbol, ref_price, now, client_id, stop_id)

    def sell_qty(self, symbol, qty, ref_price, now, client_id, stop_id=None):
        self.sells.append(symbol)
        return super().sell_qty(symbol, qty, ref_price, now, client_id, stop_id)


def setup(tmp_path, br):
    book, eng = make(tmp_path, [spec()], Always(exit="EXIT"), br)  # would sell at once, if it could
    book.entries["SPY"] = Entry("t", 0.5, 100.0, 99.0, 101.0, AT(9, 31), "s1")  # not at the broker
    alerts = []
    eng.alert = lambda lvl, msg: alerts.append((lvl, msg))
    runner.reconcile_at_startup(book, lambda lvl, msg: alerts.append((lvl, msg)))
    eng.start_day(DAY, {})
    eng.states[0].symbols["SPY"].status = "holding"
    return book, eng, alerts


def rows(tmp_path):
    p = tmp_path / "sim" / "trades.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["reason"])


def test_a_lookup_that_fails_at_startup_is_retried_and_books_the_real_fill(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Flaky(fails=1, fill=Fill("SPY", "sell", 0.5, 98.9, AT(9, 50), "s1"))
    book, eng, alerts = setup(tmp_path, br)
    assert "SPY" in book.entries and rows(tmp_path).empty  # not written off as "no fill"
    assert [lvl for lvl, _ in alerts] == ["info"] and "couldn't be read" in alerts[0][1]
    ticks(eng, bars, 5, 6)
    (r,) = rows(tmp_path).to_dict("records")
    assert (r["price"], r["reason"]) == (98.9, "closed while runner down")
    assert r["pnl_pct"] == pytest.approx(-1.1)  # real, so evidence
    assert not book.entries and not book.unresolved and br.sells == []


def test_a_position_that_vanishes_mid_session_retries_a_failed_lookup(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Flaky(fails=2, fill=Fill("SPY", "sell", 0.5, 98.95, AT(10, 0), "s1"))
    book, eng = make(tmp_path, [spec()], Always(entry="WAIT"), br)
    eng.start_day(DAY, {})
    book.entries["SPY"] = Entry("t", 0.5, 100.0, 99.0, 101.0, AT(9, 31), "s1")
    ticks(eng, bars, 5, 7)
    assert "SPY" in book.entries and rows(tmp_path).empty and book.unresolved["SPY"] == (2, False)
    ticks(eng, bars, 7, 8)
    (r,) = rows(tmp_path).to_dict("records")
    assert (r["price"], r["reason"]) == (98.95, "server stop") and not pd.isna(r["pnl_pct"])
    assert br.sells == []


def test_a_lookup_that_keeps_failing_is_recorded_at_the_stop_once_and_never_sold(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Flaky(fails=10**6)
    book, eng, alerts = setup(tmp_path, br)
    ticks(eng, bars, 5, 5 + EXIT_LOOKUP_TRIES - 2)  # the classifier says EXIT every minute meanwhile
    assert "SPY" in book.entries and rows(tmp_path).empty and br.sells == []
    assert [lvl for lvl, _ in alerts] == ["info", "info"]  # startup, then throttled
    ticks(eng, bars, 5 + EXIT_LOOKUP_TRIES - 2, 390)  # runs out, then the rest of the day and the flatten
    (r,) = rows(tmp_path).to_dict("records")
    assert r["reason"] == "closed while runner down (recorded at stop) (price estimated)"
    assert (r["price"], r["pnl"]) == (99.0, -0.5) and pd.isna(r["pnl_pct"])
    assert br.lookups == EXIT_LOOKUP_TRIES and br.sells == [] and not book.entries
    assert alerts[-1][0] == "urgent" and "recorded at the stop" in alerts[-1][1]


def test_the_eod_flatten_records_a_still_unresolved_exit_without_selling(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Flaky(fails=10**6)
    book, eng, _ = setup(tmp_path, br)
    ticks(eng, bars, 371, 374)  # 15:42-15:44: still retrying
    assert "SPY" in book.entries and rows(tmp_path).empty
    ticks(eng, bars, 374, 390)  # 15:45: the flatten looks once more, then records the guess
    (r,) = rows(tmp_path).to_dict("records")
    assert r["reason"] == "closed while runner down (recorded at stop) (price estimated)"
    assert "T15:45" in r["time"] and br.sells == [] and not book.entries
    assert br.lookups == 6 < EXIT_LOOKUP_TRIES  # startup, 15:42-15:45, and the flatten's last look


def test_a_position_found_after_all_is_managed_as_held(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Flaky(fails=10**6)
    book, eng, _ = setup(tmp_path, br)
    br.positions["SPY"] = Position("SPY", 0.5, 100.0)  # the startup positions read had lagged
    ticks(eng, bars, 5, 6)
    assert not book.unresolved
    ticks(eng, bars, 6, 390)
    assert br.sells and rows(tmp_path).reason.tolist() == ["classifier EXIT"]


def test_an_exit_genuinely_not_found_is_still_recorded_at_the_stop_at_once(tmp_path):
    br = Flaky(fails=0)
    book, eng, alerts = setup(tmp_path, br)
    (r,) = rows(tmp_path).to_dict("records")
    assert r["reason"] == "closed while runner down (recorded at stop) (price estimated)"
    assert not book.entries and not book.unresolved and alerts[0][0] == "urgent"


def test_strict_lookups_raise_instead_of_reporting_nothing():
    b = broker(FakeClient())  # no such order; no order history endpoint
    assert b.stop_fill("s9", None) is None and b.exit_fill_since("SPY", AT(9, 31), None) is None
    with pytest.raises(KeyError):
        b.stop_fill("s9", None, strict=True)
    with pytest.raises(AttributeError):
        b.exit_fill_since("SPY", AT(9, 31), None, strict=True)
