"""The calendar API down at runner start (#125): a same-day restart reuses the session times
persisted by an earlier start today; otherwise the runner raises as before, alerting hourly."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader import config, golive, runner
from trader.broker import SimBroker
from trader.data import ET


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(runner, "CALENDAR_RETRY_S", 0.0, raising=False)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "notify", lambda level, msg, **k: sent.append(msg))
    return sent


class Calendar:
    """A trading client whose calendar answers with today's session, or errors while `down`."""

    def __init__(self, down=False, close=dt.time(16, 0)):
        self.down, self.calls, self.close = down, 0, close

    def get_calendar(self, req):
        self.calls += 1
        if self.down:
            raise ConnectionError("calendar 503")
        today = dt.datetime.now(ET).date()
        return [
            SimpleNamespace(
                date=today,
                open=dt.datetime.combine(today, dt.time(9, 30)),
                close=dt.datetime.combine(today, self.close),
            )
        ]


def save(tmp_path, day, open_="09:30", close="16:00"):
    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "day": day.isoformat(),
                "open": dt.datetime.combine(day, dt.time.fromisoformat(open_), ET).isoformat(),
                "close": dt.datetime.combine(day, dt.time.fromisoformat(close), ET).isoformat(),
            }
        )
    )


def test_same_day_restart_with_the_calendar_down_reuses_the_saved_half_day(tmp_path, alerts):
    first = runner._session_for_run(Calendar(close=dt.time(13, 0)))  # the morning start: a half day
    down = Calendar(down=True)
    again = runner._session_for_run(down)  # a mid-session restart during an outage
    assert again is not None and again == first and again[1].hour == 13 and again[1].tzinfo is not None
    assert down.calls == runner.CALENDAR_TRIES
    assert len(alerts) == 1 and "reusing today's saved session times" in alerts[0] and "13:00" in alerts[0]


def test_another_days_saved_session_is_never_reused(tmp_path, alerts):
    save(tmp_path, dt.datetime.now(ET).date() - dt.timedelta(days=1))
    with pytest.raises(ConnectionError):
        runner._session_for_run(Calendar(down=True))
    assert len(alerts) == 1 and "can't start" in alerts[0]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"day": 1}',
        "",
        json.dumps({"day": "TODAY", "open": "2026-01-01T09:30:00", "close": "2026-01-01T16:00:00"}),  # naive
        json.dumps({"day": "TODAY", "open": "TODAY 16:00-04:00", "close": "TODAY 09:30-04:00"}),  # close first
    ],
)
def test_a_corrupt_saved_session_counts_as_absent(tmp_path, alerts, content):
    today = dt.datetime.now(ET).date().isoformat()
    (tmp_path / "session.json").write_text(content.replace("TODAY", today))
    assert runner._saved_session(dt.datetime.now(ET).date()) is None
    with pytest.raises(ConnectionError):
        runner._session_for_run(Calendar(down=True))


def test_a_holiday_is_only_decided_by_the_calendar(tmp_path, alerts):
    class Holiday(Calendar):
        def get_calendar(self, req):
            return []

    assert runner._session_for_run(Holiday()) is None
    assert not (tmp_path / "session.json").exists()  # nothing saved, so an outage later can't reuse it
    with pytest.raises(ConnectionError):
        runner._session_for_run(Calendar(down=True))


def test_the_calendar_down_alert_is_throttled_across_restarts(tmp_path, alerts):
    for _ in range(5):  # systemd restarts every 30 s
        with pytest.raises(ConnectionError):
            runner._session_for_run(Calendar(down=True))
    assert len(alerts) == 1
    state = tmp_path / "calendar_alerts.json"
    state.write_text(json.dumps({"down": json.loads(state.read_text())["down"] - runner.CALENDAR_ALERT_EVERY_S - 1}))
    with pytest.raises(ConnectionError):
        runner._session_for_run(Calendar(down=True))
    assert len(alerts) == 2
    state.write_text("garbage")  # an unreadable throttle file alerts rather than raising
    with pytest.raises(ConnectionError):
        runner._session_for_run(Calendar(down=True))
    assert len(alerts) == 3


class _Reached(Exception):
    pass


def test_run_session_restarted_mid_session_runs_on_the_saved_times(tmp_path, monkeypatch, alerts):
    now = dt.datetime.now(ET)
    (tmp_path / "session.json").write_text(
        json.dumps(
            {"day": now.date().isoformat(), "open": now.isoformat(), "close": (now + dt.timedelta(hours=1)).isoformat()}
        )
    )

    def broker(*a, **k):
        b = SimBroker(250.0)
        monkeypatch.setattr(b, "client", Calendar(down=True), raising=False)  # runner reads the calendar from it
        return b

    class Stream:
        def __init__(self, *a, **k):
            raise _Reached  # the session got as far as streaming bars

    import alpaca.data.live

    monkeypatch.setattr(config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"})
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "AlpacaBroker", broker)
    monkeypatch.setattr(runner, "_load_specs", lambda *a, **k: [])
    monkeypatch.setattr(runner, "fetch_alpaca", lambda *a, **k: {})
    monkeypatch.setattr(runner, "reconcile_sim_accounts", lambda *a, **k: None)
    monkeypatch.setattr(golive, "resolve_mode", lambda *a, **k: "paper")
    monkeypatch.setattr(golive, "enforce_promotion", lambda specs, *a, **k: specs)
    monkeypatch.setattr(alpaca.data.live, "StockDataStream", Stream)
    with pytest.raises(_Reached):
        runner.run_session("stub")
    assert any("reusing today's saved session times" in a for a in alerts)
