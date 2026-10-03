"""The runner's session-start fetches are bounded in time and never fail in silence."""

import datetime as dt
import threading

import pandas as pd
import pytest

from test_feed_parity import DAY, NOW, PREV, fake_fetch, feeds
from trader import runner
from trader.data import ET, split_sessions


@pytest.fixture
def hang():
    """A fetch that never answers (until the test ends), like a hung API call."""
    release = threading.Event()

    def fetch(*a, **k):
        release.wait(30)
        return {}

    yield fetch
    release.set()


def collect():
    alerts = []
    return alerts, lambda level, msg: alerts.append((level, msg))


def test_a_hung_history_fetch_is_given_up_on_and_alerted(monkeypatch, hang):
    monkeypatch.setattr(runner, "STARTUP_FETCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(runner, "fetch_alpaca", hang)
    alerts, alert = collect()
    assert runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=alert) == {}
    assert len(alerts) == 1 and "TimeoutError" in alerts[0][1] and "prev-day features are NaN" in alerts[0][1]


def test_a_hung_iex_fetch_keeps_sip_prices_and_alerts_once(monkeypatch, hang):
    sip, _ = feeds()
    good = fake_fetch(sip, sip)[0]
    monkeypatch.setattr(runner, "STARTUP_FETCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        runner, "fetch_alpaca", lambda *a, feed="sip", **k: hang() if feed == "iex" else good(*a, feed=feed)
    )
    alerts, alert = collect()
    prev = runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=alert)["AAA"]
    assert (prev.close == split_sessions(sip)[PREV].close).all() and prev.volume.isna().all()
    assert len(alerts) == 1 and "IEX history" in alerts[0][1] and "TimeoutError" in alerts[0][1]


def test_a_symbol_missing_from_the_history_is_named(monkeypatch):
    sip, iex = feeds()
    fetch = fake_fetch(sip, iex)[0]
    monkeypatch.setattr(runner, "fetch_alpaca", lambda symbols, *a, **k: fetch(["AAA"], *a, **k))  # BBB: no bars
    alerts, alert = collect()
    prev = runner.prev_day_bars(["AAA", "BBB"], DAY, NOW, {}, alert=alert)
    assert set(prev) == {"AAA"}
    assert alerts == [
        ("urgent", "no prior session in the startup history for ['BBB']: their prev-day features are NaN today")
    ]


def test_a_symbol_missing_from_iex_alone_is_named_for_its_volume(monkeypatch):
    sip, iex = feeds()
    fetch = fake_fetch(sip, iex)[0]

    def partial(symbols, *a, feed="sip", **k):
        return fetch(["AAA"] if feed == "iex" else symbols, *a, feed=feed, **k)

    monkeypatch.setattr(runner, "fetch_alpaca", partial)
    alerts, alert = collect()
    prev = runner.prev_day_bars(["AAA", "BBB"], DAY, NOW, {}, alert=alert)
    assert prev["BBB"].volume.isna().all() and not prev["AAA"].volume.isna().any()
    assert len(alerts) == 1 and alerts[0][0] == "info" and "['BBB']" in alerts[0][1]


def test_a_hung_gate_sample_fetch_is_given_up_on(monkeypatch, hang):
    """Bounded, not swallowed: like any gate-sample failure it fails the file loudly (see
    test_runner_gate_samples), but it can no longer hold the session start indefinitely."""
    import trader.features.harness

    monkeypatch.setattr(runner, "STARTUP_FETCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(runner, "fetch_alpaca", hang)
    monkeypatch.setattr(trader.features.harness, "run_gate", lambda *a, **k: pytest.fail("gated without samples"))
    with pytest.raises(runner.GateSampleError, match="TimeoutError"):  # said as a data problem, not a bad file
        runner._load_specs("unused.yaml", {})


OPEN = dt.datetime.combine(DAY, dt.time(9, 30), ET)
TICK = dt.datetime.combine(DAY, dt.time(9, 36), ET)


def today_bars():
    b = split_sessions(feeds()[1])[DAY]
    return b[b.index < OPEN + dt.timedelta(minutes=10)]


def test_a_late_stream_catches_up_on_todays_bars_without_duplicates(monkeypatch):
    b = today_bars()
    asked = []

    def fetch(symbols, start, end, secrets, feed="sip"):
        asked.append((tuple(symbols), start, end, feed))
        return {s: b for s in symbols}

    monkeypatch.setattr(runner, "fetch_alpaca", fetch)
    rows = {"AAA": [(ts, *r) for ts, r in zip(b.index[3:5], b.iloc[3:5].itertuples(index=False))]}  # the stream's
    rows = runner.defaultdict(list, rows)
    runner.catch_up_bars(rows, threading.Lock(), ["AAA"], [], OPEN, TICK, {}, alert=lambda *a: pytest.fail(str(a)))
    assert asked == [(("AAA",), OPEN, TICK, "iex")]
    got = runner.stream_bars(rows, TICK)["AAA"]
    expected = b[b.index < TICK]
    assert list(got.index) == list(expected.index) and (got.close.to_numpy() == expected.close.to_numpy()).all()


def test_a_failed_catch_up_alerts_per_group_and_never_raises(monkeypatch, hang):
    b = today_bars()

    def fetch(symbols, *a, **k):
        if symbols == ["HELD"]:
            raise ConnectionError("rejected symbol")
        return {s: b for s in symbols}

    monkeypatch.setattr(runner, "fetch_alpaca", fetch)
    rows = runner.defaultdict(list)
    alerts, alert = collect()
    runner.catch_up_bars(rows, threading.Lock(), ["AAA"], ["HELD"], OPEN, TICK, {}, alert=alert)
    assert len(rows["AAA"]) == len(b[b.index < TICK]) and not rows["HELD"]
    assert (
        len(alerts) == 1 and "['HELD']" in alerts[0][1] and "stops may be missing today's earlier bars" in alerts[0][1]
    )

    monkeypatch.setattr(runner, "CATCH_UP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(runner, "fetch_alpaca", hang)
    alerts.clear()
    runner.catch_up_bars(runner.defaultdict(list), threading.Lock(), ["AAA"], [], OPEN, TICK, {}, alert=alert)
    assert len(alerts) == 1 and "TimeoutError" in alerts[0][1]


def test_fetch_within_returns_or_raises_what_the_fetch_did(monkeypatch):
    monkeypatch.setattr(runner, "fetch_alpaca", lambda *a, **k: {"AAA": pd.DataFrame()})
    assert list(runner._fetch_within(1.0, ["AAA"])) == ["AAA"]
    monkeypatch.setattr(runner, "fetch_alpaca", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad symbol")))
    with pytest.raises(ValueError, match="bad symbol"):
        runner._fetch_within(1.0, ["AAA"])
