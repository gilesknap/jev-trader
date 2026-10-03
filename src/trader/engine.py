"""The trading engine: one code path for replay, paper and live.

Each minute `tick` receives today's bars so far per symbol. It enforces stops and
targets, runs risk checks, flattens before the close, and asks each due classifier
its entry or exit question. Every order goes through guardrails.check_entry.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import gzip
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from trader import allocator as A
from trader import features as F
from trader import guardrails as G
from trader.broker import TERMINAL, Fill, NotFilled, OrderState, PartialExit
from trader.classifier import ClassifierSpec, ClassifierState
from trader.data import ET
from trader.jev import DecisionError
from trader.nav import NavBook

DECISION_COOLDOWN = dt.timedelta(minutes=5)  # pause all decision calls after a failure
TICK_DECISION_BUDGET_S = 35.0  # wall-clock seconds per tick for decision calls
# Probes only use what trading classifiers left of the tick, and stop well before the budget.
PROBE_TICK_BUDGET_S = 20.0
# A market fill whose average price Alpaca hasn't reported by then is priced another way (#60).
UNPRICED_FILL_WAIT = dt.timedelta(minutes=3)
# The day-start equity read is retried this often before falling back (#119).
START_EQUITY_TRIES = 3
START_EQUITY_RETRY_S = 2.0
START_UNVERIFIED_NOTE = "no new entries: start equity unreadable"
# A tracked position gone from the broker whose exit fill can't be read is looked up this many
# times (once a tick) before its exit is recorded at a guessed price (#131).
EXIT_LOOKUP_TRIES = 10

TRADE_COLS = ["time", "book", "classifier", "symbol", "side", "qty", "price", "notional", "reason", "pnl", "pnl_pct"]
Alert = Callable[[str, str], None]  # (level: "urgent"|"info", message)


def stale_feed(spy: pd.DataFrame | None, now: dt.datetime) -> tuple[bool, str]:
    """Is the whole feed stale (e.g. a silently dead websocket)? SPY trades every minute, so a
    last bar more than 3 minutes old, or none at all a few minutes after the open (a stream that
    never delivered), means we're not seeing the market. Never judged per symbol: thin IEX
    names such as XLV can go minutes without a print while the feed is fine."""
    if spy is None or spy.empty:
        mso = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60 + 1
        return mso > 4, "no SPY bars this session"
    return (now - (spy.index[-1] + pd.Timedelta(minutes=1))) > dt.timedelta(minutes=3), f"last SPY bar {spy.index[-1]:%H:%M}"


@dataclass
class Entry:
    classifier: str
    qty: float
    price: float
    stop: float
    target: float
    time: dt.datetime
    stop_id: str | None = None  # server-side stop order, if Alpaca accepted one
    # Execution toolkit (#38): fixed when the position opens, so a restart or an edited
    # spec can't change the rules of a position already held.
    init_stop: float = 0.0  # stop at entry (0: same as stop, for entries saved before #38)
    high: float = 0.0  # highest price since entry: the trailing stop's anchor
    trail_pct: float | None = None
    max_hold_min: int | None = None
    scale_at: float | None = None  # scale-out price; None when not configured or done
    scale_fraction: float = 0.0
    scale_breakeven: bool = False
    orig_qty: float = 0.0  # qty at entry (0: same as qty)
    banked: float = 0.0  # P&L already realised by a scale-out
    server_stop: float = 0.0  # price of the server-side stop order (when stop_id)
    last_sell: str = ""  # ISO time the last scale-out sell completed (its fill isn't the exit)
    # Bars before this (ISO) have been checked. Persisted: after a restart, bars from before
    # the trail raised the stop must not be re-tested against the raised stop.
    scanned_to: str = ""
    # A price in this round trip is a guess (the entry's, #60, or an exit leg's, #61): its P&L is
    # not evidence (see record_exit).
    price_estimated: bool = False
    # Exit orders already booked (#61): order id -> [cumulative qty, cost] of its fill booked so
    # far. A leg reported again, by a retry or after a restart, is booked once (see _unbooked).
    sold: dict = field(default_factory=dict)
    cost: float = 0.0  # what every share bought cost (0: orig_qty x price), for pnl_pct

    def exits_since(self) -> dt.datetime:
        """Where to look for this position's exit fills in the broker's order history."""
        return dt.datetime.fromisoformat(self.last_sell) if self.last_sell else self.time

    def __post_init__(self):
        self.init_stop = self.init_stop or self.stop
        self.high = self.high or self.price
        self.orig_qty = self.orig_qty or self.qty
        self.cost = self.cost or self.orig_qty * self.price
        if self.stop_id and not self.server_stop:
            self.server_stop = self.stop


def _tol(qty: float) -> float:
    """Share quantities this close are the same (fractional qty, float arithmetic)."""
    return 1e-6 + 1e-4 * qty


def _unbooked(e: Entry, fill: Fill, commit: bool = True) -> tuple[float, float, bool]:
    """(qty, cost, estimated) of what this fill's orders sold beyond what is already booked on the
    entry, which `commit` then books. Each order is keyed by its id against its cumulative fill,
    so a leg reported again by a retry or after a restart counts once (#61). A fill with no order
    id (a sim fill, one found in the order history) counts whole."""
    q = c = 0.0
    est = False
    for f in fill.legs or [fill]:
        dq, dc = f.qty, f.qty * f.price
        if f.order_id:
            bq, bc = e.sold.get(f.order_id, (0.0, 0.0))
            dq, dc = f.qty - bq, f.qty * f.price - bc
            if dq <= 1e-9:
                continue
            if commit:
                e.sold[f.order_id] = [f.qty, f.qty * f.price]
        q, c, est = q + dq, c + dc, est or f.estimated
    return q, c, est


def _known(cls, d: dict) -> dict:
    """Only the fields this code knows, so a file written by newer or older code still loads."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _atomic_write(path: Path, text: str) -> None:
    """Readers (and a restart after a crash mid-write) never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


@dataclass
class Pending:
    """An entry order until it is settled: a resting limit, or any entry sent without a clear
    answer yet (#60). Its notional is reserved against settled cash until it fills or is
    cancelled, and it holds the symbol's one-position slot. `limit` 0 means a market order, and
    an empty `order_id` one not yet found by its `client_id`."""

    classifier: str
    order_id: str
    limit: float
    qty: float
    reserved: float
    placed: dt.datetime
    expires: dt.datetime
    stop_pct: float  # stop distance chosen when the order was placed
    target_pct: float
    params: dict  # Entry execution fields (see _entry_params)
    client_id: str
    # What of this order's cumulative fill is already booked into a position (#59). A partial
    # fill is protected at once, while the order may still be settling; anything beyond these
    # is new, so a repeat poll or a restart never books the same shares twice.
    filled_qty: float = 0.0
    filled_cost: float = 0.0
    # The position this order opened has closed (#60). Anything it fills from now on is not a new
    # position or trade: one order is at most one round trip, and those shares are sold as untracked.
    exited: bool = False
    price_estimated: bool = False  # some of the booked cost was priced by a guess (#60)


@dataclass
class Book:
    """One account (sim, Alpaca paper or Alpaca live) with its own risk state and logs."""

    name: str
    broker: object
    dir: Path
    nav: NavBook = field(default_factory=NavBook)
    day_start_equity: float = 0.0
    blocked: str | None = None  # "kill" | "halt" | "stop"
    entries: dict[str, Entry] = field(default_factory=dict)
    pending: dict[str, Pending] = field(default_factory=dict)
    realised_today: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    # Cash-account settlement ledger (#19): buys may use at most the cash that was settled at
    # the open; same-day sale proceeds settle T+1, and rebuying with them then selling the
    # same day is a good-faith violation.
    cash_at_open: float = 0.0
    buys_today: float = 0.0
    # Equity couldn't be read at the start (#119), so the day-start baseline is a stand-in and no
    # new entries are made. "exact": nothing was held, so the first clean read is the true
    # baseline and entries resume; "floor": the first clean read may only raise the baseline;
    # "blocked": no new entries for the rest of the session.
    start_unverified: str | None = None
    # Tracked positions gone from the broker whose exit fill couldn't be read (#131): symbol ->
    # (failed lookups, closed while the runner was down). Never sold, looked up again each tick.
    unresolved: dict = field(default_factory=dict)

    def __post_init__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.nav = NavBook.load(self.dir / "nav.json")
        risk = self._read_risk()
        if risk.get("halted"):
            self.blocked = "halt"
        p = self.dir / "entries.json"
        if p.exists():
            for sym, e in json.loads(p.read_text()).items():
                self.entries[sym] = Entry(**_known(Entry, e | {"time": dt.datetime.fromisoformat(e["time"])}))
        p = self.dir / "pending.json"
        if p.exists():
            for sym, o in json.loads(p.read_text()).items():
                self.pending[sym] = Pending(**_known(Pending, o | {k: dt.datetime.fromisoformat(o[k]) for k in ("placed", "expires")}))

    def save_entries(self) -> None:
        data = {s: e.__dict__ | {"time": e.time.isoformat()} for s, e in self.entries.items()}
        _atomic_write(self.dir / "entries.json", json.dumps(data))  # rewritten every bar while holding

    def save_pending(self) -> None:
        data = {s: o.__dict__ | {"placed": o.placed.isoformat(), "expires": o.expires.isoformat()}
                for s, o in self.pending.items()}
        _atomic_write(self.dir / "pending.json", json.dumps(data))

    def _read_risk(self) -> dict:
        p = self.dir / "risk.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def write_risk(self, **kw) -> None:
        risk = self._read_risk() | kw
        tmp = self.dir / "risk.json.tmp"
        tmp.write_text(json.dumps(risk))
        tmp.replace(self.dir / "risk.json")  # atomic: readers never see a torn file

    def stop_requested(self, day: dt.date) -> bool:
        """STOP lives in its own file, written only by the dashboard/CLI (no lost updates
        with the engine's risk.json writes). It applies to the trading day it was pressed."""
        p = self.dir / "stop.json"
        try:
            return json.loads(p.read_text()).get("stop_on") == day.isoformat()
        except (OSError, ValueError):
            return False

    def append_trade(self, row: dict) -> None:
        p = self.dir / "trades.csv"
        new = not p.exists()
        with p.open("a", newline="") as f:
            w = csv.DictWriter(f, TRADE_COLS)
            if new:
                w.writeheader()
            w.writerow(row)

    def record_exit(self, sym: str, e: Entry, fill: Fill, reason: str, estimated: bool = False) -> float:
        """Forget the entry and log the closing trade; returns its P&L."""
        """After a scale-out or a partial exit (#61) the row is the whole round trip: pnl includes
        the banked legs and pnl_pct is on the whole cost, so the go-live gate counts one trade.
        Only what the fill's orders sold beyond the legs already booked is this leg.
        `estimated`: the exit price (or the entry's) is a guess (the real one is unknown), so the
        row has no pnl_pct and the gate, promotion and scoreboard leave it out. So is a fill that
        accounts for fewer shares than were held: the rest went at an unknown price."""
        q, cost, est = _unbooked(e, fill)
        px = cost / q if q > 1e-9 else fill.price
        estimated = estimated or e.price_estimated or est or q < e.qty - _tol(e.qty)
        p = self.pending.get(sym)
        if p is not None and not p.exited:  # its order may still fill: marked first, so a crash can't reopen it
            p.exited = True
            self.save_pending()
        self.entries.pop(sym, None)
        self.save_entries()
        leg = (px - e.price) * e.qty
        pnl = e.banked + leg
        self.realised_today += leg  # the banked part was counted when it was sold
        self.trades_today += 1
        self.wins_today += pnl > 0
        self.append_trade({
            "time": fill.time.isoformat(timespec="minutes"), "book": self.name, "classifier": e.classifier,
            "symbol": sym, "side": "sell", "qty": f"{e.qty:.6f}", "price": f"{px:.4f}",
            "notional": f"{e.qty * px:.2f}", "reason": reason + (" (price estimated)" if estimated else ""),
            "pnl": f"{pnl:.2f}", "pnl_pct": "" if estimated else f"{pnl / e.cost * 100:.3f}",
        })
        return pnl

    def record_partial(self, sym: str, e: Entry, fill: Fill, reason: str) -> None:
        """A leg that sold part of the position (a scale-out, or part of an exit, #61): side
        `sell_part` (not a closed trade for the gate), P&L banked on the entry. Only what the
        fill's orders sold beyond what is already booked counts, so a leg reported again is
        booked once. A leg with no reported price makes the whole round trip estimated."""
        q, cost, est = _unbooked(e, fill)
        if q <= 1e-9:
            return
        px = cost / q
        leg = (px - e.price) * q
        e.banked += leg
        e.qty = max(0.0, e.qty - q)
        e.price_estimated = e.price_estimated or est
        e.last_sell = dt.datetime.now(fill.time.tzinfo).isoformat() if fill.time else ""
        self.realised_today += leg
        self.save_entries()
        self.append_trade({
            "time": fill.time.isoformat(timespec="minutes"), "book": self.name, "classifier": e.classifier,
            "symbol": sym, "side": "sell_part", "qty": f"{q:.6f}", "price": f"{px:.4f}",
            "notional": f"{q * px:.2f}", "reason": reason + (" (price estimated)" if est else ""),
            "pnl": f"{leg:.2f}", "pnl_pct": "" if est else f"{(px / e.price - 1) * 100:.3f}",
        })

    def restore_realised(self, day: dt.date) -> float:
        """Today's realised P&L after a restart, so the allocator's budget doesn't forget losses.
        A final `sell` row already includes its scale-outs' banked P&L, so `sell_part` rows are
        skipped; positions still open add only what they have banked."""
        total = sum(e.banked for e in self.entries.values())
        p = self.dir / "trades.csv"
        if p.exists():
            with p.open(newline="") as f:
                total += sum(float(r["pnl"] or 0) for r in csv.DictReader(f)
                             if r["side"] == "sell" and r["time"].startswith(day.isoformat()))
        return total

    def append_equity(self, now: dt.datetime, equity: float, nav: float | None = None) -> None:
        p = self.dir / "equity.csv"
        new = not p.exists()
        nav = self.nav.nav_per_unit if nav is None else nav
        with p.open("a") as f:
            if new:
                f.write("time,equity,nav,hwm\n")
            f.write(f"{now.isoformat(timespec='minutes')},{equity:.2f},{nav:.5f},{self.nav.hwm:.5f}\n")

    def equity_marked_on(self, day: dt.date) -> bool:
        """Whether equity.csv already has a row for `day` (rows are appended in time order, so
        the last one tells)."""
        p = self.dir / "equity.csv"
        if not p.exists():
            return False
        with p.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256))
            lines = f.read().decode(errors="replace").strip().splitlines()
        return bool(lines) and lines[-1].startswith(day.isoformat())


class Engine:
    def __init__(
        self,
        specs: list[ClassifierSpec],
        # keyed by ClassifierSpec.book_key: "live" and "shadow" (may be the same Book), plus
        # "sim:<id>" for each `mode: sim` classifier (its own simulated account)
        books: dict[str, Book],
        decider,
        universe: set[str],
        run_dir: Path,
        alert: Alert = lambda level, msg: None,
    ):
        self.states = [ClassifierState(s) for s in specs]
        self.books = books
        missing = sorted({s.book_key for s in specs if not s.probe} - set(books))
        if missing:  # never fall back to another book: a sim classifier must not trade paper
            raise ValueError(f"no book for {missing}")
        self.decider = decider
        self.universe = universe
        self.run_dir = run_dir
        self.alert = alert
        self.day: dt.date | None = None
        self._spy_day: tuple[float, float] | None = None  # (first open, last close) of SPY today
        self.decision_errors = 0
        self.decisions_paused_until: dt.datetime | None = None
        self.probes_paused_until: dt.datetime | None = None  # probe failures never pause trading decisions
        self.probe_errors = 0  # counted apart from decision_errors, which means trading is affected
        self.calls: dict[str, int] = {}  # answered decision calls per classifier this session (since start)
        self._outage_errors = 0
        self._tick_started = 0.0
        self._decisions_fh = None
        self.last_tick: dt.datetime | None = None
        self.prices: dict[str, float] = {}  # last closes seen, for a flatten outside the tick
        self.flattened_at: dt.datetime | None = None  # the last minute flatten_for_close ran
        self._last_alert: dict[str, float] = {}
        self.prev_day: dict[str, pd.DataFrame] = {}
        run_dir.mkdir(parents=True, exist_ok=True)

    def _alert_every(self, key: str, level: str, msg: str, seconds: float = 600) -> None:
        """Alert at most once per `seconds` for a recurring condition (no push storms). Never
        raises: these alerts sit on failure paths, and one that can't be sent mustn't skip the
        recovery that follows it."""
        import time as _t

        now = _t.monotonic()
        if now - self._last_alert.get(key, -1e9) >= seconds:
            self._last_alert[key] = now
            try:
                self.alert(level, msg)
            except Exception:
                pass

    # ---- lifecycle -------------------------------------------------------------

    def unique_books(self) -> list[Book]:
        seen, out = set(), []
        for b in self.books.values():
            if id(b) not in seen:
                seen.add(id(b))
                out.append(b)
        return out

    def start_day(self, day: dt.date, prev_day: dict[str, pd.DataFrame],
                  settled_at_open: dict[str, float] | None = None, opened_at: dt.datetime | None = None) -> None:
        """`settled_at_open`: per-book settled cash measured at startup, before anything sells
        (today's sale proceeds, orphan closes included, are unsettled until T+1). `opened_at`:
        the time stamped on the day's opening equity row (default 09:30 ET)."""
        opened_at = opened_at or dt.datetime.combine(day, dt.time(9, 30), ET)
        self.day = day
        self._spy_day = None
        self._last_alert.clear()  # throttles are per session (a replay runs many days in seconds)
        self.prev_day = prev_day
        self.states = [ClassifierState(s.spec) for s in self.states]
        self.decisions_paused_until = None
        self.probes_paused_until = None
        self.probe_errors = 0
        self.calls = {}
        self._outage_errors = 0
        today = day.isoformat()
        for b in self.unique_books():
            if b.blocked in ("kill", "stop"):
                b.blocked = None
            risk = b._read_risk()
            restarted = risk.get("day") == today
            eq = self._read_equity(b)
            b.start_unverified = risk.get("start_unverified") if restarted else None
            if (b.dir / "stop.json").exists() and not b.stop_requested(day):
                (b.dir / "stop.json").unlink(missing_ok=True)  # a STOP from an earlier day has expired
            if restarted:
                # Restarted mid-session: keep the loss baseline and any kill/STOP already in force.
                b.day_start_equity = risk["day_start_equity"]
                cash = risk.get("cash_at_open")
                b.cash_at_open = cash if cash is not None else self._settled_or_zero(b)
                b.buys_today = risk.get("buys_today", 0.0)
                if risk.get("blocked_today") in ("kill", "stop"):
                    b.blocked = risk["blocked_today"]
            else:
                b.day_start_equity = eq if eq is not None else self._fallback_start(b, risk)
                settled = (settled_at_open or {}).get(b.name)
                b.cash_at_open = settled if settled is not None else self._settled_or_zero(b)
                b.buys_today = 0.0
                try:  # evidence for the first live weeks; never allowed to block session start
                    snap = getattr(b.broker, "settlement_snapshot", lambda: {})()
                except Exception:
                    snap = {}
                b.write_risk(day=today, day_start_equity=b.day_start_equity, blocked_today=None,
                             cash_at_open=b.cash_at_open, buys_today=0.0, account_at_open=snap,
                             start_unverified=b.start_unverified)
            if eq is not None:
                b.nav.mark(eq)  # never marked with a stand-in: the HWM halt uses only real reads
            if b.start_unverified is None:  # a stand-in never goes in the log; see _start_equity_readable
                self._opening_mark(b, opened_at, b.day_start_equity)
            try:  # a damaged trade log must never stop the session from starting
                b.realised_today = b.restore_realised(day) if restarted else 0.0
            except Exception as ex:
                # Only tighten: today's equity change also counts unrealised loss (the safe side).
                try:
                    drop = float(b.broker.equity()) - b.day_start_equity
                except Exception:
                    drop = math.nan
                ok = math.isfinite(drop)
                b.realised_today = min(0.0, drop) if ok else 0.0
                how = "today's equity change" if ok else "0 (equity unreadable)"
                self._alert_every(f"realised:{b.name}", "urgent", f"[{b.name}] couldn't re-read today's P&L from "
                                  f"trades.csv ({ex!r}): the stop-risk budget uses {how} "
                                  f"({b.realised_today:.2f}) as today's realised loss")
            b.trades_today, b.wins_today = 0, 0
        self._restore_classifier_state(today)
        ddir = self.run_dir / "decisions"
        ddir.mkdir(exist_ok=True)
        if self._decisions_fh:
            self._decisions_fh.close()
        self._decisions_fh = (ddir / f"{day.isoformat()}.jsonl").open("a")

    def _read_equity(self, b: Book) -> float | None:
        """Equity, retried briefly; None (alerted) if it still can't be read. Never raises (#119)."""
        for i in range(START_EQUITY_TRIES):
            try:
                eq = float(b.broker.equity())
                if math.isfinite(eq):
                    return eq
                err = ValueError(f"equity {eq}")
            except Exception as ex:
                err = ex
            if i + 1 < START_EQUITY_TRIES:
                time.sleep(START_EQUITY_RETRY_S)
        self._alert_every(f"start-equity:{b.name}", "urgent", f"[{b.name}] couldn't read equity at session "
                          f"start ({err!r}): no new entries until it can be read; exits, stops and the flatten run")
        return None

    def _opening_mark(self, b: Book, at: dt.datetime, equity: float) -> None:
        """The day's first equity row, at the real day-start equity, so the go-live gate's worst
        day and the equity chart count from the open, not from the first 5-minute mark (#50).
        Once a day: a restart after any row for today adds nothing. Never raises: it's a log row,
        and must not stop the session from starting."""
        try:
            if equity > 0 and not b.equity_marked_on(at.date()):
                b.append_equity(at, equity, equity / b.nav.units if b.nav.units > 0 else None)
        except Exception as ex:
            self._alert_every(f"equity-mark:{b.name}", "urgent", f"[{b.name}] equity mark failed: {ex!r}")

    def _settled_or_zero(self, b: Book) -> float:
        """Settled cash for the day's buys; 0 (no buys today) if it can't be read."""
        try:
            return b.broker.settled_cash()
        except Exception as ex:
            self._alert_every(f"start-cash:{b.name}", "urgent", f"[{b.name}] couldn't read settled cash at "
                              f"session start ({ex!r}): no buys today")
            return 0.0

    def _fallback_start(self, b: Book, risk: dict) -> float:
        """A stand-in day-start equity when equity can't be read at the start: the last NAV mark
        (the last close's equity, adjusted for cashflows since), else the last persisted
        day_start_equity. Sets `start_unverified`, which blocks new entries: to "exact" when
        nothing is held, so the first clean read *is* today's starting equity; to "floor" when
        something is held (its moves since the open are unknown) or when there's no stand-in at
        all (the first session ever), so no new entries today."""
        fallback = b.nav.last_equity if b.nav.last_equity > 0 else float(risk.get("day_start_equity") or 0.0)
        try:
            held = bool(b.entries or b.pending or b.broker.get_positions())
        except Exception:
            held = True
        b.start_unverified = "exact" if fallback > 0 and not held else "floor"
        return fallback

    def _start_equity_readable(self, b: Book, eq: float, now: dt.datetime) -> None:
        """Equity reads cleanly again after an unreadable start. The kill switch is never
        loosened: "exact" takes the read as the baseline only because nothing was held (so
        equity hasn't moved since the open); "floor" may only raise the stand-in."""
        if b.start_unverified == "exact":
            try:  # nothing was sold today either, so settled cash is still the open's
                cash = b.broker.settled_cash()
            except Exception:
                return  # entries stay blocked; next minute tries again
            b.day_start_equity, b.cash_at_open, b.start_unverified = eq, cash, None
            b.write_risk(day_start_equity=eq, cash_at_open=cash, start_unverified=None)
            self._opening_mark(b, now, eq)  # now the real day-start equity: the day's first row
            self.alert("info", f"[{b.name}] equity readable again: today's starting equity is ${eq:.2f}; entries resume")
        elif b.start_unverified == "floor":
            b.day_start_equity = max(b.day_start_equity, eq)
            b.start_unverified = "blocked"
            b.write_risk(day_start_equity=b.day_start_equity, start_unverified="blocked")
            self.alert("info", f"[{b.name}] equity readable again (${eq:.2f}), but today's starting equity is "
                               f"unknown: kill-switch baseline ${b.day_start_equity:.2f}; no new entries today")

    # ---- per-day classifier state (survives restarts) ------------------------------

    def _classifier_state_file(self) -> Path:
        return self.run_dir / "classifier_state.json"

    def _save_classifier_state(self) -> None:
        if not self.day:
            return
        data = {"day": self.day.isoformat(), "states": {
            cs.spec.id: {s: {"status": st.status, "trades": st.trades} for s, st in cs.symbols.items()}
            for cs in self.states}}
        tmp = self._classifier_state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._classifier_state_file())

    def _restore_classifier_state(self, today: str) -> None:
        """After a mid-session restart, keep trade counts and retirements so max_trades and
        stand-downs still hold."""
        f = self._classifier_state_file()
        data = json.loads(f.read_text()) if f.exists() else {}
        if data.get("day") != today:
            return
        for cs in self.states:
            if cs.spec.probe:
                continue  # probes hold nothing and never retire
            book = self.books[cs.spec.book_key]
            for sym, saved in data.get("states", {}).get(cs.spec.id, {}).items():
                st = cs.symbols.get(sym)
                if st is None:
                    continue
                st.trades = int(saved.get("trades", 0))
                status = saved.get("status", "armed")
                e = book.entries.get(sym)
                mine = bool(e and e.classifier == cs.spec.id)
                o = book.pending.get(sym)
                if status == "pending":  # a limit entry was resting
                    st.status = "holding" if mine else "pending" if (o and o.classifier == cs.spec.id) else "armed"
                elif status == "holding" and not mine:
                    cs.on_exit(sym)  # position closed while we were down
                else:
                    st.status = status

    def end_day(self, now: dt.datetime) -> dict:
        summary = {}
        for b in self.unique_books():
            eq = b.broker.equity()
            b.nav.mark(eq)
            b.nav.save(b.dir / "nav.json")
            b.append_equity(now, eq)
            summary[b.name] = {
                "equity": round(eq, 2),
                # None after an unreadable start: the baseline was a stand-in, not the day's start
                "day_pnl": None if b.start_unverified else round(eq - b.day_start_equity, 2),
                "trades": b.trades_today,
                "wins": b.wins_today,
                "nav": round(b.nav.nav_per_unit, 5),
            }
        self._record_benchmark()
        if self._decisions_fh:
            self._decisions_fh.close()
            self._decisions_fh = None
            path = self.run_dir / "decisions" / f"{self.day.isoformat()}.jsonl"
            if path.exists():
                with path.open("rb") as src, gzip.open(str(path) + ".gz", "wb") as dst:
                    dst.write(src.read())
                path.unlink()
        return summary

    def _record_benchmark(self) -> None:
        """SPY's open and close for the day, so the dashboard can plot buy-and-hold SPY (the
        mission's yardstick) next to NAV. One row per session; a rerun of the day replaces it.
        Display only, so it never raises: end_day must go on to the gate and the daily alert."""
        if self._spy_day is None or self.day is None:
            return
        try:
            p = self.run_dir / "benchmark.csv"
            rows = [line for line in (p.read_text().splitlines()[1:] if p.exists() else [])
                    if not line.startswith(self.day.isoformat())]
            o, c = self._spy_day
            rows.append(f"{self.day.isoformat()},{o:.4f},{c:.4f}")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text("date,spy_open,spy_close\n" + "\n".join(sorted(rows)) + "\n")
            tmp.replace(p)
        except Exception as e:
            self.alert("info", f"could not record the SPY benchmark for {self.day}: {e!r}")

    # ---- per-minute tick --------------------------------------------------------

    def tick(self, now: dt.datetime, bars: dict[str, pd.DataFrame], minutes_to_close: float,
             feed_spy: pd.DataFrame | None = None) -> None:
        """`feed_spy`: the live stream's SPY bars, if `bars` also holds bars from elsewhere (an
        empty frame when the stream has delivered none, which is stale, never None)."""
        self.last_tick = now
        self._tick_started = time.monotonic()
        prices = {s: float(b.close.iloc[-1]) for s, b in bars.items() if len(b)}
        spy = bars.get("SPY")
        if spy is not None and len(spy):
            self._spy_day = (float(spy.open.iloc[0]), float(spy.close.iloc[-1]))
        self.prices = prices
        failed = []
        for b in self.unique_books():
            # One book's failure (an API error, a full disk) mustn't skip the other books or
            # the flatten below; it only blocks new entries this minute.
            try:
                self._book_checks(b, now, bars, prices)
            except Exception as ex:
                failed.append(ex)
                self._alert_every(f"book-checks:{b.name}", "urgent",
                                  f"[{b.name}] per-minute checks failed at {now:%H:%M}: {ex!r}")

        if G.flatten_due(minutes_to_close):
            self.flatten_for_close(now, prices)
        elif not failed:
            self._run_classifiers(now, bars, minutes_to_close, feed_spy)

        if now.minute % 5 == 0:
            for b in self.unique_books():
                try:
                    b.append_equity(now, b.broker.equity())
                except Exception as ex:
                    self._alert_every(f"equity-mark:{b.name}", "urgent", f"[{b.name}] equity mark failed: {ex!r}")
        self.write_status(now, minutes_to_close)
        if failed and not G.flatten_due(minutes_to_close):
            raise failed[0]  # counted by the runner; in the flatten window it was alerted above

    def _book_checks(self, b: Book, now, bars, prices) -> None:
        b.broker.update_prices(prices)
        if hasattr(b.broker, "update_bars"):
            b.broker.update_bars(bars)  # simulated limit fills
        self._poll_pending(b, now)
        self._enforce_exits(b, now, bars)
        self._risk(b, now)
        if b.blocked and (b.entries or b.pending):  # a kill/halt/STOP flatten failed earlier: keep retrying
            self._flatten(b, now, prices, f"{b.blocked} (retry)")
        if b.blocked == "stop" or (self.day and b.stop_requested(self.day)):
            if b.blocked != "stop":
                b.blocked = "stop"
                b.write_risk(blocked_today="stop")
                self._flatten(b, now, prices, "manual STOP")
                self.alert("urgent", f"[{b.name}] STOP pressed: flattened and halted for the day")

    def flatten_for_close(self, now: dt.datetime, prices: dict[str, float] | None = None) -> None:
        """The end-of-day flatten, every minute of the window. Never raises: a failure in one
        book's bookkeeping (a trade log write, a positions call) falls back to closing
        everything at the broker, and can't stop another book's flatten. Also the runner's last
        resort when a tick raises inside the window, so it must not depend on the tick."""
        prices = self.prices if prices is None else prices
        for b in self.unique_books():
            try:
                b.broker.update_prices(prices)  # sim books close at the last price, not the entry
                # Every minute of the window, not just once: retries anything a failed
                # close left behind, and catches positions the engine doesn't track.
                if b.entries or b.pending or b.broker.get_positions():
                    self._flatten(b, now, prices, "eod flatten", final=True)
            except Exception as ex:
                try:  # close first, alert after: the alert path can fail for the same reason
                    left, why = b.broker.flatten_all(now), None
                except Exception as ex2:
                    left, why = None, ex2
                self._alert_every(f"eod-flatten:{b.name}", "urgent",
                                  f"[{b.name}] eod flatten failed ({ex!r}); closed everything at the broker")
                if why is not None:
                    self._alert_every(f"flatten:{b.name}", "urgent", f"[{b.name}] flatten_all failed: {why!r}")
                elif left:
                    self._alert_every(f"holding:{b.name}", "urgent",
                                      f"[{b.name}] STILL HOLDING after flatten (eod flatten): {sorted(left)}. Check Alpaca now.")
        for cs in self.states:
            for st in cs.symbols.values():
                st.status = "retired"
        self.flattened_at = now

    def _enforce_exits(self, b: Book, now, bars) -> None:
        held = b.broker.get_positions()
        if b.broker.name != "sim" and any(s not in held and e.time < now for s, e in b.entries.items()):
            time.sleep(1.0)  # the positions endpoint can lag: look once more before calling one gone
            held = b.broker.get_positions()
        # A resting order holds its symbol, until the position it opened has closed: then any shares
        # left are a late fill of it, not a position, and are sold like any untracked holding (#60).
        orphans = sorted(held.keys() - b.entries.keys() - {s for s, p in b.pending.items() if not p.exited})
        for sym, e in list(b.entries.items()):
            bar = bars.get(sym)
            if sym not in held and e.time >= now:
                continue  # opened this tick: the positions endpoint may not show it yet
            if sym not in held:  # server-side stop filled, or closed outside the engine
                self._vanished(b, sym, e, now, float(bar.close.iloc[-1]) if bar is not None and len(bar) else e.price)
                continue
            b.unresolved.pop(sym, None)  # held after all (a lagging positions read)
            if bar is None or not len(bar):
                # No print yet this session (a restart before its first IEX bar, or a symbol left
                # out of the stream): the time stop still applies, at the last price seen. A sim
                # book fills at that price, so with none it waits for one rather than invent it.
                px = b.broker.last.get(sym)
                if e.max_hold_min and now - e.time >= dt.timedelta(minutes=e.max_hold_min) \
                        and (px is not None or b.broker.name != "sim"):
                    self._exit(b, sym, e, px or e.price, now, "time stop")
                continue
            self._scan_bars(b, sym, e, bar, now)
        for sym in orphans:  # after the tracked positions' stops: a slow close mustn't delay them
            self._close_orphan(b, sym, held[sym], now, bars)

    def _vanished(self, b: Book, sym, e: Entry, now, px: float, final: bool = False) -> None:
        """A tracked position gone from the broker: book its exit from its server stop's fill, else
        from its sells since it opened. A lookup that fails is not "no fill" (#131): the entry stays
        tracked, is never sold (its shares are gone), and is looked up again next tick, up to
        EXIT_LOOKUP_TRIES or the EOD flatten (`final`). Only then, or when nothing is found, is the
        exit a guess: at `px`, or at the stop for one that closed while the runner was down."""
        tries, down = b.unresolved.get(sym, (0, False))
        try:
            fill, how = b.broker.stop_fill(e.stop_id, now, strict=True), "server stop"
            if fill is None:
                fill, how = b.broker.exit_fill_since(sym, e.exits_since(), now, strict=True), "closed outside engine"
        except Exception as ex:
            if tries + 1 < EXIT_LOOKUP_TRIES and not final:
                b.unresolved[sym] = (tries + 1, down)
                self._alert_every(f"exit-lookup:{b.name}:{sym}", "info",
                                  f"[{b.name}] {sym} is gone at the broker but its exit fill couldn't be read ({ex}); "
                                  "looking again each minute")
                return
            fill = None
        b.unresolved.pop(sym, None)
        if fill is not None:
            self._record_exit(b, sym, e, fill, "closed while runner down" if down else how)
            if how == "closed outside engine" and not down:
                self.alert("urgent", f"[{b.name}] {sym} was closed outside the engine at {fill.price:.2f}")
        elif down:  # as reconcile_at_startup: a guess, at the stop, so today's loss budget sees a loss (#117)
            self._record_exit(b, sym, e, Fill(sym, "sell", e.qty, e.stop, now),
                              "closed while runner down (recorded at stop)", estimated=True)
            self.alert("urgent", f"[{b.name}] {sym} closed while the runner was down and its exit price is unknown; "
                                 f"recorded at the stop ({e.stop:.2f})")
        else:
            self._record_exit(b, sym, e, Fill(sym, "sell", e.qty, px, now), "closed outside engine", estimated=True)
            self.alert("urgent", f"[{b.name}] {sym} position disappeared without a stop fill; P&L estimated at last price")

    def _close_orphan(self, b: Book, sym, pos, now, bars) -> None:
        """A broker position the engine has no record of (a crash before its entry was saved, the
        remainder of a partial close, a buy made outside the engine) has no stop, so close it.
        Only ever called from the tick, so never before the open (a market sell would queue, and
        be cancelled after 20 s) or after the bell. On failure the next minute retries."""
        bar = bars.get(sym)
        ref = float(bar.close.iloc[-1]) if bar is not None and len(bar) else b.broker.last.get(sym, pos.avg_price)
        try:
            b.broker.cancel_orders({sym})  # a resting sell (e.g. an old stop) would block the close
            fill = b.broker.sell_all(sym, ref, now, f"orphan-{sym}-{now:%m%d%H%M}")
        except Exception as ex:
            self._alert_every(f"orphan-failed:{b.name}:{sym}", "urgent",
                              f"[{b.name}] could not close untracked position {sym} ({ex}); retrying each minute")
            return
        at = f" at {fill.price:.2f}" if fill else ""
        self.alert("urgent", f"[{b.name}] closed untracked position {sym} ({pos.qty:g} shares){at}; no trade recorded")

    @staticmethod
    def _set_cursor(b: Book, e: Entry, ts) -> None:
        e.scanned_to = pd.Timestamp(ts).isoformat()
        b.save_entries()

    def _scan_bars(self, b: Book, sym: str, e: Entry, bar: pd.DataFrame, now) -> None:
        """Every bar since the last check, in order, so a slow or skipped tick can't jump past
        a stop. Per bar: stop (first: the order inside a bar is unknown, so assume the worst),
        target, scale-out, then ratchet the trail from that bar's high, which therefore
        never raises the stop that the same bar's low is tested against."""
        since = pd.Timestamp(e.scanned_to or e.time)
        window = bar[bar.index >= since]
        sim = b.broker.name == "sim"
        last = float(bar.close.iloc[-1])
        dirty = False
        for ts, row in window.iterrows():
            if row.low <= e.stop:
                why = "stop" if e.stop <= e.init_stop + 1e-9 else "stop (raised)"
                self._set_cursor(b, e, ts)  # a failed exit re-scans from this bar next tick
                self._exit(b, sym, e, min(float(row.open), e.stop), now, why)
                return
            if ts < pd.Timestamp(e.time):
                continue  # the bar a limit entry filled in: only its stop risk counts
            if row.high >= e.target:
                self._set_cursor(b, e, ts)
                self._exit(b, sym, e, max(float(row.open), e.target) if sim else last, now, "target")
                return
            if e.scale_at is not None and row.high >= e.scale_at:
                if not self._scale_out(b, sym, e, max(float(row.open), e.scale_at) if sim else last, now):
                    self._set_cursor(b, e, ts)
                    return
            if row.high > e.high:
                e.high, dirty = float(row.high), True
                if e.trail_pct and e.high * (1 - e.trail_pct / 100) > e.stop:
                    e.stop = e.high * (1 - e.trail_pct / 100)
        if len(window):
            e.scanned_to, dirty = (window.index[-1] + pd.Timedelta(minutes=1)).isoformat(), True
        if dirty:
            b.save_entries()
        if e.stop_id and e.stop > e.server_stop + max(0.01, e.stop * 0.0005):
            self._move_server_stop(b, sym, e, now)
            if sym not in b.entries:
                return  # the old stop had fired for all of it
        if e.max_hold_min and now - e.time >= dt.timedelta(minutes=e.max_hold_min):
            self._exit(b, sym, e, last, now, "time stop")

    def _risk(self, b: Book, now) -> None:
        eq = b.broker.equity()
        if b.start_unverified:
            self._start_equity_readable(b, eq, now)
        nav = b.nav.mark(eq)
        # Still "exact" (settled cash unreadable): nothing is held or bought, so today's loss is 0,
        # not the read against the stand-in; the halt is still checked on the real read.
        base = eq if b.start_unverified == "exact" else b.day_start_equity
        verdict = G.risk_check(eq, base, nav, b.nav.hwm)
        if verdict == "halt" and b.blocked != "halt":
            b.blocked = "halt"
            b.nav.save(b.dir / "nav.json")  # so a human clear-halt after the close rebases from here
            b.write_risk(halted=True, reason=f"equity {eq:.2f}, nav {nav:.4f}, hwm {b.nav.hwm:.4f}", at=now.isoformat())
            self._flatten(b, now, b.broker.last, "HALT")
            self.alert("urgent", f"[{b.name}] HALT: equity ${eq:.2f}, NAV {nav:.3f} vs HWM {b.nav.hwm:.3f}. Human must clear.")
        elif verdict == "kill" and b.blocked is None:
            b.blocked = "kill"
            b.write_risk(blocked_today="kill")
            self._flatten(b, now, b.broker.last, "daily kill switch")
            self.alert("urgent", f"[{b.name}] Kill switch: day loss {eq - b.day_start_equity:.2f}. Flat until next session.")

    def _run_classifiers(self, now, bars, minutes_to_close, feed_spy=None) -> None:
        mso = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60 + 1
        spy = bars.get("SPY", pd.DataFrame())
        # Least recently asked first, so a slow model can't starve later classifiers/symbols.
        oldest = dt.datetime.min.replace(tzinfo=now.tzinfo)
        # Judged on the stream's own SPY bars when the runner passes them: bars it polled over
        # REST during an outage (#50) keep stops working, but mustn't reopen entries.
        feed_stale, seen = stale_feed(spy if feed_spy is None else feed_spy, now)
        if feed_stale:
            self._alert_every("stale-feed", "urgent", f"Market data stale: {seen}; entries blocked", 1800)
        # Trading classifiers before probes, so probes only ever use what's left of the tick.
        order = sorted(((cs, sym) for cs in self.states for sym in cs.spec.symbols),
                       key=lambda p: (p[0].spec.probe, p[0].symbols[p[1]].last_eval or oldest))
        for cs, sym in order:
            spec = cs.spec
            if not spec.in_window(now):
                continue
            st = cs.symbols[sym]
            sb = bars.get(sym)
            if st.status == "retired" or sb is None or len(sb) < 2 or not cs.due(sym, now):
                continue
            if st.status == "armed" and feed_stale:
                continue  # no new entries on stale data; exits and stops still run
            if not (self._probes_available(now) if spec.probe else self._decisions_available(now)):
                continue  # not marked evaluated, so it's asked again next tick
            st.last_eval = now
            ctx = F.FeatureContext(self.prev_day.get(sym, pd.DataFrame()), spy, mso, minutes_to_close)
            if spec.probe:
                self._probe(spec, st, sym, now, sb, ctx)
                continue
            book = self.books[spec.book_key]
            if st.status == "armed":
                if book.start_unverified and not book.blocked:
                    st.note = START_UNVERIFIED_NOTE
                elif st.note == START_UNVERIFIED_NOTE:
                    st.note = ""
                if book.blocked or book.start_unverified or sym in book.entries or sym in book.pending:
                    continue
                trig_feats = F.compute([t.feature for t in spec.trigger], sb, ctx)
                if not all(t.holds(trig_feats) for t in spec.trigger):
                    st.last_choice = "no-trigger"
                    continue
                feats = F.compute(spec.features, sb, ctx)
                choice, probs = self._ask(spec, sym, now, sb, feats, None, "entry")
                if choice is None:
                    continue
                thr = spec.entry.threshold
                if probs.get("ENTER", 0) >= thr:
                    self._enter(book, cs, sym, sb, now, minutes_to_close, ctx)
                elif probs.get("STAND_DOWN", 0) >= thr:
                    st.status = "retired"
                    st.note = "stood down"
            elif st.status == "holding":
                e = book.entries.get(sym)
                if e is None or e.classifier != spec.id:
                    cs.on_exit(sym)
                    continue
                if sym in book.unresolved:
                    continue  # gone at the broker, its exit still being looked up (#131): nothing to sell
                feats = F.compute(spec.features, sb, ctx)
                px = float(sb.close.iloc[-1])
                pos = {
                    "unrealised_pct": round((px / e.price - 1) * 100, 3),
                    "minutes_held": int((now - e.time).total_seconds() // 60),
                    "stop_dist_pct": round((e.stop / px - 1) * 100, 3),
                    "target_dist_pct": round((e.target / px - 1) * 100, 3),
                }
                if spec.scale_out:
                    pos["scaled_out"] = e.qty < e.orig_qty - 1e-9
                choice, probs = self._ask(spec, sym, now, sb, feats, pos, "exit")
                if choice is not None and probs.get("EXIT", 0) >= spec.exit.threshold:
                    self._exit(book, sym, e, px, now, "classifier EXIT")

    # ---- decisions and orders --------------------------------------------------

    def _probe(self, spec, st, sym, now, sb, ctx) -> None:
        """Ask the entry question and log the answer with the price it was asked at. Never
        orders and never stands down: `trader probe-report` scores these rows afterwards."""
        trig_feats = F.compute([t.feature for t in spec.trigger], sb, ctx)
        if not all(t.holds(trig_feats) for t in spec.trigger):
            st.last_choice = "no-trigger"
            return
        feats = F.compute(spec.features, sb, ctx)
        self._ask(spec, sym, now, sb, feats, None, "probe")

    def _ask(self, spec, sym, now, sb, feats, pos, kind):
        rets = (sb.close.pct_change().iloc[-10:] * 1e4).round(1).fillna(0).tolist()
        state = {
            "symbol": sym,
            "minutes_since_open": int((now - now.replace(hour=9, minute=30)).total_seconds() // 60) + 1,
            "features": {k: (round(v, 4) if math.isfinite(v) else None) for k, v in feats.items()},
            "recent_1m_returns_bps": rets,
            "position": pos,
            "strategy_note": spec.context,
        }
        q = spec.exit if kind == "exit" else spec.entry
        try:
            if getattr(self.decider, "offline", False) and pos is None:
                state["features"]["_trigger"] = 1.0  # trigger already passed; lets the stub enter
            d = self.decider.decide(state, q.instructions, q.criteria)
        except DecisionError as e:
            if kind == "probe":  # research only: back off probes, leave trading decisions alone
                self.probe_errors += 1
                self.probes_paused_until = now + DECISION_COOLDOWN
                if self.probe_errors == 1:
                    self.alert("info", f"Probe {spec.id} failed ({e}); probes pause for "
                                       f"{DECISION_COOLDOWN.seconds // 60} min at a time. Trading is unaffected.")
                return None, {}
            self.decision_errors += 1
            self._outage_errors += 1
            first = self.decisions_paused_until is None
            self.decisions_paused_until = now + DECISION_COOLDOWN
            if first:
                self.alert("urgent", f"Decision model failing: {e}. Pausing all decisions until "
                                     f"{self.decisions_paused_until:%H:%M} ET: no new entries and no "
                                     f"model-driven exits; stops, targets and risk checks still enforced.")
            return None, {}
        if self.decisions_paused_until is not None:
            self.decisions_paused_until = None
            self.alert("info", f"Decision model recovered after {self._outage_errors} failed call(s).")
            self._outage_errors = 0
        st = next(c for c in self.states if c.spec.id == spec.id).symbols[sym]
        st.last_choice, st.last_probs = d.choice, d.probabilities
        self.calls[spec.id] = self.calls.get(spec.id, 0) + 1
        self._log_decision({
            "t": now.strftime("%H:%M"), "c": spec.id, "s": sym, "q": kind,
            "p": {k: round(v, 3) for k, v in d.probabilities.items()},
            "f": {k: (round(v, 4) if math.isfinite(v) else None) for k, v in feats.items()},
            **({"pos": pos} if pos else {}),
            # Everything else Jev saw, so probe-report's features-only baseline gets the same inputs.
            **({"px": round(float(sb.close.iloc[-1]), 4), "m": state["minutes_since_open"], "r": rets}
               if kind == "probe" else {}),
        })
        return d.choice, d.probabilities

    def _decisions_available(self, now: dt.datetime) -> bool:
        """Circuit breaker plus a per-tick wall-clock budget, so a slow or failing decision
        model can't stall exit and risk enforcement."""
        if self.decisions_paused_until is not None and now < self.decisions_paused_until:
            return False
        return time.monotonic() - self._tick_started < TICK_DECISION_BUDGET_S

    def _probes_available(self, now: dt.datetime) -> bool:
        if self.probes_paused_until is not None and now < self.probes_paused_until:
            return False
        return self._decisions_available(now) and time.monotonic() - self._tick_started < PROBE_TICK_BUDGET_S

    def _log_decision(self, row: dict) -> None:
        if self._decisions_fh:
            self._decisions_fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._decisions_fh.flush()

    def _stop_pct(self, spec: ClassifierSpec, sb, ctx) -> float:
        """Stop distance in %: `stop_pct`, or `stop_atr_mult` x atr_14_pct capped at stop_pct
        (falls back to stop_pct while there aren't enough bars for the ATR)."""
        if spec.stop_atr_mult and ctx is not None:
            atr = F.compute(["atr_14_pct"], sb, ctx)["atr_14_pct"]
            if math.isfinite(atr) and atr > 0:
                return max(0.05, min(spec.stop_pct, spec.stop_atr_mult * atr))
        return spec.stop_pct

    @staticmethod
    def _entry_params(spec: ClassifierSpec) -> dict:
        so = spec.scale_out
        return {"trail_pct": spec.trail_pct, "max_hold_min": spec.max_hold_min,
                "scale_pct": so.at_pct if so else None, "scale_fraction": so.fraction if so else 0.0,
                "scale_breakeven": bool(so and so.stop_to_breakeven)}

    def _enter(self, book: Book, cs: ClassifierState, sym, sb, now, minutes_to_close, ctx=None) -> None:
        spec = cs.spec
        st = cs.symbols[sym]
        px = float(sb.close.iloc[-1])
        eq = book.broker.equity()
        # Never spend same-day sale proceeds: at most the cash settled at the open, less today's buys.
        cash = max(0.0, min(book.broker.settled_cash(), book.cash_at_open - book.buys_today))
        stop_pct = self._stop_pct(spec, sb, ctx)
        size = spec.size_fraction * eq
        if spec.risk_pct:  # lose about risk_pct of equity at the stop; never more than size_fraction
            size = min(size, eq * spec.risk_pct / stop_pct)
        if book.name == "live" and book._read_risk().get("live_sessions", 99) <= 5:
            size /= 2  # first live week runs at half size
        size = min(size, cash)
        positions = book.broker.get_positions()
        requested = size
        size, binding = A.allocate(sym, size, stop_pct / 100, eq, book.day_start_equity,
                                   book.realised_today, self._exposures(book, positions))
        if binding != "requested":
            floor = A.trim_floor(requested)
            st.note = f"allocator: {binding}; allowed ${size:.2f} of ${requested:.2f}"
            if size < floor:
                st.note += f"; skipped, under the ${floor:.2f} floor for a trimmed entry"
            self._log_decision({"t": now.strftime("%H:%M"), "c": spec.id, "s": sym, "q": "allocation",
                                "constraint": binding, "requested": round(requested, 2), "allowed": round(size, 2),
                                "floor": round(floor, 2)})
            if size < floor:  # never dust (#118); floor > MIN_NOTIONAL, so check_entry can't overwrite the note
                return
        limit = None
        if spec.entry_order and spec.entry_order.type == "limit":
            limit = math.floor(px * (1 - spec.entry_order.offset_pct / 100) * 100) / 100  # whole cents, rounded down
        ref = limit or px
        order = G.EntryOrder(sym, math.floor(size * 100) / 100,  # round down: never over a cap
                              ref, ref * (1 - stop_pct / 100), ref * (1 + spec.target_pct / 100))
        held = set(positions) | set(book.entries) | set(book.pending)
        acct = G.AccountView(eq, cash, held, self.universe, minutes_to_close, book.blocked is not None)
        try:
            G.check_entry(order, acct)
        except G.GuardrailViolation as e:
            st.note = f"guardrail: {e}"
            return
        cid = f"{spec.id[:20]}-{sym}-{now:%m%d%H%M}"
        # The order is on record, and its cash reserved, before it is sent (#60): if the answer is
        # lost (a timeout, a crash), the order is still found by its client id and settled, and
        # the symbol and cash stay taken until then. A market entry expires at once: whatever it
        # hasn't filled by the first look is cancelled.
        qty = math.floor(order.notional / ref * 1e6) / 1e6
        p = Pending(spec.id, "", limit or 0.0, qty, round(qty * limit, 4) if limit is not None else order.notional,
                    now, now + dt.timedelta(minutes=spec.entry_order.expire_min if limit is not None else 0),
                    stop_pct, spec.target_pct, self._entry_params(spec), cid)
        book.buys_today += p.reserved
        book.write_risk(buys_today=round(book.buys_today, 4))
        book.pending[sym] = p
        book.save_pending()
        st.status = "pending"
        try:
            if limit is not None:
                p.order_id = book.broker.buy_limit(sym, qty, limit, now, cid)
            else:
                fill = book.broker.buy_notional(sym, order.notional, px, now, cid)
        except Exception as e:
            self._entry_failed(book, st, sym, p, e, now)
            return
        if limit is not None:
            book.save_pending()
            st.note = f"limit {limit:.2f} resting" + (f" ({st.note})" if binding != "requested" else "")
            return
        p.order_id = fill.order_id  # so a fill it can't price yet is followed up by id (#134)
        self._settle_pending(book, sym, p, OrderState("filled", fill.qty, fill.price), now, place_stop=True)

    def _entry_failed(self, book: Book, st, sym, p: Pending, e: Exception, now) -> None:
        """The entry order raised. Refused outright (a 403 or 404 with no order id), or final with
        nothing filled: nothing is held, so release the cash and the symbol. Anything else may have been accepted: keep it
        reserved, and track it by its order id, or find it by its client id (#60)."""
        p.order_id = getattr(e, "order_id", None) or ""
        if not p.order_id and (isinstance(e, NotFilled) or getattr(e, "status_code", None) in (403, 404)):
            self._settle_pending(book, sym, p, OrderState("rejected"), now, place_stop=True)
            unfilled = isinstance(e, NotFilled)
            st.note = "market order not filled" if unfilled else f"order failed: {e}"
            # Nothing is held and nothing reserved, and the next ask may fail the same way (a
            # persistent broker refusal at a short cadence): once per 10 minutes per book and
            # symbol (#50). Separate keys, so an info "not filled" never hides an urgent refusal.
            self._alert_every(f"entry-{'unfilled' if unfilled else 'refused'}:{book.name}:{sym}",
                              "info" if unfilled else "urgent",
                              f"[{book.name}] entry {sym} for {p.classifier} failed: {e}"
                              " (repeats for this symbol are silenced for 10 minutes)")
            return
        book.save_pending()
        st.note = f"order outcome unknown: {e}"
        # Always sent: cash and the symbol stay reserved on an order that may be live, and it can't
        # repeat for this symbol until that order is settled (the symbol is pending until then).
        self.alert("urgent", f"[{book.name}] entry {sym} for {p.classifier} got no clear answer ({e}); its cash and "
                             "symbol stay reserved until the order is found and settled")
        self._poll_pending(book, now, only=sym)  # a fill found now is protected in this same minute

    def _find_order(self, b: Book, sym, p: Pending, now) -> bool:
        """An entry sent without a clear answer: look its order up by the client id it was sent
        with (#60). True once found. If the broker still has no such order a minute after it was
        sent, it was never placed: release its cash and symbol."""
        oid = b.broker.order_id_for(p.client_id, sym, p.placed)
        if oid:
            p.order_id = oid
            b.save_pending()
            return True
        if now - p.placed >= dt.timedelta(minutes=1):
            self._settle_pending(b, sym, p, OrderState("rejected"), now, place_stop=True)
            # Always sent: it closes the "no clear answer" alert for this order (one each).
            self.alert("urgent", f"[{b.name}] entry {sym} for {p.classifier} was never placed; its cash and symbol are released")
        return False

    @staticmethod
    def _exposures(book: Book, positions: dict) -> list[A.Exposure]:
        """This book's open risk: held entries at their current stop, resting limits at their unfilled
        reservation, and broker positions the engine isn't tracking (no stop: the worst allowed)."""
        out = [A.Exposure(s, e.qty * e.price, e.qty * max(0.0, e.price - e.stop)) for s, e in book.entries.items()]
        # A part-filled limit (#116) is already an Entry: count only the reservation still unfilled. An
        # entry order in flight (#60, market ones too) counts here once. An exited one's remainder can't
        # open a position (#60), so it counts nothing; any late shares it left count as untracked below.
        live = {s: p for s, p in book.pending.items() if not p.exited}
        out += [A.Exposure(s, left, left * p.stop_pct / 100) for s, p in live.items()
                for left in [max(0.0, p.reserved - p.filled_cost)]]
        out += [A.Exposure(s, abs(p.qty) * p.avg_price, abs(p.qty) * p.avg_price * G.MAX_STOP_DISTANCE)
                for s, p in positions.items() if s not in book.entries and s not in live]
        return out

    def _state(self, classifier: str, sym: str):
        cs = next((c for c in self.states if c.spec.id == classifier), None)
        return cs.symbols.get(sym) if cs else None

    def _open_entry(self, book: Book, classifier, sym, fill: Fill, now, stop_pct, target_pct, params, cid,
                    place_stop: bool = True, estimated: bool = False) -> None:
        if not (fill.price > 0 and fill.qty > 0):  # never track a position with no price: no stop would work
            self.alert("urgent", f"[{book.name}] {sym} filled with no usable price/qty ({fill.qty} @ {fill.price}); "
                                 "not tracked, so the next minute closes it as an untracked position")
            return
        stop = fill.price * (1 - stop_pct / 100)
        sp = params.get("scale_pct")
        entry = Entry(classifier, fill.qty, fill.price, stop, fill.price * (1 + target_pct / 100), now,
                      trail_pct=params.get("trail_pct"), max_hold_min=params.get("max_hold_min"),
                      scale_at=fill.price * (1 + sp / 100) if sp else None,
                      scale_fraction=params.get("scale_fraction", 0.0), scale_breakeven=params.get("scale_breakeven", False),
                      price_estimated=estimated)
        book.entries[sym] = entry
        book.save_entries()
        if place_stop:
            entry.stop_id = book.broker.place_stop(sym, fill.qty, stop, cid + "-s")
            entry.server_stop = stop if entry.stop_id else 0.0
            book.save_entries()
            if entry.stop_id is None and book.broker.name != "sim":
                self._alert_every(f"nostop:{book.name}:{sym}", "info",
                                  f"[{book.name}] Alpaca didn't accept a server-side stop for {sym}; the engine enforces it each minute", 3600)
        st = self._state(classifier, sym)
        if st is not None:
            st.status = "holding"
            st.trades += 1
            if st.note.startswith("limit ") and " resting" in st.note:  # filled: keep only the allocator's part
                st.note = st.note.partition(" resting")[2].strip().removeprefix("(").removesuffix(")")
        book.append_trade({
            "time": now.isoformat(timespec="minutes"), "book": book.name, "classifier": classifier, "symbol": sym,
            "side": "buy", "qty": f"{fill.qty:.6f}", "price": f"{fill.price:.4f}",
            "notional": f"{fill.qty * fill.price:.2f}", "reason": "ENTER" + (" (price estimated)" if estimated else ""),
            "pnl": "", "pnl_pct": "",
        })

    def _poll_pending(self, b: Book, now, only: str | None = None) -> None:
        """Resting limit entries: open the position when filled; cancel at expiry (keeping
        any partial fill); release the reserved cash when nothing filled. A partial fill
        cancels the rest of the order, and whatever has filled is a protected position at once,
        even while that cancel is still settling (#59). An entry sent without a clear answer is
        first found by its client id (#60)."""
        for sym, p in list(b.pending.items()):
            if only is not None and sym != only:
                continue
            o = None
            try:
                if not p.order_id and not self._find_order(b, sym, p, now):
                    continue
                o = b.broker.order_state(p.order_id)
                if o.status != "filled" and (o.filled_qty > 0 or now >= p.expires or o.status in TERMINAL):
                    o = b.broker.cancel_order(p.order_id) if o.status not in TERMINAL else o
                elif o.status != "filled":
                    continue  # still resting
            except Exception as ex:
                self._alert_every(f"pending:{b.name}:{sym}", "urgent", f"[{b.name}] entry order {sym} check failed: {ex}")
                if o is not None:
                    self._adopt_fill(b, sym, p, o, now, place_stop=True)  # a partial fill seen before the failure
                continue
            if self._unsettled(o):
                # Not final yet (a cancel still in flight, or a fill not yet reported): keep
                # the order and its reserved cash, and look again next tick. What has already
                # filled is managed as a position meanwhile.
                self._adopt_fill(b, sym, p, o, now, place_stop=True)
                self._alert_every(f"pending:{b.name}:{sym}", "urgent", f"[{b.name}] entry order {sym} not settled yet ({o.status}); retrying")
                continue
            self._settle_pending(b, sym, p, o, now, place_stop=True)

    @staticmethod
    def _unsettled(o) -> bool:
        """The order may still change: not final, or a fill not yet reported. A final fill whose
        price isn't reported (order_state has re-polled briefly) is settled now, and priced by
        _fill_price, as on the direct path (#130): waiting would leave the shares without a stop."""
        return o.status not in TERMINAL or (o.status == "filled" and o.filled_qty <= 0)

    def _fill_price(self, b: Book, sym, p: Pending, o, now, wait: bool = True) -> tuple[float, bool]:
        """(average price of the order's fill so far, whether it is a guess). A limit buy never
        fills above its limit, so an unpriced one is protected at the limit, as a guess. A market
        fill Alpaca hasn't priced after UNPRICED_FILL_WAIT (or at once, when the order is settled:
        `wait` False) takes the position's average cost, which is real; failing that the last
        price, or else the price the order was sized at (#134), as a guess, so the shares get a
        stop and their round trip is recorded, but isn't evidence. 0: wait."""
        if o.price > 0 or p.limit:
            return (o.price if o.price > 0 else p.limit), o.price <= 0
        if wait and now - p.placed < UNPRICED_FILL_WAIT:
            return 0.0, False
        try:
            pos = b.broker.get_positions().get(sym)
        except Exception:
            pos = None
        if pos is not None and pos.avg_price > 0:
            return pos.avg_price, False
        # No last price either (none seen yet): the reference price, reserved/qty, rather than leave
        # the shares stopless and untracked (#134). It is a recent print, so a stop set from it is
        # close to where it should be; and one above the market simply fires: an acceptable exit.
        guess = b.broker.last.get(sym, 0.0) or (p.reserved / p.qty if p.qty > 0 else 0.0)
        return guess, guess > 0

    def _adopt_fill(self, b: Book, sym, p: Pending, o, now, place_stop: bool, wait: bool = True) -> None:
        """Book whatever of this order has filled beyond what is already booked into its
        position, so the stop, target, trail and time stop cover it from now on. Keyed by the
        order's own cumulative fill, never by the symbol's history. Once the position has
        closed, more fills are not a new one (#60)."""
        delta = o.filled_qty - p.filled_qty
        first = p.filled_qty <= 0
        price, guessed = self._fill_price(b, sym, p, o, now, wait) if delta > 1e-9 else (0.0, False)
        if delta > 1e-9 and price > 0:  # a market fill with no price yet waits for it
            cost = o.filled_qty * price - p.filled_cost
            px = cost / delta if cost > 0 else price  # the average price of just the new shares
            # A real limit buy never fills above its limit; a simulated one pays its slippage on top.
            px = min(p.limit * (1 + getattr(b.broker, "limit_slippage", 0.0)), px) if p.limit else px
            p.filled_qty, p.filled_cost = o.filled_qty, p.filled_cost + delta * px
            p.price_estimated = p.price_estimated or guessed
            b.save_pending()  # booked before the position: see below for a crash in between
        else:
            delta = 0.0
        if p.exited:
            if delta:
                self.alert("urgent", f"[{b.name}] {sym}: {delta:g} more filled after its position closed; "
                                     "not a new trade, sold as an untracked position")
            return
        e = b.entries.get(sym)
        if e is None:
            if p.filled_qty <= 1e-9:
                return
            # The first fill; or shares booked just before a crash that never got their entry
            # (the order is known, so they're adopted, not sold as an orphan).
            fill = Fill(sym, "buy", delta, px, now) if first else \
                Fill(sym, "buy", p.filled_qty, p.filled_cost / p.filled_qty, now)
            self._open_entry(b, p.classifier, sym, fill, now, p.stop_pct, p.target_pct,
                             p.params, p.client_id if first else f"{p.client_id}-l{now:%H%M}", place_stop,
                             estimated=p.price_estimated)
            if o.filled_at is not None and sym in b.entries:  # scan the fill's own bar for the stop (conservative)
                self._set_cursor(b, b.entries[sym], pd.Timestamp(o.filled_at).tz_convert(now.tzinfo).floor("min"))
            return
        if not delta:
            return
        # More filled while the cancel was settling: the same position, under the rules it opened with.
        e.price = (e.price * e.qty + px * delta) / (e.qty + delta)
        e.qty += delta
        e.orig_qty += delta
        e.cost += delta * px
        e.price_estimated = e.price_estimated or p.price_estimated
        b.save_entries()
        b.append_trade({
            "time": now.isoformat(timespec="minutes"), "book": b.name, "classifier": e.classifier, "symbol": sym,
            "side": "buy", "qty": f"{delta:.6f}", "price": f"{px:.4f}", "notional": f"{delta * px:.2f}",
            "reason": "ENTER (late fill)", "pnl": "", "pnl_pct": "",
        })
        if place_stop:
            self._resize_server_stop(b, sym, e, now)

    def _resize_server_stop(self, b: Book, sym, e: Entry, now) -> None:
        """The position changed size: re-place the server-side stop for the qty held now. An old
        stop that has (partly) fired has its fill booked first, by its order id (#61), so replacing
        it loses nothing. One whose cancel isn't final is left alone, so two stops are never alive."""
        if e.stop_id:
            try:
                old = b.broker.cancel_order(e.stop_id)
            except Exception:
                old = None
            if old is None or old.status not in TERMINAL:
                how = "is in an unknown state (its cancel failed)" if old is None else "covers only part of it"
                self._alert_every(f"nostop:{b.name}:{sym}", "info",
                                  f"[{b.name}] the server-side stop for {sym} {how}; the engine enforces the rest each minute", 3600)
                return
            if old.filled_qty > 0:
                fill = Fill(sym, "sell", old.filled_qty, old.price if old.price > 0 else e.stop, now,
                            order_id=e.stop_id, estimated=old.price <= 0)
                if _unbooked(e, fill, commit=False)[0] >= e.qty - _tol(e.qty):
                    self._record_exit(b, sym, e, fill, "server stop")
                    return
                b.record_partial(sym, e, fill, "server stop (partial)")
        elif b.broker.name == "sim":
            return  # engine-enforced in simulation
        # Unique even for two re-placements in one minute: a reused client id is refused (#116 review).
        cid = f"{e.classifier[:20]}-{sym}-{now:%m%d%H%M}-g{time.time_ns():x}"
        e.stop_id = b.broker.place_stop(sym, round(e.qty, 9), e.stop, cid)
        e.server_stop = e.stop if e.stop_id else 0.0
        b.save_entries()

    def _settle_pending(self, b: Book, sym, p: Pending, o, now, place_stop: bool) -> None:
        # Settled: nothing more will be reported, so an unpriced fill is priced now (a market entry
        # whose price Alpaca never reported: its average cost, else a labelled guess), not dropped.
        self._adopt_fill(b, sym, p, o, now, place_stop, wait=False)
        if o.filled_qty - p.filled_qty > 1e-9 and not p.exited:
            # Shares filled but still unpriced: keep the order, so they are never sold as an orphan
            # with no trade row, and price them next tick (#134). A flatten marks it exited, then sells.
            self._alert_every(f"unpriced:{b.name}:{sym}", "urgent",
                              f"[{b.name}] {sym} filled {o.filled_qty:g} with no price to book; retrying")
            return
        b.pending.pop(sym, None)
        b.save_pending()
        b.buys_today = max(0.0, b.buys_today + p.filled_cost - p.reserved)
        b.write_risk(buys_today=round(b.buys_today, 4))
        st = self._state(p.classifier, sym)
        if p.filled_qty <= 0 and st is not None and st.status == "pending":
            st.status, st.note = "armed", "limit expired unfilled" if p.limit else "market order not filled"

    def _scale_out(self, b: Book, sym, e: Entry, ref, now) -> bool:
        """Sell scale_fraction of the position. Returns False if the scan should stop here: to
        retry next tick, or because the position has closed."""
        q = math.floor(e.qty * e.scale_fraction * 1e6) / 1e6
        if q * ref < G.MIN_NOTIONAL or (e.qty - q) * ref < G.MIN_NOTIONAL:
            e.scale_at = None  # too small to split: carry on as one position
            b.save_entries()
            return True
        try:
            fill = b.broker.sell_qty(sym, q, ref, now, f"{e.classifier[:20]}-{sym}-{now:%m%d%H%M}-p", e.stop_id)
        except Exception as ex:
            self._alert_every(f"scale:{b.name}:{sym}", "urgent", f"[{b.name}] scale-out {sym} failed: {ex}")
            self._resize_server_stop(b, sym, e, now)  # sell_qty may have cancelled the stop
            return False
        e.scale_at = None
        if fill is not None:  # None: the server stop fired meanwhile, and is booked below
            b.record_partial(sym, e, fill, "scale out")
            if e.scale_breakeven and e.price > e.stop:
                e.stop = e.price
        # The old stop covered the whole qty: book anything it sold, and re-place it for the rest.
        self._resize_server_stop(b, sym, e, now)
        b.save_entries()
        return sym in b.entries

    def _move_server_stop(self, b: Book, sym, e: Entry, now) -> None:
        try:
            new_id = b.broker.move_stop(e.stop_id, sym, e.qty, e.stop, f"{e.classifier[:20]}-{sym}-{now:%m%d%H%M}-t")
        except Exception:
            new_id = e.stop_id
        else:
            if new_id is not None and new_id == e.stop_id:  # it had (partly) fired: book that, cover the rest (#61)
                self._resize_server_stop(b, sym, e, now)
                return
        if new_id is None:
            self._alert_every(f"nostop:{b.name}:{sym}", "info",
                              f"[{b.name}] couldn't re-place the server-side stop for {sym}; the engine enforces it each minute", 3600)
        elif new_id != e.stop_id:
            e.server_stop = e.stop
        e.stop_id = new_id if new_id is not None else None
        b.save_entries()

    def _exit(self, book: Book, sym, e: Entry, ref, now, reason, protect: bool = True) -> None:
        """`protect` False (the flatten): a remainder isn't given a new server stop, which would only
        hold the shares the close-all that follows is about to sell."""
        try:
            fill = book.broker.sell_all(sym, ref, now, f"{e.classifier[:20]}-{sym}-{now:%m%d%H%M}-x", e.stop_id)
        except PartialExit as ex:  # some sold, and the rest may still be held (#61)
            self._sold_part(book, sym, e, self._resold(book, sym, e, ex.fill, now), reason, now, protect)
            if sym in book.entries:
                rest = "protected by a new server stop and retried" if protect else "left to the flatten's close-all"
                self._alert_every(f"exit:{book.name}:{sym}", "urgent",
                                  f"[{book.name}] exit {sym} only partly filled ({ex}); the rest is still held, {rest}")
            else:
                self.alert("info", f"[{book.name}] exit {sym} filled in pieces ({ex}); every leg is recorded")
            return
        except Exception as ex:
            # Keep tracking it: the next tick retries, and the flatten window closes everything.
            self._alert_every(f"exit:{book.name}:{sym}", "urgent", f"[{book.name}] exit {sym} failed, still held: {ex}")
            if protect:  # sell_all may have cancelled the server stop: keep one while the exit retries
                try:
                    self._resize_server_stop(book, sym, e, now)
                except Exception:
                    pass  # the engine's own stop still applies each minute
            return
        fill = self._resold(book, sym, e, fill, now)
        # None: nothing was held any more (closed by something else), so the price is a guess
        self._record_exit(book, sym, e, fill or Fill(sym, "sell", e.qty, ref, now), reason, estimated=fill is None)

    @staticmethod
    def _resold(b: Book, sym, e: Entry, fill: Fill | None, now) -> Fill | None:
        """An exit order booked in part before it was final (a close whose cancel was still settling)
        may have sold more since. Re-read each order already booked, and not in this fill, by its id,
        and add what it sold since as a leg at its real price, so it isn't lost to the round trip."""
        legs = list(fill.legs or [fill]) if fill is not None else []
        seen = {f.order_id for f in legs}
        more = []
        for oid, (booked, _) in list(e.sold.items()):
            if oid in seen:
                continue
            try:
                st = b.broker.order_state(oid)
            except Exception:
                continue  # can't tell now: at worst the round trip ends `(price estimated)` (a shortfall)
            if st.filled_qty > booked + 1e-9 and st.price > 0:
                more.append(Fill(sym, "sell", st.filled_qty, st.price, now, oid))
        if not more:
            return fill
        legs += more
        qty = sum(f.qty for f in legs)
        return Fill(sym, "sell", qty, sum(f.qty * f.price for f in legs) / qty, now,
                    estimated=any(f.estimated for f in legs), legs=legs)

    def _sold_part(self, b: Book, sym, e: Entry, fill: Fill, reason, now, protect: bool = True) -> None:
        """Part of the position sold, and the rest may still be held (#61). Book what sold, once
        per order. If that was all of it, the round trip closes; if not, the rest stays tracked,
        with the server stop re-placed for what's left."""
        if _unbooked(e, fill, commit=False)[0] >= e.qty - _tol(e.qty):
            self._record_exit(b, sym, e, fill, reason)
            return
        b.record_partial(sym, e, fill, f"{reason} (partial)")
        if protect:
            self._resize_server_stop(b, sym, e, now)

    def _record_exit(self, book: Book, sym, e: Entry, fill: Fill, reason, estimated: bool = False) -> None:
        book.record_exit(sym, e, fill, reason, estimated)
        for cs in self.states:
            if cs.spec.id == e.classifier and sym in cs.symbols:
                cs.on_exit(sym)

    def _flatten(self, b: Book, now, prices, reason, final: bool = False) -> None:
        """`final` (the EOD flatten): a position already gone whose exit fill can't be read is
        looked up one last time, then recorded at a guess; before that it's left to the tick (#131)."""
        for sym, p in list(b.pending.items()):  # resting entries first; any fill joins the flatten
            try:
                if not p.order_id and not self._find_order(b, sym, p, now):
                    continue
                o = b.broker.cancel_order(p.order_id)
            except Exception as ex:
                self._alert_every(f"pending:{b.name}:{sym}", "urgent", f"[{b.name}] cancel of entry order {sym} failed: {ex}")
                continue
            if self._unsettled(o):
                self._adopt_fill(b, sym, p, o, now, place_stop=False)  # so the flatten below sells what has filled
                continue  # kept: the blocked/EOD retry path cancels it again next tick
            self._settle_pending(b, sym, p, o, now, place_stop=False)
        if b.pending:  # still settling: whatever it fills from now on is sold, never a new trade (#60)
            for p in b.pending.values():
                p.exited = True
            b.save_pending()
        for sym, e in list(b.entries.items()):
            if sym in b.unresolved:  # already gone at the broker: never sold
                if final:
                    self._vanished(b, sym, e, now, prices.get(sym, e.price), final=True)
                continue
            self._exit(b, sym, e, prices.get(sym, e.price), now, reason, protect=False)
        # Then close everything at the broker, including anything the engine lost track of.
        try:
            left = b.broker.flatten_all(now)
        except Exception as ex:
            left = None
            self._alert_every(f"flatten:{b.name}", "urgent", f"[{b.name}] flatten_all failed: {ex}")
        for sym, e in list(b.entries.items()):
            if left is not None and sym not in left and sym not in b.unresolved:  # closed by flatten_all
                self._record_exit(b, sym, e, Fill(sym, "sell", e.qty, prices.get(sym, e.price), now), reason + " (close-all)",
                                  estimated=True)
        if left:
            why = getattr(b.broker, "last_flatten_error", None)
            self._alert_every(f"holding:{b.name}", "urgent",
                              f"[{b.name}] STILL HOLDING after flatten ({reason}): {sorted(left)}"
                              f"{f' ({why})' if why else ''}. Check Alpaca now.")

    # ---- status for the dashboard ----------------------------------------------

    def _status_equity(self, b: Book) -> float | None:
        """None when the broker can't be read: the heartbeat must still be written."""
        try:
            return round(b.broker.equity(), 2)
        except Exception:
            return None

    def write_status(self, now: dt.datetime, minutes_to_close: float) -> None:
        status = {
            "updated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "market_time": now.isoformat(timespec="minutes"),
            "minutes_to_close": round(minutes_to_close),
            "decision_calls": getattr(self.decider, "calls", 0),
            "decision_cost_usd": round(getattr(self.decider, "total_cost", 0.0), 5),
            "decision_errors": self.decision_errors,
            "probe_errors": self.probe_errors,
            "books": {
                b.name: {
                    "equity": self._status_equity(b),
                    "day_start_equity": round(b.day_start_equity, 2),
                    "nav": round(b.nav.nav_per_unit, 5),
                    "hwm": round(b.nav.hwm, 5),
                    "blocked": b.blocked,
                    "start_unverified": b.start_unverified,
                    "positions": {
                        s: {"classifier": e.classifier, "qty": round(e.qty, 6), "entry": round(e.price, 4),
                            "server_stop": bool(e.stop_id),
                            "stop": round(e.stop, 4), "target": round(e.target, 4),
                            "last": round(b.broker.last.get(s, e.price), 4),
                            "since": e.time.strftime("%H:%M")}
                        for s, e in b.entries.items()
                    },
                    "pending": {s: {"classifier": o.classifier, "limit": o.limit, "expires": o.expires.strftime("%H:%M")}
                                for s, o in b.pending.items()},
                    "realised_today": round(b.realised_today, 2),
                    "trades_today": b.trades_today,
                }
                for b in self.unique_books()
            },
            "classifiers": [
                {
                    "id": cs.spec.id, "mode": cs.spec.mode, "control": cs.spec.control, "family": cs.spec.family_label,
                    "window": list(cs.spec.window), "max_trades": cs.spec.max_trades,
                    "calls": self.calls.get(cs.spec.id, 0), "threshold": cs.spec.entry.threshold,
                    "symbols": {
                        s: {"status": st.status, "trades": st.trades, "last_choice": st.last_choice,
                            "probs": {k: round(v, 3) for k, v in st.last_probs.items()},
                            "last_eval": st.last_eval.strftime("%H:%M") if st.last_eval else None,
                            "note": st.note}
                        for s, st in cs.symbols.items()
                    },
                }
                for cs in self.states
            ],
        }
        self._save_classifier_state()
        tmp = self.run_dir / "status.json.tmp"
        tmp.write_text(json.dumps(status))
        tmp.replace(self.run_dir / "status.json")
