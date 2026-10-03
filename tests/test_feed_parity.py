"""Live features compare like with like: today's bars are IEX, so the prior session's volume
must be IEX too, while its prices stay SIP's (the official close)."""

import datetime as dt
import math

import pandas as pd
import pytest

from trader import features as F
from trader import runner
from trader.data import ET, prior_sessions, split_sessions

from conftest import make_session

DAY = dt.date(2026, 9, 22)
PREV = dt.date(2026, 9, 21)
NOW = dt.datetime.combine(DAY, dt.time(9, 20), ET)


def feeds(sip_vol=10_000.0, iex_vol=200.0, iex_px=1.01):
    """Two days of bars per feed: SIP with all the volume, IEX a 2% slice at slightly off prices."""
    sip = pd.concat(
        [make_session(day=PREV, seed=1).assign(volume=sip_vol), make_session(day=DAY, seed=2).assign(volume=sip_vol)]
    )
    iex = sip.assign(volume=iex_vol, close=sip.close * iex_px, high=sip.high * iex_px, low=sip.low * iex_px)
    return sip, iex


def fake_fetch(sip, iex, fail_iex=False):
    calls = []

    def fetch(symbols, start, end, secrets, feed="sip"):
        calls.append(feed)
        if feed == "iex" and fail_iex:
            raise ConnectionError("iex 503")
        b = iex if feed == "iex" else sip
        return {s: b[(b.index >= start) & (b.index < end)] for s in symbols}

    return fetch, calls


def ctx_for(prev, spy):
    return F.FeatureContext(prev, spy, 30, 360)


def test_live_rel_volume_is_about_one_at_a_normal_pace(monkeypatch):
    """The regression: IEX today over a SIP prior-session mean read ~0.02, not ~1."""
    sip, iex = feeds()
    fetch, calls = fake_fetch(sip, iex)
    monkeypatch.setattr(runner, "fetch_alpaca", fetch)
    prev = runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=lambda *a: pytest.fail(str(a)))
    assert sorted(calls) == ["iex", "sip"]
    today = split_sessions(iex)[DAY].iloc[:40]  # live bars are IEX
    rv = F.compute(["rel_volume_15m"], today, ctx_for(prev["AAA"], today))["rel_volume_15m"]
    assert rv == pytest.approx(1.0)
    # What the old SIP-only context gave: the feed ratio, not relative volume.
    old = F.compute(["rel_volume_15m"], today, ctx_for(split_sessions(sip)[PREV], today))["rel_volume_15m"]
    assert old == pytest.approx(0.02)


def test_prior_day_price_levels_stay_on_sips_official_prices(monkeypatch):
    sip, iex = feeds()
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch(sip, iex)[0])
    prev = runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=lambda *a: pytest.fail(str(a)))["AAA"]
    sip_prev = split_sessions(sip)[PREV]
    pd.testing.assert_frame_equal(prev[["open", "high", "low", "close"]], sip_prev[["open", "high", "low", "close"]])
    assert (prev.volume == 200.0).all()
    today = split_sessions(sip)[DAY].iloc[:40]
    names = ["gap_pct", "prev_high_dist_pct", "prev_low_dist_pct"]
    assert F.compute(names, today, ctx_for(prev, today)) == F.compute(names, today, ctx_for(sip_prev, today))


def test_proportional_feed_volumes_give_the_same_ratio():
    """Whatever share of the tape a feed carries, a same-feed ratio is the same."""
    out = []
    for share in (1.0, 0.03, 0.005):
        sip, iex = feeds(iex_vol=10_000.0 * share)
        sessions = {"AAA": split_sessions(sip)}
        prev = prior_sessions(sessions, DAY, volume_from={"AAA": split_sessions(iex)})["AAA"]
        today = split_sessions(iex)[DAY].iloc[:40].assign(volume=lambda d: d.volume * 1.5)  # a busier morning
        out.append(F.compute(["rel_volume_15m"], today, ctx_for(prev, today))["rel_volume_15m"])
    assert out == pytest.approx([1.5, 1.5, 1.5])


def test_failed_iex_history_leaves_volume_nan_not_sips(monkeypatch):
    sip, iex = feeds()
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch(sip, iex, fail_iex=True)[0])
    alerts = []
    prev = runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=lambda level, msg: alerts.append((level, msg)))["AAA"]
    assert len(alerts) == 1 and alerts[0][0] == "urgent" and "IEX" in alerts[0][1]
    assert prev.volume.isna().all()
    today = split_sessions(iex)[DAY].iloc[:40]
    f = F.compute(["rel_volume_15m", "gap_pct"], today, ctx_for(prev, today))
    assert math.isnan(f["rel_volume_15m"]) and math.isfinite(f["gap_pct"])


def test_failed_sip_history_alerts_and_returns_nothing(monkeypatch):
    def fetch(*a, **k):
        raise ConnectionError("sip 503")

    monkeypatch.setattr(runner, "fetch_alpaca", fetch)
    alerts = []
    assert runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=lambda level, msg: alerts.append(msg)) == {}
    assert len(alerts) == 1 and "prev-day features are NaN" in alerts[0]


def test_iex_gaps_count_as_no_volume_and_a_missing_symbol_or_session_is_nan():
    sip, iex = feeds()
    p = split_sessions(sip)[PREV]
    sparse = split_sessions(iex)[PREV].drop(p.index[10:20])
    # The live stream and alpaca-py index in microseconds; history in nanoseconds: still aligned.
    sparse.index = sparse.index.as_unit("us")
    sparse = pd.concat([sparse, sparse.iloc[[0]]])  # a repeated minute: the last copy counts, once
    prev = prior_sessions(
        {"AAA": {PREV: p}, "BBB": {PREV: p}, "CCC": {PREV: p}},
        DAY,
        volume_from={"AAA": {PREV: sparse}, "CCC": {DAY: sparse}},
    )
    assert (prev["AAA"].volume.iloc[10:20] == 0).all() and prev["AAA"].volume.sum() == 200.0 * (len(p) - 10)
    assert prev["BBB"].volume.isna().all()  # the feed has no bars for the symbol
    assert prev["CCC"].volume.isna().all()  # the feed lacks that session
    assert prior_sessions({"AAA": {DAY: p}}, DAY) == {}  # nothing earlier: no prev_day at all


def test_same_feed_forward_and_replay_give_the_same_feature_vector(monkeypatch):
    """With one feed for both, the runner's context and replay's are identical, feature for feature."""
    sip, _ = feeds()
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch(sip, sip)[0])
    live = runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=lambda *a: pytest.fail(str(a)))["AAA"]
    replayed = prior_sessions({"AAA": split_sessions(sip)}, DAY)["AAA"]
    today = split_sessions(sip)[DAY].iloc[:60]
    names = sorted(F.REGISTRY)
    a, b = F.compute(names, today, ctx_for(live, today)), F.compute(names, today, ctx_for(replayed, today))
    assert a.keys() == b.keys() and all(a[k] == b[k] or (math.isnan(a[k]) and math.isnan(b[k])) for k in a)


def test_a_failure_combining_the_feeds_alerts_and_never_stops_startup(monkeypatch):
    sip, iex = feeds()
    monkeypatch.setattr(runner, "fetch_alpaca", fake_fetch(sip, iex)[0])
    real = runner.prior_sessions

    def flaky(sessions, day, volume_from=None):
        if volume_from:
            raise ValueError("cannot reindex")
        return real(sessions, day, volume_from)

    monkeypatch.setattr(runner, "prior_sessions", flaky)
    alerts = []
    prev = runner.prev_day_bars(["AAA"], DAY, NOW, {}, alert=lambda level, msg: alerts.append(msg))["AAA"]
    assert len(alerts) == 1 and "IEX volume" in alerts[0]
    assert prev.volume.isna().all() and (prev.close == split_sessions(sip)[PREV].close).all()
