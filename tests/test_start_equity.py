"""An unreadable account at session start (#119): the session still runs, exits and the flatten
still work, no new entries until equity is known, and the kill switch and halt never loosen."""

import datetime as dt
import json

import pandas as pd
import pytest

from trader import config, golive, runner
from trader import engine as E
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine

from test_engine import Always, spec


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(E, "START_EQUITY_RETRY_S", 0.0, raising=False)


class Flaky(SimBroker):
    """The account endpoint (equity and settled cash) errors while `down`."""

    def __init__(self, cash, down=True):
        super().__init__(cash)
        self.down, self.client = down, None

    def equity(self):
        if self.down:
            raise ConnectionError("account endpoint 503")
        return super().equity()

    def settled_cash(self):
        if self.down:
            raise ConnectionError("account endpoint 503")
        return super().settled_cash()


def make(tmp_path, cash=250.0, last_equity=None, down=True):
    d = tmp_path / "sim"
    if last_equity is not None:  # yesterday's close, as end_day leaves it
        d.mkdir(parents=True, exist_ok=True)
        (d / "nav.json").write_text(json.dumps({"units": 200.0, "hwm": 1.3, "last_equity": last_equity}))
    book = Book("sim", Flaky(cash, down), d)
    alerts = []
    eng = Engine([spec(after_exit="rearm", max_trades=5)], {"live": book, "shadow": book}, Always(), {"SPY"},
                 tmp_path, alert=lambda level, msg: alerts.append(msg))
    return book, eng, alerts


def hold(book, t0, price=100.0):
    book.broker.buy_notional("SPY", 50, price, t0, "x")
    book.entries["SPY"] = E.Entry("t", book.broker.positions["SPY"].qty, price, price * 0.995, price * 1.01, t0)


def ticks(eng, bars, start, stop, up_at=None):
    """Like the runner: a tick that raises is counted and the session carries on."""
    day = bars.index[0].date()
    close = dt.datetime.combine(day, dt.time(16), ET)
    for i, ts in enumerate(bars.index[start:stop], start):
        if up_at is not None and i == up_at:
            for b in eng.unique_books():
                b.broker.down = False
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        try:
            eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
        except ConnectionError:
            pass


def trades(tmp_path):
    p = tmp_path / "sim" / "trades.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["side", "reason", "time"])


def test_unreadable_equity_at_start_blocks_entries_and_never_marks_a_stand_in(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng, alerts = make(tmp_path, last_equity=260.0)
    eng.start_day(bars.index[0].date(), {})  # raised on main
    assert book.day_start_equity == 260.0 and book.start_unverified == "exact"
    assert book.cash_at_open == 0.0
    assert (book.nav.units, book.nav.hwm, book.nav.last_equity) == (200.0, 1.3, 260.0)  # HWM halt: no made-up mark
    assert any("couldn't read equity" in a for a in alerts)
    ticks(eng, bars, 0, 390)
    assert "buy" not in set(trades(tmp_path).side)
    assert json.loads((tmp_path / "status.json").read_text())["books"]["sim"]  # heartbeat kept


@pytest.mark.parametrize("path, reason", [([100.0] * 390, "eod flatten"), ([100.0] * 20 + [98.0] * 370, "stop")])
def test_held_position_still_exits_and_flattens_when_equity_is_unreadable(tmp_path, session, path, reason):
    bars = session(path=path)
    book, eng, _ = make(tmp_path, last_equity=250.0)
    hold(book, bars.index[0].to_pydatetime())
    eng.start_day(bars.index[0].date(), {})
    assert book.start_unverified == "floor"
    ticks(eng, bars, 1, 390)
    t = trades(tmp_path)
    assert list(t.side) == ["sell"] and t.reason.iloc[0] == reason
    assert not book.entries and not book.broker.positions


def test_recovery_with_nothing_held_takes_the_read_as_baseline_and_resumes(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng, alerts = make(tmp_path, last_equity=300.0)  # a stale stand-in: the real equity is 250
    day = bars.index[0].date()
    eng.start_day(day, {})
    ticks(eng, bars, 0, 30)
    assert "buy" not in set(trades(tmp_path).side)
    ticks(eng, bars, 30, 60, up_at=30)
    assert book.start_unverified is None and book.day_start_equity == 250.0 and book.cash_at_open == 250.0
    risk = json.loads((tmp_path / "sim" / "risk.json").read_text())
    assert risk["day_start_equity"] == 250.0 and risk["start_unverified"] is None
    t = trades(tmp_path)
    assert (t[t.side == "buy"].time >= f"{day}T10:00").all() and (t.side == "buy").any()
    assert any("entries resume" in a for a in alerts)


def test_recovery_with_a_position_held_only_raises_the_baseline_and_stays_blocked(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng, alerts = make(tmp_path, last_equity=240.0)
    hold(book, bars.index[0].to_pydatetime())
    day = bars.index[0].date()
    eng.start_day(day, {})
    ticks(eng, bars, 1, 30, up_at=10)
    assert book.day_start_equity == pytest.approx(250.0, abs=0.1)  # max(stand-in 240, first read 250)
    assert book.start_unverified == "blocked" and book.entries  # the held position is still managed
    # a restart keeps the day blocked (and the raised baseline)
    book2 = Book("sim", book.broker, tmp_path / "sim")
    eng2 = Engine([spec(after_exit="rearm", max_trades=5)], {"live": book2, "shadow": book2}, Always(), {"SPY"}, tmp_path)
    eng2.start_day(day, {})
    assert book2.start_unverified == "blocked" and book2.day_start_equity == pytest.approx(250.0, abs=0.1)
    ticks(eng2, bars, 30, 390)
    assert list(trades(tmp_path).side) == ["sell"]  # only the flatten of what was held


def test_stand_in_baseline_is_never_lowered_so_the_kill_switch_only_tightens(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng, _ = make(tmp_path, last_equity=300.0)  # equity fell from 300 while a position was held
    hold(book, bars.index[0].to_pydatetime())
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 1, 20, up_at=5)
    assert book.day_start_equity == 300.0  # not rebased down to the current 250
    assert book.blocked == "kill" and not book.entries  # 250 <= 300 * 0.95


def test_no_persisted_fallback_trades_nothing_new_all_day(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng, alerts = make(tmp_path)  # first session ever: no nav.json, no risk.json
    eng.start_day(bars.index[0].date(), {})
    assert book.day_start_equity == 0.0 and book.start_unverified == "floor" and book.nav.units == 0
    ticks(eng, bars, 0, 390, up_at=10)
    assert book.day_start_equity == 250.0  # the kill switch is armed from the first clean read
    assert book.start_unverified == "blocked"
    assert "buy" not in set(trades(tmp_path).side)


def test_restart_with_a_good_baseline_does_not_block_when_only_the_mark_fails(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book, eng, _ = make(tmp_path, down=False)
    eng.start_day(day, {})
    book.broker.down = True
    book2 = Book("sim", book.broker, tmp_path / "sim")
    eng2 = Engine([spec()], {"live": book2, "shadow": book2}, Always(), {"SPY"}, tmp_path)
    eng2.start_day(day, {})  # restarted: settled cash and the NAV mark both unreadable
    assert book2.day_start_equity == 250.0 and book2.cash_at_open == 250.0 and book2.start_unverified is None


class _Reached(Exception):
    pass


def test_run_session_survives_an_unreadable_account_and_history(tmp_path, monkeypatch):
    now = dt.datetime.now(ET)
    alerts, made = [], []

    def broker(*a, **k):
        made.append(Flaky(250.0))
        return made[-1]

    class Stream:
        def __init__(self, *a, **k):
            raise _Reached  # the session got as far as streaming bars

    def no_data(*a, **k):
        raise ConnectionError("data API 503")

    import alpaca.data.live

    monkeypatch.setattr(config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"})
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "notify", lambda level, msg, **k: alerts.append(msg))
    monkeypatch.setattr(runner, "AlpacaBroker", broker)
    monkeypatch.setattr(runner, "_session_today", lambda client: (now - dt.timedelta(hours=1), now + dt.timedelta(hours=1)))
    monkeypatch.setattr(runner, "_load_specs", lambda *a, **k: [])
    monkeypatch.setattr(runner, "fetch_alpaca", no_data)
    monkeypatch.setattr(runner, "reconcile_sim_accounts", lambda *a, **k: None)
    monkeypatch.setattr(golive, "resolve_mode", lambda *a, **k: "paper")
    monkeypatch.setattr(golive, "enforce_promotion", lambda specs, *a, **k: specs)
    monkeypatch.setattr(alpaca.data.live, "StockDataStream", Stream)
    with pytest.raises(_Reached):
        runner.run_session("stub")
    assert any("couldn't read equity" in a for a in alerts)
    assert any("recent history" in a for a in alerts) and any("today's bars so far" in a for a in alerts)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["books"]["paper"]["equity"] is None


class CashFlaky(Flaky):
    """Equity recovers before settled cash does."""

    def __init__(self, cash):
        super().__init__(cash)
        self.cash_down = True

    def settled_cash(self):
        if self.cash_down:
            raise ConnectionError("account endpoint 503")
        return SimBroker.settled_cash(self)


def test_exact_waiting_for_settled_cash_never_judges_the_kill_against_the_stand_in(tmp_path, session):
    bars = session(path=[100.0] * 390)
    d = tmp_path / "sim"
    d.mkdir(parents=True)
    (d / "nav.json").write_text(json.dumps({"units": 200.0, "hwm": 1.3, "last_equity": 300.0}))
    book = Book("sim", CashFlaky(250.0), d)
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 30, up_at=5)  # equity back, settled cash still down: 250 vs stand-in 300
    assert book.blocked is None and book.start_unverified == "exact"
    assert json.loads((d / "risk.json").read_text()).get("blocked_today") is None
    assert "buy" not in set(trades(tmp_path).side)
    book.broker.cash_down = False
    ticks(eng, bars, 30, 60)
    assert book.start_unverified is None and book.day_start_equity == 250.0
    assert (trades(tmp_path).side == "buy").any()


def test_status_and_notes_show_why_nothing_enters_and_the_day_pnl_is_unknown(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng, _ = make(tmp_path)  # no fallback: blocked all day
    day = bars.index[0].date()
    eng.start_day(day, {})
    ticks(eng, bars, 0, 20, up_at=10)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["books"]["sim"]["start_unverified"] == "blocked"
    assert status["classifiers"][0]["symbols"]["SPY"]["note"] == E.START_UNVERIFIED_NOTE
    ticks(eng, bars, 20, 390)
    summary = eng.end_day(dt.datetime.combine(day, dt.time(16), ET))
    assert summary["sim"]["day_pnl"] is None and summary["sim"]["equity"] == 250.0


def test_the_next_day_starts_clean_after_a_blocked_day(tmp_path, session):
    day1 = session(path=[100.0] * 390)
    day2 = session(day=dt.date(2026, 9, 22), path=[100.0] * 390)
    book, eng, _ = make(tmp_path)  # first session ever, equity unreadable at the start
    eng.start_day(day1.index[0].date(), {})
    ticks(eng, day1, 0, 390, up_at=10)
    assert book.start_unverified == "blocked"
    eng.end_day(dt.datetime.combine(day1.index[0].date(), dt.time(16), ET))
    book2 = Book("sim", book.broker, tmp_path / "sim")  # the next day's runner
    eng2 = Engine([spec()], {"live": book2, "shadow": book2}, Always(), {"SPY"}, tmp_path)
    eng2.start_day(day2.index[0].date(), {})
    assert book2.start_unverified is None and book2.day_start_equity == 250.0
    assert json.loads((tmp_path / "sim" / "risk.json").read_text())["start_unverified"] is None
    ticks(eng2, day2, 0, 60)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["classifiers"][0]["symbols"]["SPY"]["note"] != E.START_UNVERIFIED_NOTE
    t = trades(tmp_path)
    assert (t[t.side == "buy"].time >= "2026-09-22").all() and (t.side == "buy").any()
