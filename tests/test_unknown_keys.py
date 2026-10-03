"""#150: a misspelt key drops that rule (loudly), never silently changes what it does."""
import pytest
import yaml

from trader import runner
from trader.classifier import load_specs, load_specs_report

FEATS = {"ret_5m_pct", "vwap_dist_pct"}
UNIVERSE = {"SPY", "QQQ"}


def rule(cid, **extra):
    r = {"id": cid, "family": "conventional", "symbols": ["SPY"], "features": ["ret_5m_pct"],
         "entry": {"instructions": "x", "criteria": {"ENTER": "a", "WAIT": "b"}},
         "exit": {"instructions": "x", "criteria": {"HOLD": "a", "EXIT": "b"}}}
    r.update(extra)
    return r


def write(tmp_path, *rules):
    p = tmp_path / "classifiers.yaml"
    p.write_text(yaml.safe_dump({"date": "2026-09-30", "classifiers": list(rules)}))
    return p


def test_a_misspelt_top_level_key_drops_only_that_rule(tmp_path):
    p = write(tmp_path, rule("good"), rule("typo", trail_pc=0.4))
    specs, dropped = load_specs_report(p, FEATS, UNIVERSE)
    assert [s.id for s in specs] == ["good"]
    assert dropped == {"typo": "unknown key trail_pc (did you mean trail_pct?)"}


@pytest.mark.parametrize("extra, where, hint", [
    ({"entry_order": {"type": "limit", "offest_pct": 0.1}}, "entry_order.offest_pct", "offset_pct"),
    ({"trigger": [{"feature": "ret_5m_pct", "op": ">", "val": 0.1}]}, "trigger.0.val", "value"),
    ({"scale_out": {"at_pct": 0.2, "fraction": 0.5, "stop_to_breakeven": True, "fractoin": 1}}, "scale_out.fractoin", "fraction"),
])
def test_a_misspelt_nested_key_drops_the_rule_with_a_hint(tmp_path, extra, where, hint):
    p = write(tmp_path, rule("good"), rule("typo", **extra))
    specs, dropped = load_specs_report(p, FEATS, UNIVERSE)
    assert [s.id for s in specs] == ["good"]
    assert where in dropped["typo"] and f"did you mean {hint}?" in dropped["typo"]


def test_a_misspelt_question_key_is_caught_too(tmp_path):
    bad = rule("typo")
    bad["entry"] = {**bad["entry"], "treshold": 0.7}
    specs, dropped = load_specs_report(write(tmp_path, bad), FEATS, UNIVERSE)
    assert specs == [] and "entry.treshold" in dropped["typo"] and "threshold" in dropped["typo"]


def test_strict_load_for_validate_and_replays_refuses_it(tmp_path):
    p = write(tmp_path, rule("good"), rule("typo", trail_pc=0.4))
    with pytest.raises(ValueError, match="typo: unknown key trail_pc"):
        load_specs(p, FEATS, UNIVERSE)


def test_other_problems_still_refuse_the_whole_file(tmp_path):
    # An unknown key plus a bad value isn't "only a typo": the file fails as before.
    p = write(tmp_path, rule("good"), rule("bad", trail_pc=0.4, stop_pct=50))
    with pytest.raises(Exception):
        load_specs_report(p, FEATS, UNIVERSE)


def test_a_valid_file_is_unchanged(tmp_path):
    p = write(tmp_path, rule("a"), rule("b", trail_pct=0.4, entry_order={"type": "limit", "offset_pct": 0.05}))
    specs, dropped = load_specs_report(p, FEATS, UNIVERSE)
    assert [s.id for s in specs] == ["a", "b"] and dropped == {} and specs[1].trail_pct == 0.4


def test_the_runner_alerts_urgently_and_names_the_benchmark():
    sent = []
    runner._alert_dropped({"typo": "unknown key trail_pc (did you mean trail_pct?)",
                           "control_orb": "unknown key targt_pct (did you mean target_pct?)"},
                          alert=lambda lvl, msg: sent.append((lvl, msg)))
    assert all(lvl == "urgent" for lvl, _ in sent)
    assert "classifier typo not trading today" in sent[0][1] and "did you mean trail_pct?" in sent[0][1]
    assert "control benchmark" in sent[1][1] and "control benchmark" not in sent[0][1]
