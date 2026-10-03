"""Orders Alpaca took whose answer was lost: a phantom server stop blocking an exit (#127), and a
market entry found through the OrderPending follow-up filled with no price (#130)."""

import datetime as dt
from types import SimpleNamespace as NS

import pytest

from trader import golive

from test_engine import Always
from test_execution_toolkit import ticks
from test_guessed_prices import Market
from test_orders import APIError
from test_partial_exits import DAY, T0, Venue, clock, held, open_book, trade_rows  # noqa: F401 (clock: fixture)


def phantom(v, qty=1.0, oid="p1"):
    """A server stop Alpaca accepted, but whose answer was lost: the engine doesn't know its id."""
    v.orders[oid] = {"status": "new", "filled_qty": 0, "qty": qty, "cid": "t-SPY-lost-s"}


def test_a_phantom_stop_is_cancelled_and_the_close_retried_in_the_same_call(tmp_path, clock):
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    phantom(v)
    v.closes = [(1.0, 99.0)]
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "classifier EXIT")
    assert ("cancel", "p1") in v.calls and [c for c in v.calls if c[0] == "close"] == [("close", "SPY")] * 2
    (r,) = trade_rows(tmp_path)
    assert (r["side"], r["qty"], r["price"], r["reason"]) == ("sell", "1.000000", "99.0000", "classifier EXIT")
    assert float(r["pnl_pct"]) == pytest.approx(-1.0)
    assert not book.entries and not v.positions and not v.alive()


def test_a_phantom_stop_that_partly_filled_is_booked_by_its_id_once(tmp_path, clock):
    """It sold 0.3 @ 98 before the exit: that leg is part of the round trip, at its real price, and a
    later report of the same order (a retry, a restart) doesn't count it twice."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    phantom(v)
    v.fire("p1", 0.3, 98.0)
    v.closes = [(0.7, 99.0)]
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "stop")
    (r,) = trade_rows(tmp_path)
    assert (r["side"], r["qty"], r["reason"]) == ("sell", "1.000000", "stop")
    assert float(r["pnl"]) == pytest.approx(0.3 * 98.0 + 0.7 * 99.0 - 100.0)
    assert float(r["pnl_pct"]) == pytest.approx(-1.3)
    assert golive.shadow_record("t", "2000-01-01", tmp_path / "paper") == (1, pytest.approx(-1.3 - 0.1))
    assert not book.entries and not v.positions and not v.alive()


@pytest.mark.parametrize("sold", [0.0, 0.3])
def test_if_the_retry_fails_too_the_shares_keep_a_server_stop(tmp_path, clock, sold):
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    phantom(v)
    if sold:
        v.fire("p1", sold, 98.0)
    v.closes = [APIError(500, "internal error")]  # the retried close
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "classifier EXIT")
    e = book.entries["SPY"]
    assert e.qty == pytest.approx(1.0 - sold) and v.alive() == {e.stop_id: pytest.approx(1.0 - sold)}
    assert e.stop_id not in ("s1", "p1") and v.orders["p1"]["status"] == "canceled"
    booked = trade_rows(tmp_path) if sold else []
    assert [(r["side"], r["qty"], r["price"]) for r in booked] == (
        [("sell_part", "0.300000", "98.0000")] if sold else []
    )
    v.closes = [(1.0 - sold, 99.0)]
    eng._exit(book, "SPY", e, 99.4, T0 + dt.timedelta(minutes=1), "classifier EXIT")
    r = trade_rows(tmp_path)[-1]
    assert r["side"] == "sell" and r["pnl_pct"] != "" and not book.entries and not v.positions and not v.alive()


def test_an_open_entry_buy_is_never_cancelled_to_free_the_shares(tmp_path, clock):
    """Only sells hold shares: a buy for the same symbol (a limit entry still settling) is left alone."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    phantom(v)
    v.orders["b1"] = {"status": "partially_filled", "filled_qty": 0.1, "qty": 0.5, "price": 100.0, "side": "buy"}
    v.closes = [(1.0, 99.0)]
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "classifier EXIT")
    assert ("cancel", "b1") not in v.calls and v.orders["b1"]["status"] == "partially_filled"
    assert ("cancel", "p1") in v.calls and not book.entries and "SPY" not in v.positions


class LostMarket(Market):
    """A market buy Alpaca fills, but the answer to the submit is lost: the engine finds the order by
    its client order id, and Alpaca never reports its average price."""

    def submit_order(self, req):
        o = super().submit_order(req)
        if getattr(req, "notional", None) is None:
            return o  # a server stop
        self.orders["b1"]["cid"] = req.client_order_id
        raise ConnectionError("read timeout")

    def get_order_by_client_id(self, cid):
        oid = next((k for k, o in self.orders.items() if o.get("cid") == cid), None)
        if oid is None:
            raise APIError(404, "order not found")
        return self.get_order_by_id(oid)


@pytest.mark.parametrize("avg_cost", [100.02, 0.0])
def test_an_unpriced_fill_found_by_the_follow_up_is_protected_in_the_same_minute(tmp_path, session, clock, avg_cost):
    """#130: no UNPRICED_FILL_WAIT (3 minutes) without a stop. Priced as on the direct path (#129): the
    position's average cost, which is real, else the last price, labelled as a guess."""
    bars = session(day=DAY, path=[100.0] * 390)
    v = LostMarket(avg_cost)
    book, eng = open_book(tmp_path, v)
    eng.decider = Always()
    ticks(eng, bars, 0, 6)  # enters at 09:35 on a 100.00 print
    e = book.entries["SPY"]
    assert not book.pending and v.alive() == {e.stop_id: pytest.approx(e.qty)}
    assert e.price == (avg_cost or 100.0) and e.price_estimated == (not avg_cost)
    (r,) = trade_rows(tmp_path)
    assert r["reason"] == ("ENTER" if avg_cost else "ENTER (price estimated)") and "T09:35" in r["time"]
