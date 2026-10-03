"""A probe outcome is labelled only once its whole horizon exists: never on the minutes there
happen to be so far, never across a data gap, and only shortened by the end-of-day flatten."""

import datetime as dt
import json
import math

import numpy as np
import pandas as pd
import pytest

from trader import probe
from trader.data import ET

from conftest import make_session

DAY = dt.date(2026, 9, 21)


def at(hhmm):
    return dt.datetime.combine(DAY, dt.time.fromisoformat(hhmm), ET)


def row(t, px=None, sym="SPY"):
    return {"day": DAY.isoformat(), "t": t, "c": "p", "s": sym, "p_enter": 0.5, "px": px}


def test_an_immature_horizon_is_unlabelled_until_its_endpoint_arrives():
    """The fixture from the report: closes 100 then 101, asked at 10:01; 60 minutes are not 1."""
    two = pd.DataFrame({"open": [100.0, 101.0], "high": [100.0, 101.0], "low": [100.0, 101.0],
                        "close": [100.0, 101.0], "volume": 1.0}, index=pd.DatetimeIndex([at("10:00"), at("10:01")]))
    rows = pd.DataFrame([row("10:01")])
    out = probe.forward_returns(rows, {"SPY": {DAY: two}}, [1, 60], data_end=at("10:02"))
    assert math.isnan(out.fwd_60[0]) and out.why_60[0] == "immature"
    assert out.fwd_1[0] == pytest.approx(1.0) and out.why_1[0] == ""
    # Without data_end the gap itself says so: no price within the carry limit of 11:00.
    out = probe.forward_returns(rows, {"SPY": {DAY: two}}, [60])
    assert math.isnan(out.fwd_60[0]) and out.why_60[0] == "missing endpoint"
    # A later run, after the endpoint arrived, labels it.
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    out = probe.forward_returns(rows, {"SPY": {DAY: full}}, [60], data_end=at("11:30"))
    c = full.close
    assert out.fwd_60[0] == pytest.approx((c[at("11:00")] / c[at("10:00")] - 1) * 100) and out.len_60[0] == 60


def test_the_endpoint_bar_must_have_completed_by_data_end():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    rows = pd.DataFrame([row("10:01")])
    assert probe.forward_returns(rows, {"SPY": {DAY: full}}, [15], data_end=at("10:15")).why_15[0] == "immature"
    assert probe.forward_returns(rows, {"SPY": {DAY: full}}, [15], data_end=at("10:16")).why_15[0] == ""


def test_stale_symbol_data_is_missing_not_carried():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    halted = full.drop(full.loc[at("10:05"):at("10:40")].index)
    out = probe.forward_returns(pd.DataFrame([row("10:01"), row("10:01")]), {"SPY": {DAY: halted}}, [15, 42])
    assert out.why_15[0] == "missing endpoint" and math.isnan(out.fwd_15[0])  # 10:15: last print 10:04
    c = full.close
    assert out.fwd_42[0] == pytest.approx((c[at("10:42")] / c[at("10:00")] - 1) * 100)
    # A quiet minute or two is carried: no trade, same price.
    gappy = full.drop([at("10:15"), at("10:14")])
    out = probe.forward_returns(pd.DataFrame([row("10:01")]), {"SPY": {DAY: gappy}}, [15])
    assert out.fwd_15[0] == pytest.approx((c[at("10:13")] / c[at("10:00")] - 1) * 100)


def test_close_truncation_is_marked_and_not_called_missing():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    out = probe.forward_returns(pd.DataFrame([row("15:30"), row("15:50")]), {"SPY": {DAY: full}}, [60],
                                data_end=at("17:00"))
    c = full.close
    assert out.fwd_60[0] == pytest.approx((c[at("15:44")] / c[at("15:29")] - 1) * 100)
    assert out.cut_60[0] and out.len_60[0] == 15 and out.why_60[0] == ""
    assert math.isnan(out.fwd_60[1]) and out.why_60[1] == "after flatten"


def test_the_logged_price_is_the_start_price():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    out = probe.forward_returns(pd.DataFrame([row("10:01", px=50.0), row("10:01")]), {"SPY": {DAY: full}}, [15])
    c = full.close
    assert out.fwd_15[0] == pytest.approx((c[at("10:15")] / 50.0 - 1) * 100)
    assert out.fwd_15[1] == pytest.approx((c[at("10:15")] / c[at("10:00")] - 1) * 100)  # older logs: no px


def test_report_counts_unlabelled_rows_and_horizon_lengths():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    ts = [f"{h:02d}:{m:02d}" for h in (10, 11, 12, 13, 14) for m in (1, 11, 21, 31, 41, 51)] + ["15:30", "15:50"]
    rows = pd.DataFrame([row(t) for t in ts])
    out = probe.forward_returns(rows, {"SPY": {DAY: full}}, [60], data_end=at("14:30"))
    h = probe.score(out, [60])["p"]["horizons"][60]
    assert h["unlabelled"] == {"immature": 10, "after flatten": 1}  # endpoints after 14:29: asked from 13:31
    assert h["n"] == 21 and h["cut_n"] == 0 and h["mean_len_min"] == 60


def _reference(rows, sessions, horizons, data_end=None, last_bar="15:44"):
    """The original per-row definition, kept as the oracle for the vectorised one."""
    out = {h: [] for h in horizons}
    carry = pd.Timedelta(minutes=lib_stale())
    for _, r in rows.iterrows():
        d = dt.date.fromisoformat(r["day"])
        last = dt.datetime.combine(d, dt.time.fromisoformat(last_bar), ET)
        bars = sessions.get(r["s"], {}).get(d)
        close = bars.close if bars is not None else pd.Series(dtype=float)

        def price(t):
            j = close.index.searchsorted(t, side="right")
            return float(close.iloc[j - 1]) if j and close.index[j - 1] >= t - carry else None
        asked = dt.datetime.combine(d, dt.time.fromisoformat(r["t"]), ET) - dt.timedelta(minutes=1)
        for h in horizons:
            if asked >= last:
                out[h].append((math.nan, "after flatten"))
                continue
            px = r.get("px")
            p0 = float(px) if isinstance(px, (int, float)) and math.isfinite(px) and px > 0 else price(asked)
            end = min(asked + dt.timedelta(minutes=h), last)
            p1 = price(end)
            if data_end is not None and end + dt.timedelta(minutes=1) > data_end:
                out[h].append((math.nan, "immature"))
            elif p0 is None:
                out[h].append((math.nan, "no price at ask"))
            elif p1 is None:
                out[h].append((math.nan, "missing endpoint"))
            else:
                out[h].append(((p1 / p0 - 1) * 100, ""))
    return out


def lib_stale():
    from trader.features.lib import STALE_MIN
    return STALE_MIN


def test_vectorised_matches_the_per_row_definition():
    rng = np.random.default_rng(3)
    full = make_session(day=DAY, seed=4)
    sparse = full.drop(full.index[rng.choice(len(full), 120, replace=False)])  # gaps, some longer than the carry
    halted = full.drop(full.loc[at("11:00"):at("12:30")].index)
    sessions = {"SPY": {DAY: sparse}, "QQQ": {DAY: halted}}  # "IWM" has no bars at all
    times = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 7) if (9, 31) <= (h, m) < (16, 0)]
    rows = pd.DataFrame([row(t, px=(float(rng.uniform(90, 110)) if rng.random() < 0.3 else None), sym=s)
                         for t in times for s in ("SPY", "QQQ", "IWM")])
    for data_end in (None, at("13:17")):
        out = probe.forward_returns(rows, sessions, [1, 15, 60], data_end=data_end)
        ref = _reference(rows, sessions, [1, 15, 60], data_end)
        for h in (1, 15, 60):
            assert list(out[f"why_{h}"]) == [w for _, w in ref[h]], h
            np.testing.assert_allclose(out[f"fwd_{h}"].to_numpy(dtype=float), [v for v, _ in ref[h]], equal_nan=True)


def test_a_symbol_with_no_bars_is_unlabelled_not_an_error():
    out = probe.forward_returns(pd.DataFrame([row("10:01", sym="ZZZ"), row("10:01", px=50.0, sym="ZZZ")]), {}, [15])
    assert list(out.why_15) == ["no price at ask", "missing endpoint"] and out.fwd_15.isna().all()


def test_a_horizon_with_nothing_scored_still_reports_why():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    rows = pd.DataFrame([row("10:01"), row("10:02")])
    out = probe.forward_returns(rows, {"SPY": {DAY: full}}, [15, 60], data_end=at("10:30"))
    h = probe.score(out, [15, 60])["p"]["horizons"]
    assert h[60] == {"n": 0, "cut_n": 0, "mean_len_min": None, "no_p_enter": 0, "unlabelled": {"immature": 2}}
    assert h[15]["n"] == 2
    json.dumps(probe.finite(probe.score(out, [15, 60])), allow_nan=False)  # still strict JSON


def test_every_row_is_accounted_for():
    full = make_session(day=DAY, path=list(np.linspace(100, 139, 390)))
    rows = pd.DataFrame([row(t) for t in ("10:01", "10:11", "10:21", "15:50")] * 3)
    rows.loc[[0, 5], "p_enter"] = np.nan  # answers without an ENTER probability
    out = probe.forward_returns(rows, {"SPY": {DAY: full}}, [15], data_end=at("17:00"))
    h = probe.score(out, [15])["p"]["horizons"][15]
    assert h["no_p_enter"] == 2 and h["unlabelled"] == {"after flatten": 3}
    assert h["n"] + h["no_p_enter"] + sum(h["unlabelled"].values()) == len(rows)
