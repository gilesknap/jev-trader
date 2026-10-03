"""Brokers: a simulated one for replay, and Alpaca (paper or live).

Alpaca rejects bracket orders on fractional quantities, and at this account size
almost every position is fractional. So entries are notional market orders; the
engine then places a server-side stop where Alpaca accepts one, and always
enforces stop and target itself on every bar as well.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from trader import config

SLIPPAGE = 0.0005  # 0.05% per side, applied to simulated fills


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    asset_class: str = "us_equity"


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    time: dt.datetime
    # An exit leg (#61): the broker order it came from, so the engine books each order's fill once
    # however often a retry or a restart reports it ("" for a sim fill or a blend of several).
    order_id: str = ""
    estimated: bool = False  # the broker never reported this fill's price: `price` is a reference, not evidence
    legs: list = field(default_factory=list)  # a blend of several orders: each one, with its own id


@dataclass
class OrderState:
    status: str  # new | partially_filled | filled | canceled | expired | rejected ...
    filled_qty: float = 0.0
    price: float = 0.0  # average fill price (0 until something fills)
    filled_at: dt.datetime | None = None


TERMINAL = ("filled", "canceled", "expired", "rejected")


class NotFilled(RuntimeError):
    """The order is final (canceled, expired, rejected) with nothing filled: a definitive no."""


class PartialExit(RuntimeError):
    """Some of a position sold, and the rest is (or may be) still held (#61): `fill` is what sold, by order."""

    def __init__(self, fill: Fill, why):
        super().__init__(f"{why}; {fill.qty:g} sold, the rest may still be held")
        self.fill = fill


class OrderPending(Exception):
    """An order was accepted but its outcome couldn't be read (#60): the engine tracks it by id."""

    def __init__(self, order_id: str, why: Exception):
        super().__init__(f"order {order_id} accepted, outcome unknown: {why}")
        self.order_id = order_id


def _fill_time(o, default):
    """The order's fill time in US/Eastern (like every other trade row), else `default`."""
    from zoneinfo import ZoneInfo

    at = getattr(o, "filled_at", None)
    return at.astimezone(ZoneInfo("America/New_York")) if isinstance(at, dt.datetime) and at.tzinfo else default


class SimBroker:
    """Fills at the given reference price +/- slippage. Settles instantly."""

    name = "sim"

    def __init__(self, cash: float):
        self.cash = cash
        self.positions: dict[str, Position] = {}
        self.last: dict[str, float] = {}
        self.orders: dict[str, dict] = {}  # resting limit buys

    def update_prices(self, prices: dict[str, float]) -> None:
        self.last.update(prices)

    def equity(self) -> float:
        return self.cash + sum(p.qty * self.last.get(p.symbol, p.avg_price) for p in self.positions.values())

    def settled_cash(self) -> float:
        return self.cash

    def get_positions(self) -> dict[str, Position]:
        return dict(self.positions)

    def buy_notional(self, symbol: str, notional: float, ref_price: float, now, client_id: str) -> Fill:
        price = ref_price * (1 + SLIPPAGE)
        qty = notional / price
        self.cash -= notional
        self.positions[symbol] = Position(symbol, qty, price)
        return Fill(symbol, "buy", qty, price, now)

    def place_stop(self, symbol: str, qty: float, stop_price: float, client_id: str) -> str | None:
        return None  # engine-enforced in simulation

    def move_stop(self, stop_id, symbol, qty, new_stop, client_id) -> str | None:
        return None

    def buy_limit(self, symbol: str, qty: float, limit: float, now, client_id: str) -> str:
        oid = f"sim-{client_id}"
        self.orders[oid] = {"symbol": symbol, "qty": qty, "limit": limit, "placed": now, "state": OrderState("new")}
        return oid

    def update_bars(self, bars) -> None:
        """Fill resting limit buys on bars that started at or after placement and traded
        strictly below the limit (a touch isn't a fill), at the better of open and limit."""
        import pandas as pd

        for o in self.orders.values():
            if o["state"].status != "new" or o["symbol"] not in bars:
                continue
            b = bars[o["symbol"]]
            hit = b[(b.index >= pd.Timestamp(o["placed"])) & (b.low < o["limit"])]
            if len(hit):
                px = min(float(hit.open.iloc[0]), o["limit"])
                self.cash -= o["qty"] * px
                self.positions[o["symbol"]] = Position(o["symbol"], o["qty"], px)
                o["state"] = OrderState("filled", o["qty"], px, hit.index[0].to_pydatetime())

    def order_state(self, order_id: str) -> OrderState:
        return self.orders[order_id]["state"]

    def order_id_for(self, client_id: str, symbol: str, since) -> str | None:
        oid = f"sim-{client_id}"  # market buys fill at once, so only resting limits have ids
        return oid if oid in self.orders else None

    def cancel_order(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if o["state"].status == "new":
            o["state"] = OrderState("canceled")
        return o["state"]

    def sell_qty(self, symbol: str, qty: float, ref_price: float, now, client_id: str, stop_id: str | None = None) -> Fill | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        qty = min(qty, pos.qty)
        price = ref_price * (1 - SLIPPAGE)
        self.cash += qty * price
        pos.qty -= qty
        return Fill(symbol, "sell", qty, price, now)

    def sell_all(self, symbol: str, ref_price: float, now, client_id: str, stop_id: str | None = None) -> Fill | None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        price = ref_price * (1 - SLIPPAGE)
        self.cash += pos.qty * price
        return Fill(symbol, "sell", pos.qty, price, now)

    def stop_fill(self, stop_id: str | None, now, strict: bool = False) -> Fill | None:
        return None

    def exit_fill_since(self, symbol: str, since: dt.datetime, now, strict: bool = False) -> Fill | None:
        return None

    def flatten_all(self, now=None) -> dict[str, Position]:
        for sym in list(self.positions):
            self.sell_all(sym, self.last.get(sym, self.positions[sym].avg_price), now, "flatten")
        return {}

    def cancel_all(self) -> None:
        pass

    def cancel_orders(self, symbols: set[str]) -> None:
        pass


SIM_START_CASH = float(config.SETTINGS.capital.sim_cash)  # config.yaml: each `mode: sim` classifier's own simulated account starts here


class PersistentSimBroker(SimBroker):
    """A SimBroker for one `mode: sim` classifier in the live runner: fed by the same live bars
    as paper, fills at the bar close +/- SLIPPAGE (limits as in replay), and saved to `path`
    after every change, so a restart mid-session finds the positions and resting orders the
    engine's entries.json and pending.json refer to. Cash carries over from day to day."""

    def __init__(self, path, start_cash: float = SIM_START_CASH):
        import json
        from pathlib import Path

        super().__init__(start_cash)
        self.path = Path(path)
        if self.path.exists():
            d = json.loads(self.path.read_text())
            self.cash = float(d["cash"])
            self.positions = {s: Position(s, float(q), float(px)) for s, (q, px) in d.get("positions", {}).items()}
            for oid, o in d.get("orders", {}).items():
                st = o["state"]
                self.orders[oid] = {
                    "symbol": o["symbol"], "qty": float(o["qty"]), "limit": float(o["limit"]),
                    "placed": dt.datetime.fromisoformat(o["placed"]),
                    "state": OrderState(st["status"], float(st["filled_qty"]), float(st["price"]),
                                        dt.datetime.fromisoformat(st["filled_at"]) if st.get("filled_at") else None)}

    def _save(self) -> None:
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Only today's orders matter after a restart; older ones are settled one way or the other.
        today = max((o["placed"].date() for o in self.orders.values()), default=None)
        data = {
            "cash": self.cash,
            "positions": {s: [p.qty, p.avg_price] for s, p in self.positions.items()},
            "orders": {oid: {"symbol": o["symbol"], "qty": o["qty"], "limit": o["limit"], "placed": o["placed"].isoformat(),
                             "state": {"status": o["state"].status, "filled_qty": o["state"].filled_qty, "price": o["state"].price,
                                       "filled_at": o["state"].filled_at.isoformat() if o["state"].filled_at else None}}
                       for oid, o in self.orders.items() if o["placed"].date() == today},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def buy_notional(self, *a, **kw):
        f = super().buy_notional(*a, **kw)
        self._save()
        return f

    def buy_limit(self, *a, **kw):
        oid = super().buy_limit(*a, **kw)
        self._save()
        return oid

    def update_bars(self, bars) -> None:
        before = {oid: o["state"].status for oid, o in self.orders.items()}
        super().update_bars(bars)
        if any(o["state"].status != before.get(oid) for oid, o in self.orders.items()):
            self._save()

    def order_state(self, order_id: str) -> OrderState:
        # Only today's orders are kept (see _save): anything older is long settled.
        return self.orders[order_id]["state"] if order_id in self.orders else OrderState("canceled")

    def cancel_order(self, order_id: str) -> OrderState:
        if order_id not in self.orders:
            return OrderState("canceled")
        st = super().cancel_order(order_id)
        self._save()
        return st

    def sell_qty(self, *a, **kw):
        f = super().sell_qty(*a, **kw)
        self._save()
        return f

    def sell_all(self, *a, **kw):
        f = super().sell_all(*a, **kw)
        self._save()
        return f


class AlpacaBroker:
    def __init__(self, key: str, secret: str, paper: bool):
        from alpaca.trading.client import TradingClient

        self.name = "alpaca-paper" if paper else "alpaca-live"
        self.client = TradingClient(key, secret, paper=paper)
        self.last: dict[str, float] = {}
        self.last_flatten_error: str | None = None
        # alpaca-py sets no HTTP timeout; a hung connection would stall the engine indefinitely.
        sess = getattr(self.client, "_session", None)
        if sess is not None:
            orig = sess.request
            sess.request = lambda method, url, **kw: orig(method, url, **({"timeout": 15} | kw))

    def update_prices(self, prices: dict[str, float]) -> None:
        self.last.update(prices)

    def account(self) -> dict:
        # Raw JSON rather than alpaca-py's model: the model rejects account statuses it
        # doesn't know (e.g. ACCOUNT_CLOSED_PENDING), which would crash a live session.
        return self.client.get("/account")

    def equity(self) -> float:
        return float(self.account()["equity"])

    def settlement_snapshot(self) -> dict:
        a = self.account()
        return {k: a.get(k) for k in ("cash", "non_marginable_buying_power", "buying_power", "multiplier")}

    def settled_cash(self) -> float:
        a = self.account()
        # Cash accounts: only settled funds avoid good-faith violations.
        vals = [float(a[k]) for k in ("cash", "non_marginable_buying_power") if a.get(k) is not None]
        return min(vals) if vals else 0.0

    def get_positions(self) -> dict[str, Position]:
        def cls(p):  # alpaca-py's AssetClass enum: us_equity, crypto, ...
            a = getattr(p, "asset_class", None)
            return str(getattr(a, "value", a) or "us_equity")

        return {
            p.symbol: Position(p.symbol, float(p.qty), float(p.avg_entry_price), cls(p))
            for p in self.client.get_all_positions()
        }

    # ---- order helpers --------------------------------------------------------

    @staticmethod
    def _status(o) -> str:
        return str(getattr(o.status, "value", o.status)).lower()

    def _wait_terminal(self, order_id: str, timeout: float):
        """Poll until the order is filled/canceled/expired/rejected; returns the last order seen."""
        import time

        t0 = time.time()
        o = self.client.get_order_by_id(order_id)
        while self._status(o) not in TERMINAL and time.time() - t0 < timeout:
            time.sleep(0.5)
            o = self.client.get_order_by_id(order_id)
        return o

    def _settle(self, order_id: str, timeout: float = 20.0) -> tuple[float, float]:
        """Wait for a market order; returns (filled_qty, avg_price). On timeout the order is
        cancelled and any partial fill returned. Raises only when nothing was filled.
        Alpaca can report `filled` before `filled_avg_price` is populated (0/None), so the
        price is re-polled briefly; if it still isn't reported, it is 0: unknown, never a guess."""
        import time

        o = self._wait_terminal(order_id, timeout)
        if self._status(o) != "filled":
            try:
                self.client.cancel_order_by_id(order_id)
            except Exception:
                pass
            o = self._wait_terminal(order_id, 5.0)
        qty = float(o.filled_qty or 0)
        if qty <= 0:
            final = self._status(o) in TERMINAL  # else it could still fill: the caller must follow it up
            raise (NotFilled if final else RuntimeError)(f"order {order_id} {self._status(o)} with nothing filled")
        for _ in range(6):
            if float(o.filled_avg_price or 0) > 0:
                return qty, float(o.filled_avg_price)
            time.sleep(0.5)
            o = self.client.get_order_by_id(order_id)
            qty = max(qty, float(o.filled_qty or 0))
        return qty, 0.0

    @staticmethod
    def _is_not_found(e: Exception) -> bool:
        return getattr(e, "status_code", None) == 404 or "position not found" in str(e).lower()

    @staticmethod
    def _is_held(e: Exception) -> bool:
        """Alpaca's 403 for a close whose shares are held by open orders: its body reads
        {"code":40310000,"message":"insufficient qty available for order ...",...}."""
        return getattr(e, "status_code", None) == 403 and "insufficient qty" in str(e).lower()

    # ---- orders ---------------------------------------------------------------

    def buy_notional(self, symbol, notional, ref_price, now, client_id) -> Fill:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        o = self.client.submit_order(
            MarketOrderRequest(
                symbol=symbol, notional=round(notional, 2), side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, client_order_id=client_id,
            )
        )
        try:
            qty, price = self._settle(str(o.id))  # partial fills come back with their real qty
        except NotFilled:
            raise  # final, nothing filled: nothing to follow up
        except Exception as e:  # accepted: the engine follows it up by id, never forgets it (#60)
            raise OrderPending(str(o.id), e) from e
        # price 0: Alpaca hasn't reported it; the engine prices it (_fill_price), ref_price only as a last-resort guess
        return Fill(symbol, "buy", qty, price, now, str(o.id), price <= 0)

    def order_id_for(self, client_id: str, symbol: str, since: dt.datetime) -> str | None:
        """The id of the order this runner sent as `client_id` at `since` (#60), or None if Alpaca
        has no such order: a 404, or an older order with that client id (so ours was refused as
        a duplicate). Raises when it can't tell."""
        try:
            o = self.client.get_order_by_client_id(client_id)
        except Exception as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise
        at = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
        if o.symbol != symbol or (isinstance(at, dt.datetime) and at.tzinfo and at < since - dt.timedelta(minutes=5)):
            return None
        return str(o.id)

    def place_stop(self, symbol, qty, stop_price, client_id) -> str | None:
        """Server-side protective stop; returns None if Alpaca rejects it (engine still enforces)."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        try:
            o = self.client.submit_order(
                StopOrderRequest(
                    symbol=symbol, qty=qty, side=OrderSide.SELL, stop_price=round(stop_price, 2),
                    time_in_force=TimeInForce.DAY, client_order_id=client_id,
                )
            )
            return str(o.id)
        except Exception:
            return None

    def move_stop(self, stop_id, symbol, qty, new_stop, client_id) -> str | None:
        """Raise a server-side stop (trailing). Replace in place if Alpaca allows it, else
        cancel and re-place. Returns the id now protecting the position (the old id if the
        old stop fired meanwhile, so the engine records that fill), or None if none is."""
        from alpaca.trading.requests import ReplaceOrderRequest

        try:
            return str(self.client.replace_order_by_id(stop_id, ReplaceOrderRequest(stop_price=round(new_stop, 2))).id)
        except Exception:
            pass
        try:
            self.client.cancel_order_by_id(stop_id)
        except Exception:
            pass
        old = self._wait_terminal(stop_id, 3.0)
        if float(old.filled_qty or 0) > 0:
            return stop_id
        return self.place_stop(symbol, qty, new_stop, client_id)

    def buy_limit(self, symbol, qty, limit, now, client_id) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        o = self.client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=round(limit, 2), client_order_id=client_id))
        return str(o.id)

    def order_state(self, order_id: str) -> OrderState:
        import time

        o = self.client.get_order_by_id(order_id)
        for _ in range(6):  # a fill can be reported before its average price is (see _settle)
            if not (float(o.filled_qty or 0) > 0 and float(o.filled_avg_price or 0) <= 0):
                break
            time.sleep(0.5)
            o = self.client.get_order_by_id(order_id)
        at = getattr(o, "filled_at", None)
        return OrderState(self._status(o), float(o.filled_qty or 0), float(o.filled_avg_price or 0),
                          at if isinstance(at, dt.datetime) else None)

    def cancel_order(self, order_id: str) -> OrderState:
        """Cancel and wait for the final state (it may have filled, fully or partly, meanwhile)."""
        try:
            self.client.cancel_order_by_id(order_id)
        except Exception:
            pass
        self._wait_terminal(order_id, 5.0)
        return self.order_state(order_id)

    def sell_qty(self, symbol, qty, ref_price, now, client_id, stop_id: str | None = None) -> Fill | None:
        """Sell part of a position (scale-out). The resting stop holds the whole qty, so it is
        cancelled first; the caller re-places it for the remainder. Returns None if that stop
        had already (partly) filled: the engine then records it through the normal paths."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if stop_id:
            try:
                self.client.cancel_order_by_id(stop_id)
            except Exception:
                pass
            if float(self._wait_terminal(stop_id, 3.0).filled_qty or 0) > 0:
                return None
        o = self.client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=client_id))
        q, px = self._settle(str(o.id))
        return self._leg(symbol, str(o.id), q, px, ref_price, now)

    def stop_fill(self, stop_id: str | None, now, strict: bool = False) -> Fill | None:
        """The fill of a server-side stop, if it filled (used when a position vanished). `strict`:
        a failed lookup raises, rather than looking like "not filled" (#131)."""
        if not stop_id:
            return None
        try:
            o = self.client.get_order_by_id(stop_id)
            qty, px = float(o.filled_qty or 0), float(o.filled_avg_price or 0)
            if qty > 0 and px <= 0:  # a fill can be reported before its price (see _settle): re-poll briefly
                st = self.order_state(stop_id)
                qty, px = st.filled_qty, st.price
        except Exception:
            if strict:
                raise
            return None
        if qty > 0 and px > 0:
            return Fill(o.symbol, "sell", qty, px, _fill_time(o, now), stop_id)
        return None

    def exit_fill_since(self, symbol: str, since: dt.datetime, now, strict: bool = False) -> Fill | None:
        """The real exit of a position that closed without the engine (e.g. while the runner
        was down, or closed by hand): every filled sell of `symbol` after `since`, blended.
        `strict`: a failed lookup raises, rather than looking like "no sells" (#131)."""
        from alpaca.trading.enums import OrderSide, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            orders = self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide.SELL, after=since, limit=50))
        except Exception:
            if strict:
                raise
            return None
        filled = [o for o in orders if float(o.filled_qty or 0) > 0 and float(o.filled_avg_price or 0) > 0]
        if not filled:
            return None
        legs = [(float(o.filled_qty), float(o.filled_avg_price)) for o in filled]
        qty = sum(q for q, _ in legs)
        times = [t for t in (_fill_time(o, None) for o in filled) if t is not None]
        at = max(times) if times else now  # when it actually closed, maybe an earlier session
        return Fill(symbol, "sell", qty, sum(q * p for q, p in legs) / qty, at)

    @staticmethod
    def _leg(symbol, order_id, qty, px, ref_price, now) -> Fill:
        """One sell order's cumulative fill; at `ref_price`, marked estimated, if its price was never reported."""
        return Fill(symbol, "sell", qty, px if px > 0 else ref_price, now, order_id, px <= 0)

    @staticmethod
    def _blend(symbol, legs: list[Fill], now) -> Fill:
        qty = sum(f.qty for f in legs)
        return Fill(symbol, "sell", qty, sum(f.qty * f.price for f in legs) / qty, now,
                    estimated=any(f.estimated for f in legs), legs=legs)

    def _held(self, symbol) -> bool:
        if symbol in self.get_positions():
            import time

            time.sleep(1.0)  # the positions endpoint can lag a just-filled close; look once more
        return symbol in self.get_positions()

    def sell_all(self, symbol, ref_price, now, client_id, stop_id: str | None = None) -> Fill | None:
        """Close the whole position. Returns None only if there is no position and nothing sold
        (e.g. closed by something else). The fill lists every order that sold (the stop, if it
        fired, and the close), each with its id, so the engine books each one once (#61). A close
        refused because open sells hold the shares cancels them and is retried once (#127). If
        shares are still held afterwards, PartialExit carries what did sell; any other failure
        with nothing sold raises. Either way the engine keeps tracking the rest and retries."""
        legs: list[Fill] = []
        try:
            if stop_id:
                try:
                    self.client.cancel_order_by_id(stop_id)
                except Exception:
                    pass
                stop = self._wait_terminal(stop_id, 3.0)  # the held qty is released only once canceled
                status = self._status(stop)
                if float(stop.filled_qty or 0) > 0:
                    # Re-read: a fill can be reported a poll or two before its price (see _settle), and
                    # a leg booked as estimated can't be repaired later (its qty is already booked).
                    st = self.order_state(stop_id)
                    status = st.status
                    legs.append(self._leg(symbol, stop_id, st.filled_qty, st.price, ref_price, now))
                # A stop placed before the position grew (#59) can fill and leave shares: close those too.
                if status == "filled" and not self._held(symbol):
                    return self._blend(symbol, legs, now)
            for retry in (False, True):
                try:
                    o = self.client.close_position(symbol)
                    break
                except Exception as e:
                    if self._is_not_found(e):
                        return self._blend(symbol, legs, now) if legs else None
                    # Open sells the engine doesn't know of (a stop placed but its answer lost, #127)
                    # hold the shares: cancel them, and retry once.
                    if retry or not self._is_held(e) or not self._release_sells(symbol, legs, ref_price, now):
                        raise
            qty, px = self._settle(str(o.id))
            legs.append(self._leg(symbol, str(o.id), qty, px, ref_price, now))
            held = self._held(symbol)
        except Exception as e:
            if legs:  # what did sell is still reported, so the engine books it
                raise PartialExit(self._blend(symbol, legs, now), e) from e
            raise
        if held:  # the close was cancelled part-filled on timeout
            raise PartialExit(self._blend(symbol, legs, now), f"close of {symbol} only partly filled ({qty})")
        return self._blend(symbol, legs, now)

    def _release_sells(self, symbol, legs: list[Fill], ref_price, now) -> bool:
        """Cancel the symbol's open sell orders, never a buy (an entry may still be settling). What
        each sold before its cancel joins `legs`, by its order id, so the engine books it once.
        True if all of them are now final, so a close can have the shares."""
        from alpaca.trading.enums import OrderSide, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            orders = self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[symbol], side=OrderSide.SELL, limit=500))
        except Exception:
            return False
        final = True
        for o in orders:
            if str(getattr(o.side, "value", o.side)).lower() != "sell":
                continue
            oid = str(o.id)
            try:
                self.client.cancel_order_by_id(oid)
            except Exception:
                pass
            final = final and self._status(self._wait_terminal(oid, 3.0)) in TERMINAL
            st = self.order_state(oid)  # re-read: a fill can be reported before its price (see _settle)
            if st.filled_qty > 0:
                legs[:] = [f for f in legs if f.order_id != oid]  # the known stop, re-read
                legs.append(self._leg(symbol, oid, st.filled_qty, st.price, ref_price, now))
        return final

    def flatten_all(self, now=None, timeout: float = 20.0) -> dict[str, Position]:
        """Cancel every order and close every position, including ones the engine doesn't
        track. Returns whatever is still held after the timeout (should be empty)."""
        import time

        self.last_flatten_error = None
        try:
            self.client.close_all_positions(cancel_orders=True)
        except Exception as e:
            self.last_flatten_error = str(e)[:200]
        t0 = time.time()
        held = self.get_positions()
        while held and time.time() - t0 < timeout:
            time.sleep(1)
            held = self.get_positions()
        return held

    def cancel_all(self) -> None:
        self.client.cancel_orders()

    def cancel_orders(self, symbols: set[str]) -> None:
        """Cancel open orders for these symbols only (leaves other positions' stops alone)."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        if not symbols:
            return
        for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=sorted(symbols), limit=500)):
            try:
                self.client.cancel_order_by_id(str(o.id))
            except Exception:
                pass
            self._wait_terminal(str(o.id), 3.0)  # held qty is released only once canceled
