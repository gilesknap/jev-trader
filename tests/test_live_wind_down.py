"""A paper day with live positions left over (a mid-session HOLD LIVE or clear-halt, then a
restart; or a mode.yaml switch): the live book winds down instead of going unmanaged."""

import datetime as dt
import json

import pandas as pd
import pytest

from test_engine import Always, spec
from test_orphans import Broker, _at, _bars
from trader import config, golive, runner
from trader.broker import Position
from trader.data import ET
from trader.engine import WIND_DOWN, Book, Engine, Entry

DAY = dt.date(2026, 9, 21)


def trades(d):
    p = d / "trades.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["book", "side", "symbol", "reason"])


def test_a_wound_down_live_book_closes_everything_and_never_enters(tmp_path, session):
    paper = Book("paper", Broker(), tmp_path / "paper")
    live_br = Broker()
    live = Book("live", live_br, tmp_path / "live")
    live_br.positions = {"SPY": Position("SPY", 0.5, 100.0), "QQQ": Position("QQQ", 0.2, 400.0)}
    live.entries["SPY"] = Entry("t", 0.5, 100.0, 90.0, 120.0, _at(9, 0))  # tracked; QQQ is untracked
    live.blocked = WIND_DOWN
    alerts = []
    eng = Engine([spec(symbols=["SPY", "QQQ"], max_trades=5)], {"live": paper, "shadow": paper,
                 runner.WIND_DOWN_KEY: live}, Always(), {"SPY", "QQQ"}, tmp_path / "rt",
                 alert=lambda lvl, msg: alerts.append(msg))
    eng.start_day(DAY, {})
    assert live.blocked == WIND_DOWN  # start_day leaves it in place
    for m in range(31, 40):
        now = _at(9, m)
        eng.tick(now, _bars(session, now, SPY=[100.0] * 390, QQQ=[410.0] * 390), 360)
    assert not live_br.positions and not live.entries
    t = trades(live.dir)
    assert list(t.side) == ["sell"] and list(t.reason) == ["wind-down (paper today)"]
    assert any("closed untracked position QQQ" in m for m in alerts)
    assert (trades(paper.dir).side == "buy").any()  # the paper book trades as normal


class FakeLive(Broker):
    def __init__(self, held=None, fail=False):
        super().__init__()
        self.client, self.fail = None, fail
        self.positions = {s: Position(s, 0.5, 100.0) for s in (held or [])}

    def get_positions(self):
        if self.fail:
            raise ConnectionError("positions 503")
        return super().get_positions()


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "_apply_cashflows", lambda *a: None)
    return tmp_path / "books" / "live", alerts, lambda lvl, msg: alerts.append((lvl, msg))


SECRETS = {"ALPACA_LIVE_KEY": "k", "ALPACA_LIVE_SECRET": "s"}


def test_nothing_held_means_no_wind_down(live_env, monkeypatch):
    _, alerts, alert = live_env
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive())
    assert runner.wind_down_live_book(SECRETS, alert) is None and not alerts
    assert not (runner.BOOKS_DIR / "live").exists()  # a never-live system gets no empty live book
    assert runner.wind_down_live_book({}, alert) is None  # no live keys at all


def test_live_positions_on_a_paper_day_wind_down(live_env, monkeypatch):
    _, alerts, alert = live_env
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive(["XLV"]))
    b = runner.wind_down_live_book(SECRETS, alert)
    assert b.name == "live" and b.blocked == WIND_DOWN
    assert len(alerts) == 1 and alerts[0][0] == "urgent" and "['XLV']" in alerts[0][1]


def test_tracked_entries_or_an_unreadable_account_wind_down_too(live_env, monkeypatch):
    live_dir, alerts, alert = live_env
    live_dir.mkdir(parents=True)
    (live_dir / "entries.json").write_text(json.dumps({"SPY": {
        "classifier": "t", "qty": 0.5, "price": 100.0, "stop": 90.0, "target": 120.0, "time": _at(9, 0).isoformat()}}))
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive())  # gone at the broker: still reconciled
    assert runner.wind_down_live_book(SECRETS, alert).blocked == WIND_DOWN
    assert any("still tracks ['SPY']" in m for _, m in alerts)
    (live_dir / "entries.json").unlink()
    alerts.clear()
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive(fail=True))
    assert runner.wind_down_live_book(SECRETS, alert).blocked == WIND_DOWN
    assert any("couldn't be read" in m for _, m in alerts)


def test_a_halted_live_book_stays_halted(live_env, monkeypatch):
    live_dir, _, alert = live_env
    live_dir.mkdir(parents=True)
    (live_dir / "risk.json").write_text(json.dumps({"halted": True}))
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive(["XLV"]))
    assert runner.wind_down_live_book(SECRETS, alert).blocked == "halt"


def test_an_unopenable_live_book_alerts_and_never_raises(live_env, monkeypatch):
    _, alerts, alert = live_env
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad key")))
    assert runner.wind_down_live_book(SECRETS, alert) is None
    assert alerts[0][0] == "urgent" and "unmanaged" in alerts[0][1]


def test_unreadable_tracking_files_are_set_aside_and_everything_closes_as_untracked(live_env, monkeypatch):
    live_dir, alerts, alert = live_env
    live_dir.mkdir(parents=True)
    (live_dir / "entries.json").write_text("{torn")
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive(["XLV"]))
    b = runner.wind_down_live_book(SECRETS, alert)
    assert b.blocked == WIND_DOWN and not b.entries and not (live_dir / "entries.json").exists()
    assert [p.name.split(".corrupt-")[0] for p in live_dir.glob("*.corrupt-*")] == ["entries.json"]
    assert any("set aside" in m for _, m in alerts) and any("still holds ['XLV']" in m for _, m in alerts)


def test_an_unreadable_live_equity_at_the_close_doesnt_stop_the_session_end(tmp_path):
    class NoEquity(Broker):
        def equity(self):
            raise ConnectionError("account 503")

    paper = Book("paper", Broker(), tmp_path / "paper")
    live = Book("live", NoEquity(), tmp_path / "live")
    alerts = []
    eng = Engine([], {"live": paper, "shadow": paper, runner.WIND_DOWN_KEY: live}, Always(), {"SPY"},
                 tmp_path / "rt", alert=lambda lvl, msg: alerts.append(msg))
    eng.day = DAY
    summary = eng.end_day(dt.datetime.combine(DAY, dt.time(16), ET))
    assert list(summary) == ["paper"] and any("[live] equity unreadable at the close" in m for m in alerts)


def test_a_restart_after_a_mid_session_hold_winds_the_live_book_down(tmp_path, monkeypatch):
    """The restart scenario end to end through run_session: go-live HELD (so paper), live account
    still holding: the engine gets the live book, wound down, beside the paper book."""
    import alpaca.data.live

    from test_start_equity import Flaky, _Reached

    now = dt.datetime.now(ET)
    alerts, made = [], {}
    monkeypatch.setattr(golive, "STATE_FILE", tmp_path / "golive.json")
    golive.save_state({"status": "vetoed", "vetoed_from": "live"})
    monkeypatch.setattr(golive.config, "MODE_FILE", tmp_path / "mode.yaml")  # absent: auto
    monkeypatch.setattr(config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"} | SECRETS)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "notify", lambda level, msg, **k: alerts.append(msg))
    monkeypatch.setattr(runner, "AlpacaBroker", lambda key, secret, paper: Flaky(250.0, down=False) if paper
                        else made.setdefault("live", FakeLive(["XLV"])))
    monkeypatch.setattr(runner, "_apply_cashflows", lambda *a: None)
    monkeypatch.setattr(runner, "_session_today", lambda client: (now - dt.timedelta(hours=1), now + dt.timedelta(hours=1)))
    monkeypatch.setattr(runner, "_specs_for_session", lambda *a, **k: [])
    monkeypatch.setattr(runner, "fetch_alpaca", lambda *a, **k: {})
    monkeypatch.setattr(runner, "reconcile_sim_accounts", lambda *a, **k: None)
    seen = {}

    class Stream:
        def __init__(self, *a, **k):
            pass

        def subscribe_bars(self, handler, *symbols):
            seen["symbols"] = symbols
            raise _Reached

    real_engine = runner.Engine

    def engine(specs, books, *a, **k):
        seen["books"] = books
        return real_engine(specs, books, *a, **k)
    monkeypatch.setattr(runner, "Engine", engine)
    monkeypatch.setattr(alpaca.data.live, "StockDataStream", Stream)
    with pytest.raises(_Reached):
        runner.run_session("stub")
    books = seen["books"]
    assert books["live"] is books["shadow"] and books["live"].name == "paper"
    live = books[runner.WIND_DOWN_KEY]
    assert live.name == "live" and live.broker is made["live"] and live.blocked == WIND_DOWN
    assert "XLV" in seen["symbols"]  # streamed, so the close has prices
    assert any("still holds ['XLV']" in a for a in alerts)


def test_a_corrupt_nav_or_risk_file_never_sets_healthy_tracking_aside(live_env, monkeypatch):
    live_dir, alerts, alert = live_env
    live_dir.mkdir(parents=True)
    entries = json.dumps({"SPY": {"classifier": "t", "qty": 0.5, "price": 100.0, "stop": 90.0, "target": 120.0,
                                  "time": _at(9, 0).isoformat()}})
    (live_dir / "entries.json").write_text(entries)
    (live_dir / "nav.json").write_text("{torn")
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive(["SPY"]))
    assert runner.wind_down_live_book(SECRETS, alert) is None
    assert (live_dir / "entries.json").read_text() == entries and not list(live_dir.glob("*.corrupt-*"))
    assert len(alerts) == 1 and "unmanaged" in alerts[0][1] and "['SPY']" in alerts[0][1]


def test_only_the_unloadable_tracking_file_is_set_aside(live_env, monkeypatch):
    live_dir, alerts, alert = live_env
    live_dir.mkdir(parents=True)
    (live_dir / "entries.json").write_text(json.dumps({"SPY": {"classifier": "t", "qty": 0.5, "price": 100.0,
                                                               "stop": 90.0, "target": 120.0, "time": "not a time"}}))
    (live_dir / "pending.json").write_text("{}")
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: FakeLive())
    b = runner.wind_down_live_book(SECRETS, alert)
    assert b.blocked == WIND_DOWN and (live_dir / "pending.json").exists()
    assert [p.name.split(".corrupt-")[0] for p in live_dir.glob("*.corrupt-*")] == ["entries.json"]
    assert any("holds no positions now" in m for _, m in alerts)
