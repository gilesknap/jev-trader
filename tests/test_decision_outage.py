"""Decision-model outages must not stall exits or risk checks (issue #15)."""

import datetime as dt

import pandas as pd

from trader import engine as E
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine
from trader.jev import Decision, DecisionError

from test_engine import spec


class Down:
    """Fails every call until `up_at` (a market time), then enters."""

    def __init__(self, up_at=None):
        self.seen, self.calls, self.total_cost, self.up_at, self.now = [], 0, 0.0, up_at, None

    def decide(self, state, instructions, criteria):
        self.seen.append(self.now)
        self.calls += 1
        if self.up_at is None or self.now < self.up_at:
            raise DecisionError("HTTP 503: upstream down")
        pick = "ENTER" if "ENTER" in criteria else "HOLD"
        return Decision(pick, {k: 0.9 if k == pick else 0.1 for k in criteria})


def drive(tmp_path, bars, specs, decider, step=1):
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine(specs, {"live": book, "shadow": book}, decider, {"SPY", "QQQ"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[::step]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        decider.now = now
        eng.tick(now, {"SPY": bars.loc[:ts], "QQQ": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    return eng, book


def test_circuit_breaker_pauses_all_calls_then_recovers(tmp_path, session):
    day = session().index[0].date()
    up = dt.datetime.combine(day, dt.time(10, 0), ET)
    d = Down(up_at=up)
    eng, book = drive(tmp_path, session(path=[100.0] * 60), [spec(symbols=["SPY", "QQQ"])], d)
    before = [t for t in d.seen if t < up]
    # 09:36 fails once for SPY and QQQ is skipped; then one probe every 5 minutes up to 10:00.
    assert len(before) == 5, before
    assert any(t >= up for t in d.seen) and book.entries  # recovered and traded


def test_slow_decider_does_not_delay_exit_enforcement(tmp_path, session, monkeypatch):
    monkeypatch.setattr(E, "TICK_DECISION_BUDGET_S", 0.0)  # budget exhausted: no decisions at all
    d = Down()
    path = [100.0] * 20 + [98.0] * 20
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, d, {"SPY"}, tmp_path)
    bars = session(path=path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    # hand the engine an open position, then let the price fall through its stop
    t0 = bars.index[10].to_pydatetime()
    book.broker.buy_notional("SPY", 50, 100.0, t0, "x")
    book.entries["SPY"] = E.Entry("t", book.broker.positions["SPY"].qty, 100.0, 99.5, 101.0, t0)
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[11:]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    assert not d.seen and "SPY" not in book.entries
    assert pd.read_csv(tmp_path / "sim" / "trades.csv").reason.iloc[-1] == "stop"


def test_stop_hit_in_a_skipped_minute_is_caught(tmp_path, session):
    path = [100.0] * 12 + [99.0] + [100.0] * 30  # one-minute dip through the stop, then recovery
    bars = session(path=path)
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Down(), {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    t0 = bars.index[10].to_pydatetime()
    book.broker.buy_notional("SPY", 50, 100.0, t0, "x")
    book.entries["SPY"] = E.Entry("t", book.broker.positions["SPY"].qty, 100.0, 99.5, 101.0, t0)
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in [bars.index[11], bars.index[14], bars.index[20]]:  # the tick for the dip bar never happens
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    assert "SPY" not in book.entries


class FailOnce(SimBroker):
    def __init__(self, cash):
        super().__init__(cash)
        self.failed = False

    def sell_all(self, symbol, ref, now, cid, stop_id=None):
        if not self.failed:
            self.failed = True
            raise RuntimeError("503")
        return super().sell_all(symbol, ref, now, cid, stop_id)


def test_failed_stop_exit_is_retried_even_after_price_recovers(tmp_path, session):
    bars = session(path=[100.0] * 43)
    bars.iloc[12, bars.columns.get_loc("low")] = 99.0  # intrabar-only breach; every later bar is above the stop
    book = Book("sim", FailOnce(250.0), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Down(), {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    t0 = bars.index[10].to_pydatetime()
    SimBroker.buy_notional(book.broker, "SPY", 50, 100.0, t0, "x")
    book.entries["SPY"] = E.Entry("t", book.broker.positions["SPY"].qty, 100.0, 99.5, 101.0, t0)
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[11:20]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    assert book.broker.failed and "SPY" not in book.entries  # retried on the next tick
