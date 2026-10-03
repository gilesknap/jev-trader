"""The runner's start-of-day custom-feature gate counts sessions, not calendar days (no network)."""

import datetime as dt

import pandas as pd
import pytest

from conftest import make_session
from trader import runner
from trader.data import COLS, ET

EMPTY = pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz=ET), dtype=float)


def fake_fetch(trading_days, calls):
    """fetch_alpaca over a fixed set of trading days, honouring its [start, end] window."""

    def fetch(symbols, start, end, secrets, feed="sip"):
        calls.append((start, end))
        days = [d for d in trading_days if start.date() <= d <= end.date()]
        return {s: pd.concat([make_session(d) for d in days]) if days else EMPTY for s in symbols}

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
    with pytest.raises(RuntimeError, match="SPY session"):
        runner._load_specs("unused.yaml", {})
    assert len(calls) == 3 and gated == []
