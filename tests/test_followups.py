"""Follow-ups from reviews (#32): startup reconciliation, lagging positions, exit recovery."""

import csv
import datetime as dt
from types import SimpleNamespace as NS

import pytest

from test_orders import FakeClient, broker
from trader import golive, scoreboard
from trader.broker import Fill, Position, SimBroker
from trader.data import ET
from trader.engine import Book, Entry
from trader.runner import reconcile_at_startup


def trade_rows(tmp_path):
    with (tmp_path / "paper" / "trades.csv").open() as f:
        return list(csv.DictReader(f))


def _entry(sym="SPY", stop_id=None):
    return Entry("idea", 0.5, 100.0, 99.0, 101.0, dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET), stop_id)


class Recon(SimBroker):
    def __init__(self, exit_fill=None):
        super().__init__(250.0)
        self.cancelled, self.exit_fill = None, exit_fill

    def cancel_orders(self, symbols):
        self.cancelled = set(symbols)

    def cancel_all(self):
        raise AssertionError("must not cancel every order (tracked stops would go)")

    def exit_fill_since(self, symbol, since, now, strict=False):
        return self.exit_fill


def test_startup_leaves_orphans_and_their_orders_for_the_open(tmp_path):
    # #47: before the open a market sell only queues (and is cancelled 20 s later); the engine
    # closes the orphan at its first tick instead (tests/test_orphans.py).
    br = Recon()
    br.positions = {"SPY": Position("SPY", 0.5, 100.0), "QQQ": Position("QQQ", 0.2, 400.0)}
    book = Book("paper", br, tmp_path / "paper")
    book.entries["SPY"] = _entry(stop_id="stop-1")
    reconcile_at_startup(book, lambda *a: None)
    assert br.cancelled is None and "SPY" in book.entries and "QQQ" in br.positions


def test_exit_while_down_is_recorded_with_the_real_fill(tmp_path):
    fill = Fill("SPY", "sell", 0.5, 102.0, dt.datetime(2026, 10, 6, 11, tzinfo=ET))
    book = Book("paper", Recon(exit_fill=fill), tmp_path / "paper")
    book.entries["SPY"] = _entry()
    reconcile_at_startup(book, lambda *a: None)
    rows = (tmp_path / "paper" / "trades.csv").read_text().splitlines()
    assert not book.entries and len(rows) == 2
    assert ",102.0000," in rows[1] and ",1.00," in rows[1] and "closed while runner down" in rows[1]


def test_exit_while_down_with_unknown_price_is_recorded_at_the_stop_but_is_not_evidence(tmp_path, monkeypatch):
    """#101: the stop is a guess, so the round trip is `(price estimated)` with no pnl_pct and counts
    towards nothing; its dollar loss at the stop stays, so today's loss budget (#117) still sees it."""
    monkeypatch.setattr(golive, "START_DATE", dt.date(2000, 1, 1))  # its trades predate the pinned start date
    book = Book("paper", Recon(), tmp_path / "paper")
    book.entries["SPY"] = _entry()
    alerts = []
    reconcile_at_startup(book, lambda lvl, msg: alerts.append(msg))
    (row,) = trade_rows(tmp_path)
    assert (row["price"], row["pnl"], row["pnl_pct"]) == ("99.0000", "-0.50", "")
    assert row["reason"] == "closed while runner down (recorded at stop) (price estimated)"
    assert any("exit price is unknown" in a for a in alerts)
    assert golive.shadow_record("idea", "2000-01-01", book.dir) == (0, None)
    assert scoreboard.closed_trades(trade_rows(tmp_path)) == []
    assert book.restore_realised(dt.datetime.now(ET).date()) == pytest.approx(-0.5)


def test_sell_all_rechecks_a_lagging_positions_endpoint(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)
    c = FakeClient()
    c.positions = {"SPY": 1.0}
    seen = iter([{"SPY": 1.0}, {}])  # still listed right after the fill, gone a moment later
    c.get_all_positions = lambda: [NS(symbol=s, qty=q, avg_entry_price=100.0) for s, q in next(seen).items()]
    fill = broker(c).sell_all("SPY", 100.0, None, "x")
    assert fill is not None
    assert fill.qty == 1.0 and fill.price == 99.0


def test_exit_fill_since_blends_every_filled_sell(monkeypatch):
    c = FakeClient()
    orders = [
        NS(filled_qty="0.2", filled_avg_price="101"),
        NS(filled_qty="0.3", filled_avg_price="106"),
        NS(filled_qty="0", filled_avg_price=None),
    ]
    monkeypatch.setattr(c, "get_orders", lambda req: orders)
    f = broker(c).exit_fill_since("SPY", dt.datetime(2026, 10, 6, 10, tzinfo=ET), None)
    assert f is not None
    assert abs(f.qty - 0.5) < 1e-12 and abs(f.price - 104.0) < 1e-9
