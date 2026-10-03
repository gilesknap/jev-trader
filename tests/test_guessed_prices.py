"""#101 (from #93/#96): a guessed price is never booked as a real one, and never becomes evidence."""

import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest

from test_engine import Always, spec
from test_execution_toolkit import make, ticks
from test_orders import APIError
from test_partial_exits import DAY, T0, Venue, clock, held, open_book, trade_rows  # noqa: F401 (clock: fixture)
from test_partial_fills import LIMIT, Scripted, rows
from trader import golive
from trader.broker import PartialExit
from trader.engine import Engine


class Market(Venue):
    """Also takes market buys: one fills at once, but Alpaca never reports its average price.
    The position's average cost is `avg_cost` (0: not reported either)."""

    def __init__(self, avg_cost):
        super().__init__()
        del self.positions["SPY"]
        self.avg_cost = avg_cost

    def submit_order(self, req):
        if getattr(req, "notional", None) is None:
            return super().submit_order(req)  # a server stop
        self.positions["SPY"] = qty = round(float(req.notional) / 100.02, 6)
        self.orders["b1"] = {"status": "filled", "filled_qty": qty, "price": None}
        return NS(id="b1")

    def get_all_positions(self):
        return [NS(symbol=s, qty=q, avg_entry_price=self.avg_cost) for s, q in self.positions.items()]


def _enter_unpriced(tmp_path, session, clock, avg_cost):
    bars = session(day=DAY, path=[100.0] * 390)
    v = Market(avg_cost)
    book, eng = open_book(tmp_path, v)
    eng.decider = Always()
    ticks(eng, bars, 0, 6)  # enters at 09:35 on a 100.00 print
    e = book.entries["SPY"]
    assert not book.pending and v.alive() == {e.stop_id: pytest.approx(e.qty)}  # protected in the same minute
    (tmp_path / "paper" / "stop.json").write_text(json.dumps({"stop_on": DAY.isoformat()}))
    v.closes = [(e.qty, 101.0)]
    ticks(eng, bars, 6, 7)
    return e, trade_rows(tmp_path)


def test_an_unpriced_market_entry_takes_the_positions_average_cost_not_the_reference_price(tmp_path, session, clock):
    """Item 1: the normal `buy_notional` path used to book the reference price (the last print) as the
    real fill. Now it takes the position's average cost, which is real, and is evidence."""
    e, t = _enter_unpriced(tmp_path, session, clock, 100.02)
    assert e.price == 100.02 and not e.price_estimated
    assert [r["reason"] for r in t] == ["ENTER", "manual STOP"] and t[1]["pnl_pct"] != ""


def test_an_unpriced_market_entry_with_no_average_cost_is_a_labelled_guess(tmp_path, session, clock, monkeypatch):
    """Item 1: with no average cost either, the shares are still protected at once, at the last price,
    but that is a guess: `ENTER (price estimated)`, and the round trip is not evidence."""
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    e, t = _enter_unpriced(tmp_path, session, clock, 0.0)
    assert e.price == 100.0 and e.price_estimated
    assert [r["reason"] for r in t] == ["ENTER (price estimated)", "manual STOP (price estimated)"]
    assert t[1]["pnl_pct"] == "" and t[1]["pnl"] != ""  # the dollar P&L stays for the loss budget
    assert golive.shadow_record("t", "2000-01-01", tmp_path / "paper") == (0, None)


class Blind(Market):
    """Market, but just after the buy the positions read fails once, and no last price is known (#134)."""

    def __init__(self, fail):
        super().__init__(0.0)
        self.fail, self.fail_reads = fail, 0

    def submit_order(self, req):
        if getattr(req, "notional", None) is not None:
            self.fail_reads = int(self.fail)
        return super().submit_order(req)

    def get_all_positions(self):
        if self.fail_reads:
            self.fail_reads -= 1
            raise ConnectionError("503")
        return super().get_all_positions()


def _enter_blind(tmp_path, session, fail=True):
    bars = session(day=DAY, path=[100.0] * 390)
    v = Blind(fail)
    book, eng = open_book(tmp_path, v)
    book.broker.update_prices = lambda prices: None  # no last price for SPY
    eng.decider = Always()
    ticks(eng, bars, 0, 6)  # enters at 09:35, sized on a 100.00 print
    return bars, v, book, eng


def test_an_unpriced_fill_with_no_cost_or_last_price_is_protected_at_the_reference_price(tmp_path, session, clock):
    """#134: no price reported, no average cost (the read failed) and no last price. The shares used to be
    left untracked, then sold as an orphan with no trade row. Now they're adopted at once at the reference
    price, as a guess: protected by a server stop, recorded, not evidence, and the dollar P&L kept."""
    bars, v, book, eng = _enter_blind(tmp_path, session)
    e = book.entries["SPY"]
    assert not book.pending and e.price == pytest.approx(100.0, abs=1e-3) and e.price_estimated
    assert v.alive() == {e.stop_id: pytest.approx(e.qty)}  # protected in the same minute
    (tmp_path / "paper" / "stop.json").write_text(json.dumps({"stop_on": DAY.isoformat()}))
    v.closes = [(e.qty, 99.0)]
    ticks(eng, bars, 6, 7)
    t = trade_rows(tmp_path)
    assert [r["reason"] for r in t] == ["ENTER (price estimated)", "manual STOP (price estimated)"]
    assert t[1]["pnl_pct"] == "" and float(t[1]["pnl"]) < 0  # the loss still counts toward the budget


def test_a_fill_that_cannot_be_priced_at_all_keeps_its_order_until_it_can(tmp_path, session, clock, monkeypatch):
    """#134: if no price can be found at all, the order is kept, not dropped, so its shares are never
    sold as an orphan with no trade row; they're priced on a later tick."""
    real = Engine._fill_price
    monkeypatch.setattr(Engine, "_fill_price", lambda *a, **k: (0.0, False))
    bars, v, book, eng = _enter_blind(tmp_path, session, fail=False)  # _fill_price, which reads them, is stubbed
    assert "SPY" not in book.entries and book.pending["SPY"].filled_qty == 0
    ticks(eng, bars, 6, 7)
    assert v.positions.get("SPY") and not v.closes  # shielded: not closed as an orphan
    monkeypatch.setattr(Engine, "_fill_price", real)
    ticks(eng, bars, 7, 8)
    e = book.entries["SPY"]
    assert not book.pending and e.price_estimated and v.alive() == {e.stop_id: pytest.approx(e.qty)}
    assert [r["reason"] for r in trade_rows(tmp_path)] == ["ENTER (price estimated)"]


def test_a_limit_fill_with_no_reported_price_is_protected_at_its_limit_as_a_guess(tmp_path, session):
    """The limit bounds the cost, so the shares are protected at it at once; but it is still a guess."""
    bars = session(path=[100.0] * 390)
    br = Scripted()
    br.cancel_final = True
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    p = book.pending["SPY"]
    br.set_fill(p.qty, 0.0)  # filled, and Alpaca never reports its average price
    br.status = "filled"
    ticks(eng, bars, 6, 7)
    e = book.entries["SPY"]
    assert e.price == p.limit and e.price_estimated and e.stop_id == "s1"
    assert list(rows(tmp_path).reason) == ["ENTER (price estimated)"]


def test_a_close_that_sells_more_after_its_cancel_wait_is_re_read_by_its_id(tmp_path, clock, monkeypatch):
    """Item 4: a close whose cancel isn't final after the wait is booked with what it had sold (0.4 @ 90).
    It then sells 0.3 more @ 110. The next exit re-reads it by its id, so the round trip is real: +$2."""
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.closes = [(0.4, 90.0), (0.3, 110.0)]
    close = v.close_position

    def slow(sym):
        o = close(sym)
        if o.id == "c1":  # still working when it's booked: its cancel doesn't take in time
            v.orders["c1"]["status"] = "partially_filled"
            v.sticky.add("c1")
        return o

    v.close_position = slow
    eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0, "classifier EXIT")
    assert book.entries["SPY"].qty == pytest.approx(0.6)
    v.orders["c1"].update(filled_qty=0.7, price=(0.4 * 90.0 + 0.3 * 110.0) / 0.7, status="filled")
    v._sell(0.3)
    eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0 + dt.timedelta(minutes=1), "classifier EXIT")
    t = trade_rows(tmp_path)
    assert [(r["side"], r["qty"], r["reason"]) for r in t] == [
        ("sell_part", "0.400000", "classifier EXIT (partial)"),
        ("sell", "0.600000", "classifier EXIT"),
    ]
    assert float(t[-1]["pnl"]) == pytest.approx(2.0) and float(t[-1]["pnl_pct"]) == pytest.approx(2.0)
    assert golive.shadow_record("t", "2000-01-01", tmp_path / "paper") == (1, pytest.approx(2.0 - 0.1))
    assert not book.entries and not v.positions


def test_a_leg_sold_just_before_a_crash_ends_the_round_trip_estimated(tmp_path, clock, monkeypatch):
    """Item 5 (deferred, guard): a crash between a broker sell and its booking loses that leg's order id
    (Alpaca's close_position takes no client id to find it by). The round trip is then `(price estimated)`."""
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.closes = [(0.4, 90.0), (0.6, 110.0)]
    with pytest.raises(PartialExit):
        book.broker.sell_all("SPY", 100.0, T0, "x", "s1")  # sold 0.4; the runner dies before booking it
    book, eng = open_book(tmp_path, v)
    eng._exit(book, "SPY", book.entries["SPY"], 100.0, T0 + dt.timedelta(minutes=1), "classifier EXIT")
    (r,) = trade_rows(tmp_path)
    assert r["reason"] == "classifier EXIT (price estimated)" and r["pnl_pct"] == ""
    assert golive.shadow_record("t", "2000-01-01", tmp_path / "paper") == (0, None)


def test_a_failed_exit_keeps_a_server_stop_while_it_retries(tmp_path, clock):
    """Item 6 (the liquidation race, as it stands on main): `sell_all` cancels the server stop before the
    close. If the close then fails with nothing sold, the shares must not wait for the retry unprotected."""
    v = Venue()
    book, eng = open_book(tmp_path, v, held())
    v.closes = [APIError(500, "internal error")]
    eng._exit(book, "SPY", book.entries["SPY"], 99.4, T0, "classifier EXIT")
    e = book.entries["SPY"]
    assert e.qty == 1.0 and e.stop_id != "s1" and v.alive() == {e.stop_id: pytest.approx(1.0)}
    v.closes = [(1.0, 99.0)]
    eng._exit(book, "SPY", e, 99.4, T0 + dt.timedelta(minutes=1), "classifier EXIT")
    (r,) = trade_rows(tmp_path)
    assert not book.entries and not v.alive() and r["reason"] == "classifier EXIT" and r["pnl_pct"] != ""


def test_a_stop_still_sells_a_fill_whose_order_is_kept_unpriced(tmp_path, session, clock, monkeypatch):
    """#134: the kept order doesn't shield its shares from a flatten (STOP here; EOD, kill and halt share
    the path): they're sold, and the order ends up exited, so they never become a trade."""
    monkeypatch.setattr(Engine, "_fill_price", lambda *a, **k: (0.0, False))
    bars, v, book, eng = _enter_blind(tmp_path, session, fail=False)
    qty = book.pending["SPY"].qty
    (tmp_path / "paper" / "stop.json").write_text(json.dumps({"stop_on": DAY.isoformat()}))
    v.closes = [(qty, 99.0)]
    ticks(eng, bars, 6, 8)
    assert "SPY" not in v.positions and "SPY" not in book.entries and not book.pending
