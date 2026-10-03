"""#143: a rule's per-symbol status and note (status.json, the dashboard) match what happened:
after a fill that lands once a flatten has closed its order, across a restart, and on any fill."""

import datetime as dt
import json

import pytest

from test_engine import spec
from test_execution_toolkit import make, ticks
from test_partial_fills import LIMIT, Scripted, rows
from test_uncertain_entries import Lost, day_of, filled

LATE = "entry filled after the flatten; sold as an untracked position"


def _resting(tmp_path, session, decider=None):
    bars = session(path=[100.0] * 390)
    br = Scripted()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], decider=decider, br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 6)
    st = eng.states[0].symbols["SPY"]
    assert "SPY" in book.pending and st.status == "pending" and st.note == "limit 99.95 resting"
    return bars, br, book, eng, st


def test_a_limit_that_fills_after_a_stop_flatten_no_longer_shows_as_resting(tmp_path, session):
    """Item 1: the STOP's cancel is still settling when the limit fills. The shares are sold as
    untracked (no trade), and the rule no longer shows `pending` with a resting note."""
    bars, br, book, eng, st = _resting(tmp_path, session)
    alerts = []
    eng.alert = lambda lvl, msg: alerts.append((lvl, msg))
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": day_of(bars).isoformat()}))
    ticks(eng, bars, 6, 7)
    assert book.blocked == "stop" and book.pending["SPY"].exited and st.status == "pending"  # cancel in flight
    br.set_fill(0.1, 99.95)
    br.cancel_final = True
    ticks(eng, bars, 7, 9)
    assert not book.pending and not book.entries and "SPY" not in br.positions
    assert not (tmp_path / "sim" / "trades.csv").exists()  # not a trade: sold as untracked
    assert any("more filled after its position closed" in m for _, m in alerts)
    assert st.status == "armed" and st.note == LATE and st.trades == 0
    shown = json.loads((tmp_path / "status.json").read_text())["classifiers"][0]["symbols"]["SPY"]
    assert shown["status"] == "armed" and shown["note"] == LATE


def test_a_limit_that_fills_after_the_eod_flatten_stays_retired_with_a_true_note(tmp_path, session):
    bars, br, book, eng, st = _resting(tmp_path, session)
    now = bars.index[375].to_pydatetime()
    eng.flatten_for_close(now)  # its cancel is still in flight: the rule is retired for the day
    assert book.pending["SPY"].exited and st.status == "retired" and st.note == "limit 99.95 resting"
    br.set_fill(0.1, 99.95)
    br.cancel_final = True
    eng.flatten_for_close(now + dt.timedelta(minutes=1))
    assert not book.pending and "SPY" not in br.positions
    assert st.status == "retired" and st.note == LATE


def test_a_limit_cancelled_unfilled_by_the_eod_flatten_drops_its_resting_note(tmp_path, session):
    bars, br, book, eng, st = _resting(tmp_path, session)
    now = bars.index[375].to_pydatetime()
    eng.flatten_for_close(now)
    br.cancel_final = True
    eng.flatten_for_close(now + dt.timedelta(minutes=1))
    assert not book.pending and st.status == "retired" and st.note == "limit expired unfilled"


def test_a_partial_fill_before_the_flatten_keeps_its_own_trade_and_note(tmp_path, session):
    """The late-fill note is only for an order that never opened a position: one that did is a
    trade, closed by the flatten, and its later shares change nothing about the rule."""
    bars, br, book, eng, st = _resting(tmp_path, session)
    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 6, 7)  # adopted at once; its cancel is in flight
    assert st.status == "holding" and st.note == ""
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": day_of(bars).isoformat()}))
    ticks(eng, bars, 7, 8)
    assert book.pending["SPY"].exited and not book.entries and st.trades == 1
    status = st.status
    br.set_fill(0.15, 99.95)
    br.cancel_final = True
    ticks(eng, bars, 8, 10)
    assert not book.pending and st.status == status and st.note == "" and st.trades == 1
    assert list(rows(tmp_path).reason) == ["ENTER", "manual STOP"]


def test_a_resting_limits_note_survives_a_restart(tmp_path, session):
    """Item 2: the note is saved with the rest of the rule's state."""
    bars, br, book, eng, st = _resting(tmp_path, session)
    ticks(eng, bars, 6, 7)  # saves the state
    book2, eng2 = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng2.start_day(day_of(bars), {})
    st2 = eng2.states[0].symbols["SPY"]
    assert st2.status == "pending" and st2.note == "limit 99.95 resting"
    br.set_fill(book2.pending["SPY"].qty, 99.95)
    br.status = "filled"
    ticks(eng2, bars, 7, 8)
    assert st2.status == "holding" and st2.note == ""


def test_a_note_saved_with_a_status_that_changed_while_down_is_dropped(tmp_path, session):
    """The limit filled while the runner was down: it resumes holding, not resting."""
    bars, br, book, eng, st = _resting(tmp_path, session)
    ticks(eng, bars, 6, 7)
    br.set_fill(book.pending["SPY"].qty, 99.95)
    br.status = "filled"
    eng._poll_pending(book, bars.index[7].to_pydatetime())  # booked, then a crash before the state is saved
    assert "SPY" in book.entries
    book2, eng2 = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng2.start_day(day_of(bars), {})
    st2 = eng2.states[0].symbols["SPY"]
    assert st2.status == "holding" and st2.note == ""


def test_a_state_file_from_before_notes_were_saved_still_loads(tmp_path, session):
    bars, br, book, eng, st = _resting(tmp_path, session)
    ticks(eng, bars, 6, 7)
    f = tmp_path / "classifier_state.json"
    data = json.loads(f.read_text())
    for saved in data["states"]["t"].values():
        del saved["note"], saved["alloc_note"]
    f.write_text(json.dumps(data))
    alerts = []
    book2, eng2 = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng2.alert = lambda lvl, msg: alerts.append((lvl, msg))
    eng2.start_day(day_of(bars), {})
    st2 = eng2.states[0].symbols["SPY"]
    assert st2.status == "pending" and st2.note == "" and st2.alloc_note == "" and not alerts
    assert st2.counts == st.counts  # the rest of the state still restored


def test_a_fill_shows_only_the_allocators_note_whatever_the_note_said(tmp_path, session):
    """Item 3: the fill clears the note structurally (to the allocator's own note, kept apart),
    so a note the old text match didn't know, here a market entry's `order outcome unknown`,
    doesn't outlive the uncertainty once the order is found filled."""
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.on_submit, br.lookup_error = filled(0.5, 100.02), ConnectionError("503")
    book, eng = make(tmp_path, [spec()], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 8)
    st = eng.states[0].symbols["SPY"]
    assert st.status == "pending" and st.note.startswith("order outcome unknown") and st.alloc_note == ""
    br.lookup_error = None
    ticks(eng, bars, 8, 9)
    assert "SPY" in book.entries and st.status == "holding" and st.note == ""


def test_an_earlier_attempts_allocator_note_is_not_carried_into_the_next_entry(tmp_path, session):
    bars, br, book, eng, st = _resting(tmp_path, session)
    st.alloc_note = "allocator: equity exposure; allowed $1.00 of $2.00"  # left over from an earlier attempt
    br.cancel_final = True
    eng._poll_pending(book, book.pending["SPY"].expires)  # expires unfilled
    assert st.status == "armed" and st.note == "limit expired unfilled"
    br.status, br.cancel_final = "new", False
    ticks(eng, bars, 6, 7)  # asks again: an untrimmed limit
    assert st.status == "pending" and st.note == "limit 99.95 resting" and st.alloc_note == ""
    br.set_fill(book.pending["SPY"].qty, 99.95)
    br.status = "filled"
    ticks(eng, bars, 7, 8)
    assert st.status == "holding" and st.note == ""


@pytest.mark.parametrize("field", ["note", "alloc_note"])
def test_a_damaged_saved_note_is_dropped_not_fatal(tmp_path, session, field):
    bars, br, book, eng, st = _resting(tmp_path, session)
    ticks(eng, bars, 6, 7)
    f = tmp_path / "classifier_state.json"
    data = json.loads(f.read_text())
    data["states"]["t"]["SPY"][field] = {"not": "a string"}
    f.write_text(json.dumps(data))
    book2, eng2 = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng2.start_day(day_of(bars), {})
    st2 = eng2.states[0].symbols["SPY"]
    assert st2.status == "pending" and isinstance(st2.note, str) and isinstance(st2.alloc_note, str)
    assert getattr(st2, field) == ""
