"""Alert noise (#23) and small robustness items (#24)."""

import datetime as dt
from types import SimpleNamespace as NS
from typing import Any, cast

import pandas as pd

from test_engine import Always, spec
from trader import runner
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine


def et(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ET)


def test_strategist_overdue_uses_last_session_not_wall_clock():
    fri_close = et(2026, 10, 9, 16)
    ran_fri_evening = et(2026, 10, 9, 16, 35).timestamp()
    # Monday morning after a normal weekend: last session is Friday, which has its run -> fine
    assert not runner.strategist_overdue(ran_fri_evening, fri_close, et(2026, 10, 12, 9))
    # Friday's post-close run never happened: overdue once past the grace period
    assert not runner.strategist_overdue(et(2026, 10, 9, 8).timestamp(), fri_close, et(2026, 10, 9, 17))
    assert runner.strategist_overdue(et(2026, 10, 9, 8).timestamp(), fri_close, et(2026, 10, 9, 19))


def test_stale_feed_blocks_new_entries(tmp_path, session):
    bars = session(path=[100.0] * 60)
    day = bars.index[0].date()
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    alerts = []
    eng = Engine(
        [spec(window=("09:50", "15:30"))],
        {"live": book, "shadow": book},
        Always(),
        {"SPY"},
        tmp_path,
        alert=lambda level, m: alerts.append(m),
    )
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    frozen = bars.iloc[:10]  # the feed stops delivering bars after 09:39
    for ts in bars.index[10:30]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": frozen}, (close - now).total_seconds() / 60)
    assert not book.entries and any("stale" in a for a in alerts)


def test_cashflows_use_type_for_sign_and_skip_unexecuted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    book = Book("live", SimBroker(250.0), tmp_path / "live")
    book.nav.mark(250.0)
    (tmp_path / "live" / "cashflows.csv").write_text("id,date,type,amount,nav_at_flow\n")  # not the first run
    acts = [
        {"id": "a", "date": "2026-10-20", "activity_type": "CSD", "net_amount": "100", "status": "executed"},
        {
            "id": "b",
            "date": "2026-10-21",
            "activity_type": "CSW",
            "net_amount": "50",
            "status": "executed",
        },  # positive-signed withdrawal
        {"id": "c", "date": "2026-10-22", "activity_type": "CSD", "net_amount": "999", "status": "pending"},
    ]
    broker = NS(client=NS(get=lambda path, params=None: acts))
    runner._apply_cashflows(book, cast(Any, broker))  # a stand-in with just client.get
    rows = (tmp_path / "live" / "cashflows.csv").read_text().splitlines()
    assert [r.split(",")[3] for r in rows[1:]] == ["100.00", "-50.00"]
    assert abs(book.nav.last_equity - 300.0) < 1e-9  # 250 + 100 - 50; pending 999 ignored


def uk(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=runner.LOCAL_TZ)


def test_strategist_deadline_is_on_the_crons_uk_clock():
    never = 0.0
    # Half day (13:00 ET close = 18:00 UK): close + grace would be 20:30 UK, before the timer's first try.
    half = et(2026, 11, 27, 13)
    assert not runner.strategist_overdue(never, half, uk(2026, 11, 27, 20, 30))
    assert not runner.strategist_overdue(never, half, uk(2026, 11, 27, 23, 20))  # 22:30 retry may still be running
    assert runner.strategist_overdue(never, half, uk(2026, 11, 27, 23, 50))
    # DST-mismatch week (US on EDT, UK on GMT): the close is 20:00 UK, so close + grace = the 22:30 retry.
    mismatch = et(2026, 10, 27, 16)
    assert not runner.strategist_overdue(never, mismatch, uk(2026, 10, 27, 22, 31))
    assert runner.strategist_overdue(never, mismatch, uk(2026, 10, 27, 23, 50))


def _watchdog_env(tmp_path, monkeypatch, close=None, fail=False):
    sent = []
    monkeypatch.setattr(runner, "notify", lambda level, msg: sent.append(msg))
    monkeypatch.setattr(runner.config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(runner.config, "STRATEGIST_STAMP", tmp_path / ".last_run")
    monkeypatch.setattr(runner.config, "POSTCLOSE_STAMP", tmp_path / ".last_postclose")

    def cal(*a):
        if fail:
            raise ConnectionError("calendar down")
        return close

    monkeypatch.setattr(runner, "session_info", lambda: None if not fail else cal())
    monkeypatch.setattr(runner, "_last_session_close", cal)
    return sent


def test_watchdog_calendar_failure_is_reported_and_throttled(tmp_path, monkeypatch):
    sent = _watchdog_env(tmp_path, monkeypatch, fail=True)
    t = et(2026, 10, 9, 18)
    assert "calendar lookup failed" in runner.watchdog(t)
    runner.watchdog(t + dt.timedelta(minutes=10))
    assert len(sent) == 1 and "calendar down" in sent[0]
    runner.watchdog(t + dt.timedelta(hours=4))
    assert len(sent) == 2


def test_watchdog_overdue_alerts_once_per_session(tmp_path, monkeypatch):
    fri = et(2026, 10, 9, 16)
    sent = _watchdog_env(tmp_path, monkeypatch, close=fri)
    for h in range(0, 60, 1):  # all weekend, hourly
        runner.watchdog(uk(2026, 10, 10, 0) + dt.timedelta(hours=h))
    assert len(sent) == 1 and "Fri 09 Oct" in sent[0]


def test_watchdog_postclose_stamp_and_rollout_fallback(tmp_path, monkeypatch):
    import os

    fri = et(2026, 10, 9, 16)
    sent = _watchdog_env(tmp_path, monkeypatch, close=fri)
    after = et(2026, 10, 9, 16, 40).timestamp()
    # Rollout: no per-kind stamp yet, but the any-kind stamp shows Friday's run -> quiet.
    (tmp_path / ".last_run").touch()
    os.utime(tmp_path / ".last_run", (after, after))
    assert runner.watchdog(uk(2026, 10, 10, 9)) == "ok"
    # Once the per-kind stamp exists, a later premarket/weekly touch of .last_run can't mask a missed post-close.
    (tmp_path / ".last_postclose").touch()
    before = et(2026, 10, 8, 16, 40).timestamp()
    os.utime(tmp_path / ".last_postclose", (before, before))
    assert "no post-close strategist run" in runner.watchdog(uk(2026, 10, 10, 9))
    assert len(sent) == 1


def test_stale_feed_when_stream_never_delivers(tmp_path, session):
    bars = session(path=[100.0] * 60)
    day = bars.index[0].date()
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    alerts = []
    eng = Engine(
        [spec(window=("09:30", "15:30"))],
        {"live": book, "shadow": book},
        Always(),
        {"SPY"},
        tmp_path,
        alert=lambda level, m: alerts.append(m),
    )
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    open_ = dt.datetime.combine(day, dt.time(9, 30), ET)
    eng.tick(open_ + dt.timedelta(minutes=2), {}, (close - open_).total_seconds() / 60)
    assert not any("stale" in a for a in alerts)  # too early to expect a bar
    eng.tick(open_ + dt.timedelta(minutes=6), {}, (close - open_).total_seconds() / 60)
    assert any("no SPY bars" in a for a in alerts)


def test_cashflows_count_corrected_transfers(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    book = Book("live", SimBroker(250.0), tmp_path / "live")
    book.nav.mark(250.0)
    (tmp_path / "live" / "cashflows.csv").write_text("id,date,type,amount,nav_at_flow\n")
    acts = [{"id": "a", "date": "2026-10-20", "activity_type": "CSD", "net_amount": "40", "status": "correct"}]
    runner._apply_cashflows(book, cast(Any, NS(client=NS(get=lambda path, params=None: acts))))
    assert abs(book.nav.last_equity - 290.0) < 1e-9


def test_replay_reports_why_a_day_had_no_entries(tmp_path, session):
    from trader.replay import replay

    d1, d2 = dt.date(2026, 9, 21), dt.date(2026, 9, 22)
    qqq = {d: session(day=d, path=[100.0] * 390) for d in (d1, d2)}
    spy = {d1: session(day=d1, path=[100.0] * 390)}  # SPY data gap on d2
    s = spec(symbols=["QQQ"])
    r = replay([s], d1, d2, Always(), {"SPY", "QQQ"}, tmp_path / "x", {}, sessions={"SPY": spy, "QQQ": qqq})
    assert "alerts" not in r["days"][d1.isoformat()]
    assert r["days"][d2.isoformat()]["sim"]["trades"] == 0
    assert any("no SPY bars" in a for a in r["days"][d2.isoformat()]["alerts"])
