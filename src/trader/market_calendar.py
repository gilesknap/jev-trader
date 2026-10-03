"""Exchange sessions: each trading day's open and close in ET, from Alpaca's calendar.

One source of session times for the runner, replays, the custom-feature gate's sample
sessions and probe scoring, so an early close (13:00 ET) means the same everywhere: the
flatten at close - 15 min, no entries after it, and no bars or outcomes past the close
(post-market prints after an early close are not session bars). Look a calendar up once
per run (one API call for a date range), never per bar.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from trader.data import ET
from trader.guardrails import FLATTEN_MINUTES_BEFORE_CLOSE

REGULAR_OPEN, REGULAR_CLOSE = dt.time(9, 30), dt.time(16, 0)


@dataclass(frozen=True)
class Session:
    open: dt.datetime  # tz-aware ET
    close: dt.datetime

    @property
    def day(self) -> dt.date:
        return self.open.date()

    @property
    def flatten_at(self) -> dt.datetime:
        """When the end-of-day flatten runs: the first tick this many minutes before the close."""
        return self.close - dt.timedelta(minutes=FLATTEN_MINUTES_BEFORE_CLOSE)

    @property
    def last_bar(self) -> dt.datetime:
        """Label of the session's last 1-min bar (a bar labelled 15:59 completes at 16:00)."""
        return self.close - dt.timedelta(minutes=1)

    @property
    def minutes(self) -> float:
        return (self.close - self.open).total_seconds() / 60


def regular_session(day: dt.date) -> Session:
    return Session(dt.datetime.combine(day, REGULAR_OPEN, ET), dt.datetime.combine(day, REGULAR_CLOSE, ET))


def _as_et(x: dt.datetime) -> dt.datetime:
    return x.replace(tzinfo=ET) if x.tzinfo is None else x.astimezone(ET)


def session_from_row(row) -> Session:
    """A Session from one Alpaca calendar row (naive times are ET)."""
    return Session(_as_et(row.open), _as_et(row.close))


class Calendar:
    """Sessions by date. `assumed`: built without the exchange calendar (regular 09:30-16:00
    weekdays), for offline tests and keyless dev data; say so wherever it's used."""

    def __init__(self, sessions: dict[dt.date, Session] | None = None, assumed: bool = False):
        self.sessions, self.assumed = dict(sessions or {}), assumed

    @classmethod
    def regular(cls, start: dt.date, end: dt.date) -> Calendar:
        days = (start + dt.timedelta(days=i) for i in range((end - start).days + 1))
        return cls({d: regular_session(d) for d in days if d.weekday() < 5}, assumed=True)

    def session(self, day: dt.date) -> Session:
        """The day's session; a regular one if the calendar doesn't cover the day."""
        return self.sessions.get(day) or regular_session(day)

    def trim(self, per_day: dict[dt.date, pd.DataFrame]) -> dict[dt.date, pd.DataFrame]:
        """Each day's bars without those labelled at or after its close (post-market prints after
        an early close look like regular-hours bars to split_sessions). A day left empty is dropped."""
        out = {d: b[b.index < self.session(d).close] for d, b in per_day.items()}
        return {d: b for d, b in out.items() if len(b)}


def fetch_calendar(client, start: dt.date, end: dt.date) -> Calendar:
    """The exchange calendar for [start, end] in one call to an Alpaca trading client."""
    from alpaca.trading.requests import GetCalendarRequest

    rows = client.get_calendar(GetCalendarRequest(start=start, end=end))
    return Calendar({r.date: session_from_row(r) for r in rows})


def load_calendar(secrets: dict, start: dt.date, end: dt.date) -> Calendar:
    """From Alpaca when there are keys (an error raises: a guessed close would be silently
    wrong); without keys (offline tests, keyless dev data) the assumed regular calendar."""
    if not secrets.get("ALPACA_PAPER_KEY"):
        return Calendar.regular(start, end)
    from alpaca.trading.client import TradingClient

    client = TradingClient(secrets["ALPACA_PAPER_KEY"], secrets["ALPACA_PAPER_SECRET"], paper=True)
    return fetch_calendar(client, start, end)
