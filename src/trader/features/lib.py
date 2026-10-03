"""Seed feature library. Values are dimensionless (%, ratios, z-scores) so the
decision model never sees absolute prices or dates.

Bars can be missing: IEX has no bar for a minute without an IEX trade, and a halted symbol
has none for a while. Features named in minutes (`ret_5m_pct`, `or15_*`, `rel_volume_15m`)
are therefore defined on elapsed exchange time, never on counting rows: the opening range is
the bars labelled before 09:45, and a return over n minutes compares the price now with the
price n minutes ago. A price is carried forward over missing minutes (no trade, no new price)
for at most STALE_MIN minutes; beyond that the return is NaN (unavailable). Likewise every
level feature that compares the current price with something (`or*_break_pct`,
`or15_low_dist_pct`, `vwap_dist_pct`, `ret_since_open_pct`, `rel_spy_since_open_pct`,
`range_pos`, `prev_*_dist_pct`) is NaN while the latest price is older than that, so a
halted or thin symbol's last print never passes for the current price. Indicators named
in bars (`rsi_14`, `atr_14_pct`, `ema_9_21_diff_pct`, `realized_vol_30m_pct`,
`trend_slope_30m`) count the bars there are."""

from __future__ import annotations

import math
import numbers

import numpy as np
import pandas as pd

from trader.features import FeatureContext, feature

NAN = float("nan")
MINUTE = pd.Timedelta(minutes=1)
STALE_MIN = 5  # a price older than this (minutes) at a return's endpoint is unavailable, not carried


def _open(bars: pd.DataFrame) -> pd.Timestamp:
    """Label of today's first bar (09:30 ET; early closes still open then)."""
    return bars.index[-1].replace(hour=9, minute=30, second=0, microsecond=0, nanosecond=0)


def _now(bars: pd.DataFrame, ctx) -> pd.Timestamp:
    """Label of the current minute's bar, from the session clock (the first bar is minute 1),
    so a symbol whose latest bar is old is seen as stale; the latest bar's label if later."""
    last = bars.index[-1]
    m = getattr(ctx, "minutes_since_open", None)
    if isinstance(m, numbers.Real) and not isinstance(m, bool) and math.isfinite(m):
        return max(last, _open(bars) + (m - 1) * MINUTE)
    return last


def _close_at(bars: pd.DataFrame, t: pd.Timestamp) -> float:
    """The close as of minute `t`: the last bar at or before it, carried at most STALE_MIN minutes."""
    i = bars.index.searchsorted(t, side="right")
    if i == 0 or bars.index[i - 1] < t - STALE_MIN * MINUTE:
        return NAN
    return float(bars.close.iloc[i - 1])


def _price(bars: pd.DataFrame, ctx) -> float:
    """The current price: the latest close, NaN once it is older than STALE_MIN minutes (a
    halted or thin symbol), so no level feature compares a stale print with anything."""
    return _close_at(bars, _now(bars, ctx)) if len(bars) else NAN


def _ret(bars: pd.DataFrame, ctx, n: int) -> float:
    if not len(bars):
        return NAN
    now = _now(bars, ctx)
    return (_close_at(bars, now) / _close_at(bars, now - n * MINUTE) - 1) * 100


def recent_returns_bps(bars: pd.DataFrame, now, n: int = 10) -> list[float]:
    """The last `n` one-minute returns up to the bar completed at `now`, in bps, oldest first, on
    a minute grid: a minute without a bar carries the last close (a 0 return) rather than one
    return spanning several minutes. Minutes before today's first bar are left out."""
    if not len(bars):
        return []
    grid = pd.date_range(end=pd.Timestamp(now) - MINUTE, periods=n + 1, freq="1min").as_unit(bars.index.unit)
    grid = grid[grid >= bars.index[0]]
    close = bars.close.reindex(bars.index.union(grid)).ffill().reindex(grid)
    return (close.pct_change().iloc[1:] * 1e4).round(1).fillna(0).tolist()


def _vwap(bars: pd.DataFrame) -> float:
    tp = (bars.high + bars.low + bars.close) / 3
    vol = bars.volume.sum()
    return float((tp * bars.volume).sum() / vol) if vol > 0 else NAN


@feature("ret_1m_pct")
def ret_1m(bars, ctx):
    """% change of the close over the last 1 minute of exchange time (NaN if a price is stale)."""
    return _ret(bars, ctx, 1)


@feature("ret_5m_pct")
def ret_5m(bars, ctx):
    """% change of the close over the last 5 minutes of exchange time (NaN if a price is stale)."""
    return _ret(bars, ctx, 5)


@feature("ret_15m_pct")
def ret_15m(bars, ctx):
    """% change of the close over the last 15 minutes of exchange time (NaN if a price is stale)."""
    return _ret(bars, ctx, 15)


@feature("ret_30m_pct")
def ret_30m(bars, ctx):
    """% change of the close over the last 30 minutes of exchange time (NaN if a price is stale)."""
    return _ret(bars, ctx, 30)


@feature("ret_since_open_pct")
def ret_since_open(bars, ctx):
    """% change from today's first open to the latest close."""
    return (_price(bars, ctx) / bars.open.iloc[0] - 1) * 100 if len(bars) else NAN


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
    return (_price(bars, ctx) / v - 1) * 100 if v == v else NAN


def _opening_range(bars, ctx, minutes, ready_at):
    """(high, low) of the bars labelled in the first `minutes` of the session, once the clock has
    reached `ready_at` minutes after the open (the bar labelled then is the current one); None
    before that or if the window has no bars. A missing opening bar never pulls a later one in."""
    if not len(bars):
        return None
    start = _open(bars)
    if _now(bars, ctx) < start + ready_at * MINUTE:
        return None
    w = bars[bars.index < start + minutes * MINUTE]
    return (w.high.max(), w.low.min()) if len(w) else None


def _or_break(bars, ctx, minutes):
    rng = _opening_range(bars, ctx, minutes, minutes)  # from the first bar after the range
    return (_price(bars, ctx) / rng[0] - 1) * 100 if rng else NAN


def _or_breakdown(bars, ctx, minutes):
    rng = _opening_range(bars, ctx, minutes, minutes)
    return (_price(bars, ctx) / rng[1] - 1) * 100 if rng else NAN


@feature("or15_break_pct")
def or15_break(bars, ctx):
    """% above the 15-min opening-range high (bars before 09:45; negative = below it)."""
    return _or_break(bars, ctx, 15)


@feature("or30_break_pct")
def or30_break(bars, ctx):
    """% above the 30-min opening-range high (bars before 10:00; negative = below it)."""
    return _or_break(bars, ctx, 30)


@feature("or15_low_dist_pct")
def or15_low(bars, ctx):
    """% relative to the 15-min opening-range low (bars before 09:45; negative = broke down)."""
    return _or_breakdown(bars, ctx, 15)


@feature("or15_width_pct")
def or15_width(bars, ctx):
    """Width of the 15-min opening range (bars before 09:45; high / low - 1), in %, from its last bar."""
    rng = _opening_range(bars, ctx, 15, 14)
    return (rng[0] / rng[1] - 1) * 100 if rng else NAN


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
    """Volume per minute over the last 15 minutes (a minute without a bar traded nothing) vs the
    prior session's volume per minute, both from the same feed (live: IEX; replay: SIP), so ~1
    means a normal pace whichever feed it is."""
    if not len(bars) or ctx.prev_day.empty:
        return NAN
    now = _now(bars, ctx)
    if now < _open(bars) + 14 * MINUTE:
        return NAN
    base = ctx.prev_day.volume.mean()
    return float(bars.volume[bars.index > now - 15 * MINUTE].sum() / 15 / base) if base > 0 else NAN


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
    px = _price(bars, ctx)
    if px != px:
        return NAN
    return float((px - lo) / (hi - lo)) if hi > lo else 0.5


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
    """Annualised-free: stdev of bar-to-bar returns over the last 30 bars, in %."""
    if len(bars) < 31:
        return NAN
    return float(bars.close.pct_change().iloc[-30:].std() * 100)


@feature("trend_slope_30m")
def slope30(bars, ctx):
    """OLS slope of log price over the last 30 bars, per bar, divided by its stderr (t-stat)."""
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
    return (_price(bars, ctx) / ctx.prev_day.high.max() - 1) * 100


@feature("prev_low_dist_pct")
def prev_low(bars, ctx):
    """% above (negative: below) the previous session's low."""
    if ctx.prev_day.empty:
        return NAN
    return (_price(bars, ctx) / ctx.prev_day.low.min() - 1) * 100


@feature("minutes_since_open")
def mso(bars, ctx):
    """Minutes since the 09:30 ET open, counting the current bar (the first bar is 1)."""
    return ctx.minutes_since_open


@feature("minutes_to_close")
def mtc(bars, ctx):
    """Minutes until the close."""
    return ctx.minutes_to_close
