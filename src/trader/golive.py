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
from zoneinfo import ZoneInfo

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
ET = ZoneInfo("America/New_York")


def session_date() -> dt.date:
    """Today's date in New York, the session calendar everything here is stamped in. The host's
    `date.today()` (UTC on the server) is already tomorrow on a US evening."""
    return dt.datetime.now(ET).date()


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
        return f"{self.trading_days} days, {self.trades} trades, expectancy {exp}/trade after slippage, worst day {worst}"


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
    if not all(math.isfinite(v) for v in closes) or not all(math.isfinite(v) and v > 0 for vs in days.values() for v in vs):
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


def shadow_record(classifier: str, since: str, book_dir=PAPER_BOOK) -> tuple[int, float | None]:
    """(closed paper trades since `since`, mean pnl % after round-trip slippage). Like the gate,
    nothing before the experiment's start date counts, whenever the current spec started."""
    since = max(since, START_DATE.isoformat())
    closes = []
    tp = book_dir / "trades.csv"
    if tp.exists():
        with tp.open() as f:
            for r in csv.DictReader(f):
                if r["side"] == "sell" and r["classifier"] == classifier and r["pnl_pct"] and r["time"][:10] >= since:
                    closes.append(float(r["pnl_pct"]) - 2 * SLIPPAGE_PER_SIDE_PCT)
    return len(closes), (statistics.fmean(closes) if closes else None)


def enforce_promotion(specs, notify, account_live: bool, today: dt.date | None = None,
                      book_dir=PAPER_BOOK, state_file=None, custom_dir=None) -> list:
    """Issue #20: a classifier may trade real money only after >= MIN_TRADES closed paper trades
    with positive expectancy after slippage, all on its current spec. Editing a spec (anything
    but mode/enabled) restarts its record. Non-qualifying `mode: live` specs are downgraded to
    shadow. The record is tracked in paper mode too, so it's ready when the account goes live."""
    state_file = state_file or PROMOTION_FILE
    today = (today or session_date()).isoformat()
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    digest = custom_features_digest(custom_dir)
    for s in specs:
        if s.probe:
            continue  # probes never trade: no promotion record, and nothing on the scoreboard
        h = spec_hash(s, digest)
        rec = state.get(s.id)
        if not rec or rec["hash"] != h:
            state[s.id] = rec = {"hash": h, "since": today}
        rec["family"] = s.family_label  # kept after the classifier is retired, for the scoreboard
        if s.mode == "live" and not s.control:
            n, exp = shadow_record(s.id, rec["since"], book_dir)
            if n < MIN_TRADES or exp is None or exp <= 0:
                s.mode = "shadow"
                if account_live:
                    notify("urgent", f"{s.id} asked for live but has {n}/{MIN_TRADES} paper trades on its current spec "
                                     f"(expectancy {'n/a' if exp is None else f'{exp:+.3f}%'}): running it in shadow")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(state_file)
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
    notify("urgent", f"golive.json is unreadable or invalid ({st['error']}). Failing closed: PAPER, and nothing arms or "
                     "goes live until you look. HOLD LIVE or `trader release-live` resets it (the bad file is kept "
                     "as golive.json.corrupt-<time>). (Alerted once.)")


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


def save_state(state: dict, expected: dict | None = None) -> bool:
    """Atomic write. With `expected` it's a compare-and-swap: nothing is written (False) if the
    file changed since it was read, so a HOLD pressed meanwhile always beats an automatic update."""
    with _state_lock():
        if expected is not None and load_state() != expected:
            return False
        _write_state(state)
    return True


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
            notices.append(("urgent", f"Go-live gate: {reasons[0]}. Failing closed: nothing arms or goes live "
                                      "until they read cleanly. (Alerted once.)"))
        st["evidence_error"] = True
    else:
        st.pop("evidence_error", None)
    st["last_gate"] = {"date": day.isoformat(), "summary": summary, "blocking": reasons}
    return reasons


def _disarm(st: dict, day: dt.date, reasons: list[str], notices: list) -> None:
    for k in ("sessions_left", "armed_on", "gate"):
        st.pop(k, None)
    st.update(status="pending", disarmed_on=day.isoformat(), disarmed_reasons=reasons)
    notices.append(("urgent", f"Go-live DISARMED: {'; '.join(reasons)}. Staying on paper; if the gate passes again, "
                              f"a fresh {VETO_SESSIONS}-session veto window starts."))


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
            if save_state(st, expected):  # a HOLD pressed meanwhile wins: then nothing to say
                for n in notices:
                    notify(*n)
            return "paper"
        try:
            eq = live_equity() if live_equity else None
        except Exception as e:
            notify("urgent", f"Go-live is due but the live equity lookup failed ({e}). Staying on paper.")
            return "paper"
        if eq is None or eq < MIN_LIVE_EQUITY:
            notify("urgent", f"Go-live is due but the live account has ${eq or 0:.2f} (need ${MIN_LIVE_EQUITY:.0f}). Staying on paper; fund it to proceed.")
            return "paper"
        st.update(status="live", live_since=day.isoformat())
        if not save_state(st, expected):
            return "paper"  # golive.json changed under us (a HOLD, or a release): never go live on a stale read
        notify("urgent", f"GOING LIVE today with ${eq:.2f} (half size for the first 5 live sessions). To stop today: STOP on the dashboard. HOLD LIVE returns to paper from the next session.")
    return "live" if st["status"] == "live" else "paper"


def after_session(notify, live_book_halted: bool = False, session: dt.date | None = None) -> dict:
    """Called at session end: demote on halt, re-check the gate and count down the veto window
    (disarming if it no longer passes), or arm when the gate passes. Idempotent per session date,
    so a restart can't count one session twice. Alerts go out only once the state is saved, and a
    HOLD pressed meanwhile wins (compare-and-swap): the stale update is dropped."""
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
        notices.append(("urgent", "Live book HALTED: back to paper. Going live again needs `trader release-live` after you've cleared the halt."))
    elif st["status"] == "armed":
        reasons = _check_gate(st, session, [])  # a new evidence error is named in the disarm alert instead
        if reasons:
            _disarm(st, session, reasons, notices)
        else:
            st["sessions_left"] = st.get("sessions_left", VETO_SESSIONS) - 1
            if st["sessions_left"] > 0:
                notices.append(("urgent", f"Go-live armed: {st['sessions_left']} paper session(s) left in the veto window. HOLD LIVE on the dashboard to stop it."))
            else:
                notices.append(("urgent", "Go-live armed: the account switches to LIVE at the next session (after a final gate check). HOLD LIVE on the dashboard to stop it."))
    elif st["status"] == "pending":
        if not _check_gate(st, session, notices):
            g = st["last_gate"]["summary"]
            st.update(status="armed", armed_on=session.isoformat(), sessions_left=VETO_SESSIONS, gate=g)
            notices.append(("urgent", f"Go-live gate PASSED ({g}). Live trading starts after {VETO_SESSIONS} more paper sessions unless you press HOLD LIVE."))
    if not save_state(st, expected):
        return load_state()
    for n in notices:
        notify(*n)
    return st


def hold(notify, by: str = "cli") -> str:
    with _state_lock():  # unconditional, under the same lock the automatic updates compare-and-swap in
        st = load_state()
        prev = st["status"]
        aside = None
        if prev == "corrupt":  # replaced by a clean veto, never merged into it (#115)
            aside = _set_aside_corrupt()
            st = {"corrupt_file": aside} if aside else {}
        st.update(status="vetoed", vetoed_on=session_date().isoformat(), vetoed_by=by, vetoed_from=prev)
        _write_state(st)  # atomic: if it fails, a corrupt file is still in place and still reads corrupt
    notify("urgent", f"Go-live HELD by {by} (was {prev}{f', kept as {aside}' if aside else ''}). "
                     "Paper only until `trader release-live`.")
    return f"held (was {prev})"


def release(notify) -> str:
    """Human re-arm after a veto or demotion: restarts the gate evaluation from scratch."""
    st = {"status": "pending", "released_on": session_date().isoformat()}
    with _state_lock():
        if load_state()["status"] == "corrupt":
            _set_aside_corrupt()
        _write_state(st)
    notify("info", "Go-live released: the gate will be re-evaluated after each session.")
    return "released: gate re-evaluated after each session"
