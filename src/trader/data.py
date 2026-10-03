"""Minute-bar data: Alpaca historical (SIP/IEX), yfinance (dev only), and session helpers.

Bars are pandas DataFrames indexed by tz-aware America/New_York timestamps with
columns open, high, low, close, volume. Each bar is labelled by its start minute.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
COLS = ["open", "high", "low", "close", "volume"]


def session_bounds(day: dt.date, close_time: dt.time = dt.time(16, 0)):
    open_ = dt.datetime.combine(day, dt.time(9, 30), ET)
    close = dt.datetime.combine(day, close_time, ET)
    return open_, close


def _alpaca_client(secrets: dict[str, str]):
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(
        secrets["ALPACA_PAPER_KEY"], secrets["ALPACA_PAPER_SECRET"]
    )


def fetch_alpaca(
    symbols: list[str],
    start: dt.datetime,
    end: dt.datetime,
    secrets: dict[str, str],
    feed: str = "sip",
) -> dict[str, pd.DataFrame]:
    """Historical 1-min bars. On the free plan SIP must end >15 min ago."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
    )
    df = _alpaca_client(secrets).get_stock_bars(req).df
    out: dict[str, pd.DataFrame] = {}
    if df.empty:
        return out
    for sym, sub in df.groupby(level=0):
        sub = sub.droplevel(0)[COLS].copy()
        sub.index = sub.index.tz_convert(ET)
        out[str(sym)] = sub.sort_index()
    return out


def fetch_yfinance(symbols: list[str], days: int = 7) -> dict[str, pd.DataFrame]:
    """Dev-only fallback: last ~7 days of 1-min bars, no keys needed."""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    raw = yf.download(
        symbols, period=f"{days}d", interval="1m", group_by="ticker",
        auto_adjust=False, progress=False, prepost=False,
    )
    for sym in symbols:
        sub = raw[sym] if len(symbols) > 1 else raw
        sub = sub.rename(columns=str.lower)[COLS].dropna()
        sub.index = sub.index.tz_convert(ET)
        out[sym] = sub.sort_index()
    return out


def fetch(symbols, start, end, secrets, source="alpaca") -> dict[str, pd.DataFrame]:
    if source == "yfinance":
        days = max(1, min(7, (dt.datetime.now(ET) - start).days + 1))
        bars = fetch_yfinance(symbols, days)
        return {s: b[(b.index >= start) & (b.index < end)] for s, b in bars.items()}
    return fetch_alpaca(symbols, start, end, secrets)


def split_sessions(bars: pd.DataFrame) -> dict[dt.date, pd.DataFrame]:
    """Regular-hours bars grouped by trading day."""
    rth = bars.between_time("09:30", "15:59")
    return {d: g for d, g in rth.groupby(rth.index.date)}


def prior_sessions(sessions: dict[str, dict[dt.date, pd.DataFrame]], day: dt.date,
                   volume_from: dict[str, dict[dt.date, pd.DataFrame]] | None = None) -> dict[str, pd.DataFrame]:
    """Each symbol's last session before `day`: the features' `prev_day`. Prices are always
    `sessions`' (SIP's, with the official close). When today's bars come from another feed (live
    IEX), pass that feed's sessions as `volume_from`: the volume column is then taken from it, so
    a volume ratio never divides one feed's volume by another's. A minute that feed has no bar
    for has volume 0; a symbol or session it lacks gets NaN volume, never the other feed's."""
    out = {}
    for s, per in sessions.items():
        earlier = [d for d in per if d < day]
        if not earlier:
            continue
        d = max(earlier)
        prev = per[d]
        if volume_from is not None:
            other = volume_from.get(s, {}).get(d)
            prev = prev.assign(volume=float("nan") if other is None
                               else other.volume.reindex(prev.index, fill_value=0).astype(float))
        out[s] = prev
    return out
