"""Execution toolkit (#38): trailing stop, time stop, limit entries, risk sizing, ATR stops, scale-out."""

import datetime as dt
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from test_engine import Always, run, spec
from test_orders import FakeClient, broker
from trader import golive
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import Book, Engine


def make(tmp_path, specs, decider=None, br=None):
    book = Book("sim", br or SimBroker(250.0), tmp_path / "sim")
    return book, Engine(specs, {"live": book, "shadow": book}, decider or Always(), {"SPY"}, tmp_path)


def ticks(eng, bars, start, stop):
    day = bars.index[0].date()
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[start:stop]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)


def trades(tmp_path):
    return pd.read_csv(tmp_path / "sim" / "trades.csv")


# ---- trailing stop -------------------------------------------------------------------


def test_trailing_stop_ratchets_up_and_exits_at_the_raised_stop(tmp_path, session):
    path = [100.0] * 10 + [100.0 + 0.1 * i for i in range(1, 21)] + [101.0] * 360  # up to 102, then back
    book, t = run(tmp_path, session(path=path), [spec(trail_pct=0.5, target_pct=5)], Always())
    last = t.iloc[-1]
    assert last.reason == "stop (raised)"
    high = 102.0 * 1.0002
    assert abs(last.price - high * (1 - 0.005) * (1 - 0.0005)) < 1e-3  # sold at the trail (less slippage)
    assert last.pnl > 0


def test_same_bar_high_does_not_raise_the_stop_its_low_is_tested_against(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng = make(tmp_path, [spec(trail_pct=0.3, target_pct=5)])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    e = book.entries["SPY"]
    stop0 = e.stop
    # One wide bar: high +0.8% (would trail the stop to ~100.5) and low -0.4% (above the initial 0.3% stop)
    i = 6
    bars.iloc[i, bars.columns.get_loc("high")] = 100.8
    bars.iloc[i, bars.columns.get_loc("low")] = 99.75
    ticks(eng, bars, 6, 7)
    assert "SPY" in book.entries and book.entries["SPY"].stop > stop0  # held through the bar; raised after it


def test_trail_never_lowers_the_stop(tmp_path, session):
    bars = session(path=[100.0] * 10 + [101.0] * 5 + [100.8] * 375)
    book, eng = make(tmp_path, [spec(trail_pct=2.0, target_pct=5, stop_pct=0.5)])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 30)
    e = book.entries["SPY"]
    assert e.stop == pytest.approx(e.init_stop)  # the 2% trail is below the 0.5% initial stop: unchanged


# ---- time stop -------------------------------------------------------------------------


def test_time_stop(tmp_path, session):
    book, t = run(tmp_path, session(path=[100.0] * 390), [spec(max_hold_min=30)], Always())
    assert list(t.reason) == ["ENTER", "time stop"]
    held = pd.Timestamp(t.time.iloc[1]) - pd.Timestamp(t.time.iloc[0])
    assert held == pd.Timedelta(minutes=30)


# ---- limit entries -----------------------------------------------------------------------

LIMIT = {"type": "limit", "offset_pct": 0.05, "expire_min": 5}


def test_limit_entry_fills_below_the_last_price(tmp_path, session):
    path = [100.0] * 10 + [99.9] * 380  # dips through the 99.95 limit at bar 10
    book, t = run(tmp_path, session(path=path), [spec(entry_order=LIMIT)], Always())
    assert t.side.iloc[0] == "buy" and t.price.iloc[0] == pytest.approx(99.95 * 1.0005)  # plus slippage
    assert t.time.iloc[0].startswith("2026-09-21T09:41")


def test_sim_limit_fill_pays_the_same_slippage_as_a_market_entry():
    """A limit entry saves the offset, never the cost of trading: the simulated fill, the cash
    and the position all carry the same 0.05% as a market entry, so neither style looks better
    in sim and replay just for the way it is simulated."""
    b = SimBroker(1000.0)
    t0 = dt.datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    oid = b.buy_limit("SPY", 2.0, 99.0, t0, "c")
    idx = pd.date_range(t0, periods=2, freq="1min")
    b.update_bars(
        {
            "SPY": pd.DataFrame(
                {
                    "open": [99.5, 98.5],
                    "high": [99.6, 98.6],
                    "low": [99.4, 98.0],
                    "close": [99.5, 98.5],
                    "volume": [1, 1],
                },
                index=idx,
            )
        }
    )
    st = b.order_state(oid)
    assert st.status == "filled" and st.price == pytest.approx(98.5 * 1.0005)  # gapped below: the open, plus slippage
    assert b.positions["SPY"].avg_price == pytest.approx(st.price)
    assert b.cash == pytest.approx(1000.0 - 2.0 * 98.5 * 1.0005)
    m = SimBroker(1000.0).buy_notional("SPY", 197.0, 98.5, t0, "m")
    assert m.price == pytest.approx(st.price)  # the same price as a market entry at that reference


def test_a_broker_limit_fill_is_still_booked_no_higher_than_its_limit(tmp_path, session):
    """Only the simulated broker's haircut may lift a limit fill above the limit: a real broker's
    fill reported above it (a rounding artefact) is booked at the limit, as before."""

    class Real(SimBroker):
        name = "fake-real"
        limit_slippage = 0.0

    path = [100.0] * 10 + [99.9] * 380  # dips through the 99.95 limit at bar 10
    book, t = run_with(tmp_path, session(path=path), [spec(entry_order=LIMIT)], Real(250.0), upto=20)
    assert t.side.iloc[0] == "buy" and t.price.iloc[0] == pytest.approx(99.95)


def test_a_filled_limit_drops_its_resting_note(tmp_path, session):
    path = [100.0] * 10 + [99.9] * 380  # dips through the 99.95 limit at bar 10
    bars = session(path=path)
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 5)
    st = eng.states[0].symbols["SPY"]
    assert st.status == "pending" and st.note.startswith("limit ") and st.note.endswith(" resting")
    ticks(eng, bars, 5, 13)
    assert st.status == "holding" and st.note == ""


def test_unfilled_limit_expires_releases_cash_and_is_not_a_trade(tmp_path, session):
    bars = session(path=[100.0] * 390)  # bar lows 99.98: never strictly below 99.95
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 5)  # places the limit at 09:35
    st = eng.states[0].symbols["SPY"]
    assert st.status == "pending" and book.buys_today == pytest.approx(book.pending["SPY"].reserved)
    eng.decider = Always(entry="WAIT")
    ticks(eng, bars, 5, 12)  # expires at 09:40
    assert not book.pending and st.status == "armed" and st.trades == 0 and book.buys_today == 0
    assert not (tmp_path / "sim" / "trades.csv").exists()


def test_limit_fill_bar_is_checked_for_the_stop(tmp_path, session):
    path = [100.0] * 10 + [99.0] + [99.9] * 379  # fills at 99.95 in a bar whose low breaks the 0.5% stop
    book, t = run(tmp_path, session(path=path), [spec(entry_order=LIMIT)], Always())
    assert list(t.reason[:2]) == ["ENTER", "stop"]


def test_pending_limit_survives_restart_and_fills_after_it(tmp_path, session):
    bars = session(path=[100.0] * 12 + [99.9] * 378)
    sim = SimBroker(250.0)
    book, eng = make(tmp_path, [spec(entry_order={**LIMIT, "expire_min": 30})], br=sim)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    assert "SPY" in book.pending
    book2, eng2 = make(tmp_path, [spec(entry_order={**LIMIT, "expire_min": 30})], br=sim)  # restart
    eng2.start_day(bars.index[0].date(), {})
    assert "SPY" in book2.pending and eng2.states[0].symbols["SPY"].status == "pending"
    ticks(eng2, bars, 6, 20)
    assert "SPY" in book2.entries and not book2.pending
    assert eng2.states[0].symbols["SPY"].status == "holding"


def test_stop_cancels_a_resting_limit(tmp_path, session):
    bars = session(path=[100.0] * 390)
    sim = SimBroker(250.0)
    book, eng = make(tmp_path, [spec(entry_order={**LIMIT, "expire_min": 30})], br=sim)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    oid = book.pending["SPY"].order_id
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": bars.index[0].date().isoformat()}))
    ticks(eng, bars, 6, 7)
    assert not book.pending and sim.orders[oid]["state"].status == "canceled" and book.buys_today == 0


def test_partial_limit_fill_at_expiry_keeps_the_filled_part(tmp_path, session):
    from trader.broker import OrderState

    class Partial(SimBroker):
        def update_bars(self, bars):
            pass

        def order_state(self, oid):
            return OrderState("partially_filled", 0.1, 99.95)

        def cancel_order(self, oid):
            self.positions["SPY"] = __import__("trader.broker", fromlist=["Position"]).Position("SPY", 0.1, 99.95)
            return OrderState("canceled", 0.1, 99.95)

    bars = session(path=[100.0] * 390)
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=Partial(250.0))
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 11)
    e = book.entries["SPY"]
    assert e.qty == 0.1 and e.price == 99.95 and book.buys_today == pytest.approx(0.1 * 99.95)


# ---- sizing and stops --------------------------------------------------------------------


def test_risk_sizing_uses_the_stop_distance_and_is_capped(tmp_path, session):
    _, t = run(
        tmp_path / "a", session(path=[100.0] * 390), [spec(risk_pct=0.05, stop_pct=0.5, size_fraction=0.25)], Always()
    )
    assert float(t.notional.iloc[0]) == pytest.approx(25.0, abs=0.02)  # 250 x 0.05% / 0.5%
    _, t = run(
        tmp_path / "b", session(path=[100.0] * 390), [spec(risk_pct=1.0, stop_pct=0.5, size_fraction=0.25)], Always()
    )
    assert float(t.notional.iloc[0]) == pytest.approx(62.5, abs=0.02)  # capped by size_fraction


def test_atr_stop_distance_is_capped_at_stop_pct(tmp_path, session):
    bars = session(path=[100.0] * 390)  # 1-min true range 0.04% -> 3 x ATR = 0.12%
    book, eng = make(tmp_path, [spec(window=("09:50", "15:30"), stop_atr_mult=3, stop_pct=0.5)])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 22)
    e = book.entries["SPY"]
    assert (1 - e.stop / e.price) * 100 == pytest.approx(0.12, abs=0.005)
    book2, eng2 = make(tmp_path / "wide", [spec(window=("09:50", "15:30"), stop_atr_mult=20, stop_pct=0.5)])
    eng2.start_day(bars.index[0].date(), {})
    ticks(eng2, bars, 0, 22)
    e = book2.entries["SPY"]
    assert (1 - e.stop / e.price) * 100 == pytest.approx(0.5)


# ---- scale out -----------------------------------------------------------------------------


def test_scale_out_banks_part_and_the_round_trip_is_one_trade(tmp_path, session):
    path = [100.0] * 10 + [100.6] * 20 + [100.0] * 360
    so = {"at_pct": 0.5, "fraction": 0.5, "stop_to_breakeven": True}
    book, t = run(tmp_path, session(path=path), [spec(scale_out=so, target_pct=2)], Always())
    assert list(t.side) == ["buy", "sell_part", "sell"]
    assert list(t.reason) == ["ENTER", "scale out", "stop (raised)"]
    buy, part, last = t.iloc[0], t.iloc[1], t.iloc[2]
    assert part.qty == pytest.approx(buy.qty / 2, abs=1e-6)
    assert last.price == pytest.approx(buy.price * (1 - 0.0005), abs=1e-4)  # breakeven stop, less slippage
    assert last.pnl == pytest.approx(part.pnl + (last.price - buy.price) * last.qty, abs=0.01)
    # pnl is logged to the cent, so on a $50 position the ratio is only good to about 0.01%
    assert last.pnl_pct == pytest.approx(last.pnl / (buy.qty * buy.price) * 100, abs=0.011)
    assert part.price == pytest.approx(100.6 * (1 - 0.0005))  # no resting order: sold at the close that crossed it
    assert (t.side == "sell").sum() == 1  # the go-live gate counts sells: one round trip


def test_scale_out_must_be_below_target():
    with pytest.raises(ValueError):
        spec(scale_out={"at_pct": 1.0, "fraction": 0.5}, target_pct=1.0)
    with pytest.raises(ValueError):
        spec(trail_pct=11)


def test_entry_rules_are_frozen_at_entry_and_persist(tmp_path, session):
    bars = session(path=[100.0] * 390)
    book, eng = make(tmp_path, [spec(trail_pct=0.4, max_hold_min=90, scale_out={"at_pct": 0.5, "fraction": 0.5})])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    book2 = Book("sim", book.broker, tmp_path / "sim")  # restart; the spec could have changed meanwhile
    e = book2.entries["SPY"]
    assert (e.trail_pct, e.max_hold_min, e.scale_fraction) == (0.4, 90, 0.5) and e.scale_at == pytest.approx(
        e.price * 1.005
    )


def test_old_entries_json_still_loads(tmp_path):
    d = tmp_path / "sim"
    d.mkdir()
    (d / "entries.json").write_text(
        json.dumps(
            {
                "SPY": {
                    "classifier": "t",
                    "qty": 0.5,
                    "price": 100.0,
                    "stop": 99.5,
                    "target": 101.0,
                    "time": "2026-10-06T10:00:00-04:00",
                    "stop_id": "s1",
                }
            }
        )
    )
    e = Book("sim", SimBroker(250.0), d).entries["SPY"]
    assert (e.init_stop, e.high, e.orig_qty, e.server_stop, e.scale_at) == (99.5, 100.0, 0.5, 99.5, None)


def test_unset_toolkit_fields_leave_existing_spec_identity_unchanged():
    # Hashes recorded before the toolkit existed: existing promotion records must not restart.
    assert golive.spec_hash(spec()) == "87545660ac90022c"
    assert golive.spec_hash(spec(after_exit="rearm", max_trades=3)) == "817273c93ddb0776"
    assert golive.spec_hash(spec(trail_pct=0.3)) != golive.spec_hash(spec())


# ---- Alpaca order paths ------------------------------------------------------------------


def test_alpaca_move_stop_falls_back_to_cancel_and_replace():
    c = FakeClient()
    c.orders["s1"] = {"status": "new", "filled_qty": 0}

    def no_replace(oid, req):
        raise RuntimeError("fractional orders can't be replaced")

    c.replace_order_by_id = no_replace
    c.submit_order = lambda req: NS(id="s2")
    assert broker(c).move_stop("s1", "SPY", 0.5, 101.0, "x") == "s2" and ("cancel", "s1") in c.calls


def test_alpaca_move_stop_keeps_the_old_id_if_it_fired():
    c = FakeClient()
    c.orders["s1"] = {"status": "filled", "filled_qty": 0.5, "price": 100.4}
    c.replace_order_by_id = lambda oid, req: (_ for _ in ()).throw(RuntimeError("filled"))
    assert broker(c).move_stop("s1", "SPY", 0.5, 101.0, "x") == "s1"


def test_alpaca_scale_out_cancels_the_stop_first_and_defers_if_it_fired():
    c = FakeClient()
    c.orders["s1"] = {"status": "filled", "filled_qty": 0.5, "price": 99.5}
    c.submit_order = lambda req: pytest.fail("must not sell when the stop already fired")
    assert broker(c).sell_qty("SPY", 0.25, 100.0, None, "x", "s1") is None and ("cancel", "s1") in c.calls


def test_startup_keeps_todays_limit_fill_and_drops_stale_limits(tmp_path):
    from trader.broker import Position
    from trader.engine import Pending
    from trader.runner import reconcile_at_startup

    class Recon(SimBroker):
        def cancel_orders(self, symbols):
            self.cancelled = set(symbols)

        def cancel_order(self, oid):
            self.cancelled_ids = getattr(self, "cancelled_ids", []) + [oid]

    br = Recon(250.0)
    br.positions = {"SPY": Position("SPY", 0.5, 99.95)}  # today's limit filled while the runner was down
    book = Book("paper", br, tmp_path / "paper")
    now = dt.datetime.now(ET)
    mk = lambda oid, placed: Pending(
        "t", oid, 99.95, 0.5, 49.98, placed, placed + dt.timedelta(minutes=5), 0.5, 1.0, {}, "c"
    )
    book.pending = {"SPY": mk("today", now), "QQQ": mk("old", now - dt.timedelta(days=1))}
    reconcile_at_startup(book, lambda *a: None)
    assert "SPY" in br.positions and not hasattr(br, "cancelled")  # not treated as an orphan
    assert set(book.pending) == {"SPY"} and br.cancelled_ids == ["old"]


def test_trailing_stop_moves_the_server_side_stop(tmp_path, session):
    class Server(SimBroker):
        name = "alpaca-paper"  # take the live-broker paths

        def __init__(self, cash):
            super().__init__(cash)
            self.moves, self.n = [], 0

        def place_stop(self, symbol, qty, stop_price, client_id):
            return "s0"

        def move_stop(self, stop_id, symbol, qty, new_stop, client_id):
            self.n += 1
            self.moves.append(round(new_stop, 2))
            return f"s{self.n}"

    path = [100.0] * 10 + [100.0 + 0.1 * i for i in range(1, 11)] + [101.0] * 370
    br = Server(250.0)
    book, eng = make(tmp_path, [spec(trail_pct=0.5, target_pct=5)], br=br)
    eng.start_day(session(path=path).index[0].date(), {})
    ticks(eng, session(path=path), 0, 21)
    e = book.entries["SPY"]
    assert br.moves and br.moves == sorted(br.moves) and e.stop_id == f"s{br.n}"
    assert e.server_stop == pytest.approx(e.stop)


# ---- review round 1 (#41) ----------------------------------------------------------------


def test_limit_fill_reported_before_its_price_opens_at_the_limit(tmp_path, session):
    from trader.broker import OrderState

    class NoPriceYet(SimBroker):
        def order_state(self, oid):
            st = super().order_state(oid)
            return OrderState(st.status, st.filled_qty, 0.0) if st.status == "filled" else st

    path = [100.0] * 10 + [99.9] * 380
    book, t = run_with(tmp_path, session(path=path), [spec(entry_order=LIMIT)], NoPriceYet(250.0), upto=40)
    e = book.entries["SPY"]
    assert e.price == pytest.approx(99.95) and e.stop == pytest.approx(99.95 * 0.995)
    assert book.buys_today == pytest.approx(e.qty * 99.95)


def test_filled_but_no_qty_reported_keeps_the_order_pending(tmp_path, session):
    from trader.broker import OrderState

    class NoQtyYet(SimBroker):
        def update_bars(self, bars):
            pass

        def order_state(self, oid):
            return OrderState("filled", 0.0, 0.0)

    bars = session(path=[100.0] * 390)
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=NoQtyYet(250.0))
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 5)
    oid = book.pending["SPY"].order_id
    eng.decider = Always(entry="WAIT")  # so a dropped order can't be replaced by a new one
    ticks(eng, bars, 5, 8)
    assert book.pending["SPY"].order_id == oid and not book.entries and book.buys_today > 0


def test_cancel_not_final_keeps_the_order_and_its_cash(tmp_path, session):
    from trader.broker import OrderState

    class SlowCancel(SimBroker):
        def cancel_order(self, oid):
            return OrderState("pending_cancel")

    bars = session(path=[100.0] * 390)
    book, eng = make(tmp_path, [spec(entry_order=LIMIT)], br=SlowCancel(250.0))
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 5)
    oid = book.pending["SPY"].order_id
    eng.decider = Always(entry="WAIT")
    ticks(eng, bars, 5, 12)  # past expiry
    assert book.pending["SPY"].order_id == oid and book.buys_today == pytest.approx(book.pending["SPY"].reserved)


def test_restart_after_trail_ratchet_does_not_exit_on_old_bars(tmp_path, session):
    path = [100.0] * 6 + [99.8] * 3 + [100.0 + 0.2 * i for i in range(1, 11)] + [101.9] * 371
    bars = session(path=path)
    sp = spec(trail_pct=0.5, target_pct=5)
    book, eng = make(tmp_path, [sp])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 19)
    assert book.entries["SPY"].stop > 101  # the rally raised the trail above the old dip
    book2, eng2 = make(tmp_path, [sp], br=book.broker)  # mid-session restart
    eng2.start_day(bars.index[0].date(), {})
    ticks(eng2, bars, 19, 21)
    assert "SPY" in book2.entries  # the 99.8 dip from before the ratchet isn't a hit on the raised stop


def test_fresh_limit_fill_with_lagging_positions_is_not_a_bogus_exit(tmp_path, session):
    class Lagging(SimBroker):
        def get_positions(self):
            pos = super().get_positions()
            if pos and not getattr(self, "lagged", False):
                self.lagged = True
                return {}
            return pos

    path = [100.0] * 10 + [99.9] * 380
    book, t = run_with(tmp_path, session(path=path), [spec(entry_order=LIMIT)], Lagging(250.0), upto=20)
    assert list(t.reason) == ["ENTER"] and "SPY" in book.entries


def test_limit_fill_bar_counts_only_for_the_stop(tmp_path, session):
    bars = session(path=[100.0] * 10 + [99.9] * 380)
    bars.iloc[10, bars.columns.get_loc("high")] = 101.5  # the fill bar also traded above the 1% target
    book, t = run_with(tmp_path, bars, [spec(entry_order=LIMIT)], SimBroker(250.0), upto=30)
    assert list(t.reason) == ["ENTER"]  # that high may have come before the fill: no target exit


def test_sim_limit_needs_a_trade_strictly_below(tmp_path, session):
    bars = session(path=[100.0] * 390)
    bars.iloc[6, bars.columns.get_loc("low")] = 99.95  # touches the limit exactly
    book, eng = make(tmp_path, [spec(entry_order={**LIMIT, "expire_min": 30})])
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 10)
    assert "SPY" in book.pending and not book.entries


def test_exit_found_in_order_history_keeps_its_own_fill_time():
    c = FakeClient()
    at = dt.datetime(2026, 10, 5, 19, 58, tzinfo=dt.UTC)  # 15:58 ET on the previous session
    c.get_orders = lambda req: [NS(filled_qty="0.5", filled_avg_price="101", filled_at=at)]
    f = broker(c).exit_fill_since(
        "SPY", dt.datetime(2026, 10, 5, 10, tzinfo=ET), dt.datetime(2026, 10, 6, 9, 25, tzinfo=ET)
    )
    assert f.time == at and f.time.isoformat().startswith("2026-10-05T15:58")


def run_with(tmp_path, bars, specs, br, upto):
    book, eng = make(tmp_path, specs, br=br)
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, upto)
    return book, pd.read_csv(tmp_path / "sim" / "trades.csv")


def test_limit_fill_bar_alone_breaking_the_stop_exits(tmp_path, session):
    bars = session(path=[100.0] * 10 + [99.9] * 380)
    bars.iloc[10, bars.columns.get_loc("low")] = 99.0  # only the fill bar dips through the 0.5% stop
    book, t = run_with(tmp_path, bars, [spec(entry_order=LIMIT)], SimBroker(250.0), upto=30)
    assert list(t.reason) == ["ENTER", "stop"]


def test_flatten_keeps_a_limit_whose_cancel_is_not_final(tmp_path, session):
    from trader.broker import OrderState

    class SlowCancel(SimBroker):
        def cancel_order(self, oid):
            return OrderState("pending_cancel")

    bars = session(path=[100.0] * 390)
    book, eng = make(tmp_path, [spec(entry_order={**LIMIT, "expire_min": 60})], br=SlowCancel(250.0))
    eng.start_day(bars.index[0].date(), {})
    ticks(eng, bars, 0, 6)
    oid, reserved = book.pending["SPY"].order_id, book.pending["SPY"].reserved
    (tmp_path / "sim" / "stop.json").write_text(json.dumps({"stop_on": bars.index[0].date().isoformat()}))
    ticks(eng, bars, 6, 8)
    assert book.pending["SPY"].order_id == oid and book.buys_today == pytest.approx(reserved)
    assert eng.states[0].symbols["SPY"].status == "pending"  # not re-armed while the order can still fill


def test_state_files_written_by_other_versions_still_load(tmp_path):
    d = tmp_path / "sim"
    d.mkdir()
    (d / "entries.json").write_text(
        json.dumps(
            {
                "SPY": {
                    "classifier": "t",
                    "qty": 0.5,
                    "price": 100.0,
                    "stop": 99.5,
                    "target": 101.0,
                    "time": "2026-10-06T10:00:00-04:00",
                    "from_the_future": 1,
                }
            }
        )
    )
    book = Book("sim", SimBroker(250.0), d)
    book.save_entries()
    assert "SPY" in book.entries and not (d / "entries.json.tmp").exists()
