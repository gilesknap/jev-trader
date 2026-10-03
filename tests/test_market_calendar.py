"""One exchange calendar for the runner, replays, the feature gate and probe scoring: an early
close (13:00 ET) flattens at 12:45 and stops entries everywhere, and no bar or outcome after
the close counts."""

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from conftest import make_session
from test_engine import Always, spec
from trader import probe, runner
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine
from trader.features import harness
from trader.market_calendar import Calendar, Session, fetch_calendar, load_calendar, regular_session
from trader.replay import replay

HALF = dt.date(2026, 11, 27)  # the day after Thanksgiving: 13:00 close
HALF_CAL = Calendar(
    {HALF: Session(dt.datetime.combine(HALF, dt.time(9, 30), ET), dt.datetime.combine(HALF, dt.time(13), ET))}
)


def half_day_bars():
    """390 flat bars: post-market prints after 13:00 included, as split_sessions returns them."""
    return make_session(day=HALF, path=[100.0] * 390)


def trades_of(path):
    return pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["side", "time", "reason"])


def runner_style(tmp_path, bars, calendar):
    """The runner's loop: a tick each minute after the open, none at or after the calendar's close."""
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine([spec(window=("09:35", "15:30"))], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    s = calendar.session(bars.index[0].date())
    eng.start_day(s.day, {})
    tick = s.open + dt.timedelta(minutes=1)
    while tick < s.close:
        live = bars[bars.index < tick]
        eng.tick(tick, {"SPY": live}, (s.close - tick).total_seconds() / 60)
        tick += dt.timedelta(minutes=1)
    eng.end_day(s.close)
    return trades_of(tmp_path / "sim" / "trades.csv")


def test_replay_flattens_at_1245_on_an_early_close_and_never_trades_after(tmp_path):
    out = replay(
        [spec(window=("09:35", "15:30"))],
        HALF,
        HALF,
        Always(),
        {"SPY"},
        tmp_path / "r",
        {},
        sessions={"SPY": {HALF: half_day_bars()}},
        calendar=HALF_CAL,
    )
    t = trades_of(tmp_path / "r" / "sim" / "trades.csv")
    assert list(t.side) == ["buy", "sell"] and t.reason.iloc[-1] == "eod flatten"
    assert t.time.iloc[-1].startswith(f"{HALF}T12:45")
    assert all(x < f"{HALF}T12:45" for x in t[t.side == "buy"].time)
    assert "calendar" not in out  # a real calendar: no assumption to report


def test_replay_and_the_runner_loop_agree_on_an_early_close(tmp_path):
    replay(
        [spec(window=("09:35", "15:30"))],
        HALF,
        HALF,
        Always(),
        {"SPY"},
        tmp_path / "r",
        {},
        sessions={"SPY": {HALF: half_day_bars()}},
        calendar=HALF_CAL,
    )
    live = runner_style(tmp_path / "live", half_day_bars(), HALF_CAL)
    replayed = trades_of(tmp_path / "r" / "sim" / "trades.csv")
    assert list(live.side) == list(replayed.side) == ["buy", "sell"]
    assert list(live.time) == list(replayed.time) and list(live.reason) == list(replayed.reason)


def test_after_the_flatten_no_entry_on_an_early_close(tmp_path):
    """A rule that only looks after 12:45 never enters on a 13:00 close."""
    replay(
        [spec(window=("12:46", "15:30"))],
        HALF,
        HALF,
        Always(),
        {"SPY"},
        tmp_path / "r",
        {},
        sessions={"SPY": {HALF: half_day_bars()}},
        calendar=HALF_CAL,
    )
    assert trades_of(tmp_path / "r" / "sim" / "trades.csv").empty


def test_a_full_session_is_unchanged(tmp_path):
    day = dt.date(2026, 9, 21)
    bars = make_session(day=day, path=[100.0] * 390)
    replay(
        [spec()],
        day,
        day,
        Always(),
        {"SPY"},
        tmp_path / "r",
        {},
        sessions={"SPY": {day: bars}},
        calendar=Calendar({day: regular_session(day)}),
    )
    t = trades_of(tmp_path / "r" / "sim" / "trades.csv")
    assert t.time.iloc[-1].startswith(f"{day}T15:45")
    assert list(runner_style(tmp_path / "live", bars, Calendar()).time) == list(t.time)


def test_without_keys_replay_assumes_regular_sessions_and_says_so(tmp_path):
    day = dt.date(2026, 9, 21)
    out = replay(
        [spec()],
        day,
        day,
        Always(),
        {"SPY"},
        tmp_path / "r",
        {},
        sessions={"SPY": {day: make_session(day=day, path=[100.0] * 390)}},
    )
    assert "assumed" in out["calendar"]
    assert load_calendar({}, day, day).assumed


def rows_of(calendar_rows):
    return SimpleNamespace(get_calendar=lambda req: calendar_rows)


@pytest.mark.parametrize(
    "day,offset",
    [(dt.date(2026, 10, 30), -4), (dt.date(2026, 11, 2), -5), (dt.date(2026, 3, 6), -5), (dt.date(2026, 3, 9), -4)],
)
def test_alpaca_rows_are_et_wall_times_across_dst(day, offset):
    row = SimpleNamespace(
        date=day, open=dt.datetime.combine(day, dt.time(9, 30)), close=dt.datetime.combine(day, dt.time(16))
    )
    s = fetch_calendar(rows_of([row]), day, day).session(day)
    assert s.open.utcoffset() == dt.timedelta(hours=offset) and s.open.time() == dt.time(9, 30)
    assert s.flatten_at.time() == dt.time(15, 45) and s.minutes == 390


def test_runner_session_and_replay_share_the_parsing(monkeypatch):
    today = dt.datetime.now(ET).date()
    row = SimpleNamespace(
        date=today, open=dt.datetime.combine(today, dt.time(9, 30)), close=dt.datetime.combine(today, dt.time(13))
    )
    open_, close = runner._session_today(rows_of([row]))
    assert (open_, close) == (
        fetch_calendar(rows_of([row]), today, today).session(today).open,
        fetch_calendar(rows_of([row]), today, today).session(today).close,
    )
    assert close.hour == 13 and close.tzinfo is not None


def test_trim_drops_post_close_prints():
    per = HALF_CAL.trim({HALF: half_day_bars()})
    assert per[HALF].index[-1].time() == dt.time(12, 59) and len(per[HALF]) == 210
    day = dt.date(2026, 9, 21)
    assert len(HALF_CAL.trim({day: make_session(day=day)})[day]) == 390  # not covered: regular


def test_runner_recent_calendar_failure_alerts_and_falls_back():
    alerts = []

    class Down:
        def get_calendar(self, req):
            raise ConnectionError("503")

    cal = runner._recent_calendar(Down(), HALF, lambda level, msg: alerts.append(msg))
    assert len(alerts) == 1 and cal.session(HALF).close.time() == dt.time(16)


def test_probe_horizons_stop_at_an_early_close_flatten_and_say_so():
    bars = make_session(day=HALF, path=list(np.linspace(100, 139, 390)))
    rows = pd.DataFrame(
        [{"day": HALF.isoformat(), "t": t, "c": "p", "s": "SPY", "p_enter": 0.5} for t in ("10:01", "12:30", "12:50")]
    )
    out = probe.forward_returns(rows, {"SPY": HALF_CAL.trim({HALF: bars})}, [15, 60], HALF_CAL)
    at = lambda hhmm: float(bars.close[bars.index.strftime("%H:%M") == hhmm].iloc[0])
    assert out.fwd_15[0] == pytest.approx((at("10:15") / at("10:00") - 1) * 100) and not out.cut_15[0]
    assert out.fwd_60[1] == pytest.approx((at("12:44") / at("12:29") - 1) * 100) and out.cut_60[1]
    assert not out.cut_15[1]
    assert np.isnan(out.fwd_15[2])  # asked after the flatten: nothing to score
    rep = probe.score(pd.concat([out] * 3, ignore_index=True), [60])
    assert rep["p"]["horizons"][60]["cut_n"] == 3


def test_gate_clock_follows_the_samples_own_session():
    seen = {}
    from trader import features as F

    def record(bars, c):
        seen[bars.index[-1].time()] = (c.minutes_since_open, c.minutes_to_close)
        return 0.0

    F.REGISTRY["clock_probe2"] = record
    try:
        half = HALF_CAL.trim({HALF: half_day_bars()})[HALF]
        sparse = half.drop(half.index[40:45])
        harness.evaluate(["clock_probe2"], [(sparse, sparse, sparse)])
    finally:
        F.REGISTRY.pop("clock_probe2")
    assert seen[dt.time(10, 30)] == (61, 149)  # by timestamp, not row: five bars before it are missing
    assert seen[dt.time(12, 59)] == (210, 0)


def test_runner_prior_session_is_cut_at_an_early_close(monkeypatch):
    """The day after a 13:00 close: prior-day high, low and close are the session's, not post-market prints."""
    after = make_session(day=HALF, path=[100.0] * 210 + [120.0] * 180)  # a post-market spike
    nxt = dt.date(2026, 11, 30)
    monkeypatch.setattr(
        runner, "fetch_alpaca", lambda symbols, start, end, secrets, feed="sip": {s: after for s in symbols}
    )
    now = dt.datetime.combine(nxt, dt.time(9, 20), ET)
    prev = runner.prev_day_bars(["SPY"], nxt, now, {}, lambda *a: pytest.fail(str(a)), HALF_CAL)["SPY"]
    assert prev.index[-1].time() == dt.time(12, 59) and prev.high.max() < 101 and prev.close.iloc[-1] == 100.0
    uncut = runner.prev_day_bars(["SPY"], nxt, now, {}, lambda *a: pytest.fail(str(a)))["SPY"]
    assert uncut.close.iloc[-1] == 120.0  # what an unknown calendar would have used
