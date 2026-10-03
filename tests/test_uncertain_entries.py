"""#60: an entry sent without a clear answer is found by its order identity and settled exactly once."""

import datetime as dt
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from trader import golive, runner, scoreboard
from trader.broker import NotFilled, OrderPending, OrderState, Position, SimBroker
from trader.data import ET
from trader.engine import Engine, Entry, Pending

from test_engine import Always, spec
from test_execution_toolkit import make, ticks
from test_orders import APIError, FakeClient, broker
from test_partial_fills import LIMIT, Scripted, rows


class Lost(Scripted):
    """The broker takes the entry order (unless `accepted` is False) but the answer never
    arrives: the submit raises `error`. `on_submit` scripts what the order then does. The engine
    can find the order only by the client order id it sent."""

    def __init__(self):
        super().__init__()
        self.error: Exception | None = ConnectionError("read timeout")
        self.accepted, self.on_submit, self.lookup_error = True, None, None
        self.sent: list[str] = []
        self.cids: dict[str, str] = {}  # client order id -> the broker's order id
        self.lookups = 0

    def _submit(self, client_id):
        self.sent.append(client_id)
        if self.accepted:
            self.cids[client_id] = "o1"
        if self.on_submit:
            self.on_submit(self)
        raise self.error

    def buy_notional(self, symbol, notional, ref_price, now, client_id):
        if self.error is None:
            self.sent.append(client_id)
            return SimBroker.buy_notional(self, symbol, notional, ref_price, now, client_id)
        self._submit(client_id)

    def buy_limit(self, symbol, qty, limit, now, client_id):
        self._submit(client_id)

    def order_id_for(self, client_id, symbol, since):
        self.lookups += 1
        if self.lookup_error:
            raise self.lookup_error
        return self.cids.get(client_id)


def filled(qty, avg):
    def go(br):
        br.set_fill(qty, avg)
        br.status = "filled"
    return go


def day_of(bars):
    return bars.index[0].date()


def test_a_market_entry_whose_answer_was_lost_is_adopted_and_protected_in_the_same_minute(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.on_submit = filled(0.5, 100.02)
    book, eng = make(tmp_path, [spec()], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)  # asks and enters at 09:35
    e = book.entries["SPY"]
    assert not book.pending and e.qty == 0.5 and e.price == 100.02
    assert e.stop_id == "s1" and br.stops == {"s1": 0.5}  # the server stop went on in that same minute
    assert e.time == pd.Timestamp(rows(tmp_path).time.iloc[0]).to_pydatetime()
    assert book.buys_today == pytest.approx(0.5 * 100.02)
    ticks(eng, bars, 5, 10)
    assert br.positions["SPY"].qty == 0.5 and list(rows(tmp_path).reason) == ["ENTER"]  # never orphan-sold
    assert br.sent == [br.sent[0]] and eng.states[0].symbols["SPY"].trades == 1


def test_an_unresolved_entry_holds_its_cash_and_symbol_across_a_failing_lookup_and_a_restart(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.on_submit, br.lookup_error = filled(0.5, 100.02), ConnectionError("503")
    book, eng = make(tmp_path, [spec(max_trades=3, after_exit="rearm")], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 8)
    p = book.pending["SPY"]
    assert p.order_id == "" and not book.entries and book.buys_today == pytest.approx(p.reserved)
    assert len(br.sent) == 1 and br.positions["SPY"].qty == 0.5  # no second order, and not sold as an orphan

    book2, eng2 = make(tmp_path, [spec(max_trades=3, after_exit="rearm")], br=br)  # restart while unresolved
    eng2.start_day(day_of(bars), {})
    assert book2.pending["SPY"].client_id == br.sent[0]
    br.lookup_error = None
    ticks(eng2, bars, 8, 9)
    e = book2.entries["SPY"]
    assert not book2.pending and e.qty == 0.5 and e.stop_id == "s1"
    assert book2.buys_today == pytest.approx(0.5 * 100.02)  # the reservation became the real cost, once
    assert list(rows(tmp_path).reason) == ["ENTER"] and eng2.states[0].symbols["SPY"].trades == 1
    assert len(br.sent) == 1


def test_a_limit_entry_whose_answer_was_lost_rests_and_is_tracked_when_it_fills(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()  # accepted and resting
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)
    p = book.pending["SPY"]
    assert p.order_id == "o1" and book.buys_today == pytest.approx(p.reserved)  # found by client id at once
    eng.decider = Always(entry="WAIT")
    br.set_fill(p.qty, 99.95)
    br.status = "filled"
    ticks(eng, bars, 5, 7)
    e = book.entries["SPY"]
    assert not book.pending and e.qty == pytest.approx(p.qty) and br.positions["SPY"].qty == pytest.approx(p.qty)


def test_an_entry_the_broker_never_got_is_released_after_a_minute_and_can_be_retried(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.accepted = False  # the lookup finds nothing: a 404
    book, eng = make(tmp_path, [spec()], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)
    assert "SPY" in book.pending and book.buys_today > 0  # not yet: it may just not show up yet
    br.error = None  # the next order goes through normally
    ticks(eng, bars, 5, 6)  # released, so the classifier's next ask can enter
    e = book.entries["SPY"]
    assert not book.pending and len(br.sent) == 2 and list(rows(tmp_path).reason) == ["ENTER"]
    assert book.buys_today == pytest.approx(e.qty * e.price)  # the first reservation is gone


def test_a_refused_entry_releases_its_reservation_at_once(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.accepted, br.error = False, APIError(403, "insufficient buying power")
    br.lookup_error = AssertionError("a 403 with no order id needs no lookup")
    alerts = []
    book, eng = make(tmp_path, [spec()], br=br)
    eng.alert = lambda lvl, msg: alerts.append(msg)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)
    st = eng.states[0].symbols["SPY"]
    assert not book.pending and book.buys_today == 0 and st.status == "armed" and st.note.startswith("order failed")
    assert json.loads((book.dir / "risk.json").read_text())["buys_today"] == 0
    assert any("failed" in m for m in alerts)


def _partial_then_stopped(tmp_path, session, swept):
    """0.1 of a limit fills, its cancel stays in flight, and the stop sells the 0.1. Then 0.05
    more turns out to have filled: still held (`swept` False), or already sold by that exit."""
    bars = session(path=[100.0] * 8 + [99.0] * 382)
    br = Scripted()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT, max_trades=1, after_exit="rearm")], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 6)
    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 6, 8)
    assert book.entries["SPY"].qty == pytest.approx(0.1)
    if swept:  # filled before the exit, reported after it: the close sells all 0.15
        br.positions["SPY"] = Position("SPY", 0.15, 99.95)
    ticks(eng, bars, 8, 9)  # the stop
    assert "SPY" not in book.entries and "SPY" in book.pending
    br.fill, br.avg = 0.15, 99.95
    if not swept:
        br.positions["SPY"] = Position("SPY", 0.05, 99.95)
    ticks(eng, bars, 9, 11)
    br.cancel_final = True
    ticks(eng, bars, 11, 13)
    return book, eng, br


@pytest.mark.parametrize("swept", [False, True])
def test_a_late_fill_after_the_position_closed_is_one_round_trip_and_never_a_guessed_trade(tmp_path, session, swept, monkeypatch):
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    book, eng, br = _partial_then_stopped(tmp_path, session, swept)
    t = rows(tmp_path)
    assert list(t.side) == ["buy", "sell"] and list(t.reason) == ["ENTER", "stop"]  # no second entry, no guessed sell
    assert not book.entries and not book.pending and "SPY" not in br.positions  # the late shares were sold
    assert eng.states[0].symbols["SPY"].trades == 1  # max_trades holds
    assert book.buys_today == pytest.approx(0.15 * 99.95)  # but the cash they cost is counted
    assert golive.shadow_record("t", "2000-01-01", book.dir)[0] == 1


def test_shares_booked_just_before_a_crash_are_adopted_not_orphan_sold(tmp_path, session, monkeypatch):
    class Crash(BaseException):
        pass

    bars = session(path=[100.0] * 390)
    br = Scripted()
    sp = spec(entry_order=LIMIT)
    book, eng = make(tmp_path, [sp], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 6)
    eng.decider = Always(entry="WAIT")
    br.set_fill(0.1, 99.95)
    with monkeypatch.context() as m:  # the process dies after booking the fill, before the entry is saved
        m.setattr(Engine, "_open_entry", lambda *a, **k: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            ticks(eng, bars, 6, 7)

    book2, eng2 = make(tmp_path, [sp], decider=Always(entry="WAIT"), br=br)
    eng2.start_day(day_of(bars), {})
    assert book2.pending["SPY"].filled_qty == pytest.approx(0.1) and not book2.entries
    ticks(eng2, bars, 7, 9)  # the cancel is still in flight
    e = book2.entries["SPY"]
    assert "SPY" in book2.pending and br.positions["SPY"].qty == pytest.approx(0.1)  # waited, not sold
    assert e.qty == pytest.approx(0.1) and e.price == pytest.approx(99.95) and e.stop_id == "s1"
    br.cancel_final = True
    ticks(eng2, bars, 9, 11)
    assert not book2.pending and book2.entries["SPY"].qty == pytest.approx(0.1)
    assert br.positions["SPY"].qty == pytest.approx(0.1) and list(rows(tmp_path).reason) == ["ENTER"]
    assert book2.buys_today == pytest.approx(0.1 * 99.95) and eng2.states[0].symbols["SPY"].trades == 1


def test_an_exit_at_a_guessed_price_is_not_gate_or_promotion_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    book, eng = make(tmp_path, [spec()])
    now = dt.datetime(2026, 9, 21, 10, 0, tzinfo=ET)
    eng.start_day(now.date(), {})
    book.entries["SPY"] = Entry("t", 0.5, 100.0, 99.0, 102.0, now - dt.timedelta(minutes=5))  # gone at the broker
    eng.tick(now, {}, 360)
    t = rows(tmp_path)
    assert t.reason.iloc[-1] == "closed outside engine (price estimated)" and pd.isna(t.pnl_pct.iloc[-1])
    assert golive.shadow_record("t", "2000-01-01", book.dir) == (0, None)
    assert scoreboard.closed_trades(t.fillna("").to_dict("records")) == []


def test_a_new_day_drops_an_unresolved_intent_and_rewrites_buys_today(tmp_path):
    book, eng = make(tmp_path, [spec()], br=Lost())
    then = dt.datetime(2026, 9, 21, 10, 0, tzinfo=ET)
    book.pending["SPY"] = Pending("t", "", 0.0, 0.5, 50.0, then, then, 0.5, 1.0, {}, "t-SPY-09211000")
    book.save_pending()
    book.write_risk(day=then.date().isoformat(), day_start_equity=250.0, cash_at_open=250.0, buys_today=50.0)
    runner.reconcile_at_startup(book, lambda lvl, msg: None)
    today = dt.datetime.now(ET).date()
    eng.start_day(today, {})
    assert not book.pending and book.buys_today == 0
    assert json.loads((book.dir / "risk.json").read_text())["buys_today"] == 0




# ---- the broker reports the order id, or a definitive no ------------------------------------

def test_an_accepted_entry_with_its_order_id_is_adopted_without_a_lookup(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.accepted, br.error = False, OrderPending("o1", TimeoutError("poll failed"))
    br.on_submit = filled(0.5, 100.02)
    book, eng = make(tmp_path, [spec()], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)
    e = book.entries["SPY"]
    assert not book.pending and e.qty == 0.5 and e.price == 100.02 and e.stop_id == "s1"
    assert br.lookups == 0 and list(rows(tmp_path).reason) == ["ENTER"]


def test_an_accepted_unfilled_entry_is_cancelled_released_and_retried_under_a_new_client_id(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.accepted, br.error, br.cancel_final = False, OrderPending("o1", TimeoutError("poll failed")), True
    book, eng = make(tmp_path, [spec()], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)  # the order is still new at the first look: cancelled, nothing filled
    st = eng.states[0].symbols["SPY"]
    assert not book.pending and book.buys_today == 0 and st.status == "armed" and st.note == "market order not filled"
    br.error = None
    ticks(eng, bars, 5, 6)
    e = book.entries["SPY"]
    assert len(br.sent) == 2 and br.sent[1] != br.sent[0] and br.lookups == 0
    assert book.buys_today == pytest.approx(e.qty * e.price)


def test_a_market_order_final_with_nothing_filled_is_a_definitive_no(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.accepted, br.error = False, NotFilled("order o1 canceled with nothing filled")
    alerts = []
    book, eng = make(tmp_path, [spec()], br=br)
    eng.alert = lambda lvl, msg: alerts.append((lvl, msg))
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 5)
    st = eng.states[0].symbols["SPY"]
    assert not book.pending and book.buys_today == 0 and st.note == "market order not filled" and br.lookups == 0
    assert not any(lvl == "urgent" for lvl, _ in alerts)


def _unpriced(tmp_path, session, avg_cost):
    """A market entry fills, but Alpaca never reports its average price. Its answer was lost, so it is
    found through the OrderPending follow-up, and adopted in that same minute, with its stop (#130)."""
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.accepted, br.error = False, OrderPending("o1", TimeoutError("no price"))

    def go(b):
        filled(0.5, 0.0)(b)
        b.positions["SPY"] = Position("SPY", 0.5, avg_cost)
    br.on_submit = go
    book, eng = make(tmp_path, [spec()], br=br)
    eng.start_day(day_of(bars), {})
    eng.decider = Always(exit="HOLD")
    ticks(eng, bars, 0, 5)  # asks and enters at 09:35
    assert "SPY" not in book.pending and book.entries["SPY"].stop_id == "s1"  # no 3-minute wait without a stop
    assert book.entries["SPY"].time == pd.Timestamp(rows(tmp_path).time.iloc[0]).to_pydatetime()
    ticks(eng, bars, 5, 8)
    return bars, br, book, eng


def test_an_unpriced_market_fill_takes_the_positions_average_cost_at_once(tmp_path, session):
    bars, br, book, eng = _unpriced(tmp_path, session, 100.03)
    e = book.entries["SPY"]
    assert not book.pending and e.qty == 0.5 and e.price == 100.03 and e.stop_id == "s1" and not e.price_estimated
    assert list(rows(tmp_path).reason) == ["ENTER"] and book.buys_today == pytest.approx(0.5 * 100.03)


def test_an_unpriced_market_fill_with_no_cost_is_protected_at_a_guess_that_is_not_evidence(tmp_path, session, monkeypatch):
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    bars, br, book, eng = _unpriced(tmp_path, session, 0.0)
    e = book.entries["SPY"]
    assert not book.pending and e.price == 100.0 and e.stop_id == "s1" and e.price_estimated  # the last price
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": day_of(bars).isoformat()}))
    ticks(eng, bars, 8, 9)
    t = rows(tmp_path)
    assert list(t.reason) == ["ENTER (price estimated)", "manual STOP (price estimated)"] and pd.isna(t.pnl_pct.iloc[1])
    assert golive.shadow_record("t", "2000-01-01", book.dir)[0] == 0

# ---- with the allocator (#74) ------------------------------------------------------------

def test_an_entry_in_flight_reserves_the_allowed_size_and_counts_against_the_limits_once(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Lost()
    br.on_submit, br.lookup_error = filled(0.25, 100.0), ConnectionError("503")  # filled, but unresolved
    book, eng = make(tmp_path, [spec(stop_pct=10, size_fraction=0.25)], br=br)
    eng.start_day(day_of(bars), {})
    book.realised_today = -5.0  # 3% of 250 less 5 leaves 2.5 of stop risk: $25 at a 10% stop
    ticks(eng, bars, 0, 5)
    row = [json.loads(x) for x in (tmp_path / "decisions" / f"{day_of(bars)}.jsonl").read_text().splitlines()
           if json.loads(x)["q"] == "allocation"][0]
    p = book.pending["SPY"]
    assert row["constraint"] == "aggregate stop risk" and row["allowed"] == pytest.approx(25.0, abs=0.01)
    assert p.limit == 0 and p.reserved == pytest.approx(row["allowed"], abs=0.01) and p.reserved < row["requested"]
    got = Engine._exposures(book, br.get_positions())
    assert [x.symbol for x in got] == ["SPY"] and got[0].notional == pytest.approx(p.reserved)  # not twice


def test_an_exited_orders_remainder_counts_nothing_but_its_late_shares_do(tmp_path, session):
    bars = session(path=[100.0] * 8 + [99.0] * 382)
    br = Scripted()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(day_of(bars), {})
    ticks(eng, bars, 0, 6)
    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 6, 9)  # adopted, then stopped out while the cancel is in flight
    p = book.pending["SPY"]
    assert p.exited and p.reserved - p.filled_cost > 1 and not book.entries
    assert Engine._exposures(book, br.get_positions()) == []  # the unfilled remainder can't become a position
    late = {"SPY": Position("SPY", 0.05, 99.95)}
    got = Engine._exposures(book, late)
    assert len(got) == 1 and got[0].notional == pytest.approx(0.05 * 99.95)  # untracked until it's sold

# ---- Alpaca adapter ------------------------------------------------------------------

class Client(FakeClient):
    def __init__(self):
        super().__init__()
        self.by_cid, self.submit_error = {}, None

    def submit_order(self, req):
        if self.submit_error:
            raise self.submit_error
        self.orders["b1"] = {"status": "new", "filled_qty": 0}
        return NS(id="b1")

    def get_order_by_id(self, oid):
        if self.orders[oid].get("down"):
            raise ConnectionError("poll failed")
        return super().get_order_by_id(oid)

    def get_order_by_client_id(self, cid):
        if cid not in self.by_cid:
            raise APIError(404, "order not found")
        return self.by_cid[cid]


def test_alpaca_market_entry_that_cant_be_read_reports_its_order_id(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = Client()
    b = broker(c)
    orig = c.submit_order
    c.submit_order = lambda req: (orig(req), c.orders["b1"].update(down=True))[0]
    with pytest.raises(OrderPending) as ex:
        b.buy_notional("SPY", 50.0, 100.0, None, "t-SPY-09211000")
    assert ex.value.order_id == "b1"
    c.orders["b1"] = {"status": "canceled", "filled_qty": 0}  # final with nothing filled: a definitive no
    c.submit_order = lambda req: (orig(req), c.orders["b1"].update(status="canceled"))[0]
    with pytest.raises(NotFilled):
        b.buy_notional("SPY", 50.0, 100.0, None, "t-SPY-09211002")
    c.submit_error = APIError(403, "insufficient buying power")  # refused: nothing to follow up
    with pytest.raises(APIError):
        b.buy_notional("SPY", 50.0, 100.0, None, "t-SPY-09211001")


def test_alpaca_finds_an_order_by_client_id_and_submit_time():
    c = Client()
    b = broker(c)
    sent = dt.datetime(2026, 9, 21, 10, 0, tzinfo=ET)
    assert b.order_id_for("t-SPY-09211000", "SPY", sent) is None  # 404: never placed
    c.by_cid["t-SPY-09211000"] = NS(id="b1", symbol="SPY", submitted_at=sent + dt.timedelta(seconds=20))
    assert b.order_id_for("t-SPY-09211000", "SPY", sent) == "b1"
    assert b.order_id_for("t-SPY-09211000", "QQQ", sent) is None  # someone else's order
    c.by_cid["t-SPY-09211000"].submitted_at = sent - dt.timedelta(days=365)  # a year-old order with the same id
    assert b.order_id_for("t-SPY-09211000", "SPY", sent) is None
    c.get_order_by_client_id = lambda cid: (_ for _ in ()).throw(APIError(500))
    with pytest.raises(APIError):  # can't tell: the engine keeps it reserved and asks again
        b.order_id_for("t-SPY-09211000", "SPY", sent)
