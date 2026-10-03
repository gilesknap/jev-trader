"""A damaged classifier_state.json never stops the session: the rules start fresh, matched to the
positions the book holds, with an urgent alert saying what was lost."""

import json

import pytest

from trader.broker import SimBroker
from trader.engine import Book, Engine

from test_engine import Always, spec
from test_restart_state import ticks


def make(tmp_path, specs, alerts):
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine(specs, {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path,
                 alert=lambda lvl, msg: alerts.append((lvl, msg)))
    return book, eng


@pytest.mark.parametrize("text", ["{trunc", "[1, 2]", '"x"', b"\xff\xfe".decode("latin-1"),
                                  '{"day": "%s", "states": [1]}', '{"day": "%s", "states": {"t": 5}}',
                                  "[" * 100_000 + "]" * 100_000])
def test_an_unreadable_file_starts_fresh_with_an_urgent_alert(tmp_path, session, text):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    f = tmp_path / "classifier_state.json"
    f.write_bytes(text.replace("%s", day.isoformat()).encode("latin-1"))
    alerts = []
    book, eng = make(tmp_path, [spec()], alerts)
    eng.start_day(day, {})
    st = eng.states[0].symbols["SPY"]
    assert (st.status, st.trades) == ("armed", 0)
    assert [lvl for lvl, _ in alerts] == ["urgent"] and "unreadable" in alerts[0][1]
    assert (tmp_path / "classifier_state.json.unreadable").exists()
    ticks(eng, bars, 10, 12)  # the session runs, and the next tick rewrites a good file
    assert json.loads(f.read_text())["day"] == day.isoformat()


def test_a_fresh_start_still_manages_an_open_position(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    alerts = []
    book, eng = make(tmp_path, [spec(max_trades=1)], alerts)
    eng.start_day(day, {})
    ticks(eng, bars, 0, 20)
    assert "SPY" in book.entries
    (tmp_path / "classifier_state.json").write_text("{trunc")
    book2, eng2 = make(tmp_path, [spec(max_trades=1)], alerts)
    book2.broker = book.broker
    eng2.start_day(day, {})
    st = eng2.states[0].symbols["SPY"]
    assert "SPY" in book2.entries and (st.status, st.trades) == ("holding", 1)


@pytest.mark.parametrize("trades", ["two", True, -1, 1.5, None])
def test_a_damaged_entry_starts_fresh_and_is_reported(tmp_path, session, trades):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "classifier_state.json").write_text(json.dumps({"day": day.isoformat(), "states": {
        "t": {"SPY": {"status": "retired", "trades": trades}}}}))
    alerts = []
    _, eng = make(tmp_path, [spec()], alerts)
    eng.start_day(day, {})
    st = eng.states[0].symbols["SPY"]
    assert (st.status, st.trades) == ("armed", 0)
    assert len(alerts) == 1 and alerts[0][0] == "urgent" and "t/SPY" in alerts[0][1]


def test_an_unknown_status_is_damage_not_a_new_state(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "classifier_state.json").write_text(json.dumps({"day": day.isoformat(), "states": {
        "t": {"SPY": {"status": "frozen", "trades": 1}}}}))
    alerts = []
    _, eng = make(tmp_path, [spec()], alerts)
    eng.start_day(day, {})
    assert eng.states[0].symbols["SPY"].status == "armed" and alerts


def test_a_good_file_restores_quietly(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "classifier_state.json").write_text(json.dumps({"day": day.isoformat(), "states": {
        "t": {"SPY": {"status": "retired", "trades": 1}}}}))
    alerts = []
    _, eng = make(tmp_path, [spec()], alerts)
    eng.start_day(day, {})
    st = eng.states[0].symbols["SPY"]
    assert (st.status, st.trades) == ("retired", 1) and not alerts


def test_an_alert_that_cant_be_sent_doesnt_stop_the_session(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    (tmp_path / "classifier_state.json").write_text("{trunc")

    def broken(lvl, msg):
        raise RuntimeError("push service down")

    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path, alert=broken)
    eng.start_day(day, {})
    assert eng.states[0].symbols["SPY"].status == "armed"
