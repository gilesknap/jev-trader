"""Order handling (issues #14, #18): partial fills, timeouts, failed closes, EOD close-all."""

import datetime as dt
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from test_engine import Always, spec
from trader.broker import AlpacaBroker, SimBroker
from trader.data import ET
from trader.engine import Book, Engine


class APIError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg or f"http {code}")
        self.status_code = code


class FakeClient:
    """Scripted stand-in for alpaca TradingClient."""

    def __init__(self):
        self.orders = {}
        self.calls = []
        self.close_error: Exception | None = None
        self.positions = {}

    def get_order_by_id(self, oid):
        o = self.orders[oid]
        if o.get("script"):
            o.update(o["script"].pop(0))
        return NS(
            id=oid,
            symbol=o.get("symbol", "SPY"),
            status=NS(value=o["status"]),
            filled_qty=o.get("filled_qty", 0),
            filled_avg_price=o.get("price"),
        )

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel", oid))
        o = self.orders.get(oid)
        if o and o["status"] not in ("filled",):
            o["status"] = "canceled"

    def close_position(self, sym):
        self.calls.append(("close", sym))
        if self.close_error:
            raise self.close_error
        self.orders["close1"] = {"status": "filled", "filled_qty": 1.0, "price": 99.0}
        return NS(id="close1")

    def close_all_positions(self, cancel_orders=True):
        self.calls.append(("close_all", cancel_orders))
        self.positions.clear()

    def get_all_positions(self):
        return [NS(symbol=s, qty=q, avg_entry_price=100.0) for s, q in self.positions.items()]


def broker(client):
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.name, b.client, b.last = "alpaca-paper", client, {}
    return b


def test_partially_filled_is_not_filled_and_partial_is_kept_on_timeout():
    c = FakeClient()
    c.orders["o1"] = {"status": "partially_filled", "filled_qty": 0.4, "price": 100.0}
    qty, px = broker(c)._settle("o1", timeout=0.01)
    assert ("cancel", "o1") in c.calls  # timed-out order is cancelled, not left live
    assert qty == 0.4 and px == 100.0  # and the partial fill is returned, not dropped


def test_timeout_with_nothing_filled_raises_after_cancel():
    c = FakeClient()
    c.orders["o1"] = {"status": "new", "filled_qty": 0}
    with pytest.raises(RuntimeError):
        broker(c)._settle("o1", timeout=0.01)
    assert ("cancel", "o1") in c.calls


def test_close_only_treats_404_as_gone():
    c = FakeClient()
    c.close_error = APIError(404, "position not found")
    assert broker(c).sell_all("SPY", 100, None, "x") is None
    c.close_error = APIError(500)
    with pytest.raises(APIError):
        broker(c).sell_all("SPY", 100, None, "x")
    c.close_error = APIError(403, "insufficient qty available for order")
    with pytest.raises(APIError):
        broker(c).sell_all("SPY", 100, None, "x")


def test_stop_cancelled_before_close_and_filled_stop_is_used():
    c = FakeClient()
    c.orders["s1"] = {"status": "new", "filled_qty": 0}
    fill = broker(c).sell_all("SPY", 100, None, "x", stop_id="s1")
    assert fill is not None
    assert c.calls[:2] == [("cancel", "s1"), ("close", "SPY")] and fill.price == 99.0
    c = FakeClient()
    c.orders["s1"] = {"status": "filled", "filled_qty": 1.0, "price": 98.5}
    fill = broker(c).sell_all("SPY", 100, None, "x", stop_id="s1")
    assert fill is not None
    assert fill.price == 98.5 and ("close", "SPY") not in c.calls


class FlakySim(SimBroker):
    """Sim broker whose closes fail until `fails` is exhausted; tracks flatten_all calls."""

    def __init__(self, cash, fails=10**6):
        super().__init__(cash)
        self.fails, self.flatten_calls = fails, 0

    def sell_all(self, symbol, ref_price, now, client_id, stop_id=None):
        if self.fails > 0:
            self.fails -= 1
            raise RuntimeError("503 from broker")
        return super().sell_all(symbol, ref_price, now, client_id, stop_id)

    def flatten_all(self, now=None):
        self.flatten_calls += 1
        for sym in list(self.positions):
            SimBroker.sell_all(self, sym, self.last.get(sym, 100.0), now, "flatten")
        return {}


def run_day(tmp_path, bars, specs, br):
    book = Book("sim", br, tmp_path / "sim")
    eng = Engine(specs, {"live": book, "shadow": book}, Always(exit="EXIT"), {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    return book


def test_failed_close_keeps_tracking_and_eod_closes_everything(tmp_path, session):
    br = FlakySim(250.0)  # every targeted close fails
    book = run_day(tmp_path, session(path=[100.0] * 390), [spec()], br)
    assert br.flatten_calls >= 1
    assert not br.positions and not book.entries  # nothing survives the close
    trades = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert trades.reason.iloc[-1] == "eod flatten (close-all) (price estimated)"
    assert pd.isna(trades.pnl_pct.iloc[-1])  # a guessed price is not gate or promotion evidence (#60)


def test_untracked_position_is_flattened_at_close(tmp_path, session):
    br = FlakySim(250.0, fails=0)
    br.positions["SPY"] = __import__("trader.broker", fromlist=["Position"]).Position("SPY", 0.1, 100.0)
    run_day(tmp_path, session(path=[100.0] * 390), [spec(window=("09:35", "09:36"))], br)
    assert not br.positions and br.flatten_calls >= 1


def test_a_fill_whose_price_is_never_reported_is_unknown_not_the_reference_price(monkeypatch):
    """#101: a guessed price must never look like a real fill: `_settle` reports 0 (unknown)."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeClient()
    c.orders["o1"] = {"status": "filled", "filled_qty": 1.0, "price": None}
    assert broker(c)._settle("o1") == (1.0, 0.0)
    c.orders["o2"] = {"status": "filled", "filled_qty": 1.0, "price": "0", "script": [{}, {"price": 100.2}]}
    assert broker(c)._settle("o2") == (1.0, 100.2)
    monkeypatch.setattr(c, "submit_order", lambda req: NS(id="o1"))
    fill = broker(c).buy_notional("SPY", 100.0, 101.5, None, "t-SPY-10141000")
    assert (fill.qty, fill.price, fill.order_id, fill.estimated) == (1.0, 0.0, "o1", True)


def test_partial_close_keeps_position_tracked(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeClient()
    c.positions["SPY"] = 0.4  # still held after the close order was cancelled part-filled
    with pytest.raises(RuntimeError, match="partly filled"):
        broker(c).sell_all("SPY", 100, None, "x")


def test_partly_filled_stop_blends_into_exit_price(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeClient()
    c.orders["s1"] = {"status": "canceled", "filled_qty": 1.0, "price": 97.0}
    fill = broker(c).sell_all("SPY", 100, None, "x", stop_id="s1")
    assert fill is not None
    assert fill.qty == 2.0 and fill.price == 98.0  # 1 @ 97 (stop) + 1 @ 99 (close)


def test_orphan_close_failure_does_not_raise(tmp_path):
    class Stuck(SimBroker):
        def cancel_orders(self, symbols):
            self.cancelled = set(symbols)

        def sell_all(self, *a, **k):
            raise APIError(403, "insufficient qty available for order")

    br = Stuck(250.0)
    br.positions["SPY"] = __import__("trader.broker", fromlist=["Position"]).Position("SPY", 0.1, 100.0)
    book = Book("paper", br, tmp_path / "paper")
    alerts = []
    eng = Engine(
        [], {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path, alert=lambda lvl, msg: alerts.append(msg)
    )
    now = dt.datetime(2026, 9, 21, 9, 31, tzinfo=ET)
    eng.start_day(now.date(), {})
    eng.tick(now, {}, 389)  # must not raise
    assert br.cancelled == {"SPY"} and any("could not close untracked position SPY" in m for m in alerts)
