"""The runner's start-of-day custom-feature gate counts sessions, not calendar days (no network)."""

import datetime as dt

import pandas as pd
import pytest

from conftest import make_session
from trader import runner
from trader.data import ET, GateSampleError, gate_samples


def fake_fetch(trading_days, calls):
    """fetch_alpaca over a fixed set of trading days, honouring its [start, end] window (bars
    labelled before `end`); like the real one, no bars at all is {}."""

    def fetch(symbols, start, end, secrets, feed="sip"):
        calls.append((start, end))
        days = [d for d in trading_days if start.date() <= d <= end.date()]
        if not days:
            return {}
        bars = pd.concat([make_session(d) for d in days])
        return {s: bars[bars.index < end] for s in symbols}

    return fetch


@pytest.fixture
def gated(monkeypatch):
    seen = []

    def gate(directory, samples, alert=None):
        seen.append(samples)
        return type("R", (), {"errors": {}})()

    monkeypatch.setattr("trader.features.harness.run_gate", gate)
    monkeypatch.setattr("trader.classifier.load_specs_report", lambda *a, **k: ([], {}))
    return seen


def test_runner_gate_widens_past_a_run_of_closed_days(monkeypatch, gated):
    today = dt.datetime.now(ET).date()
    trading = [today - dt.timedelta(days=n) for n in (32, 31, 30)]  # nothing in the last 7 days
    calls = []
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch(trading, calls))
    runner._load_specs("unused.yaml", {})
    assert len(calls) > 1 and len(gated) == 1
    samples = gated[0]
    assert len(samples) == 4  # SPY and QQQ, each over two consecutive-session pairs
    assert [s[0].index[0].date() for s in samples] == [trading[1], trading[2]] * 2


def test_runner_gate_uses_one_fetch_in_a_normal_week(monkeypatch, gated):
    today = dt.datetime.now(ET).date()
    calls = []
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch([today - dt.timedelta(days=n) for n in (5, 4, 3, 2, 1)], calls))
    runner._load_specs("unused.yaml", {})
    assert len(calls) == 1 and len(gated[0]) == 4


def test_runner_gate_raises_on_no_sessions_rather_than_rejecting_everything(monkeypatch, gated):
    calls = []
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch([], calls))
    with pytest.raises(GateSampleError, match="SPY session"):
        runner._load_specs("unused.yaml", {})
    assert len(calls) == 3 and gated == []


def test_a_mid_session_restart_never_samples_todays_partial_session(monkeypatch, gated):
    """Started 15 minutes after a 30-minute-old open, today has a few bars: never a gate sample."""
    now = dt.datetime.now(ET)
    trading = [now.date() - dt.timedelta(days=n) for n in (4, 3, 2, 1)] + [now.date()]
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch(trading, []))
    runner._load_specs("unused.yaml", {}, now=now.replace(hour=10, minute=15))
    sampled = {s[0].index[0].date() for s in gated[0]}
    assert sampled == {trading[2], trading[3]}  # the last two completed sessions, each with its prior day


def test_a_failed_sample_fetch_is_a_gate_error_not_an_invalid_file(monkeypatch, gated):
    def down(*a, **k):
        raise ConnectionError("bars API 503")
    monkeypatch.setattr(runner, "fetch_alpaca", down)
    with pytest.raises(GateSampleError, match="503"):
        runner._load_specs("unused.yaml", {})
    alerts = []
    assert runner._specs_for_session("unused.yaml", {}, runner.Calendar(), lambda lvl, msg: alerts.append(msg)) == []
    assert len(alerts) == 1 and "gate couldn't run" in alerts[0] and "classifiers.yaml invalid" not in alerts[0]


def test_an_invalid_file_still_says_so(monkeypatch, gated):
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch([dt.datetime.now(ET).date() - dt.timedelta(days=n) for n in (3, 2, 1)], []))

    def bad(*a, **k):
        raise ValueError("bad yaml")
    monkeypatch.setattr("trader.classifier.load_specs_report", bad)
    alerts = []
    assert runner._specs_for_session("unused.yaml", {}, runner.Calendar(), lambda lvl, msg: alerts.append(msg)) == []
    assert len(alerts) == 1 and alerts[0].startswith("classifiers.yaml invalid")


def test_gate_samples_needs_a_lookback():
    with pytest.raises(ValueError):
        gate_samples(lambda days: {}, lookbacks=())


def test_the_recent_calendar_covers_the_widest_gate_window():
    seen = []
    runner._recent_calendar(type("C", (), {"get_calendar": lambda self, req: seen.append(req) or []})(),
                            dt.date(2026, 10, 5))
    assert (dt.date(2026, 10, 5) - seen[0].start).days >= max(runner.GATE_LOOKBACKS)
