import csv
import datetime as dt

import pytest

from conftest import TEST_START_DATE
from trader import golive


@pytest.fixture
def env(tmp_path, monkeypatch):
    book = tmp_path / "books" / "paper"
    book.mkdir(parents=True)
    monkeypatch.setattr(golive, "STATE_FILE", tmp_path / "golive.json")
    monkeypatch.setattr(golive, "PAPER_BOOK", book)
    monkeypatch.setattr(golive, "LIVE_BOOK", tmp_path / "books" / "live")
    mode = tmp_path / "mode.yaml"
    mode.write_text("mode: auto\n")
    monkeypatch.setattr(golive.config, "MODE_FILE", mode)
    return book, mode


def write_book(book, days=10, trades_per_day=2, pnl_pct=0.3, classifier="idea", worst=-1.0):
    start = TEST_START_DATE  # the date the autouse fixture pins golive.START_DATE to
    sessions = [start + dt.timedelta(days=i) for i in range(40) if (start + dt.timedelta(days=i)).weekday() < 5][:days]
    with (book / "trades.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f, ["time", "book", "classifier", "symbol", "side", "qty", "price", "notional", "reason", "pnl", "pnl_pct"]
        )
        w.writeheader()
        for d in sessions:
            for _ in range(trades_per_day):
                w.writerow(
                    {
                        "time": f"{d}T11:00-04:00",
                        "book": "paper",
                        "classifier": classifier,
                        "symbol": "SPY",
                        "side": "sell",
                        "qty": "0.1",
                        "price": "600",
                        "notional": "60",
                        "reason": "x",
                        "pnl": "0.1",
                        "pnl_pct": str(pnl_pct),
                    }
                )
                w.writerow(
                    {
                        "time": f"{d}T11:00-04:00",
                        "book": "paper",
                        "classifier": "control_orb",
                        "symbol": "QQQ",
                        "side": "sell",
                        "qty": "0.1",
                        "price": "600",
                        "notional": "60",
                        "reason": "x",
                        "pnl": "5",
                        "pnl_pct": "5.0",
                    }
                )
    with (book / "equity.csv").open("w") as f:
        f.write("time,equity,nav,hwm\n")
        for d in sessions:
            f.write(f"{d}T09:35,250,1,1\n{d}T12:00,{250 * (1 + worst / 100):.2f},1,1\n{d}T15:55,251,1,1\n")


def test_gate_passes_and_excludes_control(env):
    book, _ = env
    write_book(book)
    g = golive.evaluate_gate(book, today=dt.date(2026, 12, 1))
    assert g.passed and g.trades == 20 and g.trading_days == 10
    assert abs(g.expectancy_pct - 0.2) < 1e-9  # 0.3% minus 0.1% round-trip slippage; control trades ignored


@pytest.mark.parametrize(
    "kw,reason",
    [
        (dict(days=9), "trading days"),
        (dict(trades_per_day=1), "strategist trades"),
        (dict(pnl_pct=0.08), "expectancy"),
        (dict(worst=-5.5), "a day hit"),
        (dict(classifier="control_other"), "strategist trades"),
    ],
)
def test_gate_blocks(env, kw, reason):
    book, _ = env
    write_book(book, **kw)
    g = golive.evaluate_gate(book, today=dt.date(2026, 12, 1))
    assert not g.passed and any(reason in r for r in g.reasons)


def test_arm_veto_window_then_live(env, monkeypatch):
    book, _ = env
    write_book(book)
    monkeypatch.setattr(golive, "evaluate_gate", lambda *a, **k: golive.GateResult(True, 10, 20, 0.2, -1, []))
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    days = iter(dt.date(2026, 10, 19) + dt.timedelta(days=i) for i in range(20))
    assert golive.after_session(note, session=next(days))["status"] == "armed"
    for _ in range(golive.VETO_SESSIONS):
        assert golive.resolve_mode(note, lambda: 250.0) == "paper"
        d = next(days)
        golive.after_session(note, session=d)
        golive.after_session(note, session=d)  # a restart re-processing the same session is ignored
    assert golive.resolve_mode(note, lambda: 250.0) == "live"
    assert any("GOING LIVE" in m for m in sent)
    # halt demotes back to paper
    assert golive.after_session(note, live_book_halted=True, session=next(days))["status"] == "demoted"
    assert golive.resolve_mode(note, lambda: 250.0) == "paper"


def test_veto_and_unfunded(env, monkeypatch):
    monkeypatch.setattr(golive, "evaluate_gate", lambda *a, **k: golive.GateResult(True, 10, 20, 0.2, -1, []))
    note = lambda lvl, msg: None
    days = iter(dt.date(2026, 10, 19) + dt.timedelta(days=i) for i in range(20))
    golive.after_session(note, session=next(days))
    for _ in range(golive.VETO_SESSIONS):
        golive.after_session(note, session=next(days))
    assert golive.resolve_mode(note, lambda: 0.0) == "paper"  # unfunded: stays paper, stays armed
    assert golive.load_state()["status"] == "armed"
    golive.hold(note)
    assert golive.resolve_mode(note, lambda: 250.0) == "paper"
    golive.release(note)
    assert golive.load_state()["status"] == "pending"


def test_override(env):
    _, mode = env
    mode.write_text("mode: paper\n")
    assert golive.resolve_mode(lambda *a: None, lambda: 1000.0) == "paper"
    mode.write_text("mode: live\n")
    assert golive.resolve_mode(lambda *a: None, lambda: 1000.0) == "live"


# ---- #63: re-check during the veto window and before activation ----

DAYS = [dt.date(2026, 10, 19) + dt.timedelta(days=i) for i in range(30)]


def arm(book, note, sessions_done=0):
    """A passing book, armed on DAYS[0] with `sessions_done` veto sessions already counted."""
    write_book(book)
    golive.save_state(
        {
            "status": "armed",
            "armed_on": DAYS[0].isoformat(),
            "last_session": DAYS[sessions_done].isoformat(),
            "sessions_left": golive.VETO_SESSIONS - sessions_done,
        }
    )
    return 1 + sessions_done  # index of the next unused session


@pytest.mark.parametrize(
    "breakage,reason",
    [
        (lambda book: write_book(book, pnl_pct=0.05), "expectancy"),  # a losing veto session
        (lambda book: write_book(book, worst=-5.5), "a day hit"),  # a kill-switch day
        (lambda book: (book / "risk.json").write_text('{"halted": true}'), "paper book halted"),
    ],
)
def test_veto_window_disarms_when_gate_fails(env, breakage, reason):
    book, _ = env
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    i = arm(book, note, sessions_done=1)
    breakage(book)
    st = golive.after_session(note, session=DAYS[i])
    assert st["status"] == "pending" and "sessions_left" not in st
    assert any(reason in r for r in st["disarmed_reasons"])
    assert golive.load_state()["status"] == "pending"
    assert "DISARMED" in sent[-1] and reason in sent[-1]
    assert golive.resolve_mode(note, lambda: 250.0, session=DAYS[20]) == "paper"


def test_activation_blocked_when_final_recheck_fails(env):
    book, _ = env
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    arm(book, note, sessions_done=golive.VETO_SESSIONS)
    assert golive.load_state()["sessions_left"] == 0
    write_book(book, worst=-6.0)  # e.g. a manual edit or late marks: the pre-activation check sees it
    assert golive.resolve_mode(note, lambda: 250.0, session=DAYS[10]) == "paper"
    assert golive.load_state()["status"] == "pending"
    assert not any("GOING LIVE" in m for m in sent) and "DISARMED" in sent[-1]


def test_repass_starts_a_fresh_full_window(env):
    book, _ = env
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    i = arm(book, note, sessions_done=2)  # one session from the end of the window
    write_book(book, pnl_pct=0.05)
    assert golive.after_session(note, session=DAYS[i])["status"] == "pending"
    write_book(book)  # eligible again
    st = golive.after_session(note, session=DAYS[i + 1])
    assert st["status"] == "armed" and st["sessions_left"] == golive.VETO_SESSIONS
    for k in range(golive.VETO_SESSIONS):
        assert golive.resolve_mode(note, lambda: 250.0, session=DAYS[20]) == "paper"
        golive.after_session(note, session=DAYS[i + 2 + k])
    assert (
        golive.resolve_mode(note, lambda: 250.0, session=DAYS[20]) == "live"
    )  # a continuously eligible window still completes


def test_bad_evidence_fails_closed_and_alerts_once(env):
    book, _ = env
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    i = arm(book, note, sessions_done=1)
    with (book / "trades.csv").open("a") as f:  # a corrupt row: the gate can't be trusted
        f.write("2026-10-06T11:00-04:00,paper,idea,SPY,sell,0.1,600,60,x,0.1,garbage\n")
    st = golive.after_session(note, session=DAYS[i])  # must not raise
    [reason] = st["disarmed_reasons"]
    assert st["status"] == "pending" and reason.startswith(golive.EVIDENCE_ERROR) and "ValueError" in reason
    assert st["last_gate"]["blocking"] == [reason] and len(reason) < 200
    assert "DISARMED" in sent[-1] and "ValueError" in sent[-1]  # the human sees what broke, in one alert
    n = len(sent)
    for k in range(3):  # still broken: no arming, no crash, no alert every session
        assert golive.after_session(note, session=DAYS[i + 1 + k])["status"] == "pending"
    assert len(sent) == n
    write_book(book)  # readable again: the flag clears and the gate can arm again
    st = golive.after_session(note, session=DAYS[i + 5])
    assert st["status"] == "armed" and "evidence_error" not in st


def test_bad_evidence_while_pending_alerts_once(env):
    book, _ = env
    write_book(book)
    with (book / "equity.csv").open("a") as f:
        f.write("2026-10-06T13:00,nan,1,1\n")
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    for k in range(3):
        assert golive.after_session(note, session=DAYS[k])["status"] == "pending"
    assert len(sent) == 1 and golive.EVIDENCE_ERROR in sent[0] and "ValueError" in sent[0]


def test_golive_status_cli_survives_bad_logs(env, capsys):
    import json

    from trader import cli

    book, _ = env
    golive.save_state({"status": "armed", "sessions_left": 2})
    write_book(book)
    with (book / "trades.csv").open("a") as f:
        f.write("2026-10-06T11:00-04:00,paper,idea,SPY,sell,0.1,600,60,x,0.1,nan\n")
    cli.cmd_golive_status(None)  # must not traceback
    out = json.loads(capsys.readouterr().out)
    assert out["state"]["status"] == "armed" and "ValueError" in out["gate_now"]["error"]


def test_bad_evidence_before_activation_stays_paper(env, monkeypatch):
    book, _ = env
    note = lambda lvl, msg: None
    arm(book, note, sessions_done=golive.VETO_SESSIONS)
    monkeypatch.setattr(golive, "evaluate_gate", lambda *a, **k: 1 / 0)  # a gate evaluation that raises
    assert golive.resolve_mode(note, lambda: 250.0, session=DAYS[20]) == "paper"
    assert golive.load_state()["status"] == "pending"


def test_live_equity_error_stays_paper_without_raising(env):
    book, _ = env
    note = lambda lvl, msg: None
    arm(book, note, sessions_done=golive.VETO_SESSIONS)

    def boom():
        raise ConnectionError("alpaca down")

    assert golive.resolve_mode(note, boom, session=DAYS[20]) == "paper"
    assert golive.load_state()["status"] == "armed"


def _hold_mid_evaluation(monkeypatch, passed):
    """A human presses HOLD LIVE while the runner is evaluating the gate."""

    def gate(*a, **k):
        golive.hold(lambda *x: None, by="human")
        return golive.GateResult(
            passed, 10, 20, 0.2 if passed else -0.1, -1, [] if passed else ["expectancy after slippage not positive"]
        )

    monkeypatch.setattr(golive, "evaluate_gate", gate)


def test_hold_during_final_recheck_beats_activation(env, monkeypatch):
    book, _ = env
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    arm(book, note, sessions_done=golive.VETO_SESSIONS)
    _hold_mid_evaluation(monkeypatch, passed=True)
    assert golive.resolve_mode(note, lambda: 250.0, session=DAYS[20]) == "paper"
    assert golive.load_state()["status"] == "vetoed"
    assert not any("GOING LIVE" in m for m in sent)


@pytest.mark.parametrize("sessions_done,passed", [(1, True), (1, False), (None, True)])
def test_hold_during_after_session_is_never_overwritten(env, monkeypatch, sessions_done, passed):
    book, _ = env
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    i = 0 if sessions_done is None else arm(book, note, sessions_done=sessions_done)  # None: HOLD while pending
    if sessions_done is None:
        write_book(book)
    n = len(sent)
    _hold_mid_evaluation(monkeypatch, passed)
    st = golive.after_session(note, session=DAYS[i])
    assert st["status"] == "vetoed" and golive.load_state()["status"] == "vetoed"
    assert len(sent) == n  # no stale ARMED / DISARMED / countdown alert after the HOLD
    golive.release(note)  # `trader release-live` semantics are unchanged: back to pending from scratch
    assert golive.load_state() == {"status": "pending", "released_on": golive.session_date().isoformat()}


# ---- #115: a corrupt golive.json fails closed to paper and never raises ----

CORRUPT = [
    "{not json",
    "",
    "[1, 2]",
    '{"status": "LIVE"}',
    '{"sessions_left": 1}',
    '{"status": "armed", "sessions_left": "0"}',
]


@pytest.mark.parametrize("text", CORRUPT)
def test_corrupt_state_is_paper_with_one_alert(env, text):
    book, _ = env
    write_book(book)  # a passing gate: still nothing arms over a corrupt file
    golive.STATE_FILE.write_text(text)
    sent = []
    note = lambda lvl, msg: sent.append((lvl, msg))
    for k in range(3):  # several sessions (and restarts): paper, no raise, no automatic write
        assert golive.resolve_mode(note, lambda: 1000.0, session=DAYS[k]) == "paper"
        assert golive.resolve_mode(note, lambda: 1000.0, session=DAYS[k]) == "paper"
        assert golive.after_session(note, session=DAYS[k])["status"] == "corrupt"
    assert golive.STATE_FILE.read_text() == text
    assert len(sent) == 1 and sent[0][0] == "urgent" and "golive.json" in sent[0][1]
    assert golive.config.account_mode() == "paper"


def test_corrupt_state_realerts_after_recovery(env):
    sent = []
    note = lambda lvl, msg: sent.append(msg)
    golive.STATE_FILE.write_text("{")
    golive.resolve_mode(note, session=DAYS[0])
    golive.save_state({"status": "pending"})  # fixed by hand
    golive.resolve_mode(note, session=DAYS[1])
    golive.STATE_FILE.write_text("{")  # a new episode
    golive.resolve_mode(note, session=DAYS[2])
    assert len(sent) == 2


def test_unreadable_state_is_paper(env):
    golive.STATE_FILE.mkdir()  # read fails with an OSError, not a parse error
    assert golive.resolve_mode(lambda *a: None, session=DAYS[0]) == "paper"
    assert golive.load_state()["status"] == "corrupt"


def test_hold_on_corrupt_state_vetoes_and_keeps_the_file(env):
    sent = []
    for i in range(golive.KEEP_CORRUPT + 2):
        golive.STATE_FILE.write_text(f"{{corrupt {i}")
        assert golive.hold(lambda lvl, msg: sent.append(msg), by="human") == "held (was corrupt)"
        st = golive.load_state()
        assert (
            st["status"] == "vetoed"
            and st["vetoed_from"] == "corrupt"
            and set(st) == {"status", "vetoed_on", "vetoed_by", "vetoed_from", "corrupt_file"}
        )
        assert (golive.STATE_FILE.parent / st["corrupt_file"]).read_text() == f"{{corrupt {i}"
    kept = sorted(golive.STATE_FILE.parent.glob("golive.json.corrupt-*"))
    assert len(kept) == golive.KEEP_CORRUPT and kept[-1].read_text() == f"{{corrupt {golive.KEEP_CORRUPT + 1}"
    assert golive.resolve_mode(lambda *a: None, session=DAYS[0]) == "paper"


def test_release_on_corrupt_state_gives_clean_pending(env):
    golive.STATE_FILE.write_text('{"status": "armed", "sessions_left": null}')
    golive.release(lambda *a: None)
    assert golive.load_state() == {"status": "pending", "released_on": golive.session_date().isoformat()}
    [kept] = golive.STATE_FILE.parent.glob("golive.json.corrupt-*")
    assert kept.read_text() == '{"status": "armed", "sessions_left": null}'


def test_golive_cli_and_dashboard_survive_corrupt_state(env, capsys):
    import json

    from trader import cli, dashboard

    golive.STATE_FILE.write_text("{not json")
    cli.cmd_golive_status(None)
    out = json.loads(capsys.readouterr().out)
    assert (
        out["effective"] == "paper"
        and out["state"]["status"] == "corrupt"
        and "JSONDecodeError" in out["state"]["error"]
    )
    assert dashboard._golive_summary().startswith("CORRUPT golive.json")


def test_dashboard_hold_live_on_corrupt_state(env, monkeypatch):
    from fastapi.testclient import TestClient

    from trader import alerts, dashboard

    monkeypatch.setattr(alerts, "notify", lambda *a, **k: None)
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    golive.STATE_FILE.write_text("{not json")
    r = TestClient(dashboard.app).post(
        "/api/hold-live", json={"confirm": "HOLD"}, headers={"Tailscale-User-Login": "me@example.com"}
    )
    assert r.status_code == 200 and golive.load_state()["status"] == "vetoed"


HUMAN_ACTIONS = [lambda: golive.hold(lambda *a: None, by="human"), lambda: golive.release(lambda *a: None)]


@pytest.mark.parametrize("act", HUMAN_ACTIONS, ids=["hold", "release"])
def test_failed_write_leaves_corrupt_file_in_place(env, monkeypatch, act):
    import errno
    import pathlib

    real = pathlib.Path.write_text

    def enospc(self, *a, **k):
        if self.suffix == ".tmp":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *a, **k)

    golive.STATE_FILE.write_text("{corrupt")
    monkeypatch.setattr(pathlib.Path, "write_text", enospc)
    with pytest.raises(OSError):
        act()
    assert golive.STATE_FILE.read_text() == "{corrupt" and golive.load_state()["status"] == "corrupt"  # never pending
    assert golive.resolve_mode(lambda *a: None, session=DAYS[0]) == "paper"


@pytest.mark.parametrize(
    "act,status", list(zip(HUMAN_ACTIONS, ["vetoed", "pending"], strict=True)), ids=["hold", "release"]
)
def test_failed_set_aside_still_writes_a_clean_state(env, monkeypatch, act, status):
    import pathlib

    def refuse(self, *a, **k):
        raise OSError("copy refused")

    golive.STATE_FILE.write_text("{corrupt")
    monkeypatch.setattr(pathlib.Path, "write_bytes", refuse)
    act()
    st = golive.load_state()
    assert st["status"] == status and "corrupt_file" not in st
    assert not list(golive.STATE_FILE.parent.glob("golive.json.corrupt-*"))


@pytest.mark.parametrize("act", HUMAN_ACTIONS, ids=["hold", "release"])
def test_state_path_is_a_directory_stays_corrupt(env, act):
    golive.STATE_FILE.mkdir()  # can't be copied aside or replaced: fail closed, never pending
    with pytest.raises(OSError):
        act()
    assert golive.load_state()["status"] == "corrupt"
    assert golive.resolve_mode(lambda *a: None, session=DAYS[0]) == "paper"


def test_undecodable_alert_marker_never_raises(env):
    sent = []
    golive.STATE_FILE.write_text("{corrupt")
    golive.STATE_FILE.with_name("golive.corrupt-alerted").write_bytes(b"\xff\xfe")
    for k in range(3):
        assert golive.resolve_mode(lambda lvl, msg: sent.append(msg), session=DAYS[k]) == "paper"
        golive.after_session(lambda lvl, msg: sent.append(msg), session=DAYS[k])
    assert len(sent) == 1


class _LateEvening(dt.datetime):
    """21:30 in New York on 2026-11-03: already 2026-11-04 in UTC, where the server's clock is."""

    @classmethod
    def now(cls, tz=None):
        t = dt.datetime(2026, 11, 4, 2, 30, tzinfo=dt.UTC)
        return t.astimezone(tz) if tz else t.replace(tzinfo=None)


class _UTCDate(dt.date):
    @classmethod
    def today(cls):
        return dt.date(2026, 11, 4)


def test_state_is_stamped_with_the_new_york_session_date_not_the_servers(env, monkeypatch):
    import types

    for module in (golive, golive.config):  # session_date is config.ny_today
        monkeypatch.setattr(
            module,
            "dt",
            types.SimpleNamespace(datetime=_LateEvening, date=_UTCDate, timedelta=dt.timedelta, UTC=dt.UTC),
        )
    note = lambda *a: None
    day = dt.date(2026, 11, 3)
    assert golive.session_date() == day
    golive.hold(note, by="test")
    assert golive.load_state()["vetoed_on"] == day.isoformat()
    golive.release(note)
    assert golive.load_state()["released_on"] == day.isoformat()
    golive.save_state({"status": "live", "live_since": "2026-10-20"})
    golive.after_session(note, live_book_halted=True)  # no session given: today's, in New York
    st = golive.load_state()
    assert st["status"] == "demoted" and st["demoted_on"] == st["last_session"] == day.isoformat()
