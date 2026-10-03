"""The race chart's 1D/7D/1M/1Y filter re-bases each rule's running P&L to zero at the range start.

The logic is plain JavaScript in index.html; these tests run that block under node (skipped without it).
"""

import json
import re
import shutil
import subprocess

import pytest

from trader import config

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _block() -> str:
    page = (config.CODE_ROOT / "dashboard" / "static" / "index.html").read_text()
    start = page.index("// ---- time ranges (Performance and the race) ----")
    return page[start : page.index("// ---- end time ranges ----", start)]


def _run(expr: str):
    r = subprocess.run(
        [NODE, "-e", _block() + f"\nconsole.log(JSON.stringify({expr}));"], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


DAYS = ["2026-08-20", "2026-09-21", "2026-09-25", "2026-09-29", "2026-09-30"]
ROWS = [
    {"id": "a", "curve": [1.0, 1.5, 1.2, 2.0, 2.5]},
    {"id": "b", "curve": [0.0, -0.5, -0.5, -0.5, -0.5]},  # nothing since 21 Sep
    {"id": "c", "curve": [0.0, 0.0, 0.0, 0.3, 0.3]},
]


def _win(span):
    return _run(f"raceWindow({json.dumps(DAYS)}, {json.dumps(ROWS)}, {span})")


def test_a_range_rebases_to_the_close_before_it():
    out = _win(7)  # 24 Sep to 30 Sep
    assert out["days"] == DAYS[2:]
    assert {r["id"]: r["curve"] for r in out["rows"]} == {"a": [-0.3, 0.5, 1.0], "c": [0.0, 0.3, 0.3]}
    assert out["quiet"] == 1  # b made nothing in the range: left out, and counted


def test_one_day_is_the_latest_session_only():
    out = _win(1)
    assert out["days"] == ["2026-09-30"]
    assert [(r["id"], r["curve"]) for r in out["rows"]] == [("a", [0.5])]
    assert out["quiet"] == 2


def test_a_range_covering_everything_starts_from_zero_not_the_first_day():
    out = _win(365)
    assert out["days"] == DAYS and out["quiet"] == 0
    assert out["rows"][0]["curve"] == [1.0, 1.5, 1.2, 2.0, 2.5]  # unchanged: the base is 0
    # Nothing is cut off, so a rule that traded but nets zero overall still shows.
    flat = _run(f"raceWindow({json.dumps(DAYS[:2])}, [{{id: 'z', curve: [0.0, 0.0]}}], 365)")
    assert [r["id"] for r in flat["rows"]] == ["z"] and flat["quiet"] == 0


def test_no_days_is_empty():
    assert _run("raceWindow([], [], 7)") == {"days": [], "rows": [], "quiet": 0}


def test_range_start_counts_calendar_days():
    assert _run("rangeFrom('2026-09-30', 7)") == "2026-09-24"
    assert _run("rangeFrom('2026-09-30', 1)") == "2026-09-30"


def test_the_subtitle_names_the_dates_the_range_covers():
    # ICU writes "Sep" or "Sept" depending on its version.
    assert re.fullmatch(r"1D · Tue 29 Sept?", _run("raceSpan('1d', ['2026-09-29'])"))
    assert re.fullmatch(r"7D · 23–29 Sept?", _run("raceSpan('7d', ['2026-09-23', '2026-09-25', '2026-09-29'])"))
    assert re.fullmatch(r"1M · 28 Aug – 29 Sept?", _run("raceSpan('1m', ['2026-08-28', '2026-09-29'])"))
    assert re.fullmatch(r"1Y · 30 Dec 2025 – 5 Jan 2026", _run("raceSpan('1y', ['2025-12-30', '2026-01-05'])"))
    assert _run("raceSpan('7d', [])") == "7D"
