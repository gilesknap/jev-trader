"""Minute-named features are defined on elapsed exchange time, not row counts, so sparse live
(IEX) bars mean the same as dense historical ones; and minutes_since_open is the same number
in the custom-feature gate as live."""

import datetime as dt
import math

import pandas as pd
import pytest

from trader import features as F
from trader.data import ET
from trader.features import lib

from conftest import make_session

DAY = dt.date(2026, 9, 21)
OPEN = pd.Timestamp(dt.datetime.combine(DAY, dt.time(9, 30), ET))
MINUTE_NAMES = ["ret_1m_pct", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct", "or15_break_pct", "or30_break_pct",
                "or15_low_dist_pct", "or15_width_pct", "rel_volume_15m"]


def at(h, m):
    return pd.Timestamp(dt.datetime.combine(DAY, dt.time(h, m), ET))


def ctx(bars, prev=None, now=None):
    """The context the engine gives at the tick after `now` (default: the latest bar)."""
    now = bars.index[-1] if now is None else now
    mso = (now - OPEN).total_seconds() / 60 + 1
    return F.FeatureContext(make_session(day=DAY - dt.timedelta(days=3)) if prev is None else prev,
                            bars, mso, 390 - mso)


def same(a, b):
    return all(a[k] == pytest.approx(b[k]) or (math.isnan(a[k]) and math.isnan(b[k])) for k in a)


def test_dense_bars_give_the_same_values_as_counting_rows():
    """On a full minute grid, elapsed time and row counts agree: nothing changes for SIP replays."""
    bars = make_session(day=DAY, seed=3).assign(volume=lambda d: 1000.0 + d.index.minute * 10.0)
    for n in (1, 5, 14, 15, 16, 31, 60, 200):
        b = bars.iloc[:n]
        got = F.compute(MINUTE_NAMES, b, ctx(b))
        c, hi15, lo15 = b.close, b.high.iloc[:15], b.low.iloc[:15]
        ret = {k: (c.iloc[-1] / c.iloc[-1 - k] - 1) * 100 if n > k else math.nan for k in (1, 5, 15, 30)}
        want = {
            "ret_1m_pct": ret[1], "ret_5m_pct": ret[5], "ret_15m_pct": ret[15], "ret_30m_pct": ret[30],
            "or15_break_pct": (c.iloc[-1] / hi15.max() - 1) * 100 if n > 15 else math.nan,
            "or30_break_pct": (c.iloc[-1] / b.high.iloc[:30].max() - 1) * 100 if n > 30 else math.nan,
            "or15_low_dist_pct": (c.iloc[-1] / lo15.min() - 1) * 100 if n > 15 else math.nan,
            "or15_width_pct": (hi15.max() / lo15.min() - 1) * 100 if n >= 15 else math.nan,
            "rel_volume_15m": b.volume.iloc[-15:].mean() / 1000.0 if n >= 15 else math.nan,
        }
        assert same(got, want), n


def test_the_opening_range_never_includes_bars_from_0945_on():
    bars = make_session(day=DAY, seed=4)
    spike = bars.copy()
    spike.loc[at(9, 45):, "high"] *= 1.05  # anything from 09:45 on would widen the range
    spike.loc[at(9, 45):, "low"] *= 0.95
    sparse = spike.drop([at(9, 31), at(9, 33), at(9, 40), at(9, 41), at(10, 5)])  # opening and later minutes missing
    b = sparse.loc[:at(10, 30)]
    got = F.compute(["or15_break_pct", "or15_low_dist_pct", "or15_width_pct"], b, ctx(b))
    window = sparse[sparse.index < at(9, 45)]
    assert got["or15_break_pct"] == pytest.approx((b.close.iloc[-1] / window.high.max() - 1) * 100)
    assert got["or15_low_dist_pct"] == pytest.approx((b.close.iloc[-1] / window.low.min() - 1) * 100)
    assert got["or15_width_pct"] == pytest.approx((window.high.max() / window.low.min() - 1) * 100)
    # Counting rows would have taken the first 15 bars, two of them from 09:45 and later.
    assert sparse.high.iloc[:15].max() > window.high.max()


def test_the_opening_range_is_ready_by_the_clock_not_the_bar_count():
    bars = make_session(day=DAY, seed=5).drop([at(9, 35), at(9, 36), at(9, 44)])
    b = bars.loc[:at(9, 43)]
    assert math.isnan(F.compute(["or15_width_pct"], b, ctx(b))["or15_width_pct"])
    # At the 09:44 minute the window is complete, though its last bar is missing.
    assert math.isfinite(F.compute(["or15_width_pct"], b, ctx(b, now=at(9, 44)))["or15_width_pct"])
    assert math.isnan(F.compute(["or15_break_pct"], b, ctx(b, now=at(9, 44)))["or15_break_pct"])
    b = bars.loc[:at(9, 46)]
    assert math.isfinite(F.compute(["or15_break_pct"], b, ctx(b))["or15_break_pct"])


def test_missing_opening_bars():
    bars = make_session(day=DAY, seed=6)
    late = bars[bars.index >= at(9, 50)]  # nothing printed in the opening range at all
    b = late.loc[:at(10, 19)]
    got = F.compute(["or15_break_pct", "or15_width_pct", "ret_30m_pct", "ret_15m_pct"], b, ctx(b))
    assert math.isnan(got["or15_break_pct"]) and math.isnan(got["or15_width_pct"])
    assert math.isnan(got["ret_30m_pct"])  # 09:50 was the first price: none at 09:49
    assert got["ret_15m_pct"] == pytest.approx((b.close.iloc[-1] / b.close.loc[at(10, 4)] - 1) * 100)


def test_returns_use_the_declared_time_endpoint_over_gaps():
    bars = make_session(day=DAY, seed=7)
    sparse = bars.drop([at(10, 25), at(10, 26)])  # a quiet two minutes
    b = sparse.loc[:at(10, 30)]
    got = F.compute(["ret_5m_pct", "ret_1m_pct"], b, ctx(b))
    # 5 minutes before 10:30 is 10:25, which has no bar: its price is the 10:24 close, carried.
    assert got["ret_5m_pct"] == pytest.approx((b.close.iloc[-1] / bars.close.loc[at(10, 24)] - 1) * 100)
    # Counting rows would reach back to 10:22, eight minutes.
    assert got["ret_5m_pct"] != pytest.approx((b.close.iloc[-1] / b.close.iloc[-6] - 1) * 100)
    assert got["ret_1m_pct"] == pytest.approx((b.close.iloc[-1] / bars.close.loc[at(10, 29)] - 1) * 100)


def test_a_price_older_than_the_stale_limit_is_unavailable():
    bars = make_session(day=DAY, seed=8)
    halted = bars.drop(bars.loc[at(10, 0):at(10, 19)].index)  # 20 minutes without a print
    b = halted.loc[:at(10, 23)]
    got = F.compute(["ret_5m_pct", "ret_30m_pct", "ret_1m_pct"], b, ctx(b))
    assert math.isnan(got["ret_5m_pct"])  # 10:18's price would be 09:59's close, 19 minutes old
    assert math.isfinite(got["ret_30m_pct"]) and math.isfinite(got["ret_1m_pct"])
    # Still halted at 10:15: its latest price is 16 minutes old, so no return is "the last n minutes".
    b = halted.loc[:at(9, 59)]
    got = F.compute(["ret_1m_pct", "ret_5m_pct", "rel_volume_15m"], b, ctx(b, now=at(10, 15)))
    assert math.isnan(got["ret_1m_pct"]) and math.isnan(got["ret_5m_pct"])
    assert got["rel_volume_15m"] == 0.0  # nothing traded in the last 15 minutes


def test_rel_volume_counts_a_minute_without_a_bar_as_no_volume():
    bars = make_session(day=DAY, seed=9)  # 1000 a minute, as the prior session
    b = bars.drop([at(10, 20), at(10, 21), at(10, 22)]).loc[:at(10, 30)]
    assert F.compute(["rel_volume_15m"], b, ctx(b))["rel_volume_15m"] == pytest.approx(12 / 15)


def test_recent_returns_are_one_minute_apart():
    bars = make_session(day=DAY, seed=10)
    now = at(10, 31).to_pydatetime()  # the tick after the 10:30 bar
    dense = lib.recent_returns_bps(bars.loc[:at(10, 30)], now)
    assert dense == (bars.close.loc[:at(10, 30)].pct_change().iloc[-10:] * 1e4).round(1).tolist()
    sparse = bars.drop([at(10, 25)]).loc[:at(10, 30)]
    got = lib.recent_returns_bps(sparse, now)
    assert len(got) == 10 and got[-6] == 0.0  # 10:25 carried 10:24's close
    assert got[-5] == round((bars.close.loc[at(10, 26)] / bars.close.loc[at(10, 24)] - 1) * 1e4, 1)
    # Early in the session only the minutes since the first bar, as before.
    assert len(lib.recent_returns_bps(bars.loc[:at(9, 32)], at(9, 33).to_pydatetime())) == 2
    # The live stream indexes in microseconds.
    us = sparse.copy()
    us.index = us.index.as_unit("us")
    assert lib.recent_returns_bps(us, now) == got


def test_minutes_since_open_is_the_same_in_the_gate_as_live(tmp_path, monkeypatch):
    """Bar n of the session is minute n (the first bar is 1, at the 09:31 tick) in the engine, in
    the custom-feature gate and in the probe log's `m`; minutes_to_close agrees too."""
    from trader.features import harness

    from test_probe import Always, probe_spec, run

    seen = {}

    def record(bars, c):
        seen[len(bars)] = (c.minutes_since_open, c.minutes_to_close)
        return 0.0
    monkeypatch.setitem(F.REGISTRY, "clock_probe", record)
    bars = make_session(day=DAY, n=45)
    harness.evaluate(["clock_probe"], [(bars, bars, bars)])
    gate, seen = dict(seen), {}
    _, rows = run(tmp_path, bars, [probe_spec(features=["clock_probe"], cadence_min=1, window=("09:31", "15:30"))],
                  Always(entry="STAND_DOWN"))
    live = dict(seen)
    assert live[2] == (2, 388) and gate[31] == (31, 359)  # the engine asks from the second bar
    assert {n: live[n] for n in gate} == gate
    assert rows[0]["t"] == "09:32" and [r["m"] for r in rows][:3] == [2, 3, 4]
