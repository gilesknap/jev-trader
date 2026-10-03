"""Automatic go-live with a human veto window.

The gate is evaluated by the runner from its own logs (runtime/, which the strategist
cannot write), with thresholds fixed here in code on main. When it passes, go-live is
armed; after VETO_SESSIONS more paper sessions the account switches to live, unless the
human vetoes (`trader hold-live` or HOLD LIVE on the dashboard). The gate is re-checked after
each armed session and again just before activation: if it no longer passes (or the logs can't
be read), go-live is disarmed back to pending, and a later re-pass starts a fresh veto window.

config/mode.yaml is a human override: auto (default) | paper | live.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import datetime as dt
import fcntl
import hashlib
import json
import math
import statistics
from dataclasses import dataclass

from trader import config
from trader import guardrails as G

START_DATE = config.SETTINGS.experiment.start_date  # observe phase day 1 (config.yaml)
MIN_TRADING_DAYS = 10
MIN_TRADES = 20
SLIPPAGE_PER_SIDE_PCT = 0.05
VETO_SESSIONS = 3
MIN_LIVE_EQUITY = 100.0

STATE_FILE = config.RUNTIME_DIR / "golive.json"
PAPER_BOOK = config.RUNTIME_DIR / "books" / "paper"
LIVE_BOOK = config.RUNTIME_DIR / "books" / "live"
# Alerts saved in golive.json with the state change they announce, until they've been sent: a
# crash between the save and the send leaves them there for resend_unsent() at the next start.
UNSENT = "unsent_alerts"


def session_date() -> dt.date:
    """Today's date in New York, the session calendar everything here is stamped in (config.ny_today)."""
    return config.ny_today()


@dataclass
class GateResult:
    passed: bool
    trading_days: int
    trades: int
    expectancy_pct: float | None
    worst_day_pct: float | None
    reasons: list[str]

    def summary(self) -> str:
        exp = f"{self.expectancy_pct:+.3f}%" if self.expectancy_pct is not None else "n/a"
        worst = f"{self.worst_day_pct:+.2f}%" if self.worst_day_pct is not None else "n/a"
        return (
            f"{self.trading_days} days, {self.trades} trades, expectancy {exp}/trade after slippage, worst day {worst}"
        )


def evaluate_gate(book_dir=PAPER_BOOK, today: dt.date | None = None) -> GateResult:
    """Gate from the paper book's own trade and equity logs. Control classifiers don't count."""
    today = today or session_date()
    closes = []
    tp = book_dir / "trades.csv"
    if tp.exists():
        with tp.open() as f:
            for r in csv.DictReader(f):
                if r["side"] != "sell" or r["classifier"].startswith("control_") or not r["pnl_pct"]:
                    continue
                if dt.date.fromisoformat(r["time"][:10]) >= START_DATE:
                    closes.append(float(r["pnl_pct"]) - 2 * SLIPPAGE_PER_SIDE_PCT)

    days: dict[dt.date, list[float]] = {}
    ep = book_dir / "equity.csv"
    if ep.exists():
        with ep.open() as f:
            for r in csv.DictReader(f):
                d = dt.date.fromisoformat(r["time"][:10])
                if START_DATE <= d <= today:
                    days.setdefault(d, []).append(float(r["equity"]))
    if not all(math.isfinite(v) for v in closes) or not all(
        math.isfinite(v) and v > 0 for vs in days.values() for v in vs
    ):
        raise ValueError("non-finite or non-positive values in the paper book's logs")  # fail closed (#63)
    # A session counts only once it has marked equity after its opening row (#50): a start that
    # never reached a 5-minute mark wrote just that row, and mustn't make the gate easier.
    days = {d: v for d, v in days.items() if len(v) >= 2}
    worst = min(((min(v) / v[0] - 1) * 100 for v in days.values() if v and v[0] > 0), default=None)

    expectancy = statistics.fmean(closes) if closes else None
    reasons = []
    if len(days) < MIN_TRADING_DAYS:
        reasons.append(f"{len(days)}/{MIN_TRADING_DAYS} trading days")
    if len(closes) < MIN_TRADES:
        reasons.append(f"{len(closes)}/{MIN_TRADES} strategist trades")
    if expectancy is None or expectancy <= 0:
        reasons.append("expectancy after slippage not positive")
    if worst is not None and worst <= -G.DAILY_LOSS_KILL * 100:
        reasons.append(f"a day hit {worst:.2f}%")
    return GateResult(not reasons, len(days), len(closes), expectancy, worst, reasons)


PROMOTION_FILE = config.RUNTIME_DIR / "promotion.json"


def custom_features_digest(custom_dir=None) -> str:
    """Digest of all strategist-authored feature code. Conservative: any edit to any custom
    feature file changes it (which file defines which feature is only known inside the sandbox).
    Never raises, since it runs at session start: a refused file or directory (a symlink, which
    the sandbox never loads either) counts as a fixed marker, never its target's contents (#49)."""
    import hashlib

    from trader.safeio import read_sources

    custom_dir = custom_dir or config.CUSTOM_FEATURES_DIR
    h = hashlib.sha256()
    try:
        sources, refused = read_sources(custom_dir)
    except (OSError, ValueError):
        sources, refused = {}, {"": "directory refused"}
    for name in sorted(sources.keys() | refused.keys()):
        h.update(name.encode() + b"\0" + sources.get(name, b"\0refused") + b"\0")
    return h.hexdigest()[:16]


def spec_hash(spec, custom_digest: str = "") -> str:
    """Identity of a classifier's behaviour: everything except mode/enabled/family, plus the custom
    feature code when the spec uses any non-library feature."""
    import hashlib

    from trader import features as F
    from trader.classifier import EXECUTION_FIELDS

    d = spec.model_dump(exclude={"mode", "enabled", "family"})
    for k in EXECUTION_FIELDS:  # unset optional fields don't change an existing spec's identity
        if d.get(k) is None:
            d.pop(k, None)
    body = json.dumps(d, sort_keys=True, default=str)
    used = set(spec.features) | {t.feature for t in spec.trigger}
    if any(F.SOURCES.get(n) != "lib" for n in used):
        body += "|custom:" + custom_digest
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def shadow_record(classifier: str, since: str, book_dir=PAPER_BOOK, spec_hash: str = "") -> tuple[int, float | None]:
    """(closed paper trades since `since`, mean pnl % after round-trip slippage). Like the gate,
    nothing before the experiment's start date counts, whenever the current spec started. Given
    the current `spec_hash`, a close stamped with another spec's hash doesn't count: its position
    opened under an earlier spec, even if it closed after the edit. An unstamped close (written
    before trade provenance) is judged by its date alone."""
    since = max(since, START_DATE.isoformat())
    closes = []
    tp = book_dir / "trades.csv"
    if tp.exists():
        with tp.open() as f:
            for r in csv.DictReader(f):
                if r["side"] == "sell" and r["classifier"] == classifier and r["pnl_pct"] and r["time"][:10] >= since:
                    stamped = r.get("spec_hash") or ""
                    if spec_hash and stamped and stamped != spec_hash:
                        continue
                    closes.append(float(r["pnl_pct"]) - 2 * SLIPPAGE_PER_SIDE_PCT)
    return len(closes), (statistics.fmean(closes) if closes else None)


def enforce_promotion(
    specs,
    notify,
    account_live: bool,
    today: dt.date | None = None,
    book_dir=PAPER_BOOK,
    state_file=None,
    custom_dir=None,
) -> list:
    """Issue #20: a classifier may trade real money only after >= MIN_TRADES closed paper trades
    with positive expectancy after slippage, all on its current spec. Editing a spec (anything
    but mode/enabled) restarts its record. Non-qualifying `mode: live` specs are downgraded to
    shadow. The record is tracked in paper mode too, so it's ready when the account goes live."""
    state_file = state_file or PROMOTION_FILE
    since = (today or session_date()).isoformat()
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, RecursionError) as e:
        # Fail closed, never abort the session: every record restarts today, so no rule can go live
        # until it re-earns its paper record. The file is set aside for a look.
        aside = state_file.with_name(f"{state_file.name}.corrupt-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}")
        try:
            state_file.replace(aside)
            kept = f"moved to {aside.name}"
        except OSError:
            kept = "left in place"
        notify(
            "urgent",
            f"promotion record {state_file.name} is unreadable ({e!r}; {kept}): every classifier's "
            "record restarts today, so none can go live until it earns a fresh paper record",
        )
        state = {}
    digest = custom_features_digest(custom_dir)
    for s in specs:
        if s.probe:
            continue  # probes never trade: no promotion record, and nothing on the scoreboard
        try:
            h = spec_hash(s, digest)
            rec = state.get(s.id)
            if not isinstance(rec, dict) or rec.get("hash") != h:
                state[s.id] = rec = {"hash": h, "since": since}
            rec["family"] = s.family_label  # kept after the classifier is retired, for the scoreboard
            if s.mode == "live" and not s.control:
                n, exp = shadow_record(s.id, rec["since"], book_dir, rec["hash"])
                if n < MIN_TRADES or exp is None or exp <= 0:
                    s.mode = "shadow"
                    if account_live:
                        notify(
                            "urgent",
                            f"{s.id} asked for live but has {n}/{MIN_TRADES} paper trades on its current spec "
                            f"(expectancy {'n/a' if exp is None else f'{exp:+.3f}%'}): running it in shadow",
                        )
        except Exception as e:  # fail closed for this rule only: never abort the session for all of them
            asked_live = s.mode == "live"
            if asked_live:
                s.mode = "shadow"
            notify(
                "urgent" if asked_live and account_live else "info",
                f"{s.id}: its promotion check failed ({e!r}); "
                + ("running it in shadow" if asked_live else "it can't be promoted until that is fixed"),
            )
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(state_file)
    except OSError as e:  # the modes above already hold for today; the record is simply not advanced
        notify("urgent", f"promotion record {state_file.name} couldn't be saved ({e!r}); today's modes are unaffected")
    return specs


STATUSES = ("pending", "armed", "live", "vetoed", "demoted")
KEEP_CORRUPT = 5  # golive.json.corrupt-<ts> copies kept for diagnosis


def load_state() -> dict:
    """The go-live state. Never raises (#115): an unreadable or invalid golive.json is
    {"status": "corrupt", ...}, which is never live and which no automatic update touches.
    It stays paper until a human HOLDs or releases (or replaces the file)."""
    if not STATE_FILE.exists():
        return {"status": "pending"}
    raw = None
    try:
        raw = STATE_FILE.read_bytes()
        st = json.loads(raw)
        if not isinstance(st, dict) or st.get("status") not in STATUSES:
            raise ValueError("not an object with a known status")
        if type(st.get("sessions_left", 0)) is not int:
            raise ValueError("sessions_left is not an integer")
        un = st.get(UNSENT, [])
        if not isinstance(un, list) or not all(
            isinstance(n, list) and len(n) == 2 and all(isinstance(x, str) for x in n) for n in un
        ):
            raise ValueError(f"{UNSENT} is not a list of [level, message]")
        return st
    except Exception as e:
        sha = hashlib.sha256(raw).hexdigest()[:12] if raw is not None else "unreadable"
        return {"status": "corrupt", "error": f"{type(e).__name__}: {str(e)[:120]}", "sha": sha}


def _alert_corrupt_once(st: dict, notify) -> None:
    """One urgent alert per corrupt golive.json *content* (error + sha; a marker holds the last one
    alerted), not one per session or restart. Never raises: a bad marker just means another alert."""
    marker = STATE_FILE.with_name("golive.corrupt-alerted")
    if st["status"] != "corrupt":
        with contextlib.suppress(Exception):
            marker.unlink(missing_ok=True)
        return
    key = json.dumps(st, sort_keys=True)
    with contextlib.suppress(Exception):
        if marker.read_bytes() == key.encode():
            return
    with contextlib.suppress(Exception):
        marker.write_bytes(key.encode())
    notify(
        "urgent",
        f"golive.json is unreadable or invalid ({st['error']}). Failing closed: PAPER, and nothing arms or "
        "goes live until you look. HOLD LIVE or `trader release-live` resets it (the bad file is kept "
        "as golive.json.corrupt-<time>). (Alerted once.)",
    )


def _set_aside_corrupt() -> str | None:
    """Under the lock, for a human HOLD or release only: copy the corrupt golive.json to
    golive.json.corrupt-<time> for diagnosis, keeping the newest KEEP_CORRUPT. Returns the new
    name, or None if it couldn't be copied. Never raises. It copies rather than moves, so the
    corrupt file stays in place (reading as corrupt, never as a missing file = pending) until the
    caller's atomic write replaces it: a failure at any step leaves it corrupt, never pending."""
    aside = STATE_FILE.with_name(f"{STATE_FILE.name}.corrupt-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}")
    try:
        aside.write_bytes(STATE_FILE.read_bytes())
    except Exception:
        with contextlib.suppress(Exception):
            aside.unlink(missing_ok=True)
        return None
    with contextlib.suppress(OSError):
        for old in sorted(STATE_FILE.parent.glob(f"{STATE_FILE.name}.corrupt-*"))[:-KEEP_CORRUPT]:
            old.unlink()
    return aside.name


@contextlib.contextmanager
def _state_lock():
    """Serialises every read-modify-write of golive.json (runner, CLI and dashboard)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.with_suffix(".lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _write_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def save_state(state: dict, expected: dict | None = None, guard=None) -> bool:
    """Atomic write. With `expected` it's a compare-and-swap: nothing is written (False) if the
    file changed since it was read, so a HOLD pressed meanwhile always beats an automatic update.
    `guard`: checked under the lock just before the write; a reason (str) means no write (False)."""
    with _state_lock():
        if expected is not None and load_state() != expected:
            return False
        if guard is not None and guard():
            return False
        _write_state(state)
    return True


def live_book_unready() -> str | None:
    """Why the live book can't trade, or None: halted, or its risk.json unreadable (fail closed)."""
    try:
        p = LIVE_BOOK / "risk.json"
        if p.exists() and json.loads(p.read_text()).get("halted"):
            return "the live book is halted"
    except Exception as e:
        return f"the live book's risk.json can't be read ({type(e).__name__}: {str(e)[:120]})"
    return None


def override() -> str:
    """Human override from config/mode.yaml: auto | paper | live."""
    import yaml

    if not config.MODE_FILE.exists():
        return "auto"
    mode = (yaml.safe_load(config.MODE_FILE.read_text()) or {}).get("mode", "auto")
    return mode if mode in ("auto", "paper", "live") else "paper"


EVIDENCE_ERROR = "paper book logs unreadable or invalid"


def _check_gate(st: dict, day: dt.date, notices: list) -> list[str]:
    """The gate plus 'paper book not halted', from the runner's own logs; records `last_gate` in
    `st`. Returns the blocking reasons (empty = passes). Never raises: bad evidence fails closed,
    with one alert until the logs read cleanly again."""
    try:
        g = evaluate_gate(PAPER_BOOK, today=day)
        reasons = list(g.reasons) if not g.passed else []
        if not g.passed and not reasons:
            reasons = ["gate failed"]
        risk = PAPER_BOOK / "risk.json"
        if risk.exists() and json.loads(risk.read_text()).get("halted"):
            reasons.append("paper book halted")
        summary = g.summary()
    except Exception as e:
        reasons, summary = [f"{EVIDENCE_ERROR} ({type(e).__name__}: {str(e)[:120]})"], "unavailable"
        if not st.get("evidence_error"):
            notices.append(
                (
                    "urgent",
                    f"Go-live gate: {reasons[0]}. Failing closed: nothing arms or goes live "
                    "until they read cleanly. (Alerted once.)",
                )
            )
        st["evidence_error"] = True
    else:
        st.pop("evidence_error", None)
    st["last_gate"] = {"date": day.isoformat(), "summary": summary, "blocking": reasons}
    return reasons


def _disarm(st: dict, day: dt.date, reasons: list[str], notices: list) -> None:
    for k in ("sessions_left", "armed_on", "gate"):
        st.pop(k, None)
    st.update(status="pending", disarmed_on=day.isoformat(), disarmed_reasons=reasons)
    notices.append(
        (
            "urgent",
            f"Go-live DISARMED: {'; '.join(reasons)}. Staying on paper; if the gate passes again, "
            f"a fresh {VETO_SESSIONS}-session veto window starts.",
        )
    )


def resolve_mode(notify, live_equity=None, session: dt.date | None = None) -> str:
    """Called at session start. Returns 'paper' or 'live', switching armed -> live when due,
    after re-checking the gate. Never raises on bad evidence or a failing equity lookup."""
    ov = override()
    if ov != "auto":
        return ov
    st = load_state()
    _alert_corrupt_once(st, notify)
    expected = copy.deepcopy(st)
    day = session or session_date()
    if st["status"] == "armed" and st.get("sessions_left", 1) <= 0:
        reasons = _check_gate(st, day, [])  # a new evidence error is named in the disarm alert instead
        if reasons:
            notices = []
            _disarm(st, day, reasons, notices)
            _save_then_alert(st, expected, notices, notify)  # a HOLD pressed meanwhile wins: then nothing to say
            return "paper"
        unready = (
            "urgent",
            "Go-live is due but {}. Staying on paper (still armed): clear it with `trader "
            "clear-halt live` after the close, and it goes live at the next session start.",
        )
        why = live_book_unready()
        if why:  # never switch to a live book that can't trade, however the state got here
            notify(unready[0], unready[1].format(why))
            return "paper"
        try:
            eq = live_equity() if live_equity else None
        except Exception as e:
            notify("urgent", f"Go-live is due but the live equity lookup failed ({e}). Staying on paper.")
            return "paper"
        if eq is None or eq < MIN_LIVE_EQUITY:
            notify(
                "urgent",
                f"Go-live is due but the live account has ${eq or 0:.2f} (need ${MIN_LIVE_EQUITY:.0f}). Staying on "
                "paper; fund it to proceed.",
            )
            return "paper"
        st.update(status="live", live_since=day.isoformat())
        going = (
            "urgent",
            f"GOING LIVE today with ${eq:.2f} (half size for the first 5 live sessions). To stop today: "
            "STOP on the dashboard. HOLD LIVE returns to paper from the next session.",
        )
        if not _save_then_alert(st, expected, [going], notify, guard=live_book_unready):
            why = live_book_unready()  # halted since the check above: re-checked under the lock
            if why:
                notify(unready[0], unready[1].format(why))
            return "paper"  # or golive.json changed under us (a HOLD, or a release): never go live on a stale read
    return "live" if st["status"] == "live" else "paper"


def _held_back(live_equity) -> str | None:
    """What would keep the next session start on paper although go-live is due (resolve_mode's
    checks, as they stand now), and what clears it; None if nothing would. Never raises."""
    then = "it goes live at the next session start (after a final gate check)"
    why = live_book_unready()
    if why == "the live book is halted":
        return f"{why}: staying on paper (still armed). Clear it with `trader clear-halt live` and {then}"
    if why:  # its risk.json can't be read
        return f"{why}: staying on paper (still armed). Once it reads cleanly, {then}"
    if live_equity is None:
        return None
    try:
        eq = live_equity()
    except Exception as e:
        return (
            f"the live equity lookup failed ({e}): it is checked again at the next session start, and with "
            f"${MIN_LIVE_EQUITY:.0f} or more in the account {then}"
        )
    if eq is None or eq < MIN_LIVE_EQUITY:
        return (
            f"the live account has ${eq or 0:.2f} (need ${MIN_LIVE_EQUITY:.0f}): staying on paper (still armed). "
            f"Fund it and {then}"
        )
    return None


def after_session(notify, live_book_halted: bool = False, session: dt.date | None = None, live_equity=None) -> dict:
    """Called at session end: demote on halt, re-check the gate and count down the veto window
    (disarming if it no longer passes), or arm when the gate passes. Idempotent per session date,
    so a restart can't count one session twice. Alerts go out only once the state is saved, and a
    HOLD pressed meanwhile wins (compare-and-swap): the stale update is dropped. Once the window
    has run out, `live_equity` (as for resolve_mode) lets the alert say whether the account is
    funded: while something holds go-live back, the alert says what, not that it goes live next."""
    if override() != "auto":
        return load_state()
    st = load_state()
    _alert_corrupt_once(st, notify)
    if st["status"] == "corrupt":
        return st  # no automatic reset to pending: a human should look first (#115)
    expected = copy.deepcopy(st)
    session = session or session_date()
    if st.get("last_session") == session.isoformat():
        return st
    st["last_session"] = session.isoformat()
    notices = []
    if st["status"] == "live" and live_book_halted:
        st.update(status="demoted", demoted_on=session.isoformat())
        notices.append(
            (
                "urgent",
                "Live book HALTED: back to paper. Going live again needs `trader release-live` after you've "
                "cleared the halt.",
            )
        )
    elif st["status"] == "armed":
        reasons = _check_gate(st, session, [])  # a new evidence error is named in the disarm alert instead
        if reasons:
            _disarm(st, session, reasons, notices)
        else:
            # never below 0: a due session held on paper (halted, unfunded) is not one more veto session
            st["sessions_left"] = max(0, st.get("sessions_left", VETO_SESSIONS) - 1)
            held = None if st["sessions_left"] > 0 else _held_back(live_equity)
            if held:
                notices.append(("urgent", f"Go-live is due but {held}. HOLD LIVE on the dashboard to stop it."))
            elif st["sessions_left"] > 0:
                notices.append(
                    (
                        "urgent",
                        f"Go-live armed: {st['sessions_left']} paper session(s) left in the veto window. HOLD LIVE "
                        "on the dashboard to stop it.",
                    )
                )
            else:
                notices.append(
                    (
                        "urgent",
                        "Go-live armed: the account switches to LIVE at the next session (after a final gate "
                        "check). HOLD LIVE on the dashboard to stop it.",
                    )
                )
    elif st["status"] == "pending":
        if not _check_gate(st, session, notices):
            g = st["last_gate"]["summary"]
            st.update(status="armed", armed_on=session.isoformat(), sessions_left=VETO_SESSIONS, gate=g)
            notices.append(
                (
                    "urgent",
                    f"Go-live gate PASSED ({g}). Live trading starts after {VETO_SESSIONS} more paper sessions "
                    "unless you press HOLD LIVE.",
                )
            )
    if not _save_then_alert(st, expected, notices, notify):
        return load_state()
    return st


def _save_then_alert(st: dict, expected: dict, notices: list, notify, guard=None) -> bool:
    """The compare-and-swap save of an automatic update, with its alerts saved in it (UNSENT); then
    they're sent and cleared. A crash after the save can't lose them (resend_unsent), and a dropped
    (stale) update sends nothing. True if saved."""
    if notices:  # after any left by a failed earlier send, so neither is lost
        st[UNSENT] = (st.get(UNSENT) or []) + [list(n) for n in notices]
    if not save_state(st, expected, guard):
        st.pop(UNSENT, None)
        return False
    _send_and_clear(st, notify)
    return True


def _send_and_clear(st: dict, notify, prefix: str = "") -> None:
    """Send `st`'s unsent alerts, then remove them from golive.json if they're still the ones there
    (a HOLD or release meanwhile has replaced them). At least once: a crash between sending and
    clearing re-sends them at the next start."""
    sent = st.pop(UNSENT, None)
    if not sent:
        return
    for level, msg in sent:
        notify(level, prefix + msg)
    with contextlib.suppress(Exception), _state_lock():
        cur = load_state()
        if cur.get(UNSENT) == sent:
            cur.pop(UNSENT)
            _write_state(cur)


def resend_unsent(notify) -> None:
    """At runner start: go-live alerts a crash stopped from going out after their state change was
    saved (see _save_then_alert). Never raises."""
    try:
        st = load_state()
        if st.get(UNSENT):
            _send_and_clear(st, notify, prefix="(delayed by a runner restart) ")
    except Exception:
        pass


def demote_on_halt_cleared(notify) -> bool:
    """Called when a human clears a live halt. A live halt always demotes (after_session), so a
    `live` state here means that session's after_session never ran (a crash after the close). Demote
    now, so going live again still needs `trader release-live`. True if it demoted. Under the lock;
    a non-live state is left alone."""
    with _state_lock():
        st = load_state()
        if st["status"] != "live":
            return False
        st.pop(UNSENT, None)  # superseded (a GOING LIVE never sent): this alert says where things stand
        st.update(status="demoted", demoted_on=session_date().isoformat(), demoted_by="clear-halt")
        _write_state(st)
    notify(
        "urgent",
        "Go-live was still LIVE when the live halt was cleared (the halted session's end was never "
        "processed): demoted to paper now. Going live again needs `trader release-live`.",
    )
    return True


def hold(notify, by: str = "cli") -> str:
    with _state_lock():  # unconditional, under the same lock the automatic updates compare-and-swap in
        st = load_state()
        prev = st["status"]
        aside = None
        if prev == "corrupt":  # replaced by a clean veto, never merged into it (#115)
            aside = _set_aside_corrupt()
            st = {"corrupt_file": aside} if aside else {}
        st.pop(UNSENT, None)  # superseded: this alert says where things stand now
        st.update(status="vetoed", vetoed_on=session_date().isoformat(), vetoed_by=by, vetoed_from=prev)
        _write_state(st)  # atomic: if it fails, a corrupt file is still in place and still reads corrupt
    notify(
        "urgent",
        f"Go-live HELD by {by} (was {prev}{f', kept as {aside}' if aside else ''}). "
        "Paper only until `trader release-live`.",
    )
    return f"held (was {prev})"


def release(notify) -> str:
    """Human re-arm after a veto or demotion: restarts the gate evaluation from scratch. Refused
    (nothing changed) while the live book is halted, or its risk state can't be read: clear the
    halt first, or go-live could arm and switch to a live book that can't trade."""
    st = {"status": "pending", "released_on": session_date().isoformat()}
    with _state_lock():  # checked under the lock, so no transition lands between check and write
        why = live_book_unready()
        if why:
            return (
                f"refused: {why}. Clear it first with `trader clear-halt live` (after the close), then "
                "release again. Nothing changed."
            )
        if load_state()["status"] == "corrupt":
            _set_aside_corrupt()
        _write_state(st)
    notify("info", "Go-live released: the gate will be re-evaluated after each session.")
    return "released: gate re-evaluated after each session"
