"""Historical replay through the real engine with a simulated broker.

Used for the strategist's backtests, the custom-feature gate, and dashboard demos
(`--pace` slows ticks down so you can watch a session unfold).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd

from trader import config
from trader.broker import SimBroker
from trader.classifier import ClassifierSpec
from trader.data import ET, fetch, prior_sessions, split_sessions
from trader.engine import Book, Engine


def load_sessions(symbols, start: dt.date, end: dt.date, secrets, source="alpaca"):
    """{symbol: {date: bars}} for sessions in [start - a few days, end]."""
    t0 = dt.datetime.combine(start - dt.timedelta(days=6), dt.time(0), ET)
    t1 = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), ET)
    raw = fetch(sorted(set(symbols)), t0, t1, secrets, source)
    return {s: split_sessions(b) for s, b in raw.items()}


def replay(
    specs: list[ClassifierSpec],
    start: dt.date,
    end: dt.date,
    decider,
    universe: set[str],
    run_dir: Path,
    secrets: dict,
    source: str = "alpaca",
    cash: float | None = None,  # config.yaml capital.replay_cash
    pace: float = 0.0,
    sessions=None,
) -> dict:
    if cash is None:
        cash = float(config.SETTINGS.capital.replay_cash)
    symbols = {s for sp in specs for s in sp.symbols} | {"SPY"}
    sessions = sessions or load_sessions(symbols, start, end, secrets, source)
    days = sorted({d for per in sessions.values() for d in per if start <= d <= end})
    if run_dir.exists():  # a rerun under the same name starts fresh, never resumes old state
        import shutil

        # Only ever delete a direct child of a replays directory (never `..`, `/`, symlink tricks).
        resolved = run_dir.resolve()
        if run_dir.is_symlink() or resolved.parent != run_dir.parent.resolve() or resolved.name in ("", ".", ".."):
            raise ValueError(f"refusing to delete {run_dir}")
        shutil.rmtree(resolved)
    book = Book("sim", SimBroker(cash), run_dir / "sim")
    day_alerts: list[str] = []  # e.g. a stale/missing SPY feed blocking entries; kept in the summary
    # A replay tests each spec alone on one account, whatever its mode.
    books = {"live": book, "shadow": book} | {s.book_key: book for s in specs if s.mode == "sim"}
    engine = Engine(specs, books, decider, universe, run_dir,
                    alert=lambda level, msg: day_alerts.append(f"{level}: {msg}"))
    results = {}
    for day in days:
        engine.start_day(day, prior_sessions(sessions, day))  # one feed throughout: its own volume
        day_alerts.clear()
        today = {s: per[day] for s, per in sessions.items() if day in per}
        stamps = sorted(set().union(*(b.index for b in today.values())))
        close = dt.datetime.combine(day, dt.time(16, 0), ET)
        for ts in stamps:
            now = ts + pd.Timedelta(minutes=1)  # bar labelled 09:30 is complete at 09:31
            bars = {s: b.loc[:ts] for s, b in today.items()}
            engine.tick(now.to_pydatetime(), bars, (close - now).total_seconds() / 60)
            if pace:
                time.sleep(pace)
        results[day.isoformat()] = engine.end_day(close)
        if day_alerts:
            results[day.isoformat()]["alerts"] = list(day_alerts)
    summary = {
        "start": start.isoformat(), "end": end.isoformat(), "days": results,
        "decision_calls": getattr(decider, "calls", 0),
        "decision_cost_usd": round(getattr(decider, "total_cost", 0.0), 5),
        "final_equity": round(book.broker.equity(), 2), "start_equity": cash,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary
