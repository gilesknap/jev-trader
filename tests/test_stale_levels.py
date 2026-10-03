"""A halted or thin symbol's last print is not the current price: level features are NaN once
it is older than STALE_MIN, and unchanged while the data is fresh."""

import datetime as dt
import math

import pandas as pd
import pytest

from conftest import make_session
from trader import features as F
from trader.data import ET
from trader.features import lib

DAY = dt.date(2026, 9, 21)
OPEN = pd.Timestamp(dt.datetime.combine(DAY, dt.time(9, 30), ET))
LEVELS = [
    "or15_break_pct",
    "or30_break_pct",
    "or15_low_dist_pct",
    "vwap_dist_pct",
    "ret_since_open_pct",
    "rel_spy_since_open_pct",
    "range_pos",
    "prev_high_dist_pct",
    "prev_low_dist_pct",
]


def at(hhmm):
    return pd.Timestamp(dt.datetime.combine(DAY, dt.time.fromisoformat(hhmm), ET))


def ctx(bars, now):
    mso = (now - OPEN).total_seconds() / 60 + 1  # the current minute's bar label -> minutes since open
    prev = make_session(day=DAY - dt.timedelta(days=3), seed=5)
    return F.FeatureContext(prev, make_session(day=DAY, seed=6).loc[:now], mso, 390 - mso)


def test_fresh_data_is_unchanged():
    bars = make_session(day=DAY, seed=7).loc[: at("11:00")]
    got = F.compute(LEVELS, bars, ctx(bars, at("11:00")))
    assert all(math.isfinite(v) for v in got.values())
    px = bars.close.iloc[-1]
    assert got["ret_since_open_pct"] == pytest.approx((px / bars.open.iloc[0] - 1) * 100)
    assert got["or15_break_pct"] == pytest.approx((px / bars.high.iloc[:15].max() - 1) * 100)


def test_a_late_or_quiet_minute_still_counts_as_current():
    bars = make_session(day=DAY, seed=7).loc[: at("10:57")]
    got = F.compute(LEVELS, bars, ctx(bars, at("11:00")))  # last print 3 minutes ago
    assert all(math.isfinite(v) for v in got.values())


def test_a_halted_symbols_levels_are_nan():
    bars = make_session(day=DAY, seed=7).loc[: at("10:30")]
    got = F.compute(LEVELS, bars, ctx(bars, at("11:40")))  # nothing for 70 minutes
    assert all(math.isnan(v) for v in got.values()), got
    # Exactly STALE_MIN minutes old is still current; one more is not.
    edge = at("10:30") + pd.Timedelta(minutes=lib.STALE_MIN)
    assert math.isfinite(F.compute(["vwap_dist_pct"], bars, ctx(bars, edge))["vwap_dist_pct"])
    later = edge + pd.Timedelta(minutes=1)
    assert math.isnan(F.compute(["vwap_dist_pct"], bars, ctx(bars, later))["vwap_dist_pct"])


def test_a_stale_symbol_never_fires_a_breakout_trigger():
    from trader.classifier import Condition as Trigger

    bars = make_session(day=DAY, path=[100.0] * 30 + [110.0] * 30).loc[: at("10:29")]  # broke out, then halted
    trig = [Trigger(feature="or15_break_pct", op=">", value=0.0), Trigger(feature="vwap_dist_pct", op=">", value=0.0)]
    fresh = F.compute([t.feature for t in trig], bars, ctx(bars, at("10:29")))
    assert all(t.holds(fresh) for t in trig)
    stale = F.compute([t.feature for t in trig], bars, ctx(bars, at("11:30")))
    assert not any(t.holds(stale) for t in trig)
