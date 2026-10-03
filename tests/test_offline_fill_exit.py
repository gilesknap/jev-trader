"""A limit entry that filled, and whose position closed, while the runner was down: the engine
adopts the fill late, so its exit must be looked up from the fill, not from the adoption. And a
book's first NAV units (new, or after a paper rebase) are saved when issued, so a restart that
session keeps the day's P&L in the NAV."""

import csv
import datetime as dt
import json

import pytest

from trader import scoreboard as SB
from trader.broker import Fill, OrderState, SimBroker
from trader.data import ET
from trader.engine import Book, Engine, Pending

from test_engine import Always, spec
from test_execution_toolkit import make, ticks

DAY = dt.date(2026, 9, 21)
AT = lambda h, m: dt.datetime.combine(DAY, dt.time(h, m), ET)


class Offline(SimBroker):
    """The limit order filled at `filled_at`; the position was then sold at 101 at 10:30, all
    before the engine saw any of it. Its sell is in the order history only after 10:30."""

    def __init__(self, filled_at):
        super().__init__(250.0)
        self.filled_at, self.since = filled_at, []

    def order_state(self, order_id):
        return OrderState("filled", 0.5, 100.0, self.filled_at)

    def get_positions(self):
        return {}

    def exit_fill_since(self, symbol, since, now, strict=False):
        self.since.append(since)
        return Fill(symbol, "sell", 0.5, 101.0, AT(10, 30), "x1") if since <= AT(10, 30) else None


def run(tmp_path, session, filled_at):
    br = Offline(filled_at)
    book, eng = make(tmp_path, [spec()], Always(entry="WAIT"), br)
    book.pending["SPY"] = Pending(classifier="t", order_id="o1", limit=100.0, qty=0.5, reserved=50.0,
                                  placed=AT(9, 40), expires=AT(15, 30), stop_pct=0.5, target_pct=1.0,
                                  params=Engine._entry_params(spec()), client_id="c1")
    book.save_pending()
    alerts = []
    eng.alert = lambda lvl, msg: alerts.append(msg)
    eng.start_day(DAY, {})
    ticks(eng, session(path=[100.0] * 390), 90, 93)  # 11:01 to 11:03: adopted, then found gone
    with (tmp_path / "sim" / "trades.csv").open() as f:
        return br, list(csv.DictReader(f)), alerts


def test_an_offline_fill_and_close_books_the_real_exit(tmp_path, session):
    br, rows, alerts = run(tmp_path, session, AT(10, 0))
    buy, sell = rows
    assert buy["side"] == "buy" and buy["time"].startswith("2026-09-21T10:00")  # when it really filled
    assert sell["side"] == "sell" and sell["reason"].startswith("closed outside engine")
    assert float(sell["price"]) == 101.0 and float(sell["pnl_pct"]) == pytest.approx(1.0)  # real, so evidence
    assert br.since[0] == AT(10, 0)
    assert not any("estimated" in a for a in alerts)
    (t,) = SB.closed_trades(rows)  # the rows pair in time order
    assert t["net_pct"] == pytest.approx(1.0 - 2 * SB.SLIPPAGE_PER_SIDE_PCT)


def test_a_fill_seen_promptly_is_booked_as_before(tmp_path, session):
    br, rows, _ = run(tmp_path, session, AT(11, 0))  # filled a minute before the 11:01 poll
    buy = rows[0]
    assert buy["time"].startswith("2026-09-21T11:01")  # the adoption time, unchanged
    assert br.since[0] == AT(11, 1)  # looked up from the entry's own time, unchanged


def test_first_nav_units_are_saved_so_a_restart_keeps_the_days_pnl(tmp_path):
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")  # no nav.json: units 0, as after a rebase
    Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path).start_day(DAY, {})
    saved = json.loads((tmp_path / "sim" / "nav.json").read_text())
    assert saved["units"] == 250.0
    # The runner restarts later that session with equity up to 260.
    again = Book("sim", SimBroker(260.0), tmp_path / "sim")
    Engine([spec()], {"live": again, "shadow": again}, Always(), {"SPY"}, tmp_path).start_day(DAY, {})
    assert again.nav.nav_per_unit == pytest.approx(1.04)  # not reset to 1.0 at the restart


def test_existing_units_are_not_saved_mid_session(tmp_path):
    d = tmp_path / "sim"
    d.mkdir()
    before = json.dumps({"units": 200.0, "hwm": 1.3, "last_equity": 250.0})
    (d / "nav.json").write_text(before)
    book = Book("sim", SimBroker(260.0), d)
    Engine([spec()], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path).start_day(DAY, {})
    assert (d / "nav.json").read_text() == before  # saved at the close, as before
    assert book.nav.nav_per_unit == pytest.approx(1.3)
