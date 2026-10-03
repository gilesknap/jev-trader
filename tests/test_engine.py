import datetime as dt
import json

import pandas as pd

from trader.broker import SimBroker
from trader.classifier import ClassifierSpec
from trader.data import ET
from trader.engine import Book, Engine
from trader.jev import Decision


class Always:
    """Decider that always answers the same way."""

    def __init__(self, entry="ENTER", exit="HOLD"):
        self.entry, self.exit, self.calls, self.total_cost = entry, exit, 0, 0.0

    def decide(self, state, instructions, criteria):
        self.calls += 1
        pick = self.entry if "ENTER" in criteria else self.exit
        return Decision(pick, {k: 0.9 if k == pick else 0.1 for k in criteria})


def spec(**kw):
    base = dict(id="t", family="conventional", symbols=["SPY"], window=("09:35", "15:30"), cadence_min=1, features=["ret_1m_pct"],
                entry={"instructions": "?", "criteria": {"ENTER": "a", "WAIT": "b"}},
                exit={"instructions": "?", "criteria": {"HOLD": "a", "EXIT": "b"}},
                size_fraction=0.2, stop_pct=0.5, target_pct=1.0, max_trades=1)
    return ClassifierSpec(**(base | kw))


def run(tmp_path, bars, specs, decider, cash=250.0):
    book = Book("sim", SimBroker(cash), tmp_path / "sim")
    eng = Engine(specs, {"live": book, "shadow": book}, decider, {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    eng.end_day(close)
    trades = pd.read_csv(tmp_path / "sim" / "trades.csv") if (tmp_path / "sim" / "trades.csv").exists() else pd.DataFrame()
    return book, trades


def test_enters_and_flattens_before_close(tmp_path, session):
    book, trades = run(tmp_path, session(path=[100.0] * 390), [spec()], Always())
    assert list(trades.side) == ["buy", "sell"]
    assert trades.reason.iloc[-1] == "eod flatten"
    assert trades.time.iloc[-1].startswith("2026-09-21T15:45")
    assert abs(float(trades.notional.iloc[0]) - 50.0) < 0.01  # 20% of 250


def test_stop_is_enforced(tmp_path, session):
    path = [100.0] * 10 + [99.0] * 380  # -1% drop after entry; stop is 0.5%
    book, trades = run(tmp_path, session(path=path), [spec()], Always())
    assert trades.reason.iloc[-1] == "stop"
    assert float(trades.pnl.iloc[-1]) < 0


def test_kill_switch_blocks_new_entries(tmp_path, session):
    # Two classifiers, full 25% size each, crash of 12% => >5% account loss
    path = [100.0] * 10 + [88.0] * 380
    s1 = spec(id="a", size_fraction=0.25, stop_pct=10)
    s2 = spec(id="b", size_fraction=0.25, stop_pct=10, symbols=["SPY"])
    book, trades = run(tmp_path, session(path=path), [s1, s2], Always())
    assert "daily kill switch" in set(trades.reason) or "stop" in set(trades.reason)


def test_stop_flag_flattens(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    eng.start_day(bars.index[0].date(), {})
    close = dt.datetime.combine(bars.index[0].date(), dt.time(16), ET)
    for i, ts in enumerate(bars.index):
        if i == 30:
            (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": bars.index[0].date().isoformat()}))
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    trades = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert trades.reason.iloc[-1] == "manual STOP"
    assert book.blocked == "stop" and not book.entries


def test_classifier_exit(tmp_path, session):
    book, trades = run(tmp_path, session(path=[100.0] * 390), [spec()], Always(exit="EXIT"))
    assert trades.reason.iloc[-1] == "classifier EXIT"
