import argparse
import datetime as dt
import json

import pandas as pd
import pytest

from test_engine import Always, spec
from trader import runner
from trader.broker import SIM_START_CASH, PersistentSimBroker, SimBroker
from trader.data import ET
from trader.engine import Book, Engine


def _run(tmp_path, bars, specs, books):
    eng = Engine(specs, books, Always(), {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    return eng, eng.end_day(close)


def _trades(book_dir):
    p = book_dir / "trades.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["classifier", "side", "book"])


def test_sim_classifiers_trade_their_own_accounts_beside_paper(tmp_path, session):
    """The point of sim: two experiments and a paper rule on the same symbol all get to trade."""
    paper = Book("paper", SimBroker(250), tmp_path / "paper")
    specs = [spec(id="shadow_one"), spec(id="sim_a", mode="sim"), spec(id="sim_b", mode="sim")]
    books = {"live": paper, "shadow": paper} | runner.sim_books(specs, tmp_path / "sim")
    eng, summary = _run(tmp_path, session(path=[100.0] * 390), specs, books)
    assert list(_trades(tmp_path / "paper").classifier) == ["shadow_one"] * 2
    for sid in ("sim_a", "sim_b"):
        t = _trades(tmp_path / "sim" / sid)
        assert list(t.classifier) == [sid] * 2 and set(t.book) == {f"sim:{sid}"}
    assert set(summary) == {"paper", "sim:sim_a", "sim:sim_b"}


def test_a_sim_classifier_never_falls_back_to_paper(tmp_path):
    paper = Book("paper", SimBroker(250), tmp_path / "paper")
    with pytest.raises(ValueError, match="sim:x"):
        Engine([spec(id="x", mode="sim")], {"live": paper, "shadow": paper}, Always(), {"SPY"}, tmp_path)


def test_sim_account_survives_a_restart_and_carries_cash_over(tmp_path, session):
    specs = [spec(id="sim_a", mode="sim")]
    bars = session(path=[100.0] * 10 + [101.0] * 380)
    paper = Book("paper", SimBroker(250), tmp_path / "paper")
    # Mid-session restart: a fresh broker from the same directory sees the open position.
    b1 = runner.sim_books(specs, tmp_path / "sim")["sim:sim_a"]
    eng = Engine(specs, {"live": paper, "shadow": paper} | {"sim:sim_a": b1}, Always(), {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index[:20]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    assert "SPY" in b1.entries
    b2 = runner.sim_books(specs, tmp_path / "sim")["sim:sim_a"]
    assert set(b2.broker.get_positions()) == {"SPY"} and set(b2.entries) == {"SPY"}
    assert b2.broker.cash == pytest.approx(b1.broker.cash)
    b2.broker.sell_all("SPY", 101.0, close, "x")
    b3 = runner.sim_books(specs, tmp_path / "sim")["sim:sim_a"]
    assert not b3.broker.get_positions() and b3.broker.equity() > SIM_START_CASH  # the gain carried over


def test_persistent_sim_broker_keeps_todays_orders_only(tmp_path):
    p = tmp_path / "s.json"
    b = PersistentSimBroker(p)
    t0 = dt.datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    b.buy_limit("SPY", 0.5, 99.0, t0 - dt.timedelta(days=1), "old")
    oid = b.buy_limit("SPY", 0.5, 99.0, t0, "new")
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
    again = PersistentSimBroker(p)
    assert set(again.orders) == {oid}
    st = again.order_state(oid)
    assert st.status == "filled" and st.price == pytest.approx(98.5 * 1.0005) and st.filled_at is not None
    assert again.get_positions()["SPY"].qty == pytest.approx(0.5)
    assert again.cash == pytest.approx(b.cash)
    assert again.cancel_order("sim-old").status == "canceled" and again.order_state("gone").status == "canceled"


def test_book_dirs_include_sim_accounts_for_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path)
    for d in ("paper", "sim/a", "sim/b"):
        (tmp_path / d).mkdir(parents=True)
    assert [str(d.relative_to(tmp_path)) for d in runner.book_dirs()] == ["paper", "sim/a", "sim/b"]


def test_clear_halt_names():
    from trader import cli

    assert cli._book_name("sim/my_idea") == "sim/my_idea"
    for bad in ("sim/../paper", "sim", "other", "sim/A"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._book_name(bad)


def test_archive_takes_sim_trades_but_not_their_equity(tmp_path, monkeypatch):
    from trader import compact, config

    rt = tmp_path / "runtime"
    for d, book in (("books/paper", "paper"), ("books/sim/a", "sim:a")):
        (rt / d).mkdir(parents=True)
        (rt / d / "trades.csv").write_text(f"time,book,classifier\n2026-10-05T10:00,{book},x\n")
        (rt / d / "equity.csv").write_text("time,equity,nav\n")
    monkeypatch.setattr(config, "RUNTIME_DIR", rt)
    monkeypatch.setattr(compact, "LOGS", tmp_path / "logs")
    compact.archive()
    assert set(pd.read_csv(tmp_path / "logs" / "trades.csv").book) == {"paper", "sim:a"}
    assert sorted(p.name for p in (tmp_path / "logs").glob("*equity.csv")) == ["paper_equity.csv"]


def test_dashboard_shows_sim_as_one_book(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trader import dashboard
    from trader import scoreboard as SB

    rt = tmp_path / "runtime"
    cols = "time,book,classifier,symbol,side,qty,price,notional,reason,pnl,pnl_pct"
    rows = {"paper": [("paper", "idea")], "sim/a": [("sim:a", "a")], "sim/b": [("sim:b", "b")]}
    for d, rs in rows.items():
        (rt / "books" / d).mkdir(parents=True)
        lines = [cols] + [
            x
            for bk, c in rs
            for x in (
                f"2026-10-06T10:00:00-04:00,{bk},{c},SPY,buy,1,100,100,entry,,",
                f"2026-10-06T10:30:00-04:00,{bk},{c},SPY,sell,1,101,101,target,1.0,1.0",
            )
        ]
        (rt / "books" / d / "trades.csv").write_text("\n".join(lines) + "\n")
    (rt / "books" / "paper" / "equity.csv").write_text("time,equity,nav,hwm\n2026-10-06T09:35-04:00,250,1,1\n")
    (rt / "status.json").write_text(
        json.dumps(
            {
                "classifiers": [
                    {"id": "idea", "family": "novel", "mode": "shadow", "symbols": {}},
                    {"id": "a", "family": "novel", "mode": "sim", "symbols": {}},
                    {"id": "b", "family": "conventional", "mode": "sim", "symbols": {}},
                ]
            }
        )
    )
    monkeypatch.setattr(dashboard.config, "RUNTIME_DIR", rt)
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    monkeypatch.setattr(SB, "EXPERIMENT_START", dt.date(2026, 1, 1))
    c = TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})
    d = c.get("/api/data").json()
    assert set(d["books"]) == {"paper", "sim"} and d["books"]["sim"]["accounts"] == 2
    assert {t["classifier"] for t in d["trades"] if t["book"].startswith("sim")} == {"a", "b"}
    assert {t["classifier"] for t in d["trades"]} == {"idea", "a", "b"} and d["trades_omitted"] == 0
    sb = c.get("/api/scoreboard").json()["books"]
    assert {r["id"] for r in sb["paper"]["classifiers"]} == {"idea"}
    sim = {r["id"]: r for r in sb["sim"]["classifiers"]}
    assert set(sim) == {"a", "b"} and sb["sim"]["slippage_per_side_pct"] == 0
    assert "promotion" not in sim["a"]


def test_unreadable_sim_account_is_quarantined_not_fatal(tmp_path):
    d = tmp_path / "sim" / "sim_a"
    d.mkdir(parents=True)
    (d / "sim_state.json").write_text("{trunc")
    alerts = []
    books = runner.sim_books(
        [spec(id="sim_a", mode="sim")], tmp_path / "sim", lambda lvl, m: alerts.append(m), tmp_path / "q"
    )
    assert books["sim:sim_a"].broker.cash == SIM_START_CASH  # a fresh account
    assert len(list((tmp_path / "q").iterdir())) == 1 and "unreadable" in alerts[0]


def test_stale_sim_positions_are_closed_at_startup_even_without_a_rule(tmp_path, session):
    specs = [spec(id="gone", mode="sim")]
    bars = session(path=[100.0] * 30)  # 2026-09-21, ends mid-morning: still holding
    paper = Book("paper", SimBroker(250), tmp_path / "paper")
    eng = Engine(
        specs, {"live": paper, "shadow": paper} | runner.sim_books(specs, tmp_path / "sim"), Always(), {"SPY"}, tmp_path
    )
    eng.start_day(bars.index[0].date(), {})
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, 300)
    assert "SPY" in eng.books["sim:gone"].entries
    alerts = []
    # Next day the rule is no longer in the file at all.
    runner.reconcile_sim_accounts(
        tmp_path / "sim", dt.date(2026, 9, 22), lambda lvl, m: alerts.append((lvl, m)), tmp_path / "q"
    )
    b = runner.sim_books([spec(id="gone", mode="sim")], tmp_path / "sim")["sim:gone"]
    assert not b.entries and not b.broker.get_positions()
    t = _trades(tmp_path / "sim" / "gone")
    assert list(t.side) == ["buy", "sell"] and "closed at startup at its entry price" in t.reason.iloc[-1]
    assert alerts and alerts[0][0] == "info"
    # Today's positions are the engine's business, not the reconciler's.
    runner.reconcile_sim_accounts(tmp_path / "sim", dt.date(2026, 9, 22), lambda *a: None, tmp_path / "q")
    assert len(_trades(tmp_path / "sim" / "gone")) == 2


def test_a_sim_account_sells_a_target_at_the_bar_close_never_the_next_open(tmp_path, session):
    """Only a replay sets next_open: a live sim account, like paper, sells a target touch at
    market at the close it has seen, never at the target level and never at a future bar."""
    bars = session(path=[100.0] * 390)
    bars.iloc[10, bars.columns.get_loc("high")] = 101.5  # 09:40 spikes through the 1% target
    bars.iloc[11, bars.columns.get_loc("open")] = 99.0  # a future price the account must not see
    book = Book("sim:t", PersistentSimBroker(tmp_path / "acct.json"), tmp_path / "sim")
    _run(tmp_path, bars, [spec()], {"live": book, "shadow": book})
    assert book.broker.next_open == {}
    t = _trades(tmp_path / "sim")
    sell = t[t.side == "sell"].iloc[0]
    assert sell.reason == "target" and sell.time.startswith("2026-09-21T09:41")
    assert sell.price == pytest.approx(100.0 * (1 - 0.0005))
