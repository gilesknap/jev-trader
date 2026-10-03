"""The half-size live week: every return to live starts a fresh one (runner.count_live_session)."""

import datetime as dt
import json

import pandas as pd
import pytest

from test_engine import Always, spec
from test_golive import env, write_book  # noqa: F401 (env: fixture)
from trader import golive, runner
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine

DAYS = [dt.date(2026, 11, 2) + dt.timedelta(days=i) for i in range(30)]
DAYS = [d for d in DAYS if d.weekday() < 5]


def risk(live_dir) -> dict:
    p = live_dir / "risk.json"
    return json.loads(p.read_text()) if p.exists() else {}


def start(day, live_dir, alerts=None) -> str:
    """What run_session does at a session start: resolve the mode, then count the session."""
    note = lambda lvl, msg: (alerts if alerts is not None else []).append(msg)
    mode = golive.resolve_mode(note, lambda: 1000.0, session=day)
    runner.count_live_session(mode, day, live_dir, note)
    return mode


def due():
    """Armed with the veto window run out: the next session start goes live (the gate still passes)."""
    golive.save_state({"status": "armed", "sessions_left": 0, "armed_on": DAYS[0].isoformat()})


def test_live_sessions_count_once_each_even_across_restarts(tmp_path):
    live = tmp_path / "live"
    for d in DAYS[:3]:
        runner.count_live_session("live", d, live)
        runner.count_live_session("live", d, live)  # a restart: the same session
    assert risk(live)["live_sessions"] == 3 and risk(live)["last_live_session"] == DAYS[2].isoformat()


def test_a_return_after_a_runner_demotion_starts_a_fresh_half_size_week(env, tmp_path):
    book, _ = env
    write_book(book)
    live = tmp_path / "books" / "live"
    due()
    for d in DAYS[:7]:
        assert start(d, live) == "live"
    assert risk(live)["live_sessions"] == 7  # past the half-size week
    golive.after_session(lambda *a: None, live_book_halted=True, session=DAYS[6])
    assert golive.load_state()["status"] == "demoted"
    assert start(DAYS[7], live) == "paper"
    assert risk(live)["live_sessions"] == 0
    golive.release(lambda *a: None)
    due()  # stands in for the gate passing again and a full veto window
    assert start(DAYS[8], live) == "live" and risk(live)["live_sessions"] == 1
    assert start(DAYS[9], live) == "live" and risk(live)["live_sessions"] == 2


def test_a_return_after_a_hold_live_veto_starts_a_fresh_half_size_week(env, tmp_path):
    book, _ = env
    write_book(book)
    live = tmp_path / "books" / "live"
    due()
    for d in DAYS[:6]:
        start(d, live)
    golive.hold(lambda *a: None, by="test")
    assert start(DAYS[6], live) == "paper" and risk(live)["live_sessions"] == 0
    golive.release(lambda *a: None)
    due()
    assert start(DAYS[7], live) == "live" and risk(live)["live_sessions"] == 1


def test_a_hold_and_restart_on_the_same_day_ends_the_stint(env, tmp_path):
    book, _ = env
    write_book(book)
    live = tmp_path / "books" / "live"
    due()
    for d in DAYS[:6]:
        start(d, live)
    golive.hold(lambda *a: None, by="test")
    assert start(DAYS[5], live) == "paper"  # the runner restarted after the HOLD, same session
    golive.release(lambda *a: None)
    due()
    assert start(DAYS[6], live) == "live" and risk(live)["live_sessions"] == 1


def test_a_mode_override_through_paper_also_restarts_the_week(env, tmp_path):
    _, mode = env
    live = tmp_path / "books" / "live"
    mode.write_text("mode: live\n")
    for d in DAYS[:6]:
        assert start(d, live) == "live"
    mode.write_text("mode: paper\n")
    assert start(DAYS[6], live) == "paper" and risk(live)["live_sessions"] == 0
    mode.write_text("mode: live\n")
    assert start(DAYS[7], live) == "live" and risk(live)["live_sessions"] == 1


def test_the_reset_keeps_the_rest_of_the_live_books_risk_state(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "risk.json").write_text(json.dumps({"halted": True, "reason": "x", "live_sessions": 9,
                                                "last_live_session": DAYS[0].isoformat()}))
    runner.count_live_session("paper", DAYS[1], live)
    assert risk(live) == {"halted": True, "reason": "x", "live_sessions": 0}


def test_a_paper_session_never_creates_a_live_book_or_fails_on_its_risk_file(tmp_path):
    runner.count_live_session("paper", DAYS[0], tmp_path / "live")
    assert not (tmp_path / "live").exists()
    live = tmp_path / "live"
    live.mkdir()
    (live / "risk.json").write_text("{not json")
    alerts = []
    runner.count_live_session("paper", DAYS[0], live, lambda lvl, msg: alerts.append((lvl, msg)))
    assert alerts and alerts[0][0] == "urgent" and "live_sessions" in alerts[0][1]


def _first_buy(tmp_path, session, live_sessions):
    d = tmp_path / str(live_sessions)
    d.mkdir()
    (d / "risk.json").write_text(json.dumps({"live_sessions": live_sessions}))
    book = Book("live", SimBroker(250.0), d)
    bars = session(path=[100.0] * 30)
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, d / "runtime")
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    t = pd.read_csv(d / "trades.csv")
    return float(t[t.side == "buy"].notional.iloc[0])


def test_the_engine_trades_the_first_five_live_sessions_at_half_size(tmp_path, session):
    assert _first_buy(tmp_path, session, 1) == 25.0  # 20% of 250, halved
    assert _first_buy(tmp_path, session, 5) == 25.0
    assert _first_buy(tmp_path, session, 6) == 50.0


def test_run_session_resets_the_count_on_a_paper_session(tmp_path, monkeypatch):
    import alpaca.data.live

    from test_start_equity import Flaky, _Reached
    from trader import config

    class Stream:
        def __init__(self, *a, **k):
            raise _Reached  # the session got as far as streaming bars

    now = dt.datetime.now(ET)
    live = tmp_path / "books" / "live"
    live.mkdir(parents=True)
    (live / "risk.json").write_text(json.dumps({"live_sessions": 9, "last_live_session": "2026-11-02"}))
    monkeypatch.setattr(config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"})
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: Flaky(250.0))
    monkeypatch.setattr(runner, "_session_today", lambda client: (now - dt.timedelta(hours=1), now + dt.timedelta(hours=1)))
    monkeypatch.setattr(runner, "_load_specs", lambda *a, **k: [])
    monkeypatch.setattr(runner, "fetch_alpaca", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setattr(runner, "reconcile_sim_accounts", lambda *a, **k: None)
    monkeypatch.setattr(golive, "resolve_mode", lambda *a, **k: "paper")
    monkeypatch.setattr(golive, "enforce_promotion", lambda specs, *a, **k: specs)
    monkeypatch.setattr(alpaca.data.live, "StockDataStream", Stream)
    with pytest.raises(_Reached):
        runner.run_session("stub")
    assert risk(live) == {"live_sessions": 0}
