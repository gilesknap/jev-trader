"""The equity log opens each day with the real day-start equity (#50 item 5), so the go-live
gate's worst day and the equity chart count from the open, not the first 5-minute mark. Once a
day (a restart adds nothing), and never a stand-in when equity couldn't be read at the start."""

import datetime as dt

import pandas as pd
import pytest

from test_engine import Always, spec
from test_start_equity import hold, make, ticks
from trader import engine as E
from trader import golive
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine

DAY = dt.date(2026, 10, 6)  # inside the gate's window


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(E, "START_EQUITY_RETRY_S", 0.0, raising=False)


class Scripted(SimBroker):
    """Equity is whatever the test says it is."""

    def __init__(self, eq):
        super().__init__(eq)
        self.eq = eq

    def equity(self):
        return self.eq


def rows(book_dir, day=DAY):
    p = book_dir / "equity.csv"
    if not p.exists():
        return pd.DataFrame(columns=["time", "equity", "nav", "hwm"])
    df = pd.read_csv(p)
    return df[df.time.str.startswith(day.isoformat())].reset_index(drop=True)


def drive(eng, bars, start, stop):
    close = dt.datetime.combine(bars.index[0].date(), dt.time(16), ET)
    for ts in bars.index[start:stop]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)


def test_gate_counts_a_loss_before_the_first_five_minute_mark(tmp_path, session):
    bars = session(day=DAY, path=[100.0] * 390)
    book = Book("paper", Scripted(1000.0), tmp_path / "paper")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, tmp_path)
    eng.start_day(DAY, {})
    book.broker.eq = 940.0  # down 6% in the first minutes (e.g. a gap through a stop)
    drive(eng, bars, 0, 30)
    eng.end_day(dt.datetime.combine(DAY, dt.time(16), ET))
    r = rows(tmp_path / "paper")
    assert r.time[0].startswith(f"{DAY}T09:30") and r.equity[0] == 1000.0 and r.nav[0] == 1.0
    assert (r.time.iloc[1:] >= f"{DAY}T09:35").all()  # the 5-minute marks follow it
    gate = golive.evaluate_gate(tmp_path / "paper", today=DAY)
    assert gate.worst_day_pct == pytest.approx(-6.0)  # on main: 0.0, the day started at 940
    assert any("a day hit" in reason for reason in gate.reasons)


def test_runner_start_time_stamps_the_opening_row(tmp_path):
    book = Book("paper", Scripted(500.0), tmp_path / "paper")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, tmp_path)
    eng.start_day(DAY, {}, opened_at=dt.datetime.combine(DAY, dt.time(11, 7), ET))  # a fresh start after the open
    r = rows(tmp_path / "paper")
    assert list(r.time) == [f"{DAY}T11:07-04:00"] and r.equity[0] == 500.0


def test_restart_adds_no_second_opening_row(tmp_path, session):
    bars = session(day=DAY, path=[100.0] * 390)
    broker = Scripted(1000.0)
    book = Book("paper", broker, tmp_path / "paper")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, tmp_path)
    eng.start_day(DAY, {})
    eng.start_day(DAY, {})  # restarted before the first mark
    assert len(rows(tmp_path / "paper")) == 1
    drive(eng, bars, 0, 60)
    broker.eq = 990.0
    book2 = Book("paper", broker, tmp_path / "paper")  # restarted mid-session, equity has moved
    eng2 = Engine([spec()], {"live": book2, "shadow": book2}, Always("WAIT"), {"SPY"}, tmp_path)
    eng2.start_day(DAY, {}, opened_at=dt.datetime.combine(DAY, dt.time(10, 31), ET))
    r = rows(tmp_path / "paper")
    assert r.equity[0] == 1000.0 and not r.time.str.contains("T10:31").any()
    assert r.time.is_monotonic_increasing
    # the next day opens afresh
    eng2.start_day(DAY + dt.timedelta(days=1), {})
    assert list(rows(tmp_path / "paper", DAY + dt.timedelta(days=1)).equity) == [990.0]


def test_unreadable_start_writes_no_stand_in_then_opens_at_the_first_real_read(tmp_path, session):
    """Nothing held ("exact"): the first clean read is the day's true start, so it opens the log."""
    bars = session(day=DAY, path=[100.0] * 390)
    book, eng, _ = make(tmp_path, last_equity=300.0)  # a stale stand-in: the real equity is 250
    eng.start_day(DAY, {})
    assert book.start_unverified == "exact" and rows(tmp_path / "sim").empty  # no 300 in the log
    ticks(eng, bars, 0, 30, up_at=12)
    r = rows(tmp_path / "sim")
    assert r.time[0].startswith(f"{DAY}T09:43") and r.equity[0] == 250.0  # the minute it read, before any mark
    assert r.time.is_monotonic_increasing and 300.0 not in set(r.equity)
    assert (r.time.str.contains("T09:43")).sum() == 1


@pytest.mark.parametrize("held, last_equity", [(True, 240.0), (False, None)])
def test_unknown_start_never_gets_an_opening_row(tmp_path, session, held, last_equity):
    """Held at an unreadable start, or no stand-in at all ("floor"): the day's start equity is
    never known, so the log starts at the first real mark, as before."""
    bars = session(day=DAY, path=[100.0] * 390)
    book, eng, _ = make(tmp_path, last_equity=last_equity)
    if held:
        hold(book, bars.index[0].to_pydatetime())
    eng.start_day(DAY, {})
    assert book.start_unverified == "floor"
    ticks(eng, bars, 1, 30, up_at=10)
    r = rows(tmp_path / "sim")
    assert book.start_unverified == "blocked"
    assert list(r.time.str[11:16]) == ["09:45", "09:50", "09:55", "10:00"]  # the 5-minute marks once it reads


def test_an_opening_row_only_day_is_not_a_trading_day(tmp_path, session):
    """A session that started but never reached a 5-minute mark mustn't count towards the gate."""
    book = Book("paper", Scripted(1000.0), tmp_path / "paper")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, tmp_path)
    eng.start_day(DAY, {})  # the runner died here: only the opening row
    nxt = DAY + dt.timedelta(days=1)
    eng.start_day(nxt, {})
    drive(eng, session(day=nxt, path=[100.0] * 390), 0, 10)  # a normal start: marks at 09:35, 09:40
    assert len(rows(tmp_path / "paper")) == 1 and len(rows(tmp_path / "paper", nxt)) == 3
    gate = golive.evaluate_gate(tmp_path / "paper", today=nxt)
    assert gate.trading_days == 1
