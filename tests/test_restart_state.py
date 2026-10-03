"""State that must survive a mid-session restart, and halt clearing (issues #16, #17, #22)."""

import datetime as dt
import json

import pandas as pd

from trader import runner
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine
from trader.nav import NavBook

from test_engine import Always, spec


def make(tmp_path, specs, cash=250.0):
    book = Book("sim", SimBroker(cash), tmp_path / "sim")
    return book, Engine(specs, {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)


def ticks(eng, bars, start, stop):
    day = bars.index[0].date()
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[start:stop]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)


def test_stop_survives_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book, eng = make(tmp_path, [spec(after_exit="rearm", max_trades=5)])
    eng.start_day(day, {})
    ticks(eng, bars, 0, 30)
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": day.isoformat()}))
    ticks(eng, bars, 30, 32)
    assert book.blocked == "stop"
    # crash + restart with fresh objects
    book2, eng2 = make(tmp_path, [spec(after_exit="rearm", max_trades=5)])
    book2.broker = book.broker
    eng2.start_day(day, {})
    assert book2.blocked == "stop"
    ticks(eng2, bars, 32, 200)
    assert not book2.entries
    trades = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert (trades.time > f"{day}T10:05").sum() == 0  # nothing traded after the STOP


def test_stop_from_previous_day_expires(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "sim").mkdir(parents=True)
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": "2020-01-01"}))
    book, eng = make(tmp_path, [spec()])
    eng.start_day(day, {})
    ticks(eng, bars, 0, 20)
    assert book.blocked is None and book.entries


def test_kill_switch_survives_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book, eng = make(tmp_path, [spec()])
    eng.start_day(day, {})
    book.blocked = "kill"
    book.write_risk(blocked_today="kill")
    book2, eng2 = make(tmp_path, [spec()])
    eng2.start_day(day, {})
    assert book2.blocked == "kill"
    ticks(eng2, bars, 0, 60)
    assert not book2.entries


def test_trade_counts_survive_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book, eng = make(tmp_path, [spec(max_trades=1, after_exit="rearm")])
    eng.start_day(day, {})
    ticks(eng, bars, 0, 20)  # enters once (max_trades=1)
    assert eng.states[0].symbols["SPY"].trades == 1
    book.broker.sell_all("SPY", 100.0, None, "x")  # closed while the runner was down
    book2, eng2 = make(tmp_path, [spec(max_trades=1, after_exit="rearm")])
    book2.broker = book.broker
    book2.entries.clear()
    eng2.start_day(day, {})
    st = eng2.states[0].symbols["SPY"]
    assert st.trades == 1 and st.status == "retired"


def _runner_env(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    monkeypatch.setattr(runner.config, "RUNTIME_DIR", tmp_path)


def test_halt_through_engine_then_clear_after_close_unblocks_next_day(tmp_path, session, monkeypatch):
    _runner_env(tmp_path, monkeypatch)
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    br = SimBroker(250.0)
    book = Book("live", br, tmp_path / "books" / "live")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    eng.start_day(day, {})
    ticks(eng, bars, 0, 5)
    br.cash = 100.0  # with the ~$50 position, equity ~$150: -40% from the start, trailing halt
    ticks(eng, bars, 5, 7)
    assert book.blocked == "halt"
    assert "runner is running" in runner.clear_halt("live")  # status.json is fresh: refused
    eng.end_day(dt.datetime.combine(day, dt.time(16), ET))
    import os, time
    os.utime(tmp_path / "status.json", (time.time() - 3600, time.time() - 3600))  # runner exited
    msg = runner.clear_halt("live")
    assert "rebased" in msg
    book2 = Book("live", br, tmp_path / "books" / "live")
    eng2 = Engine([spec()], {"live": book2, "shadow": book2}, Always(), {"SPY"}, tmp_path)
    eng2.start_day(day + dt.timedelta(days=1), {})
    ticks(eng2, bars, 0, 3)
    assert book2.blocked is None


def test_clear_halt_on_unhalted_book_changes_nothing(tmp_path, monkeypatch):
    _runner_env(tmp_path, monkeypatch)
    d = tmp_path / "books" / "live"
    d.mkdir(parents=True)
    nav = NavBook()
    nav.mark(250)
    nav.mark(212)  # -15%, not halted
    nav.save(d / "nav.json")
    assert "not halted" in runner.clear_halt("live")
    assert NavBook.load(d / "nav.json").hwm == 1.0


def test_rebase_paper(tmp_path, monkeypatch):
    _runner_env(tmp_path, monkeypatch)
    d = tmp_path / "books" / "paper"
    d.mkdir(parents=True)
    nav = NavBook()
    nav.mark(100000)
    nav.save(d / "nav.json")
    runner.rebase_paper()
    nav2 = NavBook.load(d / "nav.json")
    assert nav2.units == 0 and nav2.mark(250) == 1.0


def test_replay_rerun_under_same_name_starts_fresh(tmp_path, session):
    from trader.replay import replay

    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    sessions = {"SPY": {day: bars}}
    run = tmp_path / "keep-x"
    r1 = replay([spec()], day, day, Always(), {"SPY"}, run, {}, sessions=sessions)
    (run / "sim" / "risk.json").write_text(json.dumps({"day": day.isoformat(), "day_start_equity": 250, "blocked_today": "kill"}))
    r2 = replay([spec()], day, day, Always(), {"SPY"}, run, {}, sessions=sessions)
    assert r2["days"][day.isoformat()]["sim"]["trades"] == r1["days"][day.isoformat()]["sim"]["trades"] > 0


def test_replay_refuses_to_delete_outside_its_directory(tmp_path, session):
    import pytest
    from trader.replay import replay

    bars = session(path=[100.0] * 30)
    day = bars.index[0].date()
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    replays = tmp_path / "replays"
    replays.mkdir()
    with pytest.raises(ValueError):
        replay([spec()], day, day, Always(), {"SPY"}, replays / "..", {}, sessions={"SPY": {day: bars}})
    link = replays / "evil"
    link.symlink_to(victim)
    with pytest.raises(ValueError):
        replay([spec()], day, day, Always(), {"SPY"}, link, {}, sessions={"SPY": {day: bars}})
    assert (victim / "keep.txt").exists()


def test_cli_rejects_bad_replay_names():
    import pytest
    from trader.cli import main

    for bad in ["..", "../x", "/tmp/x", "a/b", ".hidden"]:
        with pytest.raises(SystemExit):
            main(["replay", "--name", bad, "--decider", "stub"])
