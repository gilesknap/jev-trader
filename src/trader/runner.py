"""The market-hours daemon: one invocation runs one session, then exits.

Started daily by a systemd timer well before the open; it asks Alpaca's calendar
whether today is a trading day (handles holidays, half-days and UK/US DST offsets),
waits for the open, streams IEX minute bars and ticks the engine each minute.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

from trader import config
from trader import features as F
from trader import guardrails as G
from trader.alerts import notify
from trader.broker import AlpacaBroker
from trader.data import ET, GATE_LOOKBACKS, GateSampleError, fetch_alpaca, gate_samples, prior_sessions, split_sessions
from trader.engine import WIND_DOWN, Book, Engine, stale_feed
from trader.market_calendar import Calendar, fetch_calendar, session_from_row

BOOKS_DIR = config.RUNTIME_DIR / "books"
# The free IEX websocket takes at most this many symbols. One subscribe over the limit is
# refused whole, which at startup leaves the stream with no symbols at all.
STREAM_SYMBOL_LIMIT = 30
EQUITY_TICKER = re.compile(r"[A-Z][A-Z.]{0,5}")  # what the stock bars API and stream accept
# While the stream is stale, held symbols' bars are polled over REST at most this long per tick,
# well inside the tick (it starts at :04 and gives decisions 35 s), so a slow API can't delay it.
REST_POLL_TIMEOUT_S = 6.0
# A fetch still running after this long is given up on and another started; at most
# REST_MAX_CALLS run at once, so a hanging API can't pile up threads.
REST_HUNG_S = 300.0
REST_MAX_CALLS = 2
# Each history fetch on the session-start path (the feature gate's samples, prior-session SIP and
# IEX bars) gets at most this long. They run about 2 minutes before the open, in turn, before the
# stream subscribes, so a hung call can't hold the start indefinitely. A failed prior-session fetch
# costs NaN context features; a failed gate-sample fetch fails the classifiers file loudly (as any
# gate-sample failure does: nothing trades that day, held positions are still managed). Today's
# bars at a late start get CATCH_UP_TIMEOUT_S.
STARTUP_FETCH_TIMEOUT_S = 60.0
CATCH_UP_TIMEOUT_S = 15.0
BAR_COLS = ["ts", "open", "high", "low", "close", "volume"]


def _fetch_within(timeout: float, *args, **kw) -> dict[str, pd.DataFrame]:
    """fetch_alpaca(*args, **kw), or TimeoutError after `timeout` s. The client has no request
    timeout of its own, so the call runs in a daemon thread that a hung API can't keep alive."""
    out: dict = {}

    def run():
        try:
            out["bars"] = fetch_alpaca(*args, **kw)
        except Exception as e:
            out["error"] = e

    call = threading.Thread(target=run, daemon=True)
    call.start()
    call.join(timeout)
    if "error" in out:
        raise out["error"]
    if "bars" not in out:
        raise TimeoutError(f"no answer in {timeout:g} s")
    return out["bars"]


def _session_today(client):
    from alpaca.trading.requests import GetCalendarRequest

    today = dt.datetime.now(ET).date()
    cal = client.get_calendar(GetCalendarRequest(start=today, end=today))
    if not cal or cal[0].date != today:
        return None
    s = session_from_row(cal[0])
    return s.open, s.close


CALENDAR_TRIES, CALENDAR_RETRY_S = 3, 5.0
CALENDAR_ALERT_EVERY_S = 3600  # systemd restarts the runner every 30 s: page hourly, not each time


def _saved_session(today: dt.date):
    """Today's open and close as persisted by an earlier start today; None if absent, another
    day's, or unreadable. Never raises."""
    try:
        s = json.loads((config.RUNTIME_DIR / "session.json").read_text())
        open_, close = (dt.datetime.fromisoformat(s[k]) for k in ("open", "close"))
        if s["day"] != today.isoformat() or open_.tzinfo is None or close.tzinfo is None:
            return None
        open_, close = open_.astimezone(ET), close.astimezone(ET)
        return (open_, close) if open_.date() == today and open_ < close else None
    except Exception:
        return None


def _calendar_alert(key: str, msg: str) -> None:
    """Urgent, at most hourly per key across restarts. Never raises."""
    p = config.RUNTIME_DIR / "calendar_alerts.json"
    try:
        last = json.loads(p.read_text())
        last = last if isinstance(last, dict) else {}
    except Exception:
        last = {}
    now = time.time()
    try:
        if now - float(last.get(key, 0)) < CALENDAR_ALERT_EVERY_S:
            return
    except (TypeError, ValueError):
        pass
    notify("urgent", msg)
    try:
        _atomic_json(p, {**last, key: now})
    except Exception:
        pass


def _session_for_run(client):
    """_session_today for run_session (#125), retried briefly. A good read is persisted. If the
    calendar still can't be read, a same-day restart reuses the persisted times; otherwise this
    raises as before and systemd's restart is the retry. Times are never guessed, and "no session
    today" is only ever decided by a successful calendar read."""
    today = dt.datetime.now(ET).date()
    for attempt in range(CALENDAR_TRIES):
        try:
            session = _session_today(client)
            break
        except Exception as e:
            err = e
            if attempt + 1 < CALENDAR_TRIES:
                time.sleep(CALENDAR_RETRY_S)
    else:
        saved = _saved_session(today)
        if saved is None:
            _calendar_alert(
                "down",
                f"calendar unreadable at runner start ({err!r}) and no session times saved "
                "today: the runner can't start and retries every 30 s. Anything held has only "
                "its server-side stop, and there is no EOD flatten; check Alpaca.",
            )
            raise err
        _calendar_alert(
            "reused",
            f"calendar unreadable at runner start ({err!r}): reusing today's saved session "
            f"times (open {saved[0]:%H:%M}, close {saved[1]:%H:%M} ET)",
        )
        return saved
    if session is not None:
        try:
            _atomic_json(
                config.RUNTIME_DIR / "session.json",
                {"day": today.isoformat(), "open": session[0].isoformat(), "close": session[1].isoformat()},
            )
        except Exception as e:  # only a later calendar outage needs it
            notify("info", f"couldn't save today's session times ({e!r}): a restart needs the calendar")
    return session


def _recent_calendar(client, today: dt.date, alert=notify) -> Calendar:
    """The exchange sessions over the gate's widest sample window, read once at startup (one call),
    so prior sessions that closed early are cut at their close. Unreadable: alerts, and those days
    count as regular sessions."""
    try:
        return fetch_calendar(client, today - dt.timedelta(days=max(GATE_LOOKBACKS) + 2), today)
    except Exception as e:
        alert(
            "info",
            f"couldn't read the recent exchange calendar ({e!r}): an early close in the last few "
            "days is treated as a full session for prior-day features and the feature gate",
        )
        return Calendar()


def _load_specs(file, secrets, calendar: Calendar | None = None, now: dt.datetime | None = None):
    """Gate custom features on recent history, then validate the classifier file."""
    from trader.classifier import load_specs_report
    from trader.features.harness import run_gate

    calendar = calendar or Calendar()
    now = now or dt.datetime.now(ET)
    end = now - dt.timedelta(minutes=20)

    def sessions_for(days):  # completed sessions only: a mid-session restart's partial today is no sample
        return {
            s: {d: b for d, b in calendar.trim(split_sessions(bars)).items() if d < now.date()}
            for s, bars in _fetch_within(
                STARTUP_FETCH_TIMEOUT_S, ["SPY", "QQQ"], end - dt.timedelta(days=days), end, secrets
            ).items()
        }

    try:
        samples = gate_samples(sessions_for)
    except GateSampleError:
        raise
    except Exception as e:  # e.g. the bars API down: a data problem, said as such by the caller
        raise GateSampleError(f"couldn't fetch the custom-feature gate's sample bars: {e!r}") from e
    report = run_gate(config.CUSTOM_FEATURES_DIR, samples, alert=notify)
    if report.errors:
        notify("urgent", f"Custom features rejected: {report.errors}")
    specs, dropped = load_specs_report(file, F.known_features(), set(config.universe()))
    _alert_dropped(dropped)
    return specs


def _specs_for_session(file, secrets, calendar: Calendar, alert=notify) -> list:
    """_load_specs for run_session: on any failure nothing trades today, with an alert that says
    why (a gate that couldn't get its sample bars is not an invalid classifiers.yaml)."""
    try:
        return _load_specs(file, secrets, calendar)
    except GateSampleError as e:
        alert(
            "urgent",
            f"custom-feature gate couldn't run, trading nothing today: {e}. A market-data "
            "problem, not classifiers.yaml",
        )
    except Exception as e:
        alert("urgent", f"classifiers.yaml invalid, trading nothing today: {e}")
    return []


def _alert_dropped(dropped: dict[str, str], alert=notify) -> None:
    """A rule with an unknown key is left out for the day, loudly; the rest still trade (#150)."""
    for cid, why in dropped.items():
        bench = " This is the control benchmark: it isn't running today." if cid.startswith("control_") else ""
        alert(
            "urgent",
            f"classifier {cid} not trading today: {why}. Fix state/classifiers.yaml; "
            f"the other classifiers trade as normal.{bench}",
        )


def _exclude_prelaunch_specs(specs, session: dt.date, alert=notify):
    """The reserved test_ prefix is plumbing only, never experiment activity."""
    from trader.golive import START_DATE

    if session < START_DATE:
        return specs
    retired = [spec.id for spec in specs if spec.id.startswith("test_")]
    if retired:
        alert(
            "urgent",
            "Disabled pre-launch plumbing rules from experiment start (remove them from "
            "state/classifiers.yaml; the test_ prefix is reserved): " + ", ".join(retired),
        )
    return [spec for spec in specs if not spec.id.startswith("test_")]


def _apply_cashflows(book: Book, broker: AlpacaBroker) -> None:
    """Deposits/withdrawals not yet seen become NAV unit issues/redemptions (deduped by id)."""
    path = book.dir / "cashflows.csv"
    seen = set()
    if path.exists():
        with path.open() as f:
            seen = {row["id"] for row in csv.DictReader(f)}
    try:
        acts = broker.client.get("/account/activities", {"activity_types": "CSD,CSW"}) or []
    except Exception as e:
        notify("urgent", f"[{book.name}] cashflow check failed: {e}")
        return
    first_run = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if first_run:
            w.writerow(["id", "date", "type", "amount", "nav_at_flow"])
        for a in sorted(acts, key=lambda a: a.get("date", "")):
            if a["id"] in seen or a.get("status", "executed") not in ("executed", "correct", None):
                continue  # pending/cancelled transfers aren't money yet
            # Sign from the activity type, not net_amount's sign convention.
            amount = abs(float(a["net_amount"])) * (-1 if a["activity_type"] == "CSW" else 1)
            if first_run:
                # Funding before the first session: the first NAV mark already includes it.
                w.writerow([a["id"], a.get("date"), a["activity_type"], f"{amount:.2f}", "initial"])
                continue
            w.writerow([a["id"], a.get("date"), a["activity_type"], f"{amount:.2f}", f"{book.nav.nav_per_unit:.5f}"])
            book.nav.cashflow(amount)
            notify("info", f"[{book.name}] recorded {a['activity_type']} {amount:+.2f} USD")
    book.nav.save(book.dir / "nav.json")


def stream_bars(rows: dict[str, list], tick: dt.datetime) -> dict[str, pd.DataFrame]:
    """The stream's rows as bars: one per minute (a repeated minute keeps the last), in order,
    and only bars that closed before `tick`. Call it under the rows lock."""
    return {
        s: pd.DataFrame(r, columns=BAR_COLS)
        .drop_duplicates("ts", keep="last")
        .set_index("ts")
        .sort_index()
        .loc[lambda d, t=tick: d.index < t]
        for s, r in rows.items()
        if r
    }


def catch_up_bars(
    rows: dict[str, list],
    lock,
    base: list[str],
    extra: list[str],
    open_: dt.datetime,
    tick: dt.datetime,
    secrets,
    alert=notify,
) -> None:
    """Today's IEX bars from the open to `tick`, added to the stream's rows: once, at the first
    tick of a stream that subscribed late (under 90 s before the open). It runs after the subscription, so no minute
    falls between the two; a minute both have is the same bar (stream_bars keeps one). Each fetch
    gets CATCH_UP_TIMEOUT_S. Never raises: a failure alerts and those symbols start from live bars."""
    for syms, why in (
        (base, "features and stops may be missing today's earlier bars"),
        (extra, "their stops may be missing today's earlier bars"),
    ):  # held from before: a rejected one mustn't stop the rest
        if not syms:
            continue
        try:
            got = _fetch_within(CATCH_UP_TIMEOUT_S, syms, open_, tick, secrets, feed="iex")
            with lock:
                for s, b in got.items():
                    rows[s].extend(
                        (ts, *r) for ts, r in zip(b.index, b.itertuples(index=False), strict=True) if ts < tick
                    )
        except Exception as e:
            alert("urgent", f"could not fetch today's bars so far for {syms}: {e!r}; {why}")


class RestBars:
    """Stops that keep working through a data outage (#50). While the whole stream is stale (the
    engine's SPY test, never a quiet single symbol) and a paper or live book holds positions,
    the held symbols' IEX bars are fetched over REST, once a tick and within
    REST_POLL_TIMEOUT_S, and merged under the stream's, so the engine's stops, targets, trails
    and time stops run on real prices. Entries stay blocked: the engine still judges staleness
    on the stream's own SPY bars. A failed or slow fetch alerts (throttled) and leaves things as
    before: the server-side stops and the kill switch."""

    def __init__(
        self, fetch, open_: dt.datetime, alert_every, timeout: float = REST_POLL_TIMEOUT_S, clock=time.monotonic
    ):
        self.fetch, self.open_, self.alert_every, self.timeout, self.clock = fetch, open_, alert_every, timeout, clock
        # Never pruned: at most a session of 1-minute bars for the few symbols held during outages.
        self.bars: dict[str, pd.DataFrame] = {}
        self._calls: list[tuple[threading.Thread, float]] = []  # fetches still running, with start times

    def poll(self, tick: dt.datetime, stream: dict[str, pd.DataFrame], held: set[str]) -> None:
        if not held or not stale_feed(stream.get("SPY"), tick)[0]:
            return
        now = self.clock()
        self._calls = [(t, at) for t, at in self._calls if t.is_alive()]
        if any(now - at < REST_HUNG_S for _, at in self._calls) or len(self._calls) >= REST_MAX_CALLS:
            self.alert_every(
                "rest-bars",
                "urgent",
                "REST bars for held positions: an earlier fetch is still "
                "running; stops rely on the server-side stop and the kill switch",
                1800,
            )
            return
        syms = sorted(held)
        # From the oldest of the held symbols' latest known bars (stream or REST); the open if one has none.
        latest = [
            max((b.index[-1] for b in (stream.get(s), self.bars.get(s)) if b is not None and len(b)), default=None)
            for s in syms
        ]
        start = self.open_ if None in latest else max(self.open_, min(latest))
        out: dict = {}

        def run():
            try:
                out["bars"] = self.fetch(syms, start, tick)
            except Exception as e:
                out["error"] = e

        call = threading.Thread(target=run, daemon=True)  # daemon: a hung call can't block exit
        call.start()
        self._calls.append((call, now))
        call.join(self.timeout)
        if "bars" not in out:
            why = repr(out["error"]) if "error" in out else f"no answer in {self.timeout:g} s"
            self.alert_every(
                "rest-bars",
                "urgent",
                f"market data stale and REST bars for held {syms} failed "
                f"({why}); stops rely on the server-side stop and the kill switch",
                1800,
            )
            return
        for s, b in out["bars"].items():
            if s in held:
                self.bars[s] = self._merge(self.bars.get(s), b[b.index < tick])

    def merge(self, stream: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """The stream's bars, gaps filled from REST. The stream wins a minute both have."""
        return stream | {s: self._merge(b, stream.get(s)) for s, b in self.bars.items()}

    @staticmethod
    def _merge(old: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame:
        parts = [d for d in (old, new) if d is not None and len(d)]
        if not parts:
            return old if old is not None else new
        m = pd.concat(parts)
        return m[~m.index.duplicated(keep="last")].sort_index()


def tick_bars(
    engine: Engine, rest: RestBars, live: dict[str, pd.DataFrame], tick: dt.datetime, minutes_to_close: float
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """The tick's bars (the stream's, plus REST bars for paper/live holdings while it's stale)
    and the stream's own SPY bars, which alone decide staleness: none from the stream is stale,
    even if SPY was polled over REST. Never raises."""
    bars = live
    try:
        if not G.flatten_due(minutes_to_close):  # the EOD flatten never waits on REST (#104)
            rest.poll(tick, live, {s for b in engine.unique_books() if b.broker.name != "sim" for s in b.entries})
        bars = rest.merge(live)
    except Exception as e:
        engine._alert_every("rest-bars", "urgent", f"REST bars for held positions failed: {e!r}", 1800)  # noqa: SLF001 (the runner drives its own engine)
    return bars, live.get("SPY", pd.DataFrame())


def prev_day_bars(
    symbols: list[str], day: dt.date, now: dt.datetime, secrets, alert=notify, calendar: Calendar | None = None
) -> dict[str, pd.DataFrame]:
    """The features' prev_day for each symbol: the last session before `day`, with SIP's prices
    (the official close, for gap and prior-day levels) and IEX's volume, the feed the live bars
    come from, so volume ratios compare like with like. These feed prev-day features only, so a
    failed fetch is never a reason to leave positions unmanaged: it alerts and those features
    are NaN today. `calendar` cuts an early-closed prior session at its close."""
    calendar = calendar or Calendar()
    start, end = now - dt.timedelta(days=7), now - dt.timedelta(minutes=16)
    try:
        sip = {
            s: calendar.trim(split_sessions(b))
            for s, b in _fetch_within(STARTUP_FETCH_TIMEOUT_S, symbols, start, end, secrets).items()
        }
    except Exception as e:
        alert("urgent", f"could not fetch recent history at startup: {e!r}; prev-day features are NaN today")
        return {}
    try:
        iex = {
            s: calendar.trim(split_sessions(b))
            for s, b in _fetch_within(STARTUP_FETCH_TIMEOUT_S, symbols, start, end, secrets, feed="iex").items()
        }
    except Exception as e:
        alert(
            "urgent",
            f"could not fetch recent IEX history at startup: {e!r}; prior-session volume is NaN today "
            "(rel_volume_15m and the like); price levels are unaffected",
        )
        iex = None  # said once above; not again per symbol
    try:
        prev = prior_sessions(sip, day, volume_from=iex or {})
    except Exception as e:  # never a startup crash loop over context data
        alert(
            "urgent", f"could not combine prior-session prices and IEX volume: {e!r}; prior-session volume is NaN today"
        )
        prev, iex = prior_sessions(sip, day, volume_from={}), None
    # A symbol either feed returned nothing for is NaN in silence otherwise: name them, once.
    missing = sorted(set(symbols) - set(prev))
    if missing:
        alert("urgent", f"no prior session in the startup history for {missing}: their prev-day features are NaN today")
    if iex is not None:
        no_volume = sorted(s for s, b in prev.items() if b.volume.isna().all())
        if no_volume:
            alert(
                "info",
                f"no IEX bars for the prior session of {no_volume}: their prior-session volume is NaN "
                "today (rel_volume_15m and the like); price levels are unaffected",
            )
    return prev


def _last_resort_flatten(engine: Engine, tick: dt.datetime, bars: dict, minutes_to_close: float) -> None:
    """The tick raised inside the flatten window. Unless it got as far as its own flatten this
    minute, close everything from here (once), then keep the heartbeat fresh: a stale status
    makes the watchdog page and clear-halt think the runner is dead. Never raises."""
    if engine.flattened_at != tick:
        try:
            prices = {s: float(b.close.iloc[-1]) for s, b in bars.items() if len(b)}
        except Exception:
            prices = {}
        try:
            engine.flatten_for_close(tick, prices or None)
        except Exception as e:  # it shouldn't raise; if it does, the next minute tries again
            engine._alert_every("eod-last-resort", "urgent", f"last-resort flatten failed: {e!r}")  # noqa: SLF001 (the runner drives its own engine)
    try:
        engine.write_status(tick, minutes_to_close)
    except Exception as e:
        engine._alert_every("eod-status", "urgent", f"status write failed in the flatten window: {e!r}")  # noqa: SLF001 (the runner drives its own engine)


WIND_DOWN_KEY = "live:wind-down"


def wind_down_live_book(secrets, alert=notify) -> Book | None:
    """On a paper day the live account can still hold positions: a HOLD LIVE (or a live halt
    cleared with `clear-halt`) mid-session, then a runner restart, or a mode.yaml switch to paper.
    Without the live book in the engine they'd be unmanaged until a later live session (their
    server-side stops lapse at the close). So when the live book tracks entries or resting orders,
    or the live account holds anything (or that can't be read), the live book joins the session in
    wind-down: no classifier trades it, and the engine closes all it holds at the first tick after
    the open (Engine._book_checks); the EOD flatten backs that up. None: nothing to wind down. Never
    raises. Every call on the trading client has its own HTTP timeout (AlpacaBroker)."""
    if not secrets.get("ALPACA_LIVE_KEY"):
        return None
    d = BOOKS_DIR / "live"
    try:
        broker = AlpacaBroker(secrets["ALPACA_LIVE_KEY"], secrets["ALPACA_LIVE_SECRET"], paper=False)
    except Exception as e:
        alert(
            "urgent",
            f"[live] paper today, and the live broker couldn't be set up ({e!r}): anything the live "
            "account holds is unmanaged today. Check Alpaca now.",
        )
        return None
    held: list[str] | None = None
    try:
        held = sorted(broker.get_positions())
    except Exception as e:
        alert(
            "urgent",
            f"[live] paper today, and the live positions couldn't be read ({e!r}): the live book "
            "winds down anyway, closing anything it finds after the open",
        )
    if held == [] and not _tracks_anything(d):
        return None  # the usual paper day: no live book, nothing created on disk
    unmanaged = f"live positions {held if held is not None else '(unknown)'} are unmanaged today. Check Alpaca now."
    try:
        book = Book("live", broker, d)
    except Exception as e:
        # Only a tracking file that itself won't load is set aside (then everything closes as
        # untracked); a corrupt nav.json or risk.json leaves the tracking alone for the human.
        bad = _unloadable_tracking(d)
        if not bad:
            alert("urgent", f"[live] paper today, and the live book couldn't be opened ({e!r}): {unmanaged}")
            return None
        aside = _set_aside_tracking(d, bad)
        try:
            book = Book("live", broker, d)
        except Exception as e2:
            alert("urgent", f"[live] paper today, and the live book couldn't be opened ({e2!r}): {unmanaged}")
            return None
        alert(
            "urgent",
            f"[live] the live book's tracking files were unreadable ({e!r}); set aside as {aside}. "
            "Its positions are closed as untracked (no trade rows).",
        )
    try:
        _apply_cashflows(book, broker)  # as on a live day, so its NAV marks stay right
    except Exception as e:
        alert("urgent", f"[live] cashflow bookkeeping failed ({e!r}); winding down anyway")
    if not book.blocked:  # a halt flattens the same way, and stays for the human to clear
        book.blocked = WIND_DOWN
    tracked = sorted(set(book.entries) | set(book.pending))
    what = (
        f"still holds {held}"
        if held
        else f"still tracks {tracked}"
        if tracked
        else "may hold positions (unreadable)"
        if held is None
        else "holds no positions now"
    )
    alert(
        "urgent",
        f"[live] paper today, but the live account {what}: closing everything it holds at the first "
        "minute after the open. No new live trades.",
    )
    return book


def _tracks_anything(d) -> bool:
    """Whether the live book's entries or resting-order files are non-empty (or unreadable: then yes)."""
    for name in ("entries.json", "pending.json"):
        p = d / name
        try:
            if p.exists() and json.loads(p.read_text()):
                return True
        except Exception:
            return True
    return False


def _unloadable_tracking(d) -> list[str]:
    """The live book's tracking files (entries.json, pending.json) that won't load as Book loads them."""
    from trader.engine import Entry, Pending, _known

    loaders = {
        "entries.json": lambda o: Entry(**_known(Entry, o | {"time": dt.datetime.fromisoformat(o["time"])})),
        "pending.json": lambda o: Pending(
            **_known(Pending, o | {k: dt.datetime.fromisoformat(o[k]) for k in ("placed", "expires")})
        ),
    }
    bad = []
    for name, load in loaders.items():
        p = d / name
        try:
            if p.exists():
                for o in json.loads(p.read_text()).values():
                    load(o)
        except Exception:
            bad.append(name)
    return bad


def _set_aside_tracking(d, names) -> list[str]:
    """Rename these live-book tracking files to <name>.corrupt-<time>. Never raises."""
    out = []
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    for name in names:
        p = d / name
        try:
            if p.exists():
                p.rename(p.with_name(f"{name}.corrupt-{stamp}"))
                out.append(f"{name}.corrupt-{stamp}")
        except OSError:
            pass
    return out


def count_live_session(mode: str, day: dt.date, live_dir, alert=None) -> None:
    """The live book's `live_sessions`: the engine trades its first 5 at half size. Every return to
    live starts a fresh half-size week, whatever the path back (a runner demotion after a live halt,
    a HOLD LIVE then a re-arm, a `config/mode.yaml` override): a paper session ends the live stint,
    so it resets the count to 0 (as does `clear_halt("live")`, for a halt forced back live by
    mode.yaml with no paper session between), and each live session counts once (not per restart). A failed
    reset never stops a paper session: it alerts, so the human can fix it before the next live one."""
    p = live_dir / "risk.json"
    if mode == "live":
        risk = json.loads(p.read_text()) if p.exists() else {}
        if risk.get("last_live_session") != day.isoformat():
            risk.update(live_sessions=risk.get("live_sessions", 0) + 1, last_live_session=day.isoformat())
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(p, risk)
        return
    try:
        risk = json.loads(p.read_text()) if p.exists() else {}
        if risk.get("live_sessions") or "last_live_session" in risk:
            risk["live_sessions"] = 0
            risk.pop("last_live_session", None)  # a same-day return to live counts as its session 1
            _atomic_json(p, risk)
    except Exception as e:
        (alert or notify)(
            "urgent",
            f"[live] could not reset the half-size week count in {p}: {e!r}. "
            "Set live_sessions to 0 there before the account goes live again.",
        )


def _record_missed_demotion(golive, session: dt.date) -> None:
    """A start after the close never processes the session again, but a crash between the close
    and after_session would otherwise lose a live-halt demotion: still `live`, the account would go
    live again once the halt is cleared, with no `release-live`. So if go-live is live and the live
    book is halted, demote now (after_session is idempotent per session date, so a session already
    processed is untouched). Nothing else of after_session runs here: a crashed session never counts
    towards the veto window or arms the gate. Never raises."""
    try:
        if golive.load_state()["status"] != "live":
            return
        p = BOOKS_DIR / "live" / "risk.json"
        if p.exists() and json.loads(p.read_text()).get("halted"):
            golive.after_session(notify, live_book_halted=True, session=session)
    except Exception as e:
        notify(
            "urgent",
            f"post-close check for a missed live-halt demotion failed: {e!r}. If the live book is "
            "halted, run `trader hold-live` before clearing it.",
        )


def run_session(decider_name: str = "jev", file=config.CLASSIFIERS_FILE) -> int:
    from trader.jev import JevClient, StubDecider

    secrets = config.load_secrets()
    from trader import golive

    golive.resend_unsent(notify)  # go-live alerts a crash kept from going out (never raises)

    def live_equity():
        if not secrets.get("ALPACA_LIVE_KEY"):
            return None
        return AlpacaBroker(secrets["ALPACA_LIVE_KEY"], secrets["ALPACA_LIVE_SECRET"], paper=False).equity()

    paper = AlpacaBroker(secrets["ALPACA_PAPER_KEY"], secrets["ALPACA_PAPER_SECRET"], paper=True)
    session = _session_for_run(paper.client)
    if session is None:
        print("not a trading day")
        return 0
    open_, close = session
    if dt.datetime.now(ET) >= close:
        # Started after the close (reboot, restart after a crash in end_day): nothing to
        # trade, and processing the session again would count it twice. But a crash in the
        # flatten window could have left positions: say so loudly.
        # Read-only position check. This touches the live key even in paper mode (if present),
        # because a position left on the live account matters regardless of the current mode.
        for kind, is_paper in (("PAPER", True), ("LIVE", False)):
            if secrets.get(f"ALPACA_{kind}_KEY"):
                try:
                    held = AlpacaBroker(
                        secrets[f"ALPACA_{kind}_KEY"], secrets[f"ALPACA_{kind}_SECRET"], is_paper
                    ).get_positions()
                    if held:
                        notify(
                            "urgent",
                            f"[{kind.lower()}] runner started after the close and positions are still held: "
                            f"{sorted(held)}. The runner closes them after tomorrow's open; check Alpaca now.",
                        )
                except Exception as e:
                    notify("urgent", f"[{kind.lower()}] post-close position check failed: {e}")
        _record_missed_demotion(golive, open_.date())
        print("session already over")
        return 0
    mode = golive.resolve_mode(notify, live_equity, session=open_.date())

    paper_book = Book("paper", paper, BOOKS_DIR / "paper")
    books = {"live": paper_book, "shadow": paper_book}
    if mode == "live":
        live = AlpacaBroker(secrets["ALPACA_LIVE_KEY"], secrets["ALPACA_LIVE_SECRET"], paper=False)
        live_book = Book("live", live, BOOKS_DIR / "live")
        _apply_cashflows(live_book, live)
        books["live"] = live_book
    else:
        winding = wind_down_live_book(secrets, notify)
        if winding is not None:  # under its own key: no classifier's book, so nothing enters on it
            books[WIND_DOWN_KEY] = winding
    count_live_session(mode, open_.date(), BOOKS_DIR / "live", notify)

    # Wait for the open; the pre-market strategist run may still be editing classifiers.
    status = config.RUNTIME_DIR / "status.json"
    while dt.datetime.now(ET) < open_ - dt.timedelta(minutes=2):
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(
            json.dumps(
                {
                    "updated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    "phase": "waiting for open",
                    "open": open_.isoformat(),
                    "books": {},
                    "classifiers": [],
                }
            )
        )
        time.sleep(30)

    recent = _recent_calendar(paper.client, open_.date(), notify)
    specs = _specs_for_session(file, secrets, recent, notify)
    specs = _exclude_prelaunch_specs(specs, open_.date())
    labels = [p for p in (s.family_problem() for s in specs) if p]
    if labels:  # a label only feeds the scoreboard: say so, but trade as normal
        notify("info", f"classifier family labels need fixing (shown as unlabelled): {'; '.join(labels)}")
    specs = golive.enforce_promotion(specs, notify, account_live=(mode == "live"), today=open_.date())
    # Sim accounts are disposable: nothing wrong with one may stop paper or live from trading.
    try:
        reconcile_sim_accounts(BOOKS_DIR / "sim", open_.date(), notify)
    except Exception as e:
        notify("urgent", f"sim account reconciliation failed: {e!r}; continuing")
    books.update(sim_books(specs, alert=notify))
    dropped = [s.id for s in specs if s.mode == "sim" and s.book_key not in books]
    specs = [s for s in specs if s.id not in dropped]

    decider = StubDecider() if decider_name == "stub" else JevClient(secrets["OPENROUTER_API_KEY"])
    universe = set(config.universe())

    def engine_alert(level, msg):  # a sim account's trouble is news, not a page
        notify("info" if msg.startswith("[sim:") else level, msg)

    engine = Engine(specs, books, decider, universe, config.RUNTIME_DIR, alert=engine_alert)
    try:  # the trial ledger is research bookkeeping: never a reason not to trade
        from trader import trials

        trials.note_shadow_starts(specs, engine.provenance.specs, open_.date(), stub=decider_name == "stub")
    except Exception as e:
        notify("info", f"trial ledger: could not record today's new specs: {e!r}")

    # Settled cash at startup, before anything sells: sale proceeds (orphan closes too) settle T+1, mustn't fund buys.
    settled_at_open = {}
    for b in engine.unique_books():
        try:
            settled_at_open[b.name] = b.broker.settled_cash()
        except Exception as e:
            notify("urgent", f"[{b.name}] could not read settled cash at startup: {e}")
    # Positions that closed while we were down. Orphans (held but untracked) are closed by the
    # engine's first tick after the open: it's before the open now, and a sell wouldn't fill.
    for b in engine.unique_books():
        reconcile_at_startup(b, notify)

    base, extra = session_symbols(specs, engine.unique_books())
    symbols = base + extra
    now = dt.datetime.now(ET)
    prev = prev_day_bars(base, open_.date(), now, secrets, notify, recent)
    # The opening equity row is stamped at the open, or now if starting later (#50).
    engine.start_day(open_.date(), prev, settled_at_open, opened_at=max(open_, now))
    engine.write_status(now, (close - now).total_seconds() / 60)
    notify("info", f"runner started session {open_:%a %d %b} ({mode}, {len(specs)} classifiers)", title="Session start")

    # Live IEX bars. A stream that subscribes after the open (a restart, or a slow start) has
    # missed today's earlier bars: they're caught up over REST at the first tick (catch_up_bars).
    lock = threading.Lock()
    rows: dict[str, list] = defaultdict(list)

    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    stream = StockDataStream(secrets["ALPACA_PAPER_KEY"], secrets["ALPACA_PAPER_SECRET"], feed=DataFeed.IEX)

    async def on_bar(bar):
        ts = pd.Timestamp(bar.timestamp).tz_convert(ET)
        with lock:
            rows[bar.symbol].append((ts, bar.open, bar.high, bar.low, bar.close, bar.volume))

    stream.subscribe_bars(on_bar, *symbols)
    threading.Thread(target=stream.run, daemon=True).start()
    # The 09:30 bar is published at about 09:31. A stream asked for well before the open has
    # connected by then (the margin covers a slow websocket connect); any later one catches up.
    caught_up = dt.datetime.now(ET) < open_ - dt.timedelta(seconds=90)

    rest = RestBars(
        lambda syms, start, end: fetch_alpaca(syms, start, end, secrets, feed="iex"),
        open_,
        engine._alert_every,  # noqa: SLF001 (the runner drives its own engine)
    )
    tick_failures = 0
    while True:
        now = dt.datetime.now(ET)
        if now >= close:
            break
        nxt = (now + dt.timedelta(minutes=1)).replace(second=4, microsecond=0)
        time.sleep(max(0.0, (nxt - now).total_seconds()))
        tick = nxt.replace(second=0)
        if tick >= close:
            break  # the 16:00 tick would send orders after the bell, to queue for tomorrow's open
        if tick <= open_:
            continue
        if not caught_up:
            caught_up = True
            if not G.flatten_due((close - tick).total_seconds() / 60):  # the EOD flatten never waits on REST
                catch_up_bars(rows, lock, base, extra, open_, tick, secrets)
        with lock:
            live = stream_bars(rows, tick)
        minutes_to_close = (close - tick).total_seconds() / 60
        bars, feed_spy = tick_bars(engine, rest, live, tick, minutes_to_close)
        try:
            engine.tick(tick, bars, minutes_to_close, feed_spy=feed_spy)
        except Exception as e:
            tick_failures += 1
            if G.flatten_due(minutes_to_close):  # close first: the alert can fail for the same reason
                _last_resort_flatten(engine, tick, bars, minutes_to_close)
            if tick_failures in (1, 10, 100) or tick_failures % 300 == 0:
                try:
                    notify("urgent", f"engine tick failed at {tick:%H:%M} ({tick_failures}x this session): {e!r}")
                except Exception:
                    pass

    try:
        stream.stop()
    except Exception:
        pass
    # After the bell nothing is sent: a market sell now would sit queued for tomorrow's open.
    for b in engine.unique_books():
        try:
            held = b.broker.get_positions()
        except Exception as e:
            engine_alert("urgent", f"[{b.name}] could not check positions at the close: {e}")
            continue
        if held:
            engine_alert(
                "urgent",
                f"[{b.name}] still holding at the close: {sorted(held)}. Nothing is sent after "
                "the bell; close them in Alpaca now.",
            )
    summary = engine.end_day(close)
    parts = []
    sims = {n: s for n, s in summary.items() if n.startswith("sim:")}
    for name, s in summary.items():
        if name in sims:
            continue
        pnl = "day P&L unknown (start equity unreadable)" if s["day_pnl"] is None else f"{s['day_pnl']:+.2f}"
        parts.append(
            f"{name}: {pnl} ({s['trades']} trades, {s['wins']}W/{s['trades'] - s['wins']}L) · equity ${s['equity']:.2f}"
        )
    if sims:  # one line for all the experiments, not one per account
        parts.append(
            f"sim ({len(sims)} accounts): {sum(s['day_pnl'] or 0 for s in sims.values()):+.2f}, "
            f"{sum(s['trades'] for s in sims.values())} trades"
        )
    halted = "live" in books and books["live"] is not books["shadow"] and books["live"].blocked == "halt"
    st = golive.after_session(notify, live_book_halted=halted, session=open_.date(), live_equity=live_equity)
    if st["status"] == "pending" and st.get("last_gate"):
        parts.append(
            "go-live gate: " + ", ".join(st["last_gate"]["blocking"])
            if st["last_gate"]["blocking"]
            else "go-live gate: passing"
        )
    notify("info", " | ".join(parts), title="Daily P&L")
    from trader.compact import compact

    compact(scope="runtime")
    return 0


SIM_QUARANTINE = config.RUNTIME_DIR / "sim_quarantine"  # unreadable sim accounts are moved here


def _open_sim_book(key: str, d: Path, alert, quarantine: Path) -> Book | None:
    """The sim account in `d`. If its files can't be read, they're moved to `quarantine` and the
    account restarts with fresh cash; None (with an alert) only if even that fails."""
    import shutil

    from trader.broker import PersistentSimBroker

    try:
        return Book(key, PersistentSimBroker(d / "sim_state.json"), d)
    except Exception as e:
        try:
            dest = quarantine / f"{d.name}-{dt.datetime.now(ET):%Y%m%dT%H%M%S}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dest))
            alert(
                "urgent",
                f"[{key}] sim account unreadable ({e!r}): moved to {dest}; it restarts with fresh cash. "
                "Paper and live are unaffected.",
            )
            return Book(key, PersistentSimBroker(d / "sim_state.json"), d)
        except Exception as e2:
            alert(
                "urgent",
                f"[{key}] sim account unusable ({e2!r}): that sim rule is off today. Paper and live are unaffected.",
            )
            return None


def sim_books(
    specs, root: Path | None = None, alert=lambda level, msg: None, quarantine: Path | None = None
) -> dict[str, Book]:
    """One simulated account per `mode: sim` classifier, under books/sim/<id>/. Its cash and any
    open positions persist there, so it carries over between days and survives a restart. A sim
    classifier whose account can't be opened is left out (the caller drops the spec)."""
    root = root or BOOKS_DIR / "sim"
    out = {}
    for s in specs:
        if s.mode == "sim":
            b = _open_sim_book(s.book_key, root / s.id, alert, quarantine or SIM_QUARANTINE)
            if b is not None:
                out[s.book_key] = b
    return out


def reconcile_sim_accounts(root: Path, today: dt.date, alert, quarantine: Path | None = None) -> None:
    """At startup, for every sim account (including ones whose rule is gone or no longer sim):
    anything still open from an earlier day (a crash after the flatten, a rule removed while
    holding) is closed at its entry price and recorded as such, so no sim position outlives its
    day or turns into a multi-day trade. Today's positions are left to the engine."""
    from trader.broker import Fill

    now = dt.datetime.now(ET)
    for d in sorted(p for p in root.glob("*") if p.is_dir()):
        b = _open_sim_book(f"sim:{d.name}", d, alert, quarantine or SIM_QUARANTINE)
        if b is None:
            continue
        for sym, p in list(b.pending.items()):
            if p.placed.date() < today:
                b.broker.cancel_order(p.order_id)
                b.pending.pop(sym)
        b.save_pending()
        for sym, e in list(b.entries.items()):
            if e.time.date() < today:
                fill = b.broker.sell_all(sym, e.price, now, f"stale-{sym}") or Fill(sym, "sell", e.qty, e.price, now)
                b.record_exit(
                    sym,
                    e,
                    fill,
                    f"left open since {e.time:%Y-%m-%d}: closed at startup at its entry price (real exit unknown)",
                )
                alert("info", f"[{b.name}] {sym} was still open from {e.time:%Y-%m-%d}; closed at its entry price")
        for sym, pos in list(b.broker.get_positions().items()):
            if sym not in b.entries and sym not in b.pending:
                b.broker.sell_all(sym, pos.avg_price, now, f"orphan-{sym}")  # untracked: no trade to record


def book_dirs() -> list[Path]:
    """Every book's directory: paper, live, and each sim account under sim/."""
    top = [p for p in BOOKS_DIR.glob("*") if p.is_dir() and p.name != "sim"]
    return sorted(top) + sorted(p for p in (BOOKS_DIR / "sim").glob("*") if p.is_dir())


def session_symbols(specs, books: list[Book], alert=notify) -> tuple[list[str], list[str]]:
    """What to stream, as (the classifiers' symbols and SPY, extras). Extras are anything already
    held or resting, whose classifier may be gone or edited (or which may have left the universe),
    so that its stops, time stop and the flatten work on real prices: tracked ones first, and only
    while there's room under STREAM_SYMBOL_LIMIT. Never raises. An untracked position needs no
    price (the engine closes it anyway), and a tracked one without bars still gets its time stop
    and the EOD flatten; anything left out is alerted."""
    base = sorted({s for sp in specs for s in sp.symbols} | {"SPY"})
    tracked, held, skipped = set(), set(), []
    for b in books:
        tracked |= set(b.entries) | set(b.pending)
        try:
            for sym, p in b.broker.get_positions().items():
                if p.asset_class != "us_equity" or not EQUITY_TICKER.fullmatch(sym):
                    skipped.append(sym)  # e.g. crypto: the stock bars API would reject the request
                else:
                    held.add(sym)
        except Exception as e:
            alert(
                "urgent",
                f"[{b.name}] could not read positions at startup: {e}; an untracked holding "
                "gets no market data today (it is still closed after the open)",
            )
    room = max(0, STREAM_SYMBOL_LIMIT - len(base))
    wanted = sorted(tracked - set(base)) + sorted(held - tracked - set(base))
    extra, dropped = wanted[:room], wanted[room:]
    if skipped:
        alert(
            "urgent",
            f"holding non-equity {sorted(set(skipped))}: no market data for it; it is still sold after the open",
        )
    if dropped:
        alert(
            "urgent",
            f"held {dropped} left out of market data (the stream takes {STREAM_SYMBOL_LIMIT} symbols); "
            "a tracked one keeps its server-side stop (where Alpaca took one), its time stop and the EOD flatten",
        )
    return base, extra


def reconcile_at_startup(b: Book, alert) -> None:
    """At startup: drop earlier sessions' resting entries, and record the exits of tracked
    positions that closed while the runner was down. One whose exit fill can't be read (an
    API error, not "none found") stays tracked for the engine to look up again (#131).
    Untracked positions are left to the engine, which closes them once the session is open.
    Never raises: a failure here must not crash-loop the runner."""
    from trader.broker import Fill

    try:
        now = dt.datetime.now(ET)
        for sym, p in list(b.pending.items()):
            if p.placed.date() < now.date():  # a DAY limit from an earlier session: dead; forget it
                try:
                    b.broker.cancel_order(p.order_id)
                except Exception:
                    pass
                b.pending.pop(sym)
        b.save_pending()
        held = b.broker.get_positions()
        for sym in sorted(set(b.entries) - set(held)):
            e = b.entries[sym]
            try:  # a failed lookup is not "no fill" (#131)
                fill = b.broker.stop_fill(e.stop_id, now, strict=True) or b.broker.exit_fill_since(
                    sym, e.exits_since(), now, strict=True
                )
            except Exception as ex:  # kept tracked (never sold): the engine looks again each tick (Engine._vanished)
                b.unresolved[sym] = (1, True)
                alert(
                    "info",
                    f"[{b.name}] {sym} closed while the runner was down but its exit fill couldn't be read "
                    f"({ex}); looking again each minute once the session is open",
                )
                continue
            if fill is not None:
                b.record_exit(sym, e, fill, "closed while runner down")
            else:  # a guess, so not evidence; at the stop, so today's loss budget sees a loss (#117)
                b.record_exit(
                    sym,
                    e,
                    Fill(sym, "sell", e.qty, e.stop, now),
                    "closed while runner down (recorded at stop)",
                    estimated=True,
                )
                alert(
                    "urgent",
                    f"[{b.name}] {sym} closed while the runner was down and its exit price is unknown; "
                    f"recorded at the stop ({e.stop:.2f})",
                )
        b.save_entries()
    except Exception as e:
        alert("urgent", f"[{b.name}] startup reconciliation failed: {e}")


def request_stop() -> str:
    """STOP: flag every book; if the daemon looks dead, flatten directly via Alpaca."""
    flagged = []
    for d in book_dirs():  # sim accounts too: STOP means everything stops
        _atomic_json(d / "stop.json", {"stop_on": dt.datetime.now(ET).date().isoformat()})  # this trading day
        flagged.append(str(d.relative_to(BOOKS_DIR)))
    status = config.RUNTIME_DIR / "status.json"
    stale = not status.exists() or time.time() - status.stat().st_mtime > 180
    msg = f"STOP flagged for {flagged or 'no books'}"
    if stale:
        secrets = config.load_secrets()
        for kind, paper in (("PAPER", True), ("LIVE", False)):
            key = secrets.get(f"ALPACA_{kind}_KEY")
            if key:
                try:
                    AlpacaBroker(key, secrets[f"ALPACA_{kind}_SECRET"], paper).client.close_all_positions(
                        cancel_orders=True
                    )
                    msg += f"; daemon stale, flattened {kind.lower()} directly"
                except Exception as e:
                    msg += f"; direct flatten of {kind.lower()} failed: {e}"
    notify("urgent", msg)
    return msg


def _atomic_json(path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _runner_live() -> bool:
    status = config.RUNTIME_DIR / "status.json"
    return status.exists() and time.time() - status.stat().st_mtime < 180


def clear_halt(book: str) -> str:
    """Human-only. Clears the halt and rebases the high-water mark to the NAV at the halt;
    otherwise the trailing-drawdown check would re-halt on the next tick. Refused while the
    runner is live (it holds NAV in memory and would overwrite the change)."""
    from trader.nav import NavBook

    d = BOOKS_DIR / book
    p = d / "risk.json"
    risk = json.loads(p.read_text()) if p.exists() else {}
    if not risk.get("halted"):
        return f"{book} is not halted: nothing changed"
    if _runner_live():
        return "the runner is running (it holds NAV in memory): retry after it exits after the close; nothing changed"
    if book == "live":  # first, so a failure leaves the halt in place: never back to live unreleased
        from trader import golive

        golive.demote_on_halt_cleared(notify)  # a demotion lost to a crash after the close
    risk.update(halted=False)
    risk.pop("reason", None)
    if book == "live":  # a live halt ends the stint: whatever the path back (even mode.yaml), half size again
        risk["live_sessions"] = 0
        risk.pop("last_live_session", None)
    _atomic_json(p, risk)
    nav = NavBook.load(d / "nav.json")
    old = nav.hwm
    nav.hwm = nav.nav_per_unit
    nav.save(d / "nav.json")
    msg = f"[{book}] halt cleared by human; high-water mark rebased {old:.4f} -> {nav.hwm:.4f}"
    notify("info", msg)
    return msg


def rebase_paper() -> str:
    """Human-only, after resetting the Alpaca paper account: restart the paper book's NAV
    at 1.0 from its next mark and clear any halt. Refused for the live book, whose NAV
    history is the performance record. The next session issues the new units at its opening
    equity and saves them at once, so a restart that day keeps the day's P&L in the NAV."""
    from trader.nav import NavBook

    if _runner_live():
        return "the runner is running: retry rebase-paper after it exits after the close; nothing changed"
    d = BOOKS_DIR / "paper"
    old = NavBook.load(d / "nav.json")
    NavBook().save(d / "nav.json")
    p = d / "risk.json"
    risk = json.loads(p.read_text()) if p.exists() else {}
    risk.update(halted=False)
    risk.pop("reason", None)
    risk.pop("day", None)  # take a fresh day-start baseline at the next session
    _atomic_json(p, risk)
    msg = f"[paper] book rebased (was NAV {old.nav_per_unit:.4f}, HWM {old.hwm:.4f}); restarts at 1.0 next session"
    notify("info", msg)
    return msg


def session_info() -> dict | None:
    """Today's session in ET, or None when the market is closed all day."""
    secrets = config.load_secrets()
    s = _session_today(AlpacaBroker(secrets["ALPACA_PAPER_KEY"], secrets["ALPACA_PAPER_SECRET"], True).client)
    if s is None:
        return None
    now = dt.datetime.now(ET)
    return {
        "open": s[0].isoformat(),
        "close": s[1].isoformat(),
        "minutes_to_open": round((s[0] - now).total_seconds() / 60),
        "minutes_to_close": round((s[1] - now).total_seconds() / 60),
    }


POST_CLOSE_GRACE = dt.timedelta(hours=2, minutes=30)
# The post-close timer runs on the operator's clock (config.yaml schedule: 21:30, retry 22:30 in
# London, each allowed 50 min), so the deadline is on that clock too: half days and the UK/US
# DST-mismatch weeks close earlier there, but the retry still can't finish before ~23:20.
POST_CLOSE_CUTOFF_LOCAL = config.SETTINGS.schedule.postclose_cutoff_time
LOCAL_TZ = config.SETTINGS.schedule.tz


def postclose_deadline(last_close: dt.datetime) -> dt.datetime:
    return max(last_close + POST_CLOSE_GRACE, dt.datetime.combine(last_close.date(), POST_CLOSE_CUTOFF_LOCAL, LOCAL_TZ))


def strategist_overdue(stamp_mtime: float, last_close: dt.datetime, now: dt.datetime | None = None) -> bool:
    """The post-close run for the most recent session is overdue: past its deadline and no
    successful post-close run has been stamped since that close. Weekends and holidays have
    no session, so they never trigger this (unlike a fixed age threshold)."""
    now = now or dt.datetime.now(ET)
    return now > postclose_deadline(last_close) and stamp_mtime < last_close.timestamp()


def _postclose_stamp_mtime() -> float:
    """When a post-close run last succeeded. Until the first run that writes the per-kind
    stamp (i.e. while this change rolls out) fall back to the any-kind stamp. With neither,
    0: the strategist has never completed a run, which alerts once per session."""
    for p in (config.POSTCLOSE_STAMP, config.STRATEGIST_STAMP):
        if p.exists():
            return p.stat().st_mtime
    return 0.0


def _last_session_close(now: dt.datetime | None = None) -> dt.datetime | None:
    from alpaca.trading.requests import GetCalendarRequest

    now = now or dt.datetime.now(ET)
    secrets = config.load_secrets()
    client = AlpacaBroker(secrets["ALPACA_PAPER_KEY"], secrets["ALPACA_PAPER_SECRET"], True).client
    cal = client.get_calendar(GetCalendarRequest(start=now.date() - dt.timedelta(days=10), end=now.date()))
    closes = []
    for c in cal:
        close = c.close if c.close.tzinfo else c.close.replace(tzinfo=ET)
        if close <= now:
            closes.append(close)
    return max(closes) if closes else None


def watchdog(now: dt.datetime | None = None) -> str:
    """Run every ~10 min: alert if the daemon is stale during market hours, if the latest
    session's post-close strategist run is overdue (once per session), or if the calendar
    can't be read (so neither check is silently skipped). Other alerts repeat hourly at most.
    `now` drives the overdue check and throttles; session_info() still reads the wall clock
    (identical in production; tests patch it)."""
    now = now or dt.datetime.now(ET)
    problems = []  # (throttle key, message, seconds between repeats)
    calendar_error = None
    try:
        info = session_info()
    except Exception as e:
        info, calendar_error = None, e
    status = config.RUNTIME_DIR / "status.json"
    if info and info["minutes_to_open"] < -3 and info["minutes_to_close"] > 0:
        age = now.timestamp() - status.stat().st_mtime if status.exists() else 1e9
        if age > 300:
            problems.append(("heartbeat", f"runner heartbeat stale ({age / 60:.0f} min) during market hours", 3600))
    try:
        last_close = _last_session_close(now)
    except Exception as e:
        last_close, calendar_error = None, e
    if last_close and strategist_overdue(_postclose_stamp_mtime(), last_close, now):
        problems.append(
            (
                f"strategist-{last_close:%Y-%m-%d}",
                f"no post-close strategist run since the {last_close:%a %d %b} session",
                8 * 86400,
            )
        )
    if calendar_error is not None:
        problems.append(
            (
                "calendar",
                f"calendar lookup failed; heartbeat and/or post-close checks skipped: {calendar_error}",
                3 * 3600,
            )
        )
    state = config.RUNTIME_DIR / "watchdog.json"
    last = json.loads(state.read_text()) if state.exists() else {}
    ts = now.timestamp()
    last = {k: v for k, v in last.items() if ts - v < 8 * 86400}  # per-session keys don't pile up
    for key, msg, every in problems:
        if ts - last.get(key, 0) > every:
            notify("urgent", f"WATCHDOG: {msg}")
            last[key] = ts
    state.write_text(json.dumps(last))
    return "; ".join(m for _, m, _ in problems) or "ok"
