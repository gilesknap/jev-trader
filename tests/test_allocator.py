"""Aggregate stop-risk and overlapping-exposure limits (#74): trader.allocator and its engine wiring."""

import datetime as dt
import json
import random

import pandas as pd
import pytest
import yaml

from test_engine import Always, spec
from test_restart_state import ticks
from trader import allocator as A
from trader import config
from trader.allocator import Exposure, allocate
from trader.broker import Position, SimBroker
from trader.engine import Book, Engine, Entry, Pending
from trader.guardrails import MAX_STOP_DISTANCE, MIN_NOTIONAL

# ---- the allocator -------------------------------------------------------------------------


def test_individually_valid_orders_are_reduced_by_combined_stop_risk():
    held = [Exposure("TLT", 250, 25)]  # a 25% position with a 10% stop: 2.5% of equity at risk
    assert allocate("GLD", 250, 0.1, 1000, 1000, 0, held) == (pytest.approx(50), "aggregate stop risk")
    assert allocate("GLD", 250, 0.1, 1000, 1000, 0, []) == (250, "requested")


def test_pending_reservations_and_realised_loss_consume_the_budget():
    pending = Exposure("TLT", 100, 10)
    assert allocate("GLD", 250, 0.1, 1000, 1000, -20, [pending])[0] == 0
    assert allocate("GLD", 250, 0.1, 1000, 1000, -20, [])[0] == pytest.approx(100)  # cancelled: released
    assert allocate("GLD", 250, 0.1, 1000, 1000, +50, [])[0] == 250  # gains never enlarge it...
    assert allocate("GLD", 250, 0.1, 1000, 1000, +50, [Exposure("TLT", 250, 25)])[0] == pytest.approx(50)


def test_budget_uses_the_smaller_of_current_and_day_start_equity():
    assert allocate("GLD", 250, 0.1, 1000, 800, 0, [])[0] == pytest.approx(240)
    assert allocate("GLD", 250, 0.1, 800, 1000, 0, [])[0] == pytest.approx(240)


def test_etf_and_its_constituents_share_a_bucket():
    held = [Exposure("QQQ", 250, 1), Exposure("NVDA", 200, 1)]
    assert allocate("MSFT", 250, 0.005, 1000, 1000, 0, held) == (50, "growth bucket")


def test_xly_shares_the_growth_bucket_with_qqq_and_nvda():
    held = [Exposure("QQQ", 250, 1), Exposure("NVDA", 200, 1)]
    assert allocate("XLY", 250, 0.005, 1000, 1000, 0, held) == (50, "growth bucket")


def test_broad_etfs_count_against_every_bucket():
    held = [Exposure("SPY", 250, 1), Exposure("XLF", 250, 1)]
    assert allocate("JPM", 250, 0.005, 1000, 1000, 0, held) == (0, "financials bucket")
    assert allocate("DIA", 250, 0.005, 1000, 1000, 0, held)[0] == 0
    assert allocate("XOM", 250, 0.005, 1000, 1000, 0, held) == (250, "requested")


def test_total_equity_exposure_is_capped_but_bonds_and_gold_are_not():
    held = [Exposure("XLI", 250, 1), Exposure("COST", 250, 1), Exposure("XOM", 200, 1)]
    assert allocate("JPM", 250, 0.005, 1000, 1000, 0, held) == (50, "equity exposure")
    assert allocate("TLT", 250, 0.005, 1000, 1000, 0, held) == (250, "requested")


def test_every_universe_symbol_is_classified_once():
    """A new ticker must be put in a bucket (or explicitly left out) before it trades."""
    universe = set(yaml.safe_load(config.UNIVERSE_FILE.read_text())["tickers"])
    groups = [A.BROAD, A.NON_EQUITY, A.UNBUCKETED, *A.BUCKETS.values()]
    assert set().union(*groups) == universe
    assert sum(map(len, groups)) == len(universe)  # no symbol in two groups


def test_a_trim_must_keep_the_larger_of_ten_dollars_and_a_quarter_of_the_request():
    assert A.trim_floor(250) == pytest.approx(62.5)
    assert A.trim_floor(30) == A.trim_floor(0) == 10.0


def test_fuzz_only_ever_tightens_and_every_limit_holds():
    """Never more than requested (so never more than the rules without it), and after the
    order, each limit it checked is still met (or it allowed nothing)."""
    rng = random.Random(74)
    skipped = 0
    symbols = sorted(set().union(A.BROAD, A.NON_EQUITY, A.UNBUCKETED, *A.BUCKETS.values()))
    for _ in range(200_000):
        equity = rng.uniform(0, 2000)
        day_start = rng.choice([0.0, rng.uniform(0, 2000)])
        realised = rng.uniform(-100, 100)
        held = [Exposure(s, rng.uniform(0, 600), rng.uniform(-5, 60)) for s in rng.sample(symbols, rng.randint(0, 6))]
        sym, desired, sf = rng.choice(symbols), rng.uniform(0, 600), rng.uniform(0.0005, 0.1)
        allowed, binding = allocate(sym, desired, sf, equity, day_start, realised, held)
        assert 0 <= allowed <= desired
        assert binding != "requested" or allowed == desired
        # The engine's rule (#118): a trim below trim_floor is skipped, never rounded up.
        placed = allowed if binding == "requested" or allowed >= A.trim_floor(desired) else 0.0
        assert 0 <= placed <= allowed
        assert placed in (0.0, desired) or placed >= max(A.MIN_TRIM_USD, A.MIN_TRIM_FRACTION * desired) > MIN_NOTIONAL
        skipped += placed < allowed
        allowed = placed
        if allowed <= 0:
            continue
        base = min(equity, day_start) if day_start > 0 else equity
        risk = sum(max(0, x.stop_loss) for x in held) + allowed * sf + max(0, -realised)
        assert risk <= base * A.OPEN_RISK_FRACTION + 1e-6
        if sym not in A.NON_EQUITY:
            assert (
                sum(x.notional for x in held if x.symbol not in A.NON_EQUITY) + allowed <= A.EQUITY_CAP * equity + 1e-6
            )
        for members in A.BUCKETS.values():
            if sym in members or sym in A.BROAD:
                inside = members | A.BROAD
                assert sum(x.notional for x in held if x.symbol in inside) + allowed <= A.BUCKET_CAP * equity + 1e-6
    assert skipped > 1000  # the floor was exercised


# ---- engine wiring -------------------------------------------------------------------------


def decisions(tmp_path, day):
    return [json.loads(line) for line in (tmp_path / "decisions" / f"{day}.jsonl").read_text().splitlines()]


def ticks_all(eng, bars, syms, n):
    day = bars.index[0].date()
    close = pd.Timestamp(dt.datetime.combine(day, dt.time(16)), tz=bars.index.tz)
    for ts in bars.index[:n]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {s: bars.loc[:ts] for s in syms}, (close - now).total_seconds() / 60)


def test_engine_caps_overlapping_entries_and_keeps_the_allocator_note(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    syms = ["QQQ", "NVDA", "XLY"]
    book = Book("sim", SimBroker(1000.0), tmp_path / "sim")
    specs = [spec(id=s.lower(), symbols=[s], size_fraction=0.25) for s in syms]
    eng = Engine(specs, {"live": book, "shadow": book}, Always(), {"SPY", *syms}, tmp_path)
    eng.start_day(day, {})
    ticks_all(eng, bars, ["SPY", *syms], 10)
    assert set(book.entries) == {"QQQ", "NVDA"}  # 25% + 25% fills the 50% growth bucket
    st = eng.states[2].symbols["XLY"]
    assert st.note.startswith("allocator: growth bucket; allowed $0.00")  # not check_entry's minimum
    rows = [r for r in decisions(tmp_path, day) if r["q"] == "allocation" and r["c"] == "xly"]
    assert rows and {"t", "c", "s", "q", "constraint", "requested", "allowed"} <= set(rows[0])
    assert (rows[0]["c"], rows[0]["s"], rows[0]["constraint"], rows[0]["allowed"]) == ("xly", "XLY", "growth bucket", 0)


def test_limits_are_per_book(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    paper = Book("paper", SimBroker(1000.0), tmp_path / "paper")
    sim = Book("sim:s", SimBroker(1000.0), tmp_path / "sim")
    specs = [
        spec(id="p", size_fraction=0.25, stop_pct=10),
        spec(id="q", symbols=["TLT"], size_fraction=0.25, stop_pct=2.5),
        spec(id="s", mode="sim", size_fraction=0.25, stop_pct=10),
    ]
    eng = Engine(specs, {"shadow": paper, "live": paper, "sim:s": sim}, Always(), {"SPY", "TLT"}, tmp_path)
    eng.start_day(day, {})
    ticks_all(eng, bars, ["SPY", "TLT"], 10)
    # Paper: SPY at 25% with a 10% stop is 2.5% at risk, so TLT (2.5% stop) gets the 0.5% left: about $200.
    assert paper.entries["SPY"].qty * paper.entries["SPY"].price == pytest.approx(250, abs=0.2)
    assert paper.entries["TLT"].qty * paper.entries["TLT"].price == pytest.approx(200, abs=2)
    # The sim account doesn't see paper's positions: its SPY entry is full size.
    assert sim.entries["SPY"].qty * sim.entries["SPY"].price == pytest.approx(250, abs=0.2)


def test_exposures_count_pending_reservations_and_untracked_positions(tmp_path):
    now = pd.Timestamp("2026-09-21 10:00", tz="America/New_York").to_pydatetime()
    book = Book("sim", SimBroker(1000.0), tmp_path / "sim")
    book.entries["QQQ"] = Entry("a", 1.0, 100.0, 98.0, 110.0, now)
    book.pending["NVDA"] = Pending("b", "id", 50.0, 2.0, 100.0, now, now, 5.0, 1.0, {}, "cid")
    positions = {
        "QQQ": Position("QQQ", 1.0, 100.0),
        "NVDA": Position("NVDA", 0.5, 50.0),
        "XOM": Position("XOM", 2.0, 30.0),
    }
    got = {x.symbol: x for x in Engine._exposures(book, positions)}
    assert got["QQQ"] == Exposure("QQQ", 100.0, pytest.approx(2.0))
    assert got["NVDA"] == Exposure("NVDA", 100.0, 5.0)  # the whole reservation, not the part filled
    assert got["XOM"] == Exposure("XOM", 60.0, pytest.approx(60.0 * MAX_STOP_DISTANCE))  # no stop: worst allowed
    book.pending["NVDA"].filled_cost = 30.0  # part-filled (#116): that part is also an Entry now
    assert {x.symbol: x for x in Engine._exposures(book, positions)}["NVDA"] == Exposure("NVDA", 70.0, 3.5)
    book.pending["NVDA"].filled_cost = 120.0  # filled above the reservation: never negative
    assert {x.symbol: x for x in Engine._exposures(book, positions)}["NVDA"] == Exposure("NVDA", 0.0, 0.0)
    book.pending.clear()  # cancelled: the reservation is released
    assert "NVDA" not in {x.symbol for x in Engine._exposures(book, {})}


def test_realised_loss_with_a_scale_out_survives_restart_and_is_counted_once(tmp_path, session):
    """A scale-out (a sell_part row) then a stop-out: the final sell row already includes the
    banked part. After a restart the budget must see the same realised loss, not a double count
    and not zero, and size the next entry from it."""
    path = [100.0] * 10 + [100.6] * 10 + [89.0] * 10 + [100.0] * 360
    so = {"at_pct": 0.5, "fraction": 0.5, "stop_to_breakeven": False}
    specs = lambda: [
        spec(size_fraction=0.25, stop_pct=10, target_pct=20, scale_out=so),  # noqa: E731
        spec(id="later", window=("10:05", "15:30"), size_fraction=0.25, stop_pct=10),
    ]
    bars = session(path=path)
    day = bars.index[0].date()
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    eng = Engine(specs(), {"live": book, "shadow": book}, Always(), {"SPY"}, tmp_path)
    eng.start_day(day, {})
    ticks(eng, bars, 0, 32)
    t = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert list(t.side) == ["buy", "sell_part", "sell"] and not book.entries
    loss = book.realised_today
    assert loss == pytest.approx(t.pnl.iloc[-1], abs=0.005) and loss < 0
    assert t.pnl.iloc[1] > 0  # summing every row would understate the loss

    book2 = Book("sim", book.broker, tmp_path / "sim")  # crash and restart mid-session
    eng2 = Engine(specs(), {"live": book2, "shadow": book2}, Always(), {"SPY"}, tmp_path)
    eng2.start_day(day, {})
    assert book2.realised_today == pytest.approx(loss, abs=0.005)  # trades.csv keeps cents
    ticks(eng2, bars, 32, 40)
    t = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert list(t.side) == ["buy", "sell_part", "sell", "buy"]
    eq = book2.day_start_equity + loss  # flat between trades: equity is day start plus what was realised
    expected = (min(eq, book2.day_start_equity) * A.OPEN_RISK_FRACTION + book2.realised_today) / 0.10
    assert expected < 0.25 * eq  # the budget binds, below the 25% the spec asks for
    assert float(t.notional.iloc[-1]) == pytest.approx(expected, abs=0.02)
    row = [r for r in decisions(tmp_path, day) if r["q"] == "allocation"][-1]
    assert row["constraint"] == "aggregate stop risk" and row["allowed"] == pytest.approx(expected, abs=0.01)


def restart_with_log(tmp_path, log, broker, day_start=260.0):
    day = dt.date(2026, 9, 21)
    (tmp_path / "sim").mkdir()
    (tmp_path / "sim" / "risk.json").write_text(json.dumps({"day": day.isoformat(), "day_start_equity": day_start}))
    (tmp_path / "sim" / "trades.csv").write_text(log)
    alerts = []
    book = Book("sim", broker, tmp_path / "sim")
    eng = Engine(
        [spec()],
        {"live": book, "shadow": book},
        Always(),
        {"SPY"},
        tmp_path,
        alert=lambda level, msg: alerts.append((level, msg)),
    )
    eng.start_day(day, {})
    return book, alerts


@pytest.mark.parametrize(
    "log",
    [
        "time,side,pnl\n2026-09-21T10:00,sell,oops\n",  # ValueError
        "time,side,pnl\n2026-09-21T10:00,sell," + "9" * 200_000 + "\n",  # csv.Error: field larger than field limit
    ],
    ids=["bad-number", "oversized-field"],
)
def test_a_damaged_trade_log_does_not_stop_a_restart_and_only_tightens(tmp_path, log):
    book, alerts = restart_with_log(tmp_path, log, SimBroker(250.0))
    assert book.realised_today == pytest.approx(-10.0)  # equity 250 vs 260 at the open: counted as realised
    assert alerts and alerts[0][0] == "urgent" and "equity change" in alerts[0][1]


def test_damaged_log_and_unreadable_equity_falls_back_to_zero(tmp_path):
    class Flaky(SimBroker):
        calls = 0

        def equity(self):
            self.calls += 1
            if self.calls > 1:  # the NAV mark at the start of the day reads it once first
                raise RuntimeError("API down")
            return super().equity()

    book, alerts = restart_with_log(tmp_path, "time,side,pnl\n2026-09-21T10:00,sell,oops\n", Flaky(250.0))
    assert book.realised_today == 0.0
    assert alerts and "equity unreadable" in alerts[0][1]


def test_a_trimmed_limit_entry_keeps_the_allocator_note(tmp_path, session):
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book = Book("sim", SimBroker(1000.0), tmp_path / "sim")
    specs = [
        spec(id="p", size_fraction=0.25, stop_pct=10),
        spec(
            id="q",
            symbols=["TLT"],
            size_fraction=0.25,
            stop_pct=2.5,
            entry_order={"type": "limit", "offset_pct": 0.05, "expire_min": 30},
        ),
    ]
    eng = Engine(specs, {"live": book, "shadow": book}, Always(), {"SPY", "TLT"}, tmp_path)
    eng.start_day(day, {})
    ticks_all(eng, bars, ["SPY", "TLT"], 10)
    assert "TLT" in book.pending and book.pending["TLT"].reserved == pytest.approx(200, abs=2)
    st = eng.states[1].symbols["TLT"]
    assert st.note.startswith("limit ") and "resting (allocator: aggregate stop risk; allowed $" in st.note
    eng.decider.entry = "WAIT"
    bars.loc[bars.index[20] :, ["low", "close"]] = 99.9  # fills the 99.95 limit: the allocator's note stays
    ticks_all(eng, bars, ["SPY", "TLT"], 25)
    assert st.status == "holding" and st.note.startswith("allocator: aggregate stop risk; allowed $")
    assert st.note == st.alloc_note  # kept apart, not parsed back out of the resting note (#143)


def test_a_trim_below_a_quarter_of_the_request_is_skipped_not_placed(tmp_path, session):
    """#118: $50 left of a $250 request is under the $62.50 floor: no order, allocator note kept."""
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    book = Book("sim", SimBroker(1000.0), tmp_path / "sim")
    specs = [
        spec(id="p", size_fraction=0.25, stop_pct=10),
        spec(id="q", symbols=["TLT"], size_fraction=0.25, stop_pct=10),
    ]
    eng = Engine(specs, {"live": book, "shadow": book}, Always(), {"SPY", "TLT"}, tmp_path)
    eng.start_day(day, {})
    ticks_all(eng, bars, ["SPY", "TLT"], 10)
    assert "SPY" in book.entries and "TLT" not in book.entries and "TLT" not in book.pending
    note = eng.states[1].symbols["TLT"].note  # equity is a few cents under $1000 after SPY's slippage
    assert note.startswith("allocator: aggregate stop risk; allowed $49.9") and note.endswith(
        "; skipped, under the $62.49 floor for a trimmed entry"
    )
    row = [r for r in decisions(tmp_path, day) if r["q"] == "allocation" and r["c"] == "q"][0]
    assert row["constraint"] == "aggregate stop risk"
    assert (row["requested"], row["allowed"], row["floor"]) == pytest.approx((250, 50, 62.5), abs=0.1)


def test_a_trim_under_ten_dollars_is_skipped_but_a_small_untrimmed_entry_is_not(tmp_path, session):
    """$9 left of a $30 request clears the quarter but not the $10 floor. A $7.50 entry that no
    limit trims is placed exactly as before."""
    bars = session(path=[100.0] * 390)
    day = bars.index[0].date()
    syms = ["QQQ", "NVDA", "XLY", "XOM"]
    book = Book("sim", SimBroker(250.0), tmp_path / "sim")
    specs = [
        spec(id="qqq", symbols=["QQQ"], size_fraction=0.25),
        spec(id="nvda", symbols=["NVDA"], size_fraction=0.214),  # 46.4% of the 50% growth bucket: $9 left
        spec(id="xly", symbols=["XLY"], size_fraction=0.12),  # asks $30; the quarter floor is $7.50
        spec(id="xom", symbols=["XOM"], size_fraction=0.03),
    ]  # $7.50, outside every bucket: untrimmed
    eng = Engine(specs, {"live": book, "shadow": book}, Always(), {"SPY", *syms}, tmp_path)
    eng.start_day(day, {})
    ticks_all(eng, bars, ["SPY", *syms], 10)
    assert set(book.entries) == {"QQQ", "NVDA", "XOM"}
    assert book.entries["XOM"].qty * book.entries["XOM"].price == pytest.approx(7.5, abs=0.05)
    row = [r for r in decisions(tmp_path, day) if r["q"] == "allocation" and r["c"] == "xly"][0]
    assert row["constraint"] == "growth bucket" and 7.5 < row["allowed"] < 10 and row["floor"] == 10
    assert "skipped, under the $10.00 floor" in eng.states[2].symbols["XLY"].note
    assert not [r for r in decisions(tmp_path, day) if r["q"] == "allocation" and r["c"] == "xom"]
