"""#48: the end-of-day flatten must not depend on the rest of the tick succeeding."""

import datetime as dt

import pandas as pd

from trader import alerts as alerts_mod
from trader import config, runner
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine
from test_engine import Always, spec


alerts: list = []


def _setup(tmp_path, session, **book_kw):
    alerts.clear()
    bars = session(path=[100.0] * 390)
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path,
                 alert=lambda level, msg: alerts.append((level, msg)))
    day = bars.index[0].date()
    eng.start_day(day, {})
    return eng, book, bars, dt.datetime.combine(day, dt.time(16), ET)


def _tick(eng, bars, close, ts):
    now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
    eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)


def _run_until(eng, bars, close, until: dt.time):
    for ts in bars.index:
        if ts.time() >= until:
            return ts
        _tick(eng, bars, close, ts)


def test_trade_log_failure_in_window_still_ends_flat(tmp_path, session):
    eng, book, bars, close = _setup(tmp_path, session)
    ts = _run_until(eng, bars, close, dt.time(15, 40))
    assert book.entries and book.broker.positions

    def disk_full(row):
        raise OSError(28, "No space left on device")

    book.append_trade = disk_full
    for t in bars.index[bars.index >= ts]:
        _tick(eng, bars, close, t)  # never raises in the window
    assert not book.broker.positions
    assert any("eod flatten failed" in m for _, m in alerts)


def test_equity_api_failure_in_window_still_flattens(tmp_path, session):
    eng, book, bars, close = _setup(tmp_path, session)
    ts = _run_until(eng, bars, close, dt.time(15, 40))
    assert book.broker.positions

    def down():
        raise ConnectionError("REST down")

    book.broker.equity = down  # _risk and the equity marks fail every minute
    for t in bars.index[bars.index >= ts]:
        try:
            _tick(eng, bars, close, t)
        except ConnectionError:
            assert t.time() < dt.time(15, 44)  # outside the window the failure still surfaces
    assert not book.broker.positions and not book.entries


def test_one_books_failure_does_not_block_another_books_flatten(tmp_path, session):
    alerts.clear()
    bars = session(path=[100.0] * 390)
    bad = Book("paper", SimBroker(250), tmp_path / "paper")
    good = Book("live", SimBroker(250), tmp_path / "live")
    eng = Engine([spec(id="a", mode="live"), spec(id="b", mode="shadow")], {"live": good, "shadow": bad},
                 Always(), {"SPY"}, tmp_path, alert=lambda level, msg: alerts.append((level, msg)))
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    ts = _run_until(eng, bars, close, dt.time(15, 40))
    assert bad.broker.positions and good.broker.positions

    def boom(*a, **k):
        raise RuntimeError("bug in the first book")

    eng._enforce_exits = lambda b, now, bars_: boom() if b is bad else Engine._enforce_exits(eng, b, now, bars_)
    for t in bars.index[bars.index >= ts]:
        try:
            _tick(eng, bars, close, t)
        except RuntimeError:
            assert t.time() < dt.time(15, 44)
    assert not good.broker.positions and not bad.broker.positions


def test_runner_last_resort_flattens_at_last_price_once(tmp_path, session):
    eng, book, bars, close = _setup(tmp_path, session)
    ts = _run_until(eng, bars, close, dt.time(15, 40))
    tick = dt.datetime.combine(ts.date(), dt.time(15, 50), ET)
    calls = []
    real = book.broker.flatten_all
    book.broker.flatten_all = lambda now=None: calls.append(now) or real(now)
    sim_bars = {"SPY": bars.loc[:ts].assign(close=lambda d: d.close.where(d.index < ts, 101.0))}
    eng.prices = {}  # the tick raised before it saw any prices
    runner._last_resort_flatten(eng, tick, sim_bars, 10.0)
    assert not book.entries and not book.broker.positions
    trades = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert trades.reason.iloc[-1] == "eod flatten" and float(trades.price.iloc[-1]) > 100.5  # last price, not entry
    assert len(calls) == 1
    runner._last_resort_flatten(eng, tick, sim_bars, 10.0)  # the tick's own flatten already ran this minute
    assert len(calls) == 1


def _disk_full(*a, **k):
    raise OSError(28, "No space left on device")


def test_raising_alerts_cannot_skip_any_books_flatten(tmp_path, session):
    """A full disk makes the alert path fail too: it mustn't skip the broker fallback or a later book."""
    bars = session(path=[100.0] * 390)
    first = Book("live", SimBroker(250), tmp_path / "live")
    second = Book("paper", SimBroker(250), tmp_path / "paper")
    eng = Engine([spec(id="a", mode="live"), spec(id="b", mode="shadow")], {"live": first, "shadow": second},
                 Always(), {"SPY"}, tmp_path, alert=_disk_full)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    ts = _run_until(eng, bars, close, dt.time(15, 40))
    assert first.broker.positions and second.broker.positions
    first.append_trade = _disk_full
    for t in bars.index[bars.index >= ts]:
        _tick(eng, bars, close, t)  # never raises in the window, even with every alert failing
    assert not first.broker.positions and not second.broker.positions


def test_runner_last_resort_survives_raising_alerts(tmp_path, session):
    eng, book, bars, close = _setup(tmp_path, session)
    ts = _run_until(eng, bars, close, dt.time(15, 40))
    eng.alert = _disk_full
    book.append_trade = _disk_full
    eng.write_status = _disk_full
    tick = dt.datetime.combine(ts.date(), dt.time(15, 50), ET)
    runner._last_resort_flatten(eng, tick, {"SPY": bars.loc[:ts]}, 10.0)  # must not raise
    assert not book.broker.positions


def test_notify_survives_an_unwritable_alert_log(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(config, "RUNTIME_DIR", blocker)  # mkdir and the log append both fail
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    alerts_mod.notify("urgent", "disk full")  # must not raise


def test_notify_survives_unwritable_log_and_closed_stderr(tmp_path, monkeypatch):
    import io
    import sys

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(config, "RUNTIME_DIR", blocker)
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    dead = io.StringIO()
    dead.close()
    monkeypatch.setattr(sys, "stderr", dead)
    alerts_mod.notify("urgent", "disk full and no journal")  # must not raise
