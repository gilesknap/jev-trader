"""A live-halt demotion lost to a crash between the close and after_session is recovered."""

import datetime as dt
import json

import pytest

from test_golive import env  # noqa: F401 (fixture)
from trader import config, golive, runner
from trader.data import ET

DAY = dt.date(2026, 9, 29)  # a session long over


class Quiet:
    client = None

    def get_positions(self):
        return {}


@pytest.fixture
def rt(env, tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)  # no status.json: the runner isn't running
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    monkeypatch.setattr(runner, "notify", lambda level, msg, **k: alerts.append(msg))
    monkeypatch.setattr(config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"})
    monkeypatch.setattr(runner, "AlpacaBroker", lambda *a, **k: Quiet())
    monkeypatch.setattr(
        runner,
        "_session_for_run",
        lambda client: (dt.datetime.combine(DAY, dt.time(9, 30), ET), dt.datetime.combine(DAY, dt.time(16), ET)),
    )
    live = tmp_path / "books" / "live"
    live.mkdir(parents=True)
    return live, alerts


def halt(live, halted=True):
    (live / "risk.json").write_text(json.dumps({"halted": halted, "reason": "x", "live_sessions": 3}))


def test_a_restart_after_the_close_records_the_lost_demotion(rt):
    live, alerts = rt
    golive.save_state({"status": "live", "live_since": "2026-09-14", "last_session": "2026-09-28"})
    halt(live)
    assert runner.run_session("stub") == 0
    st = golive.load_state()
    assert st["status"] == "demoted" and st["demoted_on"] == st["last_session"] == DAY.isoformat()
    assert any("HALTED" in a for a in alerts)
    alerts.clear()
    assert runner.run_session("stub") == 0  # another restart: nothing more to do
    assert golive.load_state() == st and not any("HALTED" in a for a in alerts)


@pytest.mark.parametrize(
    "state,halted",
    [
        ({"status": "live", "live_since": "2026-09-14", "last_session": "2026-09-28"}, False),  # not halted
        (
            {"status": "armed", "sessions_left": 2, "armed_on": "2026-09-25"},
            True,
        ),  # a crashed session never counts down
        ({"status": "pending"}, True),  # nor arms
    ],
)
def test_a_restart_after_the_close_changes_nothing_else(rt, state, halted):
    live, _ = rt
    golive.save_state(state)
    halt(live, halted)
    runner.run_session("stub")
    assert golive.load_state() == state


def test_the_post_close_check_never_stops_the_runner(rt):
    live, alerts = rt
    golive.save_state({"status": "live", "live_since": "2026-09-14"})
    (live / "risk.json").write_text("{not json")
    assert runner.run_session("stub") == 0
    assert golive.load_state()["status"] == "live" and any("hold-live" in a for a in alerts)


def test_clearing_a_live_halt_while_still_live_demotes(rt):
    live, alerts = rt
    golive.save_state({"status": "live", "live_since": "2026-09-14"})
    halt(live)
    assert "rebased" in runner.clear_halt("live")
    st = golive.load_state()
    assert st["status"] == "demoted" and st["demoted_by"] == "clear-halt"
    assert not json.loads((live / "risk.json").read_text())["halted"]
    assert any("release-live" in a for a in alerts)
    assert golive.resolve_mode(lambda *a: None, lambda: 1000.0, session=DAY + dt.timedelta(days=1)) == "paper"


def test_clearing_a_halt_leaves_any_other_state_alone(rt, tmp_path):
    live, _ = rt
    for state in ({"status": "demoted", "demoted_on": "2026-09-28"}, {"status": "vetoed", "vetoed_from": "live"}):
        golive.save_state(state)
        halt(live)
        runner.clear_halt("live")
        assert golive.load_state() == state
    golive.save_state({"status": "live", "live_since": "2026-09-14"})
    (tmp_path / "books" / "paper").mkdir(parents=True, exist_ok=True)
    (tmp_path / "books" / "paper" / "risk.json").write_text(json.dumps({"halted": True}))
    runner.clear_halt("paper")
    assert golive.load_state()["status"] == "live"


def test_a_failed_demotion_leaves_the_halt_in_place(rt, monkeypatch):
    live, _ = rt
    golive.save_state({"status": "live", "live_since": "2026-09-14"})
    halt(live)
    monkeypatch.setattr(golive, "_write_state", lambda st: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        runner.clear_halt("live")
    assert json.loads((live / "risk.json").read_text())["halted"]
