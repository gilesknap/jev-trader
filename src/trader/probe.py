"""Score `mode: probe` decisions against what price did next (`trader probe-report`).

A probe logs P(ENTER) every time it's asked. This joins those rows to SIP minute bars and
asks two questions per probe and horizon:

1. Does Jev's P(ENTER) rank later returns? The information coefficient (IC) is the Spearman
   correlation between P(ENTER) and the forward return, computed within each day and then
   averaged across days. Rows from the same day (overlapping horizons, correlated symbols)
   are far from independent, so the day is the unit of evidence and t is mean/se over days.
2. Does Jev add anything over its own inputs? A plain (ridge) linear model of the forward return on
   everything Jev was shown (the features, minutes since open and the last ten 1-minute
   returns) is fitted walk-forward (days before d only, scored on day d), with and without
   P(ENTER) as an extra input. If adding Jev doesn't lift the out-of-sample IC, its inputs
   alone carry whatever signal there is and a deterministic rule would do. A lift is weaker
   evidence: a linear model is a low bar, so it says Jev combines its inputs usefully, not
   that no rule could.

Forward returns run from the close the question was asked at, to the close `h` minutes later,
cut off at the end-of-day flatten (close - 15 min, from the exchange calendar: 15:45 ET, or
12:45 on a 13:00 early close), since no position could be held past it. `cut_<h>` marks the
rows whose horizon the flatten shortened.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from trader.data import ET
from trader.features.lib import STALE_MIN
from trader.market_calendar import Calendar

SLIPPAGE_ROUND_TRIP_PCT = 0.1  # 0.05% a side, as everywhere else
MIN_DAYS_TO_FIT = 3  # walk-forward baselines start once this many earlier days exist
DEFAULT_THRESHOLD = 0.6  # Question.threshold's default, for probes scored without their spec
RIDGE = 0.1  # ridge penalty per training row, on standardised inputs
MIN_DAYS_FOR_T = 5  # below this a t-stat across days is noise dressed as evidence: report none


N_RETURNS = 10  # the engine shows Jev the last ten 1-minute returns


def load_rows(paths: list[Path], only: set[str] | None = None) -> pd.DataFrame:
    """Probe rows from decision logs (.jsonl or .jsonl.gz named YYYY-MM-DD). Columns `f:*` are
    the classifier's features; `x:*` are the other inputs Jev saw. Unreadable lines (the last
    line of a log the runner is still writing) are skipped and counted in `.attrs["skipped"]`."""
    out, skipped = [], 0
    for p in paths:
        day = p.name[:10]
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if r.get("q") != "probe" or (only and r["c"] not in only):
                    continue
                rets = list(r.get("r") or [])
                rets = [np.nan] * (N_RETURNS - len(rets)) + rets[-N_RETURNS:]  # oldest first, like the engine
                out.append({"day": day, "t": r["t"], "c": r["c"], "s": r["s"],
                            "p_enter": r["p"].get("ENTER", float("nan")), "px": r.get("px"),
                            **{f"f:{k}": (np.nan if v is None else v) for k, v in r.get("f", {}).items()},
                            "x:minutes_since_open": r.get("m", np.nan),
                            **{f"x:ret_1m_lag{N_RETURNS - i}": v for i, v in enumerate(rets)}})
    df = pd.DataFrame(out)
    df.attrs["skipped"] = skipped
    return df


def forward_returns(rows: pd.DataFrame, sessions: dict, horizons: list[int],
                    calendar: Calendar | None = None, data_end: dt.datetime | None = None) -> pd.DataFrame:
    """Add, per horizon h: fwd_<h> (% return), cut_<h> (the flatten shortened the horizon),
    len_<h> (the minutes actually scored) and why_<h> (why fwd_<h> is missing, else "").
    `sessions`: {symbol: {date: bars}} (SIP, from replay.load_sessions). `calendar`: the
    exchange calendar for the days (regular 16:00 closes without one). `data_end`: when the
    fetched bars end; a horizon whose endpoint bar hadn't completed by then is left unlabelled
    ("immature") and labelled by a later run, never scored on the minutes there were.

    A question asked at t saw bars up to t-1 (a bar labelled 09:30 completes at 09:31). The
    start price is the one logged with the question (`px`, what Jev was shown), else the close
    of bar t-1. A price is carried over missing minutes for at most STALE_MIN minutes: an
    endpoint with no bar that recent is "missing endpoint", not the last price before a gap."""
    calendar = calendar or Calendar()
    rows = rows.copy()
    for h in horizons:
        rows[f"fwd_{h}"], rows[f"cut_{h}"], rows[f"len_{h}"], rows[f"why_{h}"] = np.nan, False, np.nan, ""
    carry = pd.Timedelta(minutes=STALE_MIN)
    for (day, sym), g in rows.groupby(["day", "s"]):
        d = dt.date.fromisoformat(day)
        last = calendar.session(d).flatten_at - dt.timedelta(minutes=1)  # once per day, not per row
        bars = sessions.get(sym, {}).get(d)
        close = bars.close if bars is not None else pd.Series(dtype=float)

        def price(t):
            j = close.index.searchsorted(t, side="right")
            return float(close.iloc[j - 1]) if j and close.index[j - 1] >= t - carry else None

        for i, r in g.iterrows():
            asked = dt.datetime.combine(d, dt.time.fromisoformat(r["t"]), ET) - dt.timedelta(minutes=1)
            if asked >= last:
                rows.loc[i, [f"why_{h}" for h in horizons]] = "after flatten"
                continue
            px = r.get("px")
            p0 = float(px) if isinstance(px, (int, float)) and math.isfinite(px) and px > 0 else price(asked)
            for h in horizons:
                end = min(asked + dt.timedelta(minutes=h), last)
                rows.at[i, f"cut_{h}"] = asked + dt.timedelta(minutes=h) > last
                rows.at[i, f"len_{h}"] = (end - asked).total_seconds() / 60
                p1 = price(end)
                if data_end is not None and end + dt.timedelta(minutes=1) > data_end:
                    rows.at[i, f"why_{h}"] = "immature"
                elif p0 is None:
                    rows.at[i, f"why_{h}"] = "no price at ask"
                elif p1 is None:
                    rows.at[i, f"why_{h}"] = "missing endpoint"
                else:
                    rows.at[i, f"fwd_{h}"] = (p1 / p0 - 1) * 100
    return rows


def _spearman(x: pd.Series, y: pd.Series) -> float:
    ok = x.notna() & y.notna()
    if ok.sum() < 5 or x[ok].nunique() < 2 or y[ok].nunique() < 2:
        return float("nan")
    return float(x[ok].rank().corr(y[ok].rank()))


def _day_stat(ics: list[float]) -> dict:
    """Mean of per-day values with a t-stat across days (the day is the unit of evidence)."""
    v = [x for x in ics if math.isfinite(x)]
    if not v:
        return {"mean": None, "t": None, "days": 0}
    m = float(np.mean(v))
    t = m / (np.std(v, ddof=1) / math.sqrt(len(v))) if len(v) >= MIN_DAYS_FOR_T and np.std(v, ddof=1) > 0 else None
    return {"mean": round(m, 4), "t": None if t is None else round(float(t), 2), "days": len(v)}


def _walk_forward_ic(g: pd.DataFrame, cols: list[str], y: str) -> dict:
    """Per-day out-of-sample IC of a ridge regression fitted only on earlier days. The ridge
    penalty keeps a dozen inputs over a few hundred noisy rows from fitting noise."""
    days = sorted(g.day.unique())
    ics = []
    for i, d in enumerate(days):
        if i < MIN_DAYS_TO_FIT:
            continue
        train = g[g.day.isin(days[:i])].dropna(subset=cols + [y])
        test = g[g.day == d].dropna(subset=cols + [y])
        if len(train) < 5 * (len(cols) + 1) or len(test) < 5:
            continue
        mu, sd = train[cols].mean(), train[cols].std().replace(0, 1)
        X = np.c_[np.ones(len(train)), ((train[cols] - mu) / sd).to_numpy()]
        pen = RIDGE * len(train) * np.eye(X.shape[1])
        pen[0, 0] = 0.0  # never shrink the intercept
        beta = np.linalg.solve(X.T @ X + pen, X.T @ train[y].to_numpy())
        pred = np.c_[np.ones(len(test)), ((test[cols] - mu) / sd).to_numpy()] @ beta
        ics.append(_spearman(pd.Series(pred, index=test.index), test[y]))
    return _day_stat(ics)


def _bins(p: pd.Series, y: pd.Series, n: int = 5) -> list[dict] | None:
    """Mean forward return by P(ENTER) bin, labelled by the P(ENTER) values actually in it.
    Jev's outputs repeat a lot: with at most `n` distinct values each gets its own bin, else
    quantile bins split on values, never within a tie (so there may be fewer than `n`)."""
    if len(p) < 5 * n or p.nunique() < 2:
        return None
    b = p if p.nunique() <= n else pd.qcut(p, n, duplicates="drop")
    return [{"p_enter_min": round(float(p[m].min()), 3), "p_enter_max": round(float(p[m].max()), 3),
             "n": int(m.sum()), "mean_gross_bps": round(float(y[m].mean()) * 100, 1)}
            for m in (b == k for k in sorted(b.unique()))]


def score(rows: pd.DataFrame, horizons: list[int], thresholds: dict[str, float] | None = None) -> dict:
    """Per probe and horizon: base rates, Jev's IC, per-feature ICs and the walk-forward baselines."""
    report = {}
    for c, g in rows.groupby("c"):
        feats = [k for k in g.columns if k.startswith("f:") and g[k].notna().any()]
        inputs = feats + [k for k in g.columns if k.startswith("x:") and g[k].notna().any()]
        thr = (thresholds or {}).get(c, DEFAULT_THRESHOLD)
        out = {"rows": len(g), "days": int(g.day.nunique()), "symbols": int(g.s.nunique()), "threshold": thr,
               "p_enter_spread": round(float(g.p_enter.std()), 4) if g.p_enter.notna().sum() > 1 else None, "horizons": {}}
        for h in horizons:
            y = f"fwd_{h}"
            gg = g[g[y].notna() & g.p_enter.notna()]  # an answer without ENTER can't be scored
            if gg.empty:
                continue
            hi = gg[gg.p_enter >= thr]
            out["horizons"][h] = {
                "n": len(gg),
                "cut_n": int(gg[f"cut_{h}"].sum()) if f"cut_{h}" in gg else 0,  # horizons shortened by the flatten
                "mean_len_min": round(float(gg[f"len_{h}"].mean()), 1) if f"len_{h}" in gg else None,
                "unlabelled": (g[f"why_{h}"][g[f"why_{h}"] != ""].value_counts().to_dict()
                               if f"why_{h}" in g else {}),  # rows left out, by reason
                "all_mean_net_pct": round(float(gg[y].mean()) - SLIPPAGE_ROUND_TRIP_PCT, 4),
                "enter_n": len(hi),
                "enter_mean_net_pct": round(float(hi[y].mean()) - SLIPPAGE_ROUND_TRIP_PCT, 4) if len(hi) else None,
                "enter_hit_rate": round(float((hi[y] > SLIPPAGE_ROUND_TRIP_PCT).mean()), 3) if len(hi) else None,
                "by_p_enter": _bins(gg.p_enter, gg[y]),
                "jev_ic": _day_stat([_spearman(d.p_enter, d[y]) for _, d in gg.groupby("day")]),
                "feature_ic": {k[2:]: _day_stat([_spearman(d[k], d[y]) for _, d in gg.groupby("day")]) for k in feats},
                "wf_inputs_only": _walk_forward_ic(gg, inputs, y) if inputs else None,
                "wf_inputs_plus_jev": _walk_forward_ic(gg, inputs + ["p_enter"], y),
            }
        report[c] = out
    return report


def finite(x):
    """The report with every NaN/inf replaced by None: strict JSON, which the dashboard's API needs."""
    if isinstance(x, dict):
        return {k: finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [finite(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def decision_files(dirs: list[Path], start: dt.date | None, end: dt.date | None) -> list[Path]:
    files = []
    for d in dirs:
        for p in sorted(d.glob("*.jsonl*")):
            try:
                day = dt.date.fromisoformat(p.name[:10])
            except ValueError:
                continue
            if (start is None or day >= start) and (end is None or day <= end):
                files.append(p)
    # One file per day: the runner's log and its archived copy in logs/ can both exist, as can
    # today's live .jsonl and its .gz after the close. Prefer a .gz.
    by_day = {}
    for p in files:
        by_day.setdefault(p.name[:10], []).append(p)
    return [sorted(ps, key=lambda p: p.suffix != ".gz")[0] for ps in by_day.values()]
