"""#59: the filled part of a resting limit entry is a protected position at once."""

import json

import pandas as pd
import pytest

from test_engine import Always, spec
from test_execution_toolkit import make, ticks
from trader.broker import OrderState, Position, SimBroker

LIMIT = {"type": "limit", "offset_pct": 0.05, "expire_min": 60}  # limit 99.95 on a 100.00 print


class PartialSim(SimBroker):
    """A limit buy that trades below its limit fills only half; cancelling keeps that half."""

    def update_bars(self, bars):
        for o in self.orders.values():
            b = bars.get(o["symbol"])
            if o["state"].status != "new" or b is None:
                continue
            hit = b[(b.index >= pd.Timestamp(o["placed"])) & (b.low < o["limit"])]
            if len(hit):
                q = o["qty"] / 2
                self.cash -= q * o["limit"]
                self.positions[o["symbol"]] = Position(o["symbol"], q, o["limit"])
                o["state"] = OrderState("partially_filled", q, o["limit"])

    def cancel_order(self, oid):
        o = self.orders[oid]
        if o["state"].status in ("new", "partially_filled"):
            o["state"] = OrderState("canceled", o["state"].filled_qty, o["state"].price)
        return o["state"]


class Scripted(SimBroker):
    """An order whose fills and cancel are driven by the test; server stops are recorded."""

    def __init__(self):
        super().__init__(250.0)
        self.fill, self.avg, self.status = 0.0, 0.0, "new"
        self.cancel_final = False
        self.stops, self.stop_cancel = {}, None

    def update_bars(self, bars):
        pass

    def set_fill(self, qty, avg):
        self.cash -= qty * avg - self.fill * self.avg
        self.fill, self.avg = qty, avg
        self.positions["SPY"] = Position("SPY", qty, avg)
        if self.status == "new":
            self.status = "partially_filled"

    def order_state(self, oid):
        return OrderState(self.status, self.fill, self.avg)

    def cancel_order(self, oid):
        if oid in self.stops:
            return self.stop_cancel or OrderState("canceled")
        self.status = "canceled" if self.cancel_final else "pending_cancel"
        return OrderState(self.status, self.fill, self.avg)

    def place_stop(self, symbol, qty, stop_price, client_id):
        sid = f"s{len(self.stops) + 1}"
        self.stops[sid] = qty
        return sid


def rows(tmp_path):
    return pd.read_csv(tmp_path / "sim" / "trades.csv")


def test_partial_fill_followed_by_a_stop_breach_exits_before_expiry(tmp_path, session):
    path = [100.0] * 10 + [99.9] * 5 + [99.0] * 375  # half fills at 99.95, then breaks the 0.5% stop
    bars = session(path=path)
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=PartialSim(250.0))
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 12)
    placed_qty = book.broker.orders[next(iter(book.broker.orders))]["qty"]
    e = book.entries["SPY"]
    assert not book.pending and e.qty == pytest.approx(placed_qty / 2)  # the rest was cancelled
    assert book.buys_today == pytest.approx(e.qty * 99.95)  # the reservation shrank to what filled
    ticks(eng, bars, 12, 20)
    t = rows(tmp_path)
    assert list(t.reason) == ["ENTER", "stop"] and t.qty.iloc[0] == pytest.approx(placed_qty / 2, abs=1e-6)
    assert pd.Timestamp(t.time.iloc[1]) < pd.Timestamp(t.time.iloc[0]) + pd.Timedelta(minutes=30)  # well before expiry
    assert not book.broker.positions


def test_partial_fill_is_protected_while_its_cancel_settles_and_late_fills_count_once(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Scripted()
    sp = spec(entry_order=LIMIT)
    book, eng = make(tmp_path, [sp], br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    reserved = book.pending["SPY"].reserved
    eng.decider = Always(entry="WAIT")

    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 6, 9)  # the cancel stays in flight for several minutes
    e = book.entries["SPY"]
    assert "SPY" in book.pending and e.qty == pytest.approx(0.1) and e.price == pytest.approx(99.95)
    assert br.stops == {"s1": pytest.approx(0.1)} and e.stop_id == "s1"  # protected at the broker too
    assert book.buys_today == pytest.approx(reserved)  # the whole order stays reserved while it can fill
    assert eng.states[0].symbols["SPY"].status == "holding"
    assert len(rows(tmp_path)) == 1  # repeat polls don't book the same shares twice

    book2, eng2 = make(tmp_path, [sp], decider=Always(entry="WAIT"), br=br)  # restart mid-settle
    eng2.start_day(bars.index[0].date(), {})
    assert book2.pending["SPY"].filled_qty == pytest.approx(0.1)
    br.set_fill(0.15, 99.93)  # 0.05 more filled at 99.89 before the cancel took
    br.cancel_final = True
    ticks(eng2, bars, 9, 12)
    e = book2.entries["SPY"]
    assert not book2.pending and e.qty == pytest.approx(0.15) and e.orig_qty == pytest.approx(0.15)
    assert e.price == pytest.approx(99.93)
    assert book2.buys_today == pytest.approx(0.15 * 99.93)
    assert e.stop_id == "s2" and br.stops["s2"] == pytest.approx(0.15)  # the server stop covers it all
    t = rows(tmp_path)
    assert list(t.reason) == ["ENTER", "ENTER (late fill)"]
    assert list(t.qty) == pytest.approx([0.1, 0.05]) and t.price.iloc[1] == pytest.approx(99.89)
    assert eng2.states[0].symbols["SPY"].trades == 1
    assert br.positions["SPY"].qty == pytest.approx(0.15)  # tracked, so never sold as an orphan


def test_partly_fired_server_stop_is_booked_and_the_rest_re_protected_when_the_position_grows(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Scripted()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    eng.decider = Always(entry="WAIT")
    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 6, 7)
    br.stop_cancel = OrderState("canceled", 0.04, 99.4)
    br.set_fill(0.15, 99.95)
    br.cancel_final = True
    ticks(eng, bars, 7, 8)
    e = book.entries["SPY"]
    # #61: the stop's 0.04 is booked by its order id, so the new stop can cover exactly what's left.
    t = rows(tmp_path)
    assert list(t.reason) == ["ENTER", "ENTER (late fill)", "server stop (partial)"]
    assert t.qty.iloc[2] == pytest.approx(0.04) and t.price.iloc[2] == pytest.approx(99.4)
    assert e.qty == pytest.approx(0.11) and e.stop_id == "s2" and br.stops["s2"] == pytest.approx(0.11)


def test_stop_during_a_settling_cancel_sells_and_records_the_filled_part(tmp_path, session):
    bars = session(path=[100.0] * 390)
    br = Scripted()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    eng.decider = Always(entry="WAIT")
    br.set_fill(0.1, 99.95)
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": bars.index[0].date().isoformat()}))
    ticks(eng, bars, 6, 8)
    t = rows(tmp_path)
    assert list(t.side) == ["buy", "sell"] and t.qty.iloc[1] == pytest.approx(0.1)
    assert not br.positions and not book.entries and "SPY" in book.pending  # the order is still settling
    br.cancel_final = True
    ticks(eng, bars, 8, 9)
    assert not book.pending and book.buys_today == pytest.approx(0.1 * 99.95)
    assert len(rows(tmp_path)) == 2  # nothing more filled: nothing more booked


def test_eod_flatten_sells_and_records_a_partial_fill_its_cancel_reveals(tmp_path, session):
    class LateReport(Scripted):
        def order_state(self, oid):  # the fill shows up only in the cancel's answer
            return OrderState("new") if self.status == "partially_filled" else super().order_state(oid)

    bars = session(path=[100.0] * 390)
    br = LateReport()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 345, 346)  # placed at 15:16; it would rest until 16:16
    eng.decider = Always(entry="WAIT")
    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 370, 374)
    assert not book.entries and "SPY" in book.pending  # the fill hasn't been reported yet
    ticks(eng, bars, 374, 375)  # 15:45, the first minute of the flatten window; the cancel stays in flight
    t = rows(tmp_path)
    assert list(t.side) == ["buy", "sell"] and list(t.qty) == pytest.approx([0.1, 0.1])
    assert t.time.iloc[1] == t.time.iloc[0]  # sold by that same flatten, at its real price
    assert not br.positions and "SPY" in book.pending
    ticks(eng, bars, 376, 377)
    br.cancel_final = True
    ticks(eng, bars, 377, 378)
    assert not book.pending and book.buys_today == pytest.approx(0.1 * 99.95) and len(rows(tmp_path)) == 2


def test_a_failing_cancel_still_protects_the_partial_fill_it_saw(tmp_path, session):
    class CancelFails(Scripted):
        def cancel_order(self, oid):
            if oid not in self.stops:
                raise ConnectionError("timeout")
            return super().cancel_order(oid)

    bars = session(path=[100.0] * 390)
    br = CancelFails()
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    eng.decider = Always(entry="WAIT")
    br.set_fill(0.1, 99.95)
    ticks(eng, bars, 6, 8)
    assert (
        "SPY" in book.pending and book.entries["SPY"].qty == pytest.approx(0.1) and book.entries["SPY"].stop_id == "s1"
    )
