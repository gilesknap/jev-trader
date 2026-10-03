"""The Trades page gets every fill from today plus a bounded tail of earlier ones, and is told how many it misses."""
import csv

import pytest
from fastapi.testclient import TestClient

from trader import config, dashboard
from trader.dashboard import TRADES_TAIL, recent_trades


def _t(time, sym="SPY", book="paper"):
    return {"time": time, "symbol": sym, "book": book}


def _day(date, n):
    return [_t(f"{date}T{9 + i // 60:02d}:{i % 60:02d}-04:00") for i in range(n)]


def test_a_busy_day_keeps_all_its_fills_and_the_tail_before_them():
    rows = _day("2026-09-29", 60) + _day("2026-09-30", 100)
    kept, omitted = recent_trades(rows, "2026-09-30")
    assert len(kept) == 100 + TRADES_TAIL and omitted == 60 - TRADES_TAIL
    assert [r["time"] for r in kept] == sorted((r["time"] for r in kept), reverse=True)
    assert sum(r["time"].startswith("2026-09-30") for r in kept) == 100


def test_a_quiet_history_is_sent_whole():
    rows = _day("2026-09-29", 5) + _day("2026-09-30", 3)
    kept, omitted = recent_trades(rows, "2026-09-30")
    assert len(kept) == 8 and omitted == 0


def test_no_fills_today_still_shows_the_tail():
    kept, omitted = recent_trades(_day("2026-09-29", TRADES_TAIL + 5), "2026-09-30")
    assert len(kept) == TRADES_TAIL and omitted == 5


def test_a_replay_keeps_its_latest_day_whole():
    rows = _day("2026-09-29", 50) + _day("2026-09-30", 70)
    kept, omitted = recent_trades(rows, None)
    assert sum(r["time"].startswith("2026-09-30") for r in kept) == 70 and omitted == 50 - TRADES_TAIL


def test_rows_without_a_time_are_dropped():
    assert recent_trades([{"symbol": "SPY"}, _t("2026-09-30T10:00-04:00")], "2026-09-30") == (
        [_t("2026-09-30T10:00-04:00")], 0)


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "symbol", "book"])
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    return TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})


def test_live_data_merges_every_book_and_sim_account(tmp_path, monkeypatch, client):
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "recent_trades", lambda rows, today: (sorted(rows, key=lambda r: r["time"]), 7))
    _write(tmp_path / "books" / "paper" / "trades.csv", [_t("2026-09-30T10:00-04:00")])
    _write(tmp_path / "books" / "sim" / "orb" / "trades.csv", [_t("2026-09-30T11:00-04:00", book="sim")])
    out = client.get("/api/data").json()
    assert [r["book"] for r in out["trades"]] == ["paper", "sim"] and out["trades_omitted"] == 7
    assert "trades" not in out["books"]["paper"] and out["books"]["sim"]["accounts"] == 1
