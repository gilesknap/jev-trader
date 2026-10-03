"""#47, #62: positions held from before startup, tracked or not, are closed and priced in-session."""

import datetime as dt
from types import SimpleNamespace as NS

import pandas as pd

from test_engine import Always, spec
from test_orders import FakeClient, broker
from trader.broker import OrderState, Position, SimBroker
from trader.data import ET
from trader.engine import Book, Engine, Entry, Pending
from trader.runner import STREAM_SYMBOL_LIMIT, session_symbols

DAY = dt.date(2026, 9, 21)


class Broker(SimBroker):
    def __init__(self):
        super().__init__(250.0)
        self.cancelled: list[set] = []
        self.fail_sells = 0
        self.sold: list[str] = []

    def cancel_orders(self, symbols):
        self.cancelled.append(set(symbols))

    def sell_all(self, *a, **kw):
        if self.fail_sells:
            self.fail_sells -= 1
            raise RuntimeError("order 1 canceled with nothing filled")
        self.sold.append(a[0])
        return super().sell_all(*a, **kw)


def _engine(tmp_path, specs=(), broker_name="sim"):
    alerts = []
    br = Broker()
    br.name = broker_name
    book = Book("paper", br, tmp_path / "paper")
    eng = Engine(
        list(specs),
        {"live": book, "shadow": book},
        Always(),
        {"SPY", "QQQ", "XLV"},
        tmp_path,
        alert=lambda lvl, msg: alerts.append(msg),
    )
    eng.start_day(DAY, {})
    return eng, book, alerts


def _bars(session, now, **paths):
    return {s: session(path=p).loc[lambda d: d.index < now] for s, p in paths.items()}


def _at(hh, mm):
    return dt.datetime.combine(DAY, dt.time(hh, mm), ET)


def test_first_tick_closes_orphan_at_market_and_leaves_tracked_positions_alone(tmp_path, session):
    eng, book, alerts = _engine(tmp_path)
    br = book.broker
    br.positions = {"SPY": Position("SPY", 0.5, 100.0), "QQQ": Position("QQQ", 0.2, 400.0)}
    book.entries["SPY"] = Entry("gone", 0.5, 100.0, 90.0, 120.0, _at(9, 0))
    now = _at(9, 31)
    eng.tick(now, _bars(session, now, SPY=[100.0] * 390, QQQ=[410.0] * 390), 389)
    assert "QQQ" not in br.positions and br.cancelled == [{"QQQ"}]  # only the orphan's own orders
    assert abs(br.cash - (250.0 + 0.2 * 410.0 * (1 - 0.0005))) < 1e-6  # at the market, not its cost
    assert "SPY" in br.positions and "SPY" in book.entries
    assert any("closed untracked position QQQ" in m for m in alerts)
    assert not (book.dir / "trades.csv").exists()  # untracked: there is no trade to record


def test_a_resting_limit_entry_is_not_an_orphan(tmp_path):
    eng, book, _ = _engine(tmp_path)
    now = _at(10, 0)
    book.broker.positions = {"QQQ": Position("QQQ", 0.1, 400.0)}  # partly filled, still resting
    book.broker.orders["o1"] = {"symbol": "QQQ", "qty": 0.2, "limit": 400.0, "placed": now, "state": OrderState("new")}
    book.pending["QQQ"] = Pending("t", "o1", 400.0, 0.2, 80.0, now, now + dt.timedelta(minutes=5), 0.5, 1.0, {}, "c")
    eng.tick(now + dt.timedelta(minutes=1), {}, 360)
    assert "QQQ" in book.broker.positions and book.broker.cancelled == []


def test_failed_orphan_close_alerts_and_retries_next_minute(tmp_path):
    eng, book, alerts = _engine(tmp_path)
    br = book.broker
    br.positions = {"QQQ": Position("QQQ", 0.2, 400.0)}
    br.fail_sells = 1
    eng.tick(_at(9, 31), {}, 389)  # doesn't raise
    assert "QQQ" in br.positions and any("could not close untracked position QQQ" in m for m in alerts)
    eng.tick(_at(9, 32), {}, 388)
    assert "QQQ" not in br.positions


def test_tracked_stops_run_before_orphans_are_closed(tmp_path, session):
    eng, book, _ = _engine(tmp_path)
    br = book.broker
    br.positions = {"QQQ": Position("QQQ", 0.2, 400.0), "XLV": Position("XLV", 0.5, 100.0)}
    book.entries["XLV"] = Entry("gone", 0.5, 100.0, 99.0, 110.0, _at(9, 40))
    now = _at(10, 30)
    eng.tick(now, _bars(session, now, XLV=[100.0] * 45 + [98.0] * 345), 330)
    assert br.sold == ["XLV", "QQQ"]


def test_every_orphan_close_is_alerted(tmp_path):
    eng, book, alerts = _engine(tmp_path)
    for minute in (31, 32):  # closed, then bought again by hand
        book.broker.positions = {"QQQ": Position("QQQ", 0.2, 400.0)}
        eng.tick(_at(9, minute), {}, 360)
    assert sum("closed untracked position QQQ" in m for m in alerts) == 2


def test_session_symbols_cover_held_resting_and_unreadable_books(tmp_path):
    class Down(SimBroker):
        def get_positions(self):
            raise ConnectionError("alpaca blip")

    held = Book("paper", SimBroker(250.0), tmp_path / "paper")
    held.broker.positions = {"ZZZZ": Position("ZZZZ", 1.0, 10.0)}  # not in the universe: still streamed
    held.entries["XLV"] = Entry("removed", 0.5, 100.0, 99.0, 101.0, _at(9, 40))
    down = Book("live", Down(250.0), tmp_path / "live")
    down.pending["AAPL"] = Pending("t", "o", 200.0, 0.1, 20.0, _at(9, 40), _at(9, 45), 0.5, 1.0, {}, "c")
    alerts = []
    syms = session_symbols([spec(symbols=["QQQ"])], [held, down], alert=lambda lvl, msg: alerts.append(msg))
    assert syms == (["QQQ", "SPY"], ["AAPL", "XLV", "ZZZZ"])  # tracked extras first
    assert any("[live] could not read positions at startup" in m for m in alerts)


def test_session_symbols_stay_within_the_stream_limit(tmp_path):
    # One subscribe over the limit is refused whole: the stream would get no symbols at all.
    book = Book("paper", SimBroker(250.0), tmp_path / "paper")
    book.broker.positions = {"ZZZZ": Position("ZZZZ", 1.0, 10.0)}
    book.entries["XLV"] = Entry("removed", 0.5, 100.0, 99.0, 101.0, _at(9, 40))
    names = [f"S{i:02d}" for i in range(STREAM_SYMBOL_LIMIT - 2)]
    alerts = []
    base, extra = session_symbols([spec(symbols=names)], [book], alert=lambda lvl, msg: alerts.append(msg))
    assert len(base) == STREAM_SYMBOL_LIMIT - 1 and extra == ["XLV"]  # the tracked one gets the room
    assert any("['ZZZZ'] left out of market data" in m for m in alerts)
    base, extra = session_symbols([spec(symbols=names + ["QQQ"])], [book], alert=lambda lvl, msg: alerts.append(msg))
    assert len(base) == STREAM_SYMBOL_LIMIT and extra == []


def test_non_equity_holdings_are_neither_streamed_nor_fetched(tmp_path):
    c = FakeClient()
    c.get_all_positions = lambda: [
        NS(symbol="BTCUSD", qty=0.001, avg_entry_price=60000.0, asset_class=NS(value="crypto")),
        NS(symbol="BTC/USD", qty=0.001, avg_entry_price=60000.0),
        NS(symbol="AMD", qty=1.0, avg_entry_price=150.0, asset_class=NS(value="us_equity")),
    ]
    book = Book("paper", broker(c), tmp_path / "paper")
    alerts = []
    assert session_symbols([], [book], alert=lambda lvl, msg: alerts.append(msg)) == (["SPY"], ["AMD"])
    assert any("non-equity ['BTC/USD', 'BTCUSD']" in m for m in alerts)


def test_removed_classifier_position_keeps_its_stop_without_a_server_stop(tmp_path, session):
    # #62 acceptance: restart holding XLV after its classifier was removed; its bars now arrive.
    eng, book, _ = _engine(tmp_path, [spec()])
    book.broker.positions = {"XLV": Position("XLV", 0.5, 100.0)}
    book.entries["XLV"] = Entry("removed", 0.5, 100.0, 99.0, 101.0, _at(9, 40))  # no stop_id
    now = _at(10, 30)
    eng.tick(now, _bars(session, now, XLV=[100.0] * 45 + [98.0] * 345), 330)
    assert "XLV" not in book.entries and "XLV" not in book.broker.positions
    assert pd.read_csv(book.dir / "trades.csv").reason.iloc[-1] == "stop"


def test_time_stop_applies_before_the_first_bar_at_the_last_price_seen(tmp_path):
    eng, book, _ = _engine(tmp_path)
    book.broker.positions = {"XLV": Position("XLV", 0.5, 100.0)}
    book.broker.last["XLV"] = 104.0
    book.entries["XLV"] = Entry("removed", 0.5, 100.0, 99.0, 110.0, _at(9, 31), max_hold_min=30)
    eng.tick(_at(10, 0), {}, 360)
    assert "XLV" in book.entries  # 29 minutes: not yet
    eng.tick(_at(10, 2), {}, 358)
    trade = pd.read_csv(book.dir / "trades.csv").iloc[-1]
    assert "XLV" not in book.entries and trade.reason == "time stop"
    assert abs(trade.price - 104.0 * (1 - 0.0005)) < 1e-3


def test_time_stop_without_any_price_waits_in_a_sim_book_but_sells_at_the_broker(tmp_path):
    for name, sold in (("sim", False), ("alpaca-paper", True)):
        eng, book, _ = _engine(tmp_path / name, broker_name=name)
        book.broker.positions = {"XLV": Position("XLV", 0.5, 100.0)}
        book.entries["XLV"] = Entry("removed", 0.5, 100.0, 99.0, 110.0, _at(9, 31), max_hold_min=30)
        eng.tick(_at(10, 2), {}, 358)
        assert ("XLV" not in book.entries) == sold  # a sim fill at the entry price would be invented P&L
