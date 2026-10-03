"""Seed feature library. Values are dimensionless (%, ratios, z-scores) so the
decision model never sees absolute prices or dates."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from trader.features import FeatureContext, feature

NAN = float("nan")


def _ret(bars: pd.DataFrame, n: int) -> float:
    if len(bars) <= n:
        return NAN
    return (bars.close.iloc[-1] / bars.close.iloc[-1 - n] - 1) * 100


def _vwap(bars: pd.DataFrame) -> float:
    tp = (bars.high + bars.low + bars.close) / 3
    vol = bars.volume.sum()
    return float((tp * bars.volume).sum() / vol) if vol > 0 else NAN


@feature("ret_1m_pct")
def ret_1m(bars, ctx):
    """% change of the close over the last 1 bar."""
    return _ret(bars, 1)


@feature("ret_5m_pct")
def ret_5m(bars, ctx):
    """% change of the close over the last 5 bars."""
    return _ret(bars, 5)


@feature("ret_15m_pct")
def ret_15m(bars, ctx):
    """% change of the close over the last 15 bars."""
    return _ret(bars, 15)


@feature("ret_30m_pct")
def ret_30m(bars, ctx):
    """% change of the close over the last 30 bars."""
    return _ret(bars, 30)


@feature("ret_since_open_pct")
def ret_since_open(bars, ctx):
    """% change from today's first open to the latest close."""
    return (bars.close.iloc[-1] / bars.open.iloc[0] - 1) * 100 if len(bars) else NAN


@feature("gap_pct")
def gap(bars, ctx):
    """% gap from the previous session's last close to today's first open."""
    if ctx.prev_day.empty or not len(bars):
        return NAN
    return (bars.open.iloc[0] / ctx.prev_day.close.iloc[-1] - 1) * 100


@feature("vwap_dist_pct")
def vwap_dist(bars, ctx):
    """% above (negative: below) today's volume-weighted average price."""
    v = _vwap(bars)
    return (bars.close.iloc[-1] / v - 1) * 100 if v == v else NAN


def _or_break(bars, minutes):
    if len(bars) <= minutes:
        return NAN
    hi = bars.high.iloc[:minutes].max()
    return (bars.close.iloc[-1] / hi - 1) * 100


def _or_breakdown(bars, minutes):
    if len(bars) <= minutes:
        return NAN
    lo = bars.low.iloc[:minutes].min()
    return (bars.close.iloc[-1] / lo - 1) * 100


@feature("or15_break_pct")
def or15_break(bars, ctx):
    """% above the 15-min opening-range high (negative = below it)."""
    return _or_break(bars, 15)


@feature("or30_break_pct")
def or30_break(bars, ctx):
    """% above the 30-min opening-range high (negative = below it)."""
    return _or_break(bars, 30)


@feature("or15_low_dist_pct")
def or15_low(bars, ctx):
    """% relative to the 15-min opening-range low (negative = broke down)."""
    return _or_breakdown(bars, 15)


@feature("or15_width_pct")
def or15_width(bars, ctx):
    """Width of the 15-min opening range (high / low - 1), in %."""
    if len(bars) < 15:
        return NAN
    hi, lo = bars.high.iloc[:15].max(), bars.low.iloc[:15].min()
    return (hi / lo - 1) * 100


@feature("rsi_14")
def rsi14(bars, ctx):
    """14-bar RSI of 1-min closes (simple averages), 0..100."""
    if len(bars) < 15:
        return NAN
    d = bars.close.diff().iloc[-14:]
    up, dn = d.clip(lower=0).mean(), (-d.clip(upper=0)).mean()
    if dn == 0:
        return 100.0
    return 100 - 100 / (1 + up / dn)


@feature("rel_volume_15m")
def rel_volume(bars, ctx):
    """Volume of the last 15 bars vs the prior session's average per-bar volume."""
    if len(bars) < 15 or ctx.prev_day.empty:
        return NAN
    base = ctx.prev_day.volume.mean()
    return float(bars.volume.iloc[-15:].mean() / base) if base > 0 else NAN


@feature("atr_14_pct")
def atr14(bars, ctx):
    """Average true range of the last 14 bars, as % of the latest close."""
    if len(bars) < 15:
        return NAN
    pc = bars.close.shift(1)
    tr = pd.concat([bars.high - bars.low, (bars.high - pc).abs(), (bars.low - pc).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-14:].mean() / bars.close.iloc[-1] * 100)


@feature("range_pos")
def range_pos(bars, ctx):
    """Where the close sits in today's high-low range, 0..1."""
    hi, lo = bars.high.max(), bars.low.min()
    return float((bars.close.iloc[-1] - lo) / (hi - lo)) if hi > lo else 0.5


@feature("rel_spy_since_open_pct")
def rel_spy(bars, ctx):
    """Return since open minus SPY's return since open, in percentage points."""
    if ctx.spy.empty or not len(bars):
        return NAN
    spy = (ctx.spy.close.iloc[-1] / ctx.spy.open.iloc[0] - 1) * 100
    return ret_since_open(bars, ctx) - spy


@feature("ema_9_21_diff_pct")
def ema_diff(bars, ctx):
    """% difference between the 9- and 21-bar EMAs of the close."""
    if len(bars) < 21:
        return NAN
    c = bars.close
    return float((c.ewm(span=9).mean().iloc[-1] / c.ewm(span=21).mean().iloc[-1] - 1) * 100)


@feature("realized_vol_30m_pct")
def rv30(bars, ctx):
    """Annualised-free: stdev of 1-min returns over 30 bars, in %."""
    if len(bars) < 31:
        return NAN
    return float(bars.close.pct_change().iloc[-30:].std() * 100)


@feature("trend_slope_30m")
def slope30(bars, ctx):
    """OLS slope of log price over 30 bars, in bps/min, divided by its stderr (t-stat)."""
    if len(bars) < 30:
        return NAN
    y = np.log(bars.close.iloc[-30:].to_numpy())
    x = np.arange(30.0)
    b, a = np.polyfit(x, y, 1)
    resid = y - (a + b * x)
    se = math.sqrt((resid**2).sum() / 28) / math.sqrt(((x - x.mean()) ** 2).sum())
    return float(b / se) if se > 0 else 0.0


@feature("prev_high_dist_pct")
def prev_high(bars, ctx):
    """% above (negative: below) the previous session's high."""
    if ctx.prev_day.empty:
        return NAN
    return (bars.close.iloc[-1] / ctx.prev_day.high.max() - 1) * 100


@feature("prev_low_dist_pct")
def prev_low(bars, ctx):
    """% above (negative: below) the previous session's low."""
    if ctx.prev_day.empty:
        return NAN
    return (bars.close.iloc[-1] / ctx.prev_day.low.min() - 1) * 100


@feature("minutes_since_open")
def mso(bars, ctx):
    """Minutes since the 09:30 ET open, counting the current bar (the first bar is 1)."""
    return ctx.minutes_since_open


@feature("minutes_to_close")
def mtc(bars, ctx):
    """Minutes until the close."""
    return ctx.minutes_to_close
