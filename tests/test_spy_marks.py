"""SPY's intraday marks (spy_marks.csv), so the performance chart's SPY line moves during the day.

The engine writes SPY's last price beside each 5-minute equity mark; the dashboard serves them and
the chart's spyLine uses them, falling back to the daily benchmark.csv where there are none. The
spyLine tests run that block of index.html under node (skipped without it).
"""

import datetime as dt
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from test_engine import Always, run, spec
from trader import config, dashboard
from trader.broker import SimBroker
from trader.data import ET
from trader.engine import SPY_MARK_DAYS, Book, Engine

NODE = shutil.which("node")


def _marks(path):
    lines = path.read_text().splitlines()
    assert lines[0] == "time,spy"
    return [line.split(",") for line in lines[1:]]


# ---- the writer ------------------------------------------------------------------


def test_spy_is_marked_beside_each_equity_mark(tmp_path, session):
    bars = session(n=120, seed=3)
    run(tmp_path, bars, [spec()], Always("WAIT"))
    marks = _marks(tmp_path / "spy_marks.csv")
    equity = [line.split(",")[0] for line in (tmp_path / "sim" / "equity.csv").read_text().splitlines()[1:]]
    # Every mark but the opening one (written before any bar, at the day-start equity) has a twin.
    assert [t for t, _ in marks] == equity[1:]
    for t, v in marks[:-1]:  # the tick at t has seen the bar that closed a minute before
        seen = bars.loc[: dt.datetime.fromisoformat(t) - dt.timedelta(minutes=1)]
        assert float(v) == pytest.approx(seen.close.iloc[-1], abs=1e-3)
    assert marks[-1][0].endswith("T16:00-04:00")  # the closing mark, at the day's last price
    assert float(marks[-1][1]) == pytest.approx(bars.close.iloc[-1], abs=1e-3)


def test_unknown_spy_price_is_left_blank(tmp_path):
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, tmp_path)
    day = dt.date(2026, 9, 21)
    eng.start_day(day, {})
    eng.tick(dt.datetime.combine(day, dt.time(9, 35), ET), {}, 385)  # no SPY bar yet
    assert _marks(tmp_path / "spy_marks.csv") == [["2026-09-21T09:35-04:00", ""]]


def test_marks_older_than_the_kept_window_are_pruned_at_the_close(tmp_path, session):
    first = dt.date(2026, 9, 1)
    run(tmp_path, session(day=first, n=10), [spec()], Always("WAIT"))
    run(tmp_path, session(day=first + dt.timedelta(days=SPY_MARK_DAYS - 1), n=10), [spec()], Always("WAIT"))
    assert {t[:10] for t, _ in _marks(tmp_path / "spy_marks.csv")} == {"2026-09-01", "2026-09-10"}
    run(tmp_path, session(day=first + dt.timedelta(days=SPY_MARK_DAYS), n=10), [spec()], Always("WAIT"))
    assert {t[:10] for t, _ in _marks(tmp_path / "spy_marks.csv")} == {"2026-09-10", "2026-09-11"}


def test_a_failed_spy_mark_never_breaks_the_tick_or_the_close(tmp_path, session):
    (tmp_path / "spy_marks.csv").mkdir()  # unwritable as a file
    alerts = []
    bars = session(n=30)
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine(
        [spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, tmp_path, alert=lambda _, m: alerts.append(m)
    )
    eng.start_day(bars.index[0].date(), {})
    for ts in bars.index:
        eng.tick((ts + dt.timedelta(minutes=1)).to_pydatetime(), {"SPY": bars.loc[:ts]}, 300)
    assert "sim" in eng.end_day(dt.datetime.combine(bars.index[0].date(), dt.time(16), ET))
    assert (tmp_path / "benchmark.csv").exists()  # the daily benchmark is still recorded
    assert sum("SPY's intraday mark" in a for a in alerts) == 1  # throttled


# ---- the reader ------------------------------------------------------------------


def test_reader_keeps_recent_priced_marks_only():
    rows = [
        {"time": "2026-09-01T10:00-04:00", "spy": "500"},  # more than DETAIL_DAYS before the last
        {"time": "2026-09-10T09:35-04:00", "spy": ""},  # SPY unknown at the mark
        {"time": "2026-09-10T09:40-04:00", "spy": "0"},
        {"time": "2026-09-10T09:45-04:00", "spy": "nan"},
        {"time": "junk", "spy": "1"},
        {"time": "2026-09-10T09:50-04:00"},
        {"time": "2026-09-10T09:55-04:00", "spy": "501.25"},
        {"time": "2026-09-04T16:00-04:00", "spy": "499"},
    ]
    assert dashboard._spy_marks(rows) == {"2026-09-10T09:55-04:00": 501.25, "2026-09-04T16:00-04:00": 499.0}
    assert dashboard._spy_marks([]) == {}


def _client(monkeypatch, runtime):
    monkeypatch.setattr(dashboard.config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    return TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})


def test_data_endpoint_serves_the_marks_and_copes_without_them(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    (runtime / "books" / "paper").mkdir(parents=True)
    (runtime / "books" / "paper" / "equity.csv").write_text(
        "time,equity,nav,hwm\n2026-09-21T09:30-04:00,250.00,1.00000,1.00000\n"
    )
    c = _client(monkeypatch, runtime)
    assert c.get("/api/data").json()["spy_marks"] == {}  # an older runtime: none recorded yet
    (runtime / "spy_marks.csv").write_text("time,spy\n2026-09-21T09:35-04:00,\n2026-09-21T09:40-04:00,501.5\n")
    assert c.get("/api/data").json()["spy_marks"] == {"2026-09-21T09:40-04:00": 501.5}


# ---- the chart's SPY line (JavaScript) -------------------------------------------


def _js(expr: str):
    page = (config.CODE_ROOT / "dashboard" / "static" / "index.html").read_text()
    start = page.index("// ---- SPY line ----")
    block = page[start : page.index("// ---- end SPY line ----", start)]
    assert NODE is not None  # the tests skip without node
    r = subprocess.run(
        [NODE, "-e", block + f"\nconsole.log(JSON.stringify({expr}));"], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


BENCH = [{"date": "2026-09-21", "spy_open": "100", "spy_close": "101"}]
EQUITY = [
    ["2026-09-21T09:30-04:00", 250, 1.0],
    ["2026-09-21T16:00-04:00", 251, 1.004],
    ["2026-09-22T09:30-04:00", 251, 1.004],  # today's opening mark: no SPY yet
    ["2026-09-22T10:00-04:00", 252, 1.008],
    ["2026-09-22T10:05-04:00", 250, 1.0],
]


def _line(marks=None, bench=BENCH, equity=EQUITY):
    args = f"{json.dumps(equity)}, {json.dumps(bench)}" + (f", {json.dumps(marks)}" if marks is not None else "")
    out = _js(f"spyLine({args})")
    return out and {"vals": [None if v is None else round(v, 6) for v in out["vals"]], "from": out["from"]}


js = pytest.mark.skipif(NODE is None, reason="node not installed")


@js
def test_without_marks_spy_is_flat_at_the_previous_close_all_day():
    assert _line()["vals"] == [1.0, 1.01, 1.01, 1.01, 1.01]
    assert _line({}) == _line()


@js
def test_intraday_marks_move_the_line_on_the_same_base():
    out = _line({"2026-09-22T10:00-04:00": 102.01, "2026-09-22T10:05-04:00": 99.99})
    assert out == {"vals": [1.0, 1.01, 1.01, 1.0201, 0.9999], "from": "2026-09-21"}
    # The 1D view rebases on the mark before the range, the previous close: +1% and -1% on it.
    assert out["vals"][3] / out["vals"][1] == pytest.approx(1.01)
    assert out["vals"][4] / out["vals"][1] == pytest.approx(0.99)


@js
def test_a_days_last_mark_is_its_benchmark_close_and_gaps_fall_back():
    marks = {"2026-09-21T16:00-04:00": 100.5, "2026-09-22T10:00-04:00": 0}  # a close-time mark, a bad one
    assert _line(marks)["vals"] == [1.0, 1.01, 1.01, 1.01, 1.01]


@js
def test_a_past_day_with_marks_but_no_benchmark_row_still_plots():
    equity = [*EQUITY[:2], ["2026-09-22T10:00-04:00", 252, 1.008], ["2026-09-23T10:00-04:00", 252, 1.008]]
    out = _line({"2026-09-22T10:00-04:00": 102.0}, equity=equity)
    assert out["vals"] == [1.0, 1.01, 1.02, 1.01]  # 23rd is today: back to the last known close
    assert _line(None, equity=equity)["vals"] == [1.0, 1.01, None, 1.01]  # without it, a gap


@js
def test_no_benchmark_rows_means_no_line_even_with_marks():
    assert _line({"2026-09-22T10:00-04:00": 102.0}, bench=[]) is None


# ---- 1D: SPY from the session's open (spyFromOpen) ----------------------------------

TODAY = ["2026-09-22T09:30-04:00", "2026-09-22T09:35-04:00", "2026-09-22T09:40-04:00", "2026-09-22T09:45-04:00"]


def _open(times=TODAY, bench=BENCH, marks=None):
    out = _js(f"spyFromOpen({json.dumps(times)}, {json.dumps(bench)}, {json.dumps(marks or {})})")
    return out and {
        "vals": [round(v, 6) for v in out["vals"]],
        "gap": None if out["gap"] is None else round(out["gap"], 6),
    }


@js
def test_one_session_starts_level_at_its_open_and_reports_the_gap_apart():
    # Prev close 101; the first mark (102) stands in for the open: a gap, but the line starts at 1.
    out = _open(marks={TODAY[1]: 102.0, TODAY[3]: 103.02})
    assert out["vals"] == [1.0, 1.0, 1.0, 1.01]  # 09:30 and 09:40 carry the last price
    assert out["gap"] == pytest.approx(102 / 101 - 1, abs=1e-6)


@js
def test_once_the_day_has_a_benchmark_row_its_open_and_close_are_used():
    bench = [*BENCH, {"date": "2026-09-22", "spy_open": "102", "spy_close": "100.98"}]
    out = _open(bench=bench, marks={TODAY[1]: 102.51})
    assert out["vals"] == [1.0, 1.005, 1.005, 0.99]  # the last mark is the day's close
    assert out["gap"] == pytest.approx(102 / 101 - 1, abs=1e-6)


@js
def test_no_open_known_means_no_line_and_no_previous_close_means_no_gap():
    assert _open(marks={}) is None
    assert _open(bench=[], marks={TODAY[0]: 100})["gap"] is None
