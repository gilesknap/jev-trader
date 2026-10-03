"""Per-classifier and per-family scoreboard for the dashboard.

Answers "are the strategist's novel ideas beating its conventional ones, and the control?"
from a book's own trades.csv, net of 0.05%/side slippage (charged here for broker fills; replay
fills already include it). Every number comes with its sample size, a 95% interval and a plain
verdict, because at a few trades a day one idea will look dominant for weeks before the
difference means anything.

`daily` is the objective itself: the whole book's return per session from its equity.csv
(zero-trade days included) against buy-and-hold SPY. Per-trade figures describe the rules; they
don't say whether the book is beating SPY.

Trades on the same day share that day's tape (and often the same symbols), so they aren't
independent. Intervals are therefore clustered by trading day: the evidence grows with the
number of distinct days traded, not just the trade count.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics

from trader.golive import MIN_TRADES, SLIPPAGE_PER_SIDE_PCT, START_DATE

MIN_TO_JUDGE = 10  # below this many closed trades, don't even show a verdict on the sign
MIN_DAYS = 3  # ...or this many distinct trading days
FAMILIES = ("novel", "conventional", "control", "unlabelled")
EXPERIMENT_START = START_DATE  # the live board, like the go-live gate, ignores earlier test sessions

# Two-sided 95% Student-t critical values by degrees of freedom; 1.96 beyond the table.
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26,
        10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04, 60: 2.00, 120: 1.98}


def t95(df: float) -> float:
    if df < 1:
        return math.inf
    keys = [k for k in _T95 if k <= df]
    return _T95[max(keys)] if df <= 120 else 1.96


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def closed_trades(rows: list[dict], slippage_per_side_pct: float = SLIPPAGE_PER_SIDE_PCT,
                  since: str | None = None) -> list[dict]:
    """Closed round trips from a trades.csv, oldest first, each with its net return.
    Slippage is charged on the entry's notional, matched to the latest earlier buy of the same
    classifier and symbol (scale-out legs, `sell_part`, are part of the final sell's P&L).
    Malformed rows are skipped rather than failing the page."""
    open_cost: dict[tuple[str, str], float] = {}
    out = []
    slip = 2 * slippage_per_side_pct
    for r in sorted((r for r in rows if isinstance(r.get("time"), str)), key=lambda r: r["time"]):
        key = (r.get("classifier") or "?", r.get("symbol") or "?")
        if r.get("side") == "buy":
            # A late fill (#59) adds to the same position: it is more of the same cost.
            late = str(r.get("reason") or "").startswith("ENTER (late fill)")
            open_cost[key] = (open_cost.get(key, 0.0) if late else 0.0) + _num(r.get("notional"))
        elif r.get("side") == "sell" and r.get("pnl_pct"):
            pnl_pct = _num(r.get("pnl_pct"), None)
            if pnl_pct is None:
                continue
            cost = open_cost.pop(key, None)
            if cost is None:  # buy row missing (e.g. trimmed history): recover cost from the sell
                cost = _num(r.get("notional")) / (1 + pnl_pct / 100)
            if since and r["time"][:10] < since:
                continue
            out.append({
                "time": r["time"], "day": r["time"][:10], "classifier": key[0], "symbol": key[1],
                "net_pct": pnl_pct - slip, "net_usd": _num(r.get("pnl")) - cost * slip / 100,
            })
    return out


def _clustered(trades: list[dict]) -> tuple[float | None, float | None, int]:
    """(mean, variance of the mean, number of days), with day-clustered standard errors."""
    n = len(trades)
    if not n:
        return None, None, 0
    mean = statistics.fmean(t["net_pct"] for t in trades)
    sums: dict[str, float] = {}
    for t in trades:
        sums[t["day"]] = sums.get(t["day"], 0.0) + t["net_pct"] - mean
    g = len(sums)
    if g < 2:
        return mean, None, g
    return mean, g / (g - 1) * sum(s * s for s in sums.values()) / (n * n), g


def summarise(trades: list[dict]) -> dict:
    """n, mean net return per trade (%), its 95% interval (day-clustered), win rate, verdict."""
    n = len(trades)
    mean, var, g = _clustered(trades)
    half = t95(g - 1) * math.sqrt(var) if var is not None else None
    wins = sum(t["net_pct"] > 0 for t in trades)
    return {"n": n, "days": g, "mean_pct": mean, "ci_pct": half, "win_rate": wins / n if n else None,
            "verdict": verdict(n, g, mean, half)}


def verdict(n: int, days: int, mean: float | None, half: float | None, behind: str = "losing") -> str:
    if n == 0:
        return "no trades yet"
    if n < MIN_TO_JUDGE or days < MIN_DAYS or half is None or not math.isfinite(half):
        return f"too few to judge ({n} trades, {days} days)"
    if mean - half > 0:
        return "ahead: 95% interval above zero"
    if mean + half < 0:
        return f"{behind}: 95% interval below zero"
    return "can't tell from luck yet"


def compare(a: list[dict], b: list[dict]) -> dict | None:
    """Difference in mean net return per trade, a minus b, with a 95% interval from the two
    day-clustered variances (conservative df: the smaller side's days minus one)."""
    ma, va, ga = _clustered(a)
    mb, vb, gb = _clustered(b)
    if va is None or vb is None or va + vb == 0:
        return None
    diff = ma - mb
    half = t95(min(ga, gb) - 1) * math.sqrt(va + vb)
    return {"diff_pct": diff, "ci_pct": half,
            "verdict": verdict(min(len(a), len(b)), min(ga, gb), diff, half, behind="behind")}


# ---- the objective: daily returns of the whole book vs SPY (descriptive per-trade stats above) ----

SESSION_MIN = 390
MIN_DAYS_TO_JUDGE = 10  # sessions before the daily comparison with SPY gets a verdict


def _daily_closes(rows: list[dict], key: str, day_key: str = "time") -> dict[str, float]:
    """Last positive value of `key` per day, from rows in time order."""
    out: dict[str, float] = {}
    for r in sorted((r for r in rows if isinstance(r.get(day_key), str)), key=lambda r: r[day_key]):
        v = _num(r.get(key), None)
        if v is not None and v > 0:
            out[r[day_key][:10]] = v
    return out


def _returns(closes: dict[str, float], first_base: float | None) -> dict[str, float]:
    """Day -> return (%) from the previous day's close (the first day's from `first_base`)."""
    out, prev = {}, first_base
    for d in sorted(closes):
        if prev:
            out[d] = (closes[d] / prev - 1) * 100
        prev = closes[d]
    return out


def _max_drawdown(rets: list[float]) -> float:
    """Largest peak-to-trough fall (%, <= 0) of the compounded path, starting from 1."""
    level = peak = 1.0
    worst = 0.0
    for r in rets:
        level *= 1 + r / 100
        peak = max(peak, level)
        worst = min(worst, (level / peak - 1) * 100)
    return worst


def _series(rets: dict[str, float]) -> dict:
    v = [rets[d] for d in sorted(rets)]
    total = math.prod(1 + r / 100 for r in v) - 1 if v else None
    return {"days": len(v), "total_pct": None if total is None else total * 100,
            "mean_pct": statistics.fmean(v) if v else None,
            "sd_pct": statistics.stdev(v) if len(v) > 1 else None,
            "max_drawdown_pct": _max_drawdown(v) if v else None}


def _exposure(trades: list[dict], day_equity: dict[str, float]) -> dict[str, float]:
    """Day -> average share of the day's starting equity held in positions over the session (%).
    Time-weighted from buy to final sell (scale-outs are ignored, so it errs high)."""
    opened: dict[tuple[str, str], tuple[dt.datetime, float]] = {}
    held: dict[str, float] = {}
    for r in sorted((r for r in trades if isinstance(r.get("time"), str)), key=lambda r: r["time"]):
        key = (r.get("classifier") or "?", r.get("symbol") or "?")
        try:
            t = dt.datetime.fromisoformat(r["time"])
        except ValueError:
            continue
        if r.get("side") == "buy":
            start, notional = opened.get(key, (t, 0.0))
            opened[key] = (start, notional + _num(r.get("notional")))
        elif r.get("side") == "sell" and key in opened:
            start, notional = opened.pop(key)
            day = start.date().isoformat()
            held[day] = held.get(day, 0.0) + notional * max(0.0, (t - start).total_seconds() / 60)
    return {d: min(100.0, held.get(d, 0.0) / (SESSION_MIN * eq) * 100) for d, eq in day_equity.items() if eq > 0}


def daily(equity: list[dict], benchmark: list[dict] | None = None, trades: list[dict] | None = None,
          from_date: str | None = None) -> dict | None:
    """The book's daily return series from its equity.csv (unit NAV, so deposits aren't gains),
    one row per session it marked, zero-trade days included, against buy-and-hold SPY on the same
    days. This is the primary measure of the objective: two books with the same daily returns
    score the same, however many trades they took. The SPY comparison pairs the days both have
    and treats days as independent (an approximation: a 95% interval, no serial correction)."""
    key = "nav" if any(_num(r.get("nav"), None) for r in equity) else "equity"
    closes = _daily_closes(equity, key)
    first_day = min(closes, default=None)
    first = next((_num(r.get(key), None) for r in sorted(equity, key=lambda r: str(r.get("time")))
                  if str(r.get("time", ""))[:10] == first_day), None)
    rets = {d: r for d, r in _returns(closes, first).items() if not from_date or d >= from_date}
    if not rets:
        return None
    day_equity = {}
    for r in sorted((r for r in equity if isinstance(r.get("time"), str)), key=lambda r: r["time"]):
        day_equity.setdefault(r["time"][:10], _num(r.get("equity")))
    exp = _exposure(trades or [], {d: day_equity.get(d, 0.0) for d in rets})
    traded = {t["time"][:10] for t in trades or [] if isinstance(t.get("time"), str) and t.get("side") == "buy"}
    out = {"book": _series(rets) | {
        "exposure_pct": statistics.fmean(exp.get(d, 0.0) for d in rets),
        "days_traded": sum(d in traded for d in rets)},
        "rows": [{"day": d, "return_pct": round(rets[d], 4), "exposure_pct": round(exp.get(d, 0.0), 2)} for d in sorted(rets)],
        "spy": None, "vs_spy": None}
    bench = [r for r in benchmark or [] if _num(r.get("spy_open"), 0) > 0 and _num(r.get("spy_close"), 0) > 0]
    if bench:
        bench.sort(key=lambda r: str(r.get("date")))
        spy_closes = {str(r["date"])[:10]: _num(r["spy_close"]) for r in bench}
        spy = _returns(spy_closes, _num(bench[0]["spy_open"]))
        both = sorted(set(rets) & set(spy))
        if both:
            out["spy"] = _series({d: spy[d] for d in both})
            out["book_on_spy_days"] = _series({d: rets[d] for d in both})
            for row in out["rows"]:
                row["spy_pct"] = round(spy[row["day"]], 4) if row["day"] in spy else None
            diff = [rets[d] - spy[d] for d in both]
            mean = statistics.fmean(diff)
            half = t95(len(diff) - 1) * statistics.stdev(diff) / math.sqrt(len(diff)) if len(diff) > 1 else None
            if len(diff) < MIN_DAYS_TO_JUDGE or half is None:
                v = f"too few to judge ({len(diff)} days)"
            elif mean - half > 0:
                v = "ahead of SPY: 95% interval above zero"
            elif mean + half < 0:
                v = "behind SPY: 95% interval below zero"
            else:
                v = "can't tell from luck yet"
            out["vs_spy"] = {"days": len(diff), "mean_diff_pct": mean, "ci_pct": half, "verdict": v}
    return out


def build(rows: list[dict], families: dict[str, str], current: set[str], start_equity: float | None,
          since: dict[str, str] | None = None, slippage_per_side_pct: float = SLIPPAGE_PER_SIDE_PCT,
          from_date: str | None = None, equity: list[dict] | None = None,
          benchmark: list[dict] | None = None) -> dict:
    """Scoreboard for one book.

    families: classifier id -> novel | conventional (anything else shows as "unlabelled";
              control_* ids are always control).
    current: ids in today's classifiers file; the rest are shown as retired.
    since: id -> date its current spec started (the paper book's promotion record), else None.
    slippage_per_side_pct: 0 for replays, whose simulated fills already include it.
    from_date: ignore trades before this date (the experiment's start, for the live board).
    equity, benchmark: the book's equity.csv and SPY's benchmark.csv rows, for `daily` (the
              objective; omitted when there's no equity file).
    """
    trades = closed_trades(rows, slippage_per_side_pct, from_date)
    # Said on the board, so a 0 there isn't mistaken for "nothing traded" (#148).
    hidden = len(closed_trades(rows, slippage_per_side_pct)) - len(trades) if from_date else 0

    def fam(cid: str) -> str:
        if cid.startswith("control_"):
            return "control"
        f = families.get(cid)
        return f if f in ("novel", "conventional") else "unlabelled"

    days = sorted({t["day"] for t in trades})
    by_cls: dict[str, list[dict]] = {}
    for t in trades:
        by_cls.setdefault(t["classifier"], []).append(t)
    for cid in current:
        by_cls.setdefault(cid, [])

    def curve(ts: list[dict]) -> list[float]:
        """Cumulative net P&L at the end of each day, as % of the book's starting equity."""
        per_day: dict[str, float] = {}
        for t in ts:
            per_day[t["day"]] = per_day.get(t["day"], 0.0) + t["net_usd"]
        out, cum = [], 0.0
        for d in days:
            cum += per_day.get(d, 0.0)
            out.append(round(cum / start_equity * 100, 4) if start_equity else round(cum, 4))
        return out

    classifiers = []
    for cid, ts in by_cls.items():
        row = {"id": cid, "family": fam(cid), "active": cid in current, **summarise(ts),
               "net_usd": round(sum(t["net_usd"] for t in ts), 2), "curve": curve(ts)}
        if since and cid in since and fam(cid) != "control":
            # Counted like golive.shadow_record: nothing before the start date, even when all days are shown.
            start = max(since[cid], EXPERIMENT_START.isoformat())
            row["promotion"] = {"n": sum(t["day"] >= start for t in ts), "of": MIN_TRADES, "since": start}
        classifiers.append(row)
    classifiers.sort(key=lambda r: (FAMILIES.index(r["family"]), not r["active"], -r["net_usd"], r["id"]))

    pooled = {f: [t for t in trades if fam(t["classifier"]) == f] for f in FAMILIES}
    fams = [{"family": f, "classifiers": sum(r["family"] == f for r in classifiers), **summarise(ts),
             "net_usd": round(sum(t["net_usd"] for t in ts), 2), "curve": curve(ts)}
            for f, ts in pooled.items() if ts or any(r["family"] == f for r in classifiers)]
    return {
        "days": days, "units": "% of starting equity" if start_equity else "$", "hidden_before_start": hidden,
        "slippage_per_side_pct": slippage_per_side_pct,
        "classifiers": classifiers, "families": fams,
        "novel_vs_conventional": compare(pooled["novel"], pooled["conventional"]),
        "novel_vs_control": compare(pooled["novel"], pooled["control"]),
        "daily": daily(equity, benchmark, rows, from_date) if equity else None,
    }
