"""Why Jev was or wasn't called: per rule x stock outcome counts in the status snapshot, kept
across a restart, and the dashboard's one-line summary of them."""

import datetime as dt
import json

from trader.dashboard import _with_why, why_summary
from trader.data import ET

from test_engine import Always, run, spec
from test_restart_state import make, ticks


def _sym(tmp_path, cid="t", sym="SPY"):
    status = json.loads((tmp_path / "status.json").read_text())
    return next(c for c in status["classifiers"] if c["id"] == cid)["symbols"][sym]


def test_a_trigger_that_never_passes_is_counted_with_its_last_values(tmp_path, session):
    bars = session(path=[100.0] * 390)
    never = spec(trigger=[{"feature": "ret_1m_pct", "op": ">", "value": 50}])
    run(tmp_path, bars, [never], Always())
    x = _sym(tmp_path)
    assert x["counts"]["checks"] == x["counts"]["no_trigger"] > 0
    assert not any(k.startswith("asked_") for k in x["counts"])
    feature, op, need, value, held = x["last_trigger"]["conditions"][0]
    assert (feature, op, need, held) == ("ret_1m_pct", ">", 50, False) and value == 0
    assert why_summary(x).startswith(f"Checked {x['counts']['checks']} times; the trigger never passed.")
    assert "ret_1m_pct = 0 (needs > 50)" in why_summary(x)


def test_asking_jev_is_counted(tmp_path, session):
    bars = session(path=[100.0] * 390)
    run(tmp_path, bars, [spec()], Always())
    x = _sym(tmp_path)
    assert x["counts"]["asked_entry"] == 1 and x["counts"].get("asked_exit", 0) > 0
    assert "no_trigger" not in x["counts"] and x["last_trigger"] is None


def test_a_paused_decision_model_is_a_skip_not_a_check(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book, eng = make(tmp_path, [spec()])
    eng.start_day(day, {})
    eng.decisions_paused_until = dt.datetime.combine(day, dt.time(15), ET)
    ticks(eng, bars, 10, 20)
    x = _sym(tmp_path)
    assert x["counts"] == {"skip_paused": 10} and not book.entries
    assert why_summary(x) == "Not checked yet today. Skipped: Jev calls paused after errors (10)."


def test_counts_survive_a_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    never = spec(trigger=[{"feature": "ret_1m_pct", "op": ">", "value": 50}])
    _, eng = make(tmp_path, [never])
    eng.start_day(day, {})
    ticks(eng, bars, 10, 20)
    before = _sym(tmp_path)
    _, eng2 = make(tmp_path, [never])
    eng2.start_day(day, {})
    ticks(eng2, bars, 20, 25)
    after = _sym(tmp_path)
    assert after["counts"]["checks"] == before["counts"]["checks"] + 5
    assert after["last_trigger"]["at"] > before["last_trigger"]["at"]


def test_a_damaged_tally_is_dropped_on_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "classifier_state.json").write_text(json.dumps({"day": day.isoformat(), "states": {"t": {"SPY": {
        "status": "armed", "trades": 0, "counts": {"checks": "lots", "no_trigger": 3}, "last_trigger": "x"}}}}))
    _, eng = make(tmp_path, [spec()])
    eng.start_day(day, {})
    st = eng.states[0].symbols["SPY"]
    assert st.counts == {"no_trigger": 3} and st.last_trigger is None


def test_a_new_day_starts_from_zero(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    _, eng = make(tmp_path, [spec()])
    eng.start_day(day, {})
    ticks(eng, bars, 10, 20)
    _, eng2 = make(tmp_path, [spec()])
    eng2.start_day(day + dt.timedelta(days=1), {})
    assert eng2.states[0].symbols["SPY"].counts == {}


def test_summary_lines():
    assert why_summary({}) == "Not checked yet today."
    assert why_summary({"counts": {"checks": 5, "no_trigger": 3, "asked_entry": 2}}) == (
        "Checked 5 times; asked Jev twice. The trigger didn't pass on 3 of the checks.")
    assert why_summary({"counts": {"checks": 4, "skip_symbol_busy": 4, "skip_outside_window": 9}}) == (
        "Checked 4 times; Jev not asked. Skipped: the account already held or was buying the stock (4).")
    assert why_summary({"counts": {"checks": 1, "asked_entry": 1, "jev_error": 1}}) == (
        "Checked once; asked Jev once. Jev failed to answer once.")
    x = {"counts": {"checks": 2, "no_trigger": 2}, "last_trigger": {"at": "10:42", "conditions": [
        ["or15_break_pct", ">", 0, -0.12, False], ["above_vwap", ">", 0, 1.0, True], ["gap_pct", "<", 2, None, False]]}}
    assert why_summary(x) == ("Checked twice; the trigger never passed. Last miss at 10:42: "
                              "or15_break_pct = -0.12 (needs > 0); gap_pct = unavailable (needs < 2).")


def test_malformed_status_never_breaks_the_page():
    status = {"classifiers": [
        {"id": "a", "mode": "shadow", "symbols": {"SPY": {"counts": {"checks": 1, "no_trigger": 1},
                                                          "last_trigger": {"conditions": [["f", ">", "x", 1, False]]}}}},
        {"id": "p", "mode": "probe", "symbols": {"SPY": {}}}, "junk"]}
    out = _with_why(status)
    assert out["classifiers"][0]["symbols"]["SPY"]["why"] == ""
    assert "why" not in out["classifiers"][1]["symbols"]["SPY"]
    assert _with_why(None) is None and _with_why([1]) == [1]


def test_a_non_dict_saved_entry_is_ignored_on_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "classifier_state.json").write_text(json.dumps({"day": day.isoformat(), "states": {"t": {"SPY": 5}}}))
    _, eng = make(tmp_path, [spec()])
    eng.start_day(day, {})
    assert eng.states[0].symbols["SPY"].counts == {}


def test_a_probe_skipped_while_decisions_are_paused_says_paused(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    _, eng = make(tmp_path, [spec(id="p", mode="probe")])
    eng.start_day(day, {})
    eng.decisions_paused_until = dt.datetime.combine(day, dt.time(15), ET)
    ticks(eng, bars, 10, 13)
    assert _sym(tmp_path, "p")["counts"] == {"skip_paused": 3}
