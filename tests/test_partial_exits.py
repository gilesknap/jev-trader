"""#61: every sold leg of an exit is booked once, by its order, and the round trip is one trade."""

import csv
import datetime as dt
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from trader import golive, scoreboard
from trader.broker import AlpacaBroker, Fill, OrderState, SimBroker
from trader.data import ET
from trader.engine import Book, Engine, Entry, Pending

from test_engine import Always, spec
from test_orders import APIError, FakeClient

DAY = dt.date(2026, 10, 14)
T0 = dt.datetime(2026, 10, 14, 10, 0, tzinfo=ET)


class Venue(FakeClient):
    """Alpaca as the broker adapter sees it: positions, server stops and close orders. Each
    close_position does what the test scripted in `closes`: (qty, price) sells that much (the
    order ends `canceled` part-filled if shares are left), or an exception is raised. Like
    Alpaca, a close is refused (403) while open sell orders hold any of the shares (#127)."""

    def __init__(self, held=1.0):
        super().__init__()
        self.positions = {"SPY": held}
        self.closes: list = []
        self.sticky: set[str] = set()  # orders whose cancel doesn't take
        self.cids: set[str] = set()
        self.n = 0

    def submit_order(self, req):  # server stops only
        if req.client_order_id in self.cids:
            raise APIError(422, "client_order_id must be unique")
        self.cids.add(req.client_order_id)
        self.n += 1
        oid = f"s{self.n}"
        self.orders[oid] = {"status": "new", "filled_qty": 0, "qty": float(req.qty), "cid": req.client_order_id}
        return NS(id=oid)

    def get_orders(self, req):
        """Open orders, filtered by symbol and side as Alpaca does (an order with no side is a sell)."""
        side = getattr(req.side, "value", req.side)
        return [NS(id=oid, symbol=o.get("symbol", "SPY"), side=NS(value=o.get("side", "sell")), status=NS(value=o["status"]))
                for oid, o in self.orders.items()
                if o["status"] in ("new", "accepted", "partially_filled") and o.get("symbol", "SPY") in req.symbols
                and (side is None or o.get("side", "sell") == side)]

    def _held_for_orders(self, sym):
        return sum(float(o.get("qty", 0)) - float(o.get("filled_qty") or 0) for o in self.orders.values()
                   if o.get("side", "sell") == "sell" and o.get("symbol", "SPY") == sym
                   and o["status"] in ("new", "accepted", "partially_filled"))

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel", oid))
        o = self.orders.get(oid)
        if o and oid not in self.sticky and o["status"] not in ("filled", "canceled"):
            o["status"] = "canceled"

    def fire(self, oid, qty, price, status="partially_filled"):
        """The stop sells `qty` more at `price`; its cumulative fill is blended like Alpaca's."""
        o = self.orders[oid]
        done = float(o.get("filled_qty") or 0)
        o["price"] = (done * float(o.get("price") or 0) + qty * price) / (done + qty)
        o["filled_qty"], o["status"] = done + qty, status
        self._sell(qty)

    def _sell(self, qty):
        self.positions["SPY"] = round(self.positions["SPY"] - qty, 9)
        if self.positions["SPY"] <= 1e-9:
            del self.positions["SPY"]

    def close_position(self, sym):
        self.calls.append(("close", sym))
        if sym in self.positions and (held := self._held_for_orders(sym)) > 1e-9:
            raise APIError(403, '{"code":40310000,"message":"insufficient qty available for order '
                                f'(requested: {self.positions[sym]:g}, available: {max(0.0, self.positions[sym] - held):g})"}}')
        what = self.closes.pop(0)
        if isinstance(what, Exception):
            raise what
        qty, price = what
        qty = min(qty, self.positions[sym])
        self._sell(qty)
        oid = f"c{sum(1 for c in self.calls if c[0] == 'close')}"
        self.orders[oid] = {"status": "canceled" if sym in self.positions else "filled", "filled_qty": qty, "price": price}
        return NS(id=oid)

    def close_all_positions(self, cancel_orders=True):
        for oid in list(self.orders):
            self.cancel_order_by_id(oid)
        super().close_all_positions(cancel_orders)

    def alive(self):
        """Server stops still working."""
        return {oid: o["qty"] for oid, o in self.orders.items()
                if oid.startswith("s") and o["status"] in ("new", "partially_filled")}


class Paper(AlpacaBroker):
    def __init__(self, client):
        self.name, self.client, self.last, self.last_flatten_error = "alpaca-paper", client, {}, None

    def equity(self):
        return 250.0

    def settled_cash(self):
        return 250.0

    def settlement_snapshot(self):
        return {}


@pytest.fixture
def clock(monkeypatch):
    """The adapter's waits and polls run on a fake clock, so a cancel that never takes costs nothing."""
    now = [0.0]
    monkeypatch.setattr("time.sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr("time.time", lambda: now[0])
    return now


def open_book(tmp_path, venue, entry=None):
    """A paper book holding 1 SPY bought at 100 (stop 99.5, server stop s1); a fresh Book/Engine is a restart."""
    book = Book("paper", Paper(venue), tmp_path / "paper")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(entry="WAIT"), {"SPY"}, tmp_path)
    eng.start_day(DAY, {})
    if entry:
        venue.orders.setdefault("s1", {"status": "new", "filled_qty": 0, "qty": entry.qty, "cid": "t-SPY-s"})
        venue.n = max(venue.n, 1)
        book.entries["SPY"] = entry
        book.save_entries()
    return book, eng


def held(qty=1.0, **kw):
    return Entry("t", qty, 100.0, 99.5, 101.0, T0 - dt.timedelta(minutes=30), stop_id="s1", **kw)


def trade_rows(tmp_path):
    with (tmp_path / "paper" / "trades.csv").open() as f:
        return list(csv.DictReader(f))


def test_the_issue_example_is_one_trade_at_plus_two_across_retries_and_a_restart(tmp_path, clock):
    """#61: buy 1 @ 100; the exit sells 0.4 @ 90, then (after a restart) 0.3 @ 110, then 0.3 @ 110.
    Real P&L is +$2 (not +$10 from pricing the whole qty at the last leg), and it's one trade."""
    v = Venue()
    v.closes = [(0.4, 90.0), (0.3, 110.0), (0.3, 110.0)]
    book, eng = open_book(tmp_path, v, held())
    eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0, "classifier EXIT")
    e = book.entries["SPY"]
    assert e.qty == pytest.approx(0.6) and v.positions == {"SPY": pytest.approx(0.6)}
    assert v.alive() == {e.stop_id: pytest.approx(0.6)} and e.stop_id != "s1"  # the rest is protected, by one stop
    eng.tick(T0 + dt.timedelta(minutes=1), {}, 300)  # a tracked remainder is never sold as an orphan
    assert [c for c in v.calls if c[0] == "close"] == [("close", "SPY")]

    book, eng = open_book(tmp_path, v)  # restart between legs
    for i in (2, 3):
        eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0 + dt.timedelta(minutes=i), "classifier EXIT")
    assert not book.entries and not v.positions and not v.alive()
    t = trade_rows(tmp_path)
    assert [(r["side"], r["qty"], r["price"]) for r in t] == [
        ("sell_part", "0.400000", "90.0000"), ("sell_part", "0.300000", "110.0000"), ("sell", "0.300000", "110.0000")]
    assert float(t[-1]["pnl"]) == pytest.approx(2.0) and float(t[-1]["pnl_pct"]) == pytest.approx(2.0)
    assert golive.shadow_record("t", "2026-10-01", tmp_path / "paper") == (1, pytest.approx(2.0 - 0.1))
    assert len(scoreboard.closed_trades(t)) == 1
    assert book.restore_realised(DAY) == pytest.approx(2.0)


def test_a_partly_filled_server_stop_is_booked_once_and_never_doubled_by_a_second_stop(tmp_path, clock):
    """The stop has sold 0.2 and its cancel doesn't take: that leg is booked, the stop keeps its id
    (no second stop beside it), and when it's reported again with more filled only the new part counts."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.fire("s1", 0.2, 99.0)
    v.sticky.add("s1")
    v.closes = [APIError(403, "insufficient qty available for order")]  # the stop still holds the shares
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "stop")
    e = book.entries["SPY"]
    assert e.qty == pytest.approx(0.8) and e.stop_id == "s1" and list(v.alive()) == ["s1"]

    book, eng = open_book(tmp_path, v)  # restart: the booked leg is remembered
    v.fire("s1", 0.3, 98.0)  # 0.5 cumulative at 98.4
    v.sticky.clear()
    v.closes = [(0.5, 97.0)]
    eng._exit(book, "SPY", book.entries["SPY"], 98.0, T0 + dt.timedelta(minutes=1), "stop")
    t = trade_rows(tmp_path)
    assert [(r["side"], r["qty"]) for r in t] == [("sell_part", "0.200000"), ("sell", "0.800000")]
    # proceeds 0.5 x 98.4 + 0.5 x 97 = 97.7 for shares that cost 100
    assert float(t[-1]["pnl"]) == pytest.approx(-2.3) and float(t[-1]["pnl_pct"]) == pytest.approx(-2.3)
    assert not book.entries and not v.positions and not v.alive()


def test_a_stop_whose_price_is_reported_a_poll_late_is_still_real_evidence(tmp_path, clock):
    """Alpaca can report a stop `filled` a poll before its average price. The leg must wait for the
    price, not book the stop-out as estimated (that would drop losers from the gate's evidence)."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.fire("s1", 1.0, 98.0, status="filled")
    v.orders["s1"].update(price=None, script=[{}, {"price": 98.0}])  # the first poll has no price yet
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "stop")
    (r,) = trade_rows(tmp_path)
    assert (r["side"], r["reason"], r["price"]) == ("sell", "stop", "98.0000")
    assert float(r["pnl_pct"]) == pytest.approx(-2.0)
    assert golive.shadow_record("t", "2026-10-01", tmp_path / "paper") == (1, pytest.approx(-2.0 - 0.1))


def test_a_vanished_positions_stop_fill_waits_for_its_price(clock):
    v = Venue()
    v.orders["s1"] = {"status": "filled", "filled_qty": 1.0, "price": None, "script": [{}, {"price": 98.0}]}
    fill = Paper(v).stop_fill("s1", T0)
    assert fill is not None and fill.price == 98.0 and fill.order_id == "s1"


def test_a_stop_that_filled_for_fewer_shares_than_are_held_closes_the_rest_too(clock):
    """A server stop placed before the position grew (#59) can fill and still leave shares."""
    v = Venue(held=1.5)
    v.orders["s1"] = {"status": "new", "filled_qty": 0, "qty": 1.0}
    v.fire("s1", 1.0, 97.0, status="filled")
    v.closes = [(0.5, 99.0)]
    fill = Paper(v).sell_all("SPY", 99.0, T0, "x", stop_id="s1")
    assert fill.qty == pytest.approx(1.5) and fill.price == pytest.approx((97.0 + 0.5 * 99.0) / 1.5)
    assert [f.order_id for f in fill.legs] == ["s1", "c1"] and not v.positions


def test_a_leg_with_no_reported_price_makes_the_round_trip_estimated(tmp_path, clock):
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.closes = [(0.4, None), (0.6, 101.0)]  # Alpaca never reports the first leg's average price
    eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0, "classifier EXIT")
    eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0 + dt.timedelta(minutes=1), "classifier EXIT")
    t = trade_rows(tmp_path)
    assert [r["reason"] for r in t] == ["classifier EXIT (partial) (price estimated)", "classifier EXIT (price estimated)"]
    assert t[0]["pnl_pct"] == "" and t[1]["pnl_pct"] == ""  # not gate, promotion or scoreboard evidence
    assert golive.shadow_record("t", "2026-10-01", tmp_path / "paper") == (0, None)


def test_stop_after_a_partial_close_records_the_sold_leg_at_its_real_price(tmp_path, clock):
    """STOP: the targeted close sells 0.4 and fails for the rest; close-all sweeps the remainder."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.closes = [(0.4, 99.0)]
    (tmp_path / "paper" / "stop.json").write_text(json.dumps({"stop_on": DAY.isoformat()}))
    eng.tick(T0, {}, 300)
    t = trade_rows(tmp_path)
    assert [(r["side"], r["qty"], r["price"]) for r in t][0] == ("sell_part", "0.400000", "99.0000")
    assert [(r["side"], r["qty"]) for r in t][1:] == [("sell", "0.600000")]
    assert t[1]["reason"] == "manual STOP (close-all) (price estimated)"  # close-all's price is still a guess
    assert not book.entries and not v.positions and not v.alive()
    assert v.n == 1  # no remainder stop placed a moment before close-all (it would hold the shares it sells)


def test_a_scale_out_deferred_by_a_partly_fired_stop_books_it_and_protects_the_rest(tmp_path, clock):
    v = Venue()
    book, eng = open_book(tmp_path, v, held(scale_at=100.5, scale_fraction=0.5))
    v.fire("s1", 0.3, 99.5)  # between the trigger and the cancel
    assert eng._scale_out(book, "SPY", book.entries["SPY"], 100.5, T0)
    e = book.entries["SPY"]
    t = trade_rows(tmp_path)
    assert [(r["side"], r["qty"], r["reason"]) for r in t] == [("sell_part", "0.300000", "server stop (partial)")]
    assert e.qty == pytest.approx(0.7) and v.alive() == {e.stop_id: pytest.approx(0.7)} and e.stop_id != "s1"


def test_two_stop_re_placements_in_one_minute_use_different_client_ids(tmp_path, clock):
    """#116 review, item 4: a reused client order id is refused, which left no server stop."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    eng._resize_server_stop(book, "SPY", book.entries["SPY"], T0)
    first = book.entries["SPY"].stop_id
    eng._resize_server_stop(book, "SPY", book.entries["SPY"], T0)
    second = book.entries["SPY"].stop_id
    assert first and second and first != second and list(v.alive()) == [second]


def test_late_entry_fill_after_a_scale_out_gives_pnl_pct_on_the_real_cost(tmp_path):
    """#116 review, item 5: 0.1 @ 100, scale out 0.05 @ 101, a late 0.05 @ 99, exit 0.1 @ 102."""
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    eng.start_day(DAY, {})
    book.entries["SPY"] = Entry("t", 0.1, 100.0, 99.5, 101.0, T0)
    p = Pending("t", "o1", 100.0, 0.15, 15.0, T0, T0, 0.5, 1.0, {}, "c", filled_qty=0.1, filled_cost=10.0)
    book.pending["SPY"] = p
    book.record_partial("SPY", book.entries["SPY"], Fill("SPY", "sell", 0.05, 101.0, T0), "scale out")
    eng._adopt_fill(book, "SPY", p, OrderState("canceled", 0.15, (10.0 + 0.05 * 99.0) / 0.15), T0, place_stop=False)
    book.record_exit("SPY", book.entries["SPY"], Fill("SPY", "sell", 0.1, 102.0, T0), "target")
    last = pd.read_csv(tmp_path / "sim" / "trades.csv").iloc[-1]
    cost = 0.1 * 100.0 + 0.05 * 99.0
    pnl = 0.05 * 101.0 + 0.1 * 102.0 - cost
    assert last.pnl == pytest.approx(pnl, abs=0.005) and last.pnl_pct == pytest.approx(pnl / cost * 100, abs=5e-4)


def test_scoreboard_charges_slippage_on_the_whole_cost_of_a_late_filled_entry():
    """#116 review, item 3: an `ENTER (late fill)` row adds to the open cost, it doesn't replace it."""
    rows = [
        {"time": "2026-10-14T10:00", "classifier": "t", "symbol": "SPY", "side": "buy", "notional": "10.00", "reason": "ENTER"},
        {"time": "2026-10-14T10:02", "classifier": "t", "symbol": "SPY", "side": "buy", "notional": "5.00",
         "reason": "ENTER (late fill)"},
        {"time": "2026-10-14T11:00", "classifier": "t", "symbol": "SPY", "side": "sell", "notional": "15.30",
         "reason": "target", "pnl": "0.30", "pnl_pct": "2.000"},
    ]
    (t,) = scoreboard.closed_trades(rows, slippage_per_side_pct=0.05)
    assert t["net_usd"] == pytest.approx(0.30 - 15.0 * 0.1 / 100)
