"""Score `mode: probe` decisions against what price did next (`trader probe-report`).

A probe logs P(ENTER) every time it's asked. This joins those rows to SIP minute bars and
asks two questions per probe and horizon:

1. Does Jev's P(ENTER) rank later returns? The information coefficient (IC) is the Spearman
   correlation between P(ENTER) and the forward return, computed within each day and then
   averaged across days. Rows from the same day (overlapping horizons, correlated symbols)
   are far from independent, so the day is the unit of evidence and t is mean/se over days.
2. Does Jev add anything over its own inputs? A plain (ridge) linear model of the forward return on
   everything Jev was shown (the symbol, the features, minutes since open and the last ten
   1-minute returns) is fitted walk-forward (days before d only, scored on day d), with and
   without P(ENTER) as an extra input. Both are fitted and scored on exactly the same rows and
   days, and `jev_increment` is the paired per-day difference in IC (with Jev minus without)
   with a 95% interval. Only an interval above zero says Jev adds something its inputs don't;
   one below zero says Jev does worse than its inputs alone; anything else is "inconclusive:
   no detectable incremental value", which is not proof that a plain rule would do. The test
   is per probe and horizon, so a few readings will disagree by chance. And a linear model is
   a low bar: a lift says Jev combines its inputs usefully, not that no rule could, nor that
   it trades better.

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
from trader.stats import t95

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
                out.append(
                    {
                        "day": day,
                        "t": r["t"],
                        "c": r["c"],
                        "s": r["s"],
                        "p_enter": r["p"].get("ENTER", float("nan")),
                        "px": r.get("px"),
                        **{f"f:{k}": (np.nan if v is None else v) for k, v in r.get("f", {}).items()},
                        "x:minutes_since_open": r.get("m", np.nan),
                        **{f"x:ret_1m_lag{N_RETURNS - i}": v for i, v in enumerate(rets)},
                    }
                )
    df = pd.DataFrame(out)
    df.attrs["skipped"] = skipped
    return df


def forward_returns(
    rows: pd.DataFrame,
    sessions: dict,
    horizons: list[int],
    calendar: Calendar | None = None,
    data_end: dt.datetime | None = None,
) -> pd.DataFrame:
    """Add, per horizon h: fwd_<h> (% return), cut_<h> (the flatten shortened the horizon),
    len_<h> (the minutes actually scored) and why_<h> (why fwd_<h> is missing, else "").
    `sessions`: {symbol: {date: bars}} (SIP, from replay.load_sessions). `calendar`: the
    exchange calendar for the days (regular 16:00 closes without one). `data_end`: when the
    fetched bars end; a horizon whose endpoint bar hadn't completed by then is left unlabelled
    ("immature"), never scored on the minutes there were. Only "immature" rows are labelled by a
    later run; "no price at ask", "missing endpoint" and "after flatten" are final for that row.

    A question asked at t saw bars up to t-1 (a bar labelled 09:30 completes at 09:31). The
    start price is the one logged with the question (`px`, what Jev was shown), else the close
    of bar t-1. A price is carried over missing minutes for at most STALE_MIN minutes: an
    endpoint with no bar that recent is "missing endpoint", not the last price before a gap.
    Vectorised per (day, symbol): one searchsorted over the session per horizon."""
    calendar = calendar or Calendar()
    rows = rows.copy()
    for h in horizons:
        rows[f"fwd_{h}"], rows[f"cut_{h}"], rows[f"len_{h}"], rows[f"why_{h}"] = np.nan, False, np.nan, ""
    if rows.empty:
        return rows
    carry = pd.Timedelta(minutes=STALE_MIN).value
    minute = pd.Timedelta(minutes=1).value
    done = None if data_end is None else pd.Timestamp(data_end).value  # an endpoint bar must complete by then
    for (day, sym), g in rows.groupby(["day", "s"]):
        d = dt.date.fromisoformat(day)
        last = pd.Timestamp(calendar.session(d).flatten_at).value  # once per day, not per row
        bars = sessions.get(sym, {}).get(d)
        if bars is not None and len(bars):
            idx = bars.index.as_unit("ns").asi8
            close = bars.close.to_numpy(dtype=float)
        else:
            idx, close = np.empty(0, dtype="int64"), np.empty(0)

        def price(t):
            """The close as of each instant in `t` (ns), NaN past the carry limit or before any bar."""
            j = np.searchsorted(idx, t, side="right")
            ok = j > 0
            k = np.where(ok, j - 1, 0)
            ok &= len(idx) > 0
            if len(idx):
                ok &= idx[k] >= t - carry
            return np.where(ok, close[k] if len(idx) else np.nan, np.nan)

        asked = (
            pd.DatetimeIndex(pd.to_datetime(day + " " + g["t"].astype(str))).tz_localize(ET).as_unit("ns").asi8 - minute
        )  # the bar the question saw
        after = asked >= last - minute
        px = pd.to_numeric(g["px"], errors="coerce").to_numpy(dtype=float) if "px" in g else np.full(len(g), np.nan)
        p0 = np.where(np.isfinite(px) & (px > 0), px, price(asked))
        for h in horizons:
            want = asked + h * minute
            end = np.minimum(want, last - minute)
            p1 = price(end)
            why = np.full(len(g), "", dtype=object)
            why[np.isnan(p1)] = "missing endpoint"
            why[np.isnan(p0)] = "no price at ask"
            if done is not None:
                why[end + minute > done] = "immature"
            why[after] = "after flatten"
            fwd = np.where(why == "", (p1 / p0 - 1) * 100, np.nan)
            rows.loc[g.index, f"fwd_{h}"] = fwd
            rows.loc[g.index, f"why_{h}"] = why
            rows.loc[g.index, f"cut_{h}"] = np.where(after, False, want > last - minute)
            rows.loc[g.index, f"len_{h}"] = np.where(after, np.nan, (end - asked) / minute)
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


def _ridge_ic(train: pd.DataFrame, test: pd.DataFrame, cols: list[str], y: str) -> float:
    """Out-of-sample IC on `test` of a ridge regression fitted on `train`. The ridge penalty keeps
    a dozen inputs over a few hundred noisy rows from fitting noise. The symbol is an input as
    one 0/1 column per symbol seen in training (a symbol first seen on the test day gets none),
    so a ticker effect Jev can see is open to the baseline too. Dummies are scaled by their
    training sd like every other input (but not centred, so an unseen symbol sits on the
    intercept): under one shared penalty, a rare symbol's effect would otherwise be shrunk far
    more than the same effect carried by P(ENTER), and Jev would get credit for the ticker."""
    syms = sorted(train.s.unique()) if "s" in train and train.s.nunique() > 1 else []
    mu, sd = train[cols].mean(), train[cols].std().replace(0, 1)
    dsd = [(train.s == s).to_numpy(float).std(ddof=1) or 1.0 for s in syms]

    def design(df):
        return np.c_[
            np.ones(len(df)),
            ((df[cols] - mu) / sd).to_numpy(),
            *[(df.s == s).to_numpy(float) / k for s, k in zip(syms, dsd, strict=True)],
        ]

    X = design(train)
    pen = RIDGE * len(train) * np.eye(X.shape[1])
    pen[0, 0] = 0.0  # never shrink the intercept
    beta = np.linalg.solve(X.T @ X + pen, X.T @ train[y].to_numpy())
    return _spearman(pd.Series(design(test) @ beta, index=test.index), test[y])


def _walk_forward(g: pd.DataFrame, cols: list[str], y: str) -> dict:
    """Per-day out-of-sample IC with only Jev's inputs and with P(ENTER) added, each fitted on
    earlier days only. Both use exactly the same rows (any missing input drops the row from
    both) and the same days, so their per-day difference is a fair pairing."""
    need = cols + ["p_enter", y]
    g = g.dropna(subset=need)
    days = sorted(g.day.unique())
    base, plus = [], []
    for i, d in enumerate(days):
        if i < MIN_DAYS_TO_FIT:
            continue
        train, test = g[g.day.isin(days[:i])], g[g.day == d]
        # Five rows per coefficient of the larger (with-Jev) model, symbol dummies included.
        if len(train) < 5 * (len(cols) + 2 + train.s.nunique()) or len(test) < 5:
            continue
        b, p = _ridge_ic(train, test, cols, y), _ridge_ic(train, test, cols + ["p_enter"], y)
        if math.isfinite(b) and math.isfinite(p):
            base.append(b)
            plus.append(p)
    return {
        "wf_inputs_only": _day_stat(base),
        "wf_inputs_plus_jev": _day_stat(plus),
        "jev_increment": _increment([p - b for b, p in zip(base, plus, strict=True)]),
    }


def _increment(deltas: list[float]) -> dict:
    """Mean per-day IC gain from adding Jev, with a 95% interval across days and a verdict that
    is allowed to say "inconclusive"."""
    n = len(deltas)
    m = float(np.mean(deltas)) if n else None
    sd = float(np.std(deltas, ddof=1)) if n > 1 else 0.0
    half = (
        float(t95(n - 1) * sd / math.sqrt(n)) if n >= MIN_DAYS_FOR_T and sd > 1e-9 else None
    )  # identical deltas: fp dust
    if n < MIN_DAYS_FOR_T:
        verdict = f"inconclusive: too few days ({n})"
    elif half is None:
        verdict = "inconclusive: no spread across days"
    elif m - half > 0:
        verdict = "Jev adds to its inputs: 95% interval above zero"
    elif m + half < 0:
        verdict = "Jev does worse than its inputs alone: 95% interval below zero"
    else:
        verdict = "inconclusive: no detectable incremental value"
    return {
        "mean": None if m is None else round(m, 4),
        "ci95": None if half is None else round(half, 4),
        "days": n,
        "verdict": verdict,
    }


def _bins(p: pd.Series, y: pd.Series, n: int = 5) -> list[dict] | None:
    """Mean forward return by P(ENTER) bin, labelled by the P(ENTER) values actually in it.
    Jev's outputs repeat a lot: with at most `n` distinct values each gets its own bin, else
    quantile bins split on values, never within a tie (so there may be fewer than `n`)."""
    if len(p) < 5 * n or p.nunique() < 2:
        return None
    b = p if p.nunique() <= n else pd.qcut(p, n, duplicates="drop")
    return [
        {
            "p_enter_min": round(float(p[m].min()), 3),
            "p_enter_max": round(float(p[m].max()), 3),
            "n": int(m.sum()),
            "mean_gross_bps": round(float(y[m].mean()) * 100, 1),
        }
        for m in (b == k for k in sorted(b.unique()))
    ]


def score(rows: pd.DataFrame, horizons: list[int], thresholds: dict[str, float] | None = None) -> dict:
    """Per probe and horizon: base rates, Jev's IC, per-feature ICs and the walk-forward baselines."""
    report = {}
    for c, g in rows.groupby("c"):
        feats = [k for k in g.columns if k.startswith("f:") and g[k].notna().any()]
        inputs = feats + [k for k in g.columns if k.startswith("x:") and g[k].notna().any()]
        thr = (thresholds or {}).get(c, DEFAULT_THRESHOLD)
        out = {
            "rows": len(g),
            "days": int(g.day.nunique()),
            "symbols": int(g.s.nunique()),
            "threshold": thr,
            "p_enter_spread": round(float(g.p_enter.std()), 4) if g.p_enter.notna().sum() > 1 else None,
            "horizons": {},
        }
        for h in horizons:
            y = f"fwd_{h}"
            gg = g[g[y].notna() & g.p_enter.notna()]  # an answer without ENTER can't be scored
            # Every row is accounted for: n scored + no_p_enter + unlabelled (by reason) == rows.
            counts = {
                "no_p_enter": int((g[y].notna() & g.p_enter.isna()).sum()),
                "unlabelled": (
                    {str(k): int(v) for k, v in g[f"why_{h}"][g[f"why_{h}"] != ""].value_counts().items()}
                    if f"why_{h}" in g
                    else {}
                ),  # rows left out, by reason
            }
            if gg.empty:  # nothing to score yet (e.g. all immature): say why rather than drop the horizon
                out["horizons"][h] = {"n": 0, "cut_n": 0, "mean_len_min": None, **counts}
                continue
            hi = gg[gg.p_enter >= thr]
            out["horizons"][h] = {
                "n": len(gg),
                "cut_n": int(gg[f"cut_{h}"].sum()) if f"cut_{h}" in gg else 0,  # horizons shortened by the flatten
                "mean_len_min": round(float(gg[f"len_{h}"].mean()), 1) if f"len_{h}" in gg else None,
                **counts,
                "all_mean_net_pct": round(float(gg[y].mean()) - SLIPPAGE_ROUND_TRIP_PCT, 4),
                "enter_n": len(hi),
                "enter_mean_net_pct": round(float(hi[y].mean()) - SLIPPAGE_ROUND_TRIP_PCT, 4) if len(hi) else None,
                "enter_hit_rate": round(float((hi[y] > SLIPPAGE_ROUND_TRIP_PCT).mean()), 3) if len(hi) else None,
                "by_p_enter": _bins(gg.p_enter, gg[y]),
                "jev_ic": _day_stat([_spearman(d.p_enter, d[y]) for _, d in gg.groupby("day")]),
                "feature_ic": {k[2:]: _day_stat([_spearman(d[k], d[y]) for _, d in gg.groupby("day")]) for k in feats},
                **_walk_forward(gg, inputs, y),
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
