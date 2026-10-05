"""The decision model's optional inputs (a spec's `inputs:`, #73): per-symbol news headlines,
and the pre-market run's daily note and per-classifier playbooks (forward-only: never in replays).
See ADR 0018 for why these exist.

Headlines:

Alpaca's news API, read with a short timeout and no SDK retries, so a slow or failing news
call can't hold up a tick. Every source cuts at the decision time: only items published at
or before `now` are returned, so a replay never shows Jev a headline from its future.

- `LiveNews` (the runner) re-fetches a symbol at most every `REFRESH_S` seconds, and after a
  failure fetches nothing for as long again.
- `HistoricalNews` (replays) fetches each symbol's whole day once (the look-back plus the
  session) and caches it on disk for days already over.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import httpx

from trader.data import ET

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
LOOKBACK = dt.timedelta(hours=18)  # covers the previous evening and pre-market
MAX_ITEMS = 5
SUMMARY_CHARS = 240
REFRESH_S = 180
TIMEOUT_S = 3.0
REPLAY_PAGES = 6  # up to 300 items per symbol-day


class NewsError(Exception):
    pass


def _parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def as_of(items: list[dict], now: dt.datetime, lookback: dt.timedelta = LOOKBACK, n: int = MAX_ITEMS) -> list[dict]:
    """The newest `n` items published in (now - lookback, now], newest first, in the shape Jev
    sees: when (ET), how many minutes before now, the headline and a short summary."""
    keep = []
    for it in items:
        try:
            t = _parse_time(it["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - lookback < t <= now and it.get("headline"):
            keep.append((t, it))
    keep.sort(key=lambda p: p[0], reverse=True)
    out = []
    for t, it in keep[:n]:
        summary = " ".join(str(it.get("summary") or "").split())
        if len(summary) > SUMMARY_CHARS:
            summary = summary[: SUMMARY_CHARS - 1].rstrip() + "…"
        out.append(
            {
                "at": t.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                "minutes_ago": int((now - t).total_seconds() // 60),
                "headline": " ".join(str(it["headline"]).split()),
                **({"summary": summary} if summary else {}),
            }
        )
    return out


class AlpacaNewsAPI:
    """One page (newest first) of a symbol's news in [start, end]; raises NewsError on any failure."""

    def __init__(self, secrets: dict[str, str], timeout: float = TIMEOUT_S):
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "APCA-API-KEY-ID": secrets["ALPACA_PAPER_KEY"],
                "APCA-API-SECRET-KEY": secrets["ALPACA_PAPER_SECRET"],
            },
        )

    def fetch(self, symbol: str, start: dt.datetime, end: dt.datetime, pages: int = 1) -> list[dict]:
        """Newest first, 50 a page. Live wants only the newest page; a replay asks for more, so a
        busy symbol's morning items aren't crowded out by the afternoon's."""
        params = {
            "symbols": symbol,
            "start": start.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 50,
            "sort": "desc",
        }
        out: list[dict] = []
        for _ in range(pages):
            try:
                r = self._http.get(NEWS_URL, params=params)
                if r.status_code != 200:
                    raise NewsError(f"HTTP {r.status_code}: {r.text[:200]}")
                body = r.json()
                news = body.get("news")
            except (httpx.HTTPError, ValueError, AttributeError) as e:
                raise NewsError(repr(e)) from e
            if not isinstance(news, list):
                raise NewsError("no news list in the response")
            keys = ("created_at", "headline", "summary")
            out += [{k: it.get(k) for k in keys} for it in news if isinstance(it, dict)]
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out


class LiveNews:
    """Cached per symbol; refreshed at most every `refresh_s` seconds of wall-clock time. After any
    failure no symbol is fetched for `refresh_s` (each fetch can take up to its timeout, and a
    down API must not cost every symbol's ask that long): asks get what was cached, or nothing."""

    def __init__(self, api, refresh_s: float = REFRESH_S, clock=time.monotonic):
        self.api, self.refresh_s, self.clock = api, refresh_s, clock
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._down_until = -1e18

    def headlines(self, symbol: str, now: dt.datetime) -> list[dict]:
        t = self.clock()
        hit = self._cache.get(symbol)
        if (hit is None or t - hit[0] >= self.refresh_s) and t >= self._down_until:
            try:
                items = self.api.fetch(symbol, now - LOOKBACK, now)
            except NewsError:
                self._down_until = t + self.refresh_s
                raise
            self._cache[symbol] = (t, items)
            hit = self._cache[symbol]
        return as_of(hit[1], now) if hit else []


class HistoricalNews:
    """For replays: each (symbol, day) fetched once, from the look-back before the open to the
    close, then cut at each decision's time. Days before `today` are cached under `cache_dir`."""

    def __init__(self, api, cache_dir: Path | None = None, today: dt.date | None = None):
        self.api, self.cache_dir = api, cache_dir
        self.today = today or dt.datetime.now(ET).date()
        self._mem: dict[tuple[str, dt.date], list[dict]] = {}

    def _day(self, symbol: str, day: dt.date) -> list[dict]:
        key = (symbol, day)
        if key in self._mem:
            return self._mem[key]
        path = self.cache_dir / day.isoformat() / f"{symbol}.json" if self.cache_dir else None
        if path is not None and path.is_file():
            try:
                items = json.loads(path.read_text())
                self._mem[key] = items
                return items
            except (OSError, ValueError):
                pass
        open_ = dt.datetime.combine(day, dt.time(9, 30), ET)
        items = self.api.fetch(symbol, open_ - LOOKBACK, dt.datetime.combine(day, dt.time(16), ET), pages=REPLAY_PAGES)
        self._mem[key] = items
        if path is not None and day < self.today:  # a finished day's news won't change
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(items))
            except OSError:
                pass
        return items

    def headlines(self, symbol: str, now: dt.datetime) -> list[dict]:
        return as_of(self._day(symbol, now.astimezone(ET).date()), now)


NOTE_MAX_CHARS = 3000


def read_daily_note(path: Path, day: dt.date) -> tuple[str | None, str | None]:
    """(note, problem) for `day`, from the pre-market run's `state/daily_note.md`. The note counts
    only if its first line carries the day's date (YYYY-MM-DD): a note left over from an earlier
    day, when the pre-market run failed, would tell Jev about the wrong day's events. Over
    NOTE_MAX_CHARS it's cut, and that's reported. Read without following symlinks, like the
    strategist's other files."""
    from trader.safeio import read_text

    try:
        text = read_text(path).strip()
    except FileNotFoundError:
        return None, f"no {path.name}"
    except (OSError, ValueError) as e:
        return None, f"{path.name} unreadable ({e})"
    if not text:
        return None, f"{path.name} is empty"
    first = text.splitlines()[0]
    if day.isoformat() not in first:
        return None, f"{path.name} isn't dated {day.isoformat()} on its first line (stale?)"
    if len(text) > NOTE_MAX_CHARS:
        return text[:NOTE_MAX_CHARS], f"{path.name} cut to {NOTE_MAX_CHARS} characters (it has {len(text)})"
    return text, None


def read_playbooks(path: Path, day: dt.date) -> tuple[dict[str, str], str | None]:
    """({classifier id: playbook}, problem) for `day`, from the pre-market run's
    `state/playbook.yaml`:

        date: 2026-10-06
        playbooks:
          my_idea: |
            ...

    A missing, stale (another `date:`), malformed or empty file gives no playbooks, with the
    problem said; it is never an error. Each playbook over NOTE_MAX_CHARS is cut."""
    import yaml

    from trader.safeio import read_text

    try:
        raw = yaml.safe_load(read_text(path))
    except FileNotFoundError:
        return {}, f"no {path.name}"
    except (OSError, ValueError, yaml.YAMLError) as e:
        return {}, f"{path.name} unreadable ({type(e).__name__})"
    if not raw:
        return {}, f"{path.name} is empty"
    if not isinstance(raw, dict) or not isinstance(raw.get("playbooks"), dict):
        return {}, f"{path.name} has no `playbooks:` mapping"
    date = raw.get("date")
    date = date.isoformat() if isinstance(date, dt.date) else str(date)
    if date != day.isoformat():
        return {}, f"{path.name} is dated {date}, not {day.isoformat()} (stale?)"
    out, cut = {}, []
    for cid, text in raw["playbooks"].items():
        text = str(text or "").strip()
        if len(text) > NOTE_MAX_CHARS:
            text = text[:NOTE_MAX_CHARS]
            cut.append(str(cid))
        if text:
            out[str(cid)] = text
    return out, (f"{path.name}: cut to {NOTE_MAX_CHARS} characters for {', '.join(cut)}" if cut else None)
