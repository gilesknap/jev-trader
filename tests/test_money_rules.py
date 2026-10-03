"""Cash-account settlement ledger (#19) and enforced shadow-to-live promotion (#20)."""

import csv
import datetime as dt

import pandas as pd

from trader import golive
from trader.broker import SimBroker
from trader.classifier import ClassifierSpec
from trader.data import ET
from trader.engine import Book, Engine

from test_engine import Always, spec


def run(tmp_path, bars, specs, cash=250.0):
    book = Book("sim", SimBroker(cash), tmp_path / "sim")
    eng = Engine(specs, {"live": book, "shadow": book}, Always(exit="EXIT"), {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    return book, pd.read_csv(tmp_path / "sim" / "trades.csv")


def test_same_day_proceeds_are_never_rebought(tmp_path, session):
    # rearm after every exit, 25% size, many trades allowed: without the ledger this would
    # recycle the same cash all day. With it, total buys can't exceed the cash settled at the open.
    s = spec(size_fraction=0.25, max_trades=20, after_exit="rearm", cadence_min=1)
    book, trades = run(tmp_path, session(path=[100.0] * 390), [s], cash=250.0)
    buys = trades[trades.side == "buy"].notional.sum()
    assert buys <= 250.0 + 1e-6
    assert (trades.side == "buy").sum() >= 4  # it did keep trading while settled cash remained


def test_ledger_survives_restart(tmp_path, session):
    s = spec(size_fraction=0.25, max_trades=20, after_exit="rearm", cadence_min=1)
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine([s], {"live": book, "shadow": book}, Always(exit="EXIT"), {"SPY"}, tmp_path)
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[:40]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    spent = book.buys_today
    book2 = Book("sim", book.broker, tmp_path / "sim")
    eng2 = Engine([s], {"live": book2, "shadow": book2}, Always(exit="EXIT"), {"SPY"}, tmp_path)
    eng2.start_day(day, {})
    assert book2.buys_today == round(spent, 4) and book2.cash_at_open == 250.0


def _trades(path, cid, n, pnl_pct, day="2026-10-20"):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, ["time", "book", "classifier", "symbol", "side", "qty", "price", "notional", "reason", "pnl", "pnl_pct"])
        w.writeheader()
        for _ in range(n):
            w.writerow({"time": f"{day}T11:00-04:00", "book": "paper", "classifier": cid, "symbol": "SPY", "side": "sell",
                        "qty": 1, "price": 1, "notional": 1, "reason": "x", "pnl": 0.1, "pnl_pct": pnl_pct})


def test_promotion_requires_record_on_current_spec(tmp_path):
    state = tmp_path / "promotion.json"
    alerts = []
    note = lambda lvl, msg: alerts.append(msg)
    s = spec(id="idea", mode="live")
    # first sighting: record starts today, no trades yet -> downgraded
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 19), tmp_path, state)
    assert s.mode == "shadow" and alerts
    # 20 profitable closed trades since then -> allowed live
    _trades(tmp_path / "trades.csv", "idea", 20, 0.3)
    s = spec(id="idea", mode="live")
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 21), tmp_path, state)
    assert s.mode == "live"
    # editing the spec restarts the record -> downgraded again
    s = spec(id="idea", mode="live", stop_pct=0.7)
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 22), tmp_path, state)
    assert s.mode == "shadow"


def test_promotion_rejects_negative_expectancy_and_ignores_mode_only_changes(tmp_path):
    state = tmp_path / "promotion.json"
    note = lambda lvl, msg: None
    golive.enforce_promotion([spec(id="idea", mode="shadow")], note, True, dt.date(2026, 10, 19), tmp_path, state)
    _trades(tmp_path / "trades.csv", "idea", 25, 0.08)  # +0.08% gross, -0.02% after 0.1% round-trip slippage
    s = spec(id="idea", mode="live")  # same spec, only mode changed: record kept
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 21), tmp_path, state)
    assert s.mode == "shadow"
    _trades(tmp_path / "trades.csv", "idea", 25, 0.3)
    s = spec(id="idea", mode="live")
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 22), tmp_path, state)
    assert s.mode == "live"


def test_editing_custom_feature_code_restarts_the_record(tmp_path, monkeypatch):
    from trader import features as F

    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "mine.py").write_text("v1")
    monkeypatch.delitem(F.SOURCES, "my_custom", raising=False)  # sandboxed features never enter SOURCES in-process
    state = tmp_path / "promotion.json"
    note = lambda lvl, msg: None
    s = spec(id="idea", mode="shadow", features=["my_custom"])
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 19), tmp_path, state, custom)
    _trades(tmp_path / "trades.csv", "idea", 25, 0.3)
    s = spec(id="idea", mode="live", features=["my_custom"])
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 21), tmp_path, state, custom)
    assert s.mode == "live"
    (custom / "mine.py").write_text("v2: different logic")
    s = spec(id="idea", mode="live", features=["my_custom"])
    golive.enforce_promotion([s], note, True, dt.date(2026, 10, 22), tmp_path, state, custom)
    assert s.mode == "shadow"
    # a lib-only classifier is unaffected by custom edits
    golive.enforce_promotion([spec(id="plain", mode="shadow")], note, True, dt.date(2026, 10, 19), tmp_path, state, custom)
    before = __import__("json").loads(state.read_text())["plain"]["hash"]
    (custom / "mine.py").write_text("v3")
    golive.enforce_promotion([spec(id="plain", mode="shadow")], note, True, dt.date(2026, 10, 23), tmp_path, state, custom)
    assert __import__("json").loads(state.read_text())["plain"]["hash"] == before


def test_control_classifiers_are_never_downgraded(tmp_path):
    s = ClassifierSpec(**(spec().model_dump() | {"id": "control_x", "control": True, "family": None, "mode": "live"}))
    golive.enforce_promotion([s], lambda *a: None, True, dt.date(2026, 10, 19), tmp_path, tmp_path / "p.json")
    assert s.mode == "live"


def test_orphan_proceeds_are_not_counted_as_settled(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    br = SimBroker(200.0)  # 200 settled; pretend a 50 orphan sale just added unsettled proceeds
    br.cash += 50.0
    book = Book("sim", br, tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    eng.start_day(day, {}, settled_at_open={"sim": 200.0})
    assert book.cash_at_open == 200.0
