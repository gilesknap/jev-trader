"""Go-live: release refuses while the live book is halted; state-change alerts survive a crash
after the save; the trading API calls on the session-start path have a timeout."""

import json

import pytest

from test_golive import DAYS, arm, env, write_book  # noqa: F401 (env: fixture)
from trader import cli, golive


class Crash(BaseException):
    """The process dying between golive.json's save and the alert."""


def live_risk(tmp_path, data):
    d = tmp_path / "books" / "live"
    d.mkdir(parents=True, exist_ok=True)
    (d / "risk.json").write_text(data if isinstance(data, str) else json.dumps(data))


# ---- release while the live book is halted ---------------------------------------------


def test_release_is_refused_while_the_live_book_is_halted(env, tmp_path):
    golive.save_state({"status": "demoted", "demoted_on": "2026-10-20"})
    live_risk(tmp_path, {"halted": True, "reason": "x"})
    sent = []
    msg = golive.release(lambda *a: sent.append(a))
    assert msg.startswith("refused") and "clear-halt live" in msg
    assert golive.load_state() == {"status": "demoted", "demoted_on": "2026-10-20"} and not sent


def test_release_is_refused_when_the_live_risk_state_is_unreadable(env, tmp_path):
    golive.save_state({"status": "vetoed"})
    live_risk(tmp_path, "{not json")
    assert golive.release(lambda *a: None).startswith("refused")
    assert golive.load_state() == {"status": "vetoed"}


def test_release_goes_ahead_once_the_halt_is_cleared(env, tmp_path):
    golive.save_state({"status": "demoted"})
    live_risk(tmp_path, {"halted": False, "live_sessions": 0})
    assert golive.release(lambda *a: None).startswith("released")
    assert golive.load_state()["status"] == "pending"


def test_the_cli_exits_non_zero_on_a_refused_release(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("trader.alerts.notify", lambda *a, **k: None)
    golive.save_state({"status": "demoted"})
    live_risk(tmp_path, {"halted": True})
    with pytest.raises(SystemExit) as e:
        cli.main(["release-live"])
    assert e.value.code == 1 and "refused" in capsys.readouterr().out


# ---- alerts saved with the state change ---------------------------------------------------


def crashing_notify():
    def notify(level, msg):
        raise Crash

    return notify


def test_an_arming_alert_lost_to_a_crash_is_sent_at_the_next_start(env):
    book, _ = env
    write_book(book)
    with pytest.raises(Crash):
        golive.after_session(crashing_notify(), session=DAYS[20])
    st = golive.load_state()
    assert st["status"] == "armed" and "Go-live gate PASSED" in st[golive.UNSENT][0][1]
    sent = []
    golive.resend_unsent(lambda level, msg: sent.append((level, msg)))
    assert len(sent) == 1 and sent[0][0] == "urgent"
    assert sent[0][1].startswith("(delayed by a runner restart) Go-live gate PASSED")
    assert golive.UNSENT not in golive.load_state()
    golive.resend_unsent(lambda *a: pytest.fail("sent twice"))


def test_a_demotion_alert_lost_to_a_crash_is_sent_at_the_next_start(env):
    golive.save_state({"status": "live", "live_since": "2026-10-20"})
    with pytest.raises(Crash):
        golive.after_session(crashing_notify(), live_book_halted=True, session=DAYS[20])
    sent = []
    golive.resend_unsent(lambda level, msg: sent.append(msg))
    assert golive.load_state()["status"] == "demoted" and len(sent) == 1 and "HALTED" in sent[0]


def test_a_going_live_alert_lost_to_a_crash_is_sent_at_the_next_start(env):
    book, _ = env
    arm(book, None, sessions_done=golive.VETO_SESSIONS)  # due
    golive.save_state(golive.load_state() | {"sessions_left": 0})
    with pytest.raises(Crash):
        golive.resolve_mode(crashing_notify(), lambda: 1000.0, session=DAYS[20])
    assert golive.load_state()["status"] == "live"
    sent = []
    golive.resend_unsent(lambda level, msg: sent.append(msg))
    assert len(sent) == 1 and "GOING LIVE" in sent[0]


def test_alerts_sent_normally_leave_nothing_behind(env):
    book, _ = env
    write_book(book)
    sent = []
    golive.after_session(lambda level, msg: sent.append(msg), session=DAYS[20])
    assert len(sent) == 1 and golive.UNSENT not in golive.load_state()


def test_a_hold_supersedes_unsent_alerts(env):
    book, _ = env
    write_book(book)
    with pytest.raises(Crash):
        golive.after_session(crashing_notify(), session=DAYS[20])
    golive.hold(lambda *a: None, by="test")
    assert golive.UNSENT not in golive.load_state()
    golive.resend_unsent(lambda *a: pytest.fail("a stale ARMED alert after the HOLD"))


def test_resend_never_raises(env):
    golive.STATE_FILE.write_text("{broken")
    golive.resend_unsent(lambda *a: pytest.fail("nothing to send"))
    golive.save_state({"status": "pending", golive.UNSENT: "not a list of alerts"})
    golive.resend_unsent(lambda *a: None)
    assert golive.load_state()["status"] == "corrupt"  # fails closed, like any malformed field


def test_alerts_left_by_a_failed_send_go_out_with_the_next_ones(env):
    golive.save_state({"status": "live", "live_since": "2026-10-20", golive.UNSENT: [["urgent", "earlier"]]})
    sent = []
    golive.after_session(lambda level, msg: sent.append(msg), live_book_halted=True, session=DAYS[20])
    assert sent[0] == "earlier" and "HALTED" in sent[1] and golive.UNSENT not in golive.load_state()


# ---- trading API timeouts on the session-start path -------------------------------------


def test_every_trading_client_request_has_a_timeout(monkeypatch):
    """The calendar, settled cash, reconciliation, positions and equity reads at session start all
    go through an AlpacaBroker's trading client, whose every request carries a timeout."""
    import requests

    from trader.broker import AlpacaBroker

    seen = []

    class Stop(Exception):
        pass

    def request(self, method, url, **kw):
        seen.append(kw.get("timeout"))
        raise Stop

    monkeypatch.setattr(requests.Session, "request", request)
    b = AlpacaBroker("key", "secret", paper=True)
    for call in (
        b.equity,
        b.get_positions,
        b.settled_cash,
        lambda: __import__("trader.runner").runner._session_today(b.client),
    ):
        with pytest.raises(Stop):
            call()
    assert seen == [15] * 4


# ---- never switch to a halted live book -------------------------------------------------


def due(book):
    arm(book, None, sessions_done=golive.VETO_SESSIONS)


def test_go_live_waits_while_the_live_book_is_halted(env, tmp_path):
    book, _ = env
    due(book)
    live_risk(tmp_path, {"halted": True})
    sent = []
    assert (
        golive.resolve_mode(
            lambda level, msg: sent.append(msg), lambda: pytest.fail("no equity lookup"), session=DAYS[20]
        )
        == "paper"
    )
    assert golive.load_state()["status"] == "armed" and len(sent) == 1 and "halted" in sent[0]
    live_risk(tmp_path, {"halted": False})
    assert golive.resolve_mode(lambda *a: None, lambda: 1000.0, session=DAYS[21]) == "live"


def test_a_halt_landing_before_the_switch_is_caught_under_the_lock(env, tmp_path):
    book, _ = env
    due(book)
    sent = []

    def equity():  # the halt arrives after the first check, before the save
        live_risk(tmp_path, {"halted": True})
        return 1000.0

    assert golive.resolve_mode(lambda level, msg: sent.append(msg), equity, session=DAYS[20]) == "paper"
    assert golive.load_state()["status"] == "armed"
    assert len(sent) == 1 and "halted" in sent[0] and not any("GOING LIVE" in m for m in sent)


def test_a_hold_between_send_and_clear_is_never_overwritten(env):
    book, _ = env
    write_book(book)

    def notify(level, msg):  # a HOLD pressed while the arming alert is going out
        golive.hold(lambda *a: None, by="test")

    golive.after_session(notify, session=DAYS[20])
    st = golive.load_state()
    assert st["status"] == "vetoed" and golive.UNSENT not in st


# ---- the close-of-day alert while go-live is due but held back -------------------------


def close(session, equity=None):
    """after_session for `session`; returns (its alerts, the saved state)."""
    sent = []
    golive.after_session(lambda level, msg: sent.append(msg), session=session, live_equity=equity)
    return sent, golive.load_state()


def test_a_halted_live_book_holds_the_countdown_at_zero_and_says_so(env, tmp_path):
    book, _ = env
    due(book)
    live_risk(tmp_path, {"halted": True})
    for day in DAYS[20:22]:  # the morning said "staying on paper": every close agrees
        assert golive.resolve_mode(lambda *a: None, lambda: 1000.0, session=day) == "paper"
        sent, st = close(day, lambda: 1000.0)
        assert st["status"] == "armed" and st["sessions_left"] == 0
        assert len(sent) == 1 and "the live book is halted" in sent[0] and "`trader clear-halt live`" in sent[0]
        assert "switches to LIVE" not in sent[0]
    live_risk(tmp_path, {"halted": False})
    sent, st = close(DAYS[22], lambda: 1000.0)
    assert st["sessions_left"] == 0 and len(sent) == 1 and "switches to LIVE at the next session" in sent[0]
    assert golive.resolve_mode(lambda *a: None, lambda: 1000.0, session=DAYS[23]) == "live"


def test_an_unreadable_live_risk_state_is_named_at_the_close(env, tmp_path):
    book, _ = env
    due(book)
    live_risk(tmp_path, "{not json")
    sent, st = close(DAYS[20], lambda: pytest.fail("no equity lookup"))
    assert st["sessions_left"] == 0 and len(sent) == 1
    assert "risk.json can't be read" in sent[0] and "reads cleanly" in sent[0] and "switches to LIVE" not in sent[0]


def test_an_unfunded_account_is_named_at_the_close(env):
    book, _ = env
    due(book)
    assert golive.resolve_mode(lambda *a: None, lambda: 40.0, session=DAYS[20]) == "paper"
    sent, st = close(DAYS[20], lambda: 40.0)
    assert st["status"] == "armed" and st["sessions_left"] == 0
    assert len(sent) == 1 and "$40.00 (need $100)" in sent[0] and "Fund it" in sent[0]
    assert "switches to LIVE" not in sent[0]
    sent, _ = close(DAYS[21], lambda: None)  # no live key: as unfunded, like resolve_mode
    assert "$0.00 (need $100)" in sent[0]


def test_a_failing_equity_lookup_at_the_close_never_raises(env):
    book, _ = env
    due(book)

    def boom():
        raise ConnectionError("down")

    sent, st = close(DAYS[20], boom)
    assert st["status"] == "armed" and st["sessions_left"] == 0 and st["last_session"] == DAYS[20].isoformat()
    assert len(sent) == 1 and "lookup failed (down)" in sent[0] and "switches to LIVE" not in sent[0]


def test_the_last_veto_session_names_a_halt_instead_of_promising_live(env, tmp_path):
    book, _ = env
    i = arm(book, None, sessions_done=golive.VETO_SESSIONS - 1)
    live_risk(tmp_path, {"halted": True})
    sent, st = close(DAYS[i], lambda: 1000.0)
    assert st["sessions_left"] == 0 and len(sent) == 1
    assert "the live book is halted" in sent[0] and "switches to LIVE" not in sent[0]


def test_mid_window_closes_never_look_up_the_live_account(env, tmp_path):
    book, _ = env
    i = arm(book, None, sessions_done=1)
    live_risk(tmp_path, {"halted": True})  # not due yet: the countdown alert is unchanged
    sent, st = close(DAYS[i], lambda: pytest.fail("no equity lookup"))
    assert st["sessions_left"] == golive.VETO_SESSIONS - 2 and len(sent) == 1 and "session(s) left" in sent[0]
