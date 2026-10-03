import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from conftest import TEST_START_DATE as START_DATE  # what the autouse fixture pins config.yaml's start date to
from test_engine import run, spec
from trader import cli, config
from trader import features as F
from trader.classifier import load_specs
from trader.jev import Decision
from trader.runner import _exclude_prelaunch_specs


def test_plumbing_rules_are_disabled_on_start_date_even_if_left_configured():
    tests = [spec(id="test_market"), spec(id="test_exit", enabled=False)]
    experiment = [spec(id="idea"), spec(id="control_orb", control=True), spec(id="sim_x", mode="sim")]
    alerts = []
    notify = lambda level, message: alerts.append((level, message))
    assert _exclude_prelaunch_specs(tests + experiment, START_DATE - dt.timedelta(days=1), notify) == tests + experiment
    assert not alerts
    for day in (START_DATE, START_DATE + dt.timedelta(days=30)):
        assert _exclude_prelaunch_specs(tests + experiment, day, notify) == experiment
    assert len(alerts) == 2 and all(lvl == "urgent" and "test_market" in m and "test_exit" in m for lvl, m in alerts)


# The pre-launch pack: a new data repo's strategist branch starts with it as state/classifiers.yaml (#169).
PACK = Path(__file__).resolve().parents[1] / "templates" / "data" / "strategist" / "state" / "classifiers.yaml"


def test_prelaunch_pack_leaves_only_the_control_and_probe_from_start_date():
    specs = load_specs(PACK, set(F.REGISTRY), set(config.universe()))
    plumbing = [s.id for s in specs if s.id.startswith("test_")]
    assert plumbing, "the pack exists to push orders through every execution path before launch"
    notes = []
    kept = _exclude_prelaunch_specs(specs, START_DATE, lambda level, message: notes.append(message))
    assert sorted(s.id for s in kept) == ["control_orb", "probe_universe_baseline"]
    assert len(notes) == 1 and all(i in notes[0] for i in plumbing)


def test_validate_rejects_the_reserved_prefix_from_start_date(tmp_path, monkeypatch, capsys):
    f = tmp_path / "c.yaml"
    f.write_text("classifiers: []\n")
    monkeypatch.setattr(cli, "_specs", lambda *a, **k: [spec(id="test_gap_fill"), spec(id="idea")])
    monkeypatch.setattr(
        "trader.features.harness.run_gate", lambda *a, **k: type("R", (), {"ok": True, "features": [], "errors": {}})()
    )
    monkeypatch.setattr(cli, "_gate_samples", lambda *a, **k: [])

    day = [START_DATE - dt.timedelta(days=1)]
    monkeypatch.setattr(cli.config, "ny_today", lambda: day[0])  # New York's date, not the host's
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--file", str(f)])
    assert e.value.code == 0 and "classifiers OK" in capsys.readouterr().out
    day[0] = START_DATE
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--file", str(f)])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "reserved" in out and "test_gap_fill" in out


class PlumbingDecision:
    calls, total_cost = 0, 0.0

    def decide(self, state, instructions, criteria):
        self.calls += 1
        if "ENTER" in criteria:
            return Decision("ENTER", {"ENTER": 0.55, "WAIT": 0.45})
        exit_now = state["position"]["minutes_held"] >= 6
        return Decision(
            "EXIT" if exit_now else "HOLD", {"EXIT": 0.55 if exit_now else 0.45, "HOLD": 0.45 if exit_now else 0.55}
        )


def test_exit_threshold_of_half_takes_a_classifier_exit_at_jevs_observed_scores(tmp_path, session):
    """Jev scores 'always' criteria at ~45-57%, so a 0.5 exit threshold is needed for test_jev_exit
    to exercise the classifier-EXIT path rather than its time stop."""
    rule = spec(
        id="test_jev_exit",
        max_trades=2,
        after_exit="rearm",
        max_hold_min=30,
        entry={"instructions": "?", "criteria": {"ENTER": "a", "WAIT": "b"}, "threshold": 0.5},
        exit={"instructions": "?", "criteria": {"HOLD": "a", "EXIT": "b"}, "threshold": 0.5},
    )
    _, trades = run(tmp_path, session(path=[100.0] * 390), [rule], PlumbingDecision())
    exits = trades[trades.side == "sell"].reset_index(drop=True)
    buys = trades[trades.side == "buy"].reset_index(drop=True)
    assert len(exits) == 2 and set(exits.reason) == {"classifier EXIT"}
    assert all(
        (pd.Timestamp(s) - pd.Timestamp(b)).total_seconds() < 30 * 60
        for b, s in zip(buys.time, exits.time, strict=False)
    )
