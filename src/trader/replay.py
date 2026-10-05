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
from trader.market_calendar import Calendar, load_calendar


def load_sessions(symbols, start: dt.date, end: dt.date, secrets, source="alpaca", calendar: Calendar | None = None):
    """{symbol: {date: bars}} for sessions in [start - a few days, end], each cut at its close."""
    t0 = dt.datetime.combine(start - dt.timedelta(days=6), dt.time(0), ET)
    t1 = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), ET)
    calendar = calendar or load_calendar(secrets, t0.date(), end)
    raw = fetch(sorted(set(symbols)), t0, t1, secrets, source)
    return {s: calendar.trim(split_sessions(b)) for s, b in raw.items()}


def _replay_news(specs, decider, secrets) -> tuple[object | None, dict]:
    """(news source, summary notes) for specs with extra inputs (#73). Headlines come from
    Alpaca's news history, cut at each decision's time; the stub decider never reads them, so
    it gets none. The daily note can't be reproduced for past days: no replay sends one."""
    from trader import jev_inputs as N

    notes: dict = {}
    if any("daily_note" in s.inputs for s in specs):
        notes["daily_note"] = "not available in replays: specs asking for it were sent an empty note"
    if any("playbook" in s.inputs for s in specs):
        notes["playbook"] = "not available in replays: specs asking for it were sent an empty playbook"
    if not any("headlines" in s.inputs for s in specs):
        return None, notes
    if getattr(decider, "offline", False):
        notes["headlines"] = "not fetched: the offline stub decider doesn't read them"
        return None, notes
    try:
        return N.HistoricalNews(N.AlpacaNewsAPI(secrets), config.REPLAY_DIR / ".news"), notes
    except Exception as e:
        notes["headlines"] = f"unavailable ({e!r}): specs asking for them were sent none"
        return None, notes


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
    calendar: Calendar | None = None,  # the exchange calendar (default: Alpaca's, assumed regular without keys)
) -> dict:
    if cash is None:
        cash = float(config.SETTINGS.capital.replay_cash)
    symbols = {s for sp in specs for s in sp.symbols} | {"SPY"}
    calendar = calendar or load_calendar(secrets, start - dt.timedelta(days=6), end)
    sessions = sessions or load_sessions(symbols, start, end, secrets, source, calendar)
    sessions = {s: calendar.trim(per) for s, per in sessions.items()}  # no bars past a (early) close
    days = sorted({d for per in sessions.values() for d in per if start <= d <= end})
    if run_dir.exists():  # a rerun under the same name starts fresh, never resumes old state
        import shutil

        # Only ever delete a direct child of a replays directory (never `..`, `/`, symlink tricks).
        resolved = run_dir.resolve()
        if run_dir.is_symlink() or resolved.parent != run_dir.parent.resolve() or resolved.name in ("", ".", ".."):
            raise ValueError(f"refusing to delete {run_dir}")
        shutil.rmtree(resolved)
    broker = SimBroker(cash)
    book = Book("sim", broker, run_dir / "sim")
    day_alerts: list[str] = []  # e.g. a stale/missing SPY feed blocking entries; kept in the summary
    # A replay tests each spec alone on one account, whatever its mode.
    books = {"live": book, "shadow": book} | {s.book_key: book for s in specs if s.mode == "sim"}
    news, inputs_note = _replay_news(specs, decider, secrets)
    engine = Engine(
        specs,
        books,
        decider,
        universe,
        run_dir,
        alert=lambda level, msg: day_alerts.append(f"{level}: {msg}"),
        news=news,
    )
    news_errors = 0
    results = {}
    for day in days:
        engine.start_day(day, prior_sessions(sessions, day))  # one feed throughout: its own volume
        day_alerts.clear()
        today = {s: per[day] for s, per in sessions.items() if day in per}
        stamps = sorted(set().union(*(b.index for b in today.values())))
        close = calendar.session(day).close  # an early close flattens and stops entries early, as live
        for ts in stamps:
            now = ts + pd.Timedelta(minutes=1)  # bar labelled 09:30 is complete at 09:31
            bars = {s: b.loc[:ts] for s, b in today.items()}
            # The engine decides on bars up to `ts` only. A market order it sends now executes at
            # the next bar's open, not at the close it just saw; none after the last bar of
            # the session: the close, as the live runner would.
            broker.next_open = {
                s: float(b.open.iloc[i])
                for s, b in today.items()
                for i in [b.index.searchsorted(ts, side="right")]
                if i < len(b)
            }
            engine.tick(now.to_pydatetime(), bars, (close - now).total_seconds() / 60)
            if pace:
                time.sleep(pace)
        broker.next_open = {}
        news_errors += engine.news_errors  # start_day resets the engine's count
        results[day.isoformat()] = engine.end_day(close)
        if day_alerts:
            results[day.isoformat()]["alerts"] = list(day_alerts)
    summary = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": results,
        "decision_calls": getattr(decider, "calls", 0),
        "decision_cost_usd": round(getattr(decider, "total_cost", 0.0), 5),
        "final_equity": round(book.broker.equity(), 2),
        "start_equity": cash,
        **({"calendar": "assumed regular 09:30-16:00 sessions (no exchange calendar)"} if calendar.assumed else {}),
        **inputs_note,
        **({"news_errors": news_errors} if news is not None else {}),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary
