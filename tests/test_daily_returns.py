"""The objective on the scoreboard: the whole book's daily returns vs SPY (scoreboard.daily)."""

import json

import pytest

from trader import cli, config
from trader import scoreboard as SB

DAYS = ["2026-10-05", "2026-10-06", "2026-10-07"]
NAV = [1.0, 1.01, 0.99, 1.0]  # opening mark on day 1, then each day's close


def equity(navs=NAV, equities=None):
    equities = equities or [250 * n for n in navs]
    rows = [{"time": f"{DAYS[0]}T09:30-04:00", "equity": equities[0], "nav": navs[0]}]
    for i, d in enumerate(DAYS):
        rows.append({"time": f"{d}T12:00-04:00", "equity": equities[i + 1] * 0.999, "nav": navs[i + 1] * 0.999})
        rows.append({"time": f"{d}T16:00-04:00", "equity": equities[i + 1], "nav": navs[i + 1]})
    return [{k: str(v) for k, v in r.items()} for r in rows]


BENCH = [{"date": d, "spy_open": "100", "spy_close": c} for d, c in zip(DAYS, ("101", "100", "102"), strict=True)]


def trades(day, n, notional, held_min=30):
    out = []
    for k in range(n):
        out.append({"time": f"{day}T10:{k:02d}-04:00", "classifier": "c", "symbol": f"S{k}", "side": "buy",
                    "notional": str(notional), "pnl": "", "pnl_pct": ""})
        out.append({"time": f"{day}T{10 + held_min // 60}:{k + held_min % 60:02d}-04:00", "classifier": "c",
                    "symbol": f"S{k}", "side": "sell", "notional": str(notional), "pnl": "0", "pnl_pct": "0"})
    return out


def test_series_return_drawdown_and_spy():
    d = SB.daily(equity(), BENCH, trades(DAYS[0], 1, 50))
    assert [round(r["return_pct"], 4) for r in d["rows"]] == [1.0, round((0.99 / 1.01 - 1) * 100, 4), round((1 / 0.99 - 1) * 100, 4)]
    assert d["book"]["days"] == 3 and d["book"]["days_traded"] == 1  # the quiet days count
    assert d["book"]["max_drawdown_pct"] == pytest.approx((0.99 / 1.01 - 1) * 100)
    assert d["book"]["total_pct"] == pytest.approx(0.0, abs=1e-9)
    # SPY buy-and-hold: first day from the open, then close to close
    assert [r["spy_pct"] for r in d["rows"]] == [1.0, round((100 / 101 - 1) * 100, 4), 2.0]
    assert d["spy"]["total_pct"] == pytest.approx(2.0)
    assert d["vs_spy"]["days"] == 3 and d["vs_spy"]["verdict"].startswith("too few to judge")
    # 50 of 250 held for 30 of 390 minutes on day 1, nothing on the others
    assert d["rows"][0]["exposure_pct"] == pytest.approx(50 / 250 * 30 / 390 * 100, abs=0.01)
    assert d["book"]["exposure_pct"] == pytest.approx(d["rows"][0]["exposure_pct"] / 3, abs=0.01)


def test_unequal_trades_same_daily_returns_compare_equal():
    """Many big trades or one small one: with the same daily NAV the objective scores them the same."""
    busy = SB.daily(equity(), BENCH, trades(DAYS[0], 5, 100) + trades(DAYS[1], 3, 40) + trades(DAYS[2], 4, 80))
    quiet = SB.daily(equity(), BENCH, trades(DAYS[1], 1, 10))
    for k in ("total_pct", "mean_pct", "sd_pct", "max_drawdown_pct", "days"):
        assert busy["book"][k] == quiet["book"][k]
    assert busy["vs_spy"] == quiet["vs_spy"]
    assert busy["book"]["exposure_pct"] > quiet["book"]["exposure_pct"]  # described, not scored


def test_a_deposit_is_not_a_gain():
    flat = [1.0] * 4
    d = SB.daily(equity(flat, equities=[250, 250, 500, 500]), BENCH)
    assert d["book"]["total_pct"] == pytest.approx(0.0) and d["book"]["max_drawdown_pct"] == 0.0


def test_from_date_starts_from_the_previous_close():
    d = SB.daily(equity(), BENCH, from_date=DAYS[1])
    assert [r["day"] for r in d["rows"]] == DAYS[1:]
    assert d["rows"][0]["return_pct"] == pytest.approx((0.99 / 1.01 - 1) * 100, abs=1e-4)
    assert SB.daily([], BENCH) is None and SB.daily(equity(), BENCH, from_date="2027-01-01") is None


def test_verdict_after_enough_sessions():
    days = [f"2026-11-{k:02d}" for k in range(2, 16)]
    eq = [{"time": f"{days[0]}T09:30-04:00", "equity": "250", "nav": "1"}]
    bench = []
    nav = spy = 1.0
    for k, d in enumerate(days):
        nav *= 1.002 + 0.001 * (k % 2)
        spy *= 1.0005 - 0.001 * (k % 2)
        eq.append({"time": f"{d}T16:00-04:00", "equity": str(250 * nav), "nav": str(nav)})
        bench.append({"date": d, "spy_open": "100", "spy_close": str(100 * spy)})  # only day 1's open is used
    v = SB.daily(eq, bench)["vs_spy"]
    assert v["days"] == len(days) and v["verdict"] == "ahead of SPY: 95% interval above zero"


def test_build_carries_daily_only_with_an_equity_file():
    assert SB.build([], {}, set(), 250.0)["daily"] is None
    b = SB.build(trades(DAYS[0], 1, 50), {}, {"c"}, 250.0, equity=equity(), benchmark=BENCH)
    assert b["daily"]["book"]["days"] == 3


def test_cli_daily_returns_reads_each_book(tmp_path, monkeypatch, capsys):
    import csv

    rt = tmp_path / "runtime"
    (rt / "books" / "paper").mkdir(parents=True)
    (rt / "books" / "live").mkdir()  # no equity file: not listed
    with (rt / "books" / "paper" / "equity.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, ["time", "equity", "nav", "hwm"], restval="")
        w.writeheader()
        w.writerows(equity())
    with (rt / "benchmark.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, ["date", "spy_open", "spy_close"])
        w.writeheader()
        w.writerows(BENCH)
    monkeypatch.setattr(config, "RUNTIME_DIR", rt)
    cli.main(["daily-returns", "--since", DAYS[0]])
    out = json.loads(capsys.readouterr().out)
    assert list(out["books"]) == ["paper"] and out["books"]["paper"]["book"]["days"] == 3
    assert not (rt / "books" / "paper" / "trades.csv").exists()  # read-only
