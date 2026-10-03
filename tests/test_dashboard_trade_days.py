"""The Trades page groups fills by New York trading day and says when none are from today.

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
    start = page.index("// ---- trades by New York trading day ----")
    return page[start : page.index("\n\n", start)]


def _run(expr: str):
    assert NODE is not None  # the tests skip without node
    r = subprocess.run(
        [NODE, "-e", _block() + f"\nconsole.log(JSON.stringify({expr}));"], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _t(time, sym="SPY"):
    return {"time": time, "symbol": sym}


def test_no_trades_today_says_so_and_names_the_day():
    trades = [_t("2026-09-29T15:44-04:00"), _t("2026-09-29T10:01-04:00"), _t("2026-09-28T11:00-04:00")]
    out = _run(f"tradeDays({json.dumps(trades)}, '2026-09-30')")
    assert out["nToday"] == 0
    # ICU writes "Sep" or "Sept" depending on its version.
    assert re.fullmatch(r"No trades today yet\. Showing the most recent, from Tue 29 Sept?\.", out["note"])
    assert [(r["day"], r["first"]) for r in out["rows"]] == [
        ("2026-09-29", True),
        ("2026-09-29", False),
        ("2026-09-28", True),
    ]


def test_trades_today_are_labelled_today_and_counted():
    trades = [_t("2026-09-30T09:45-04:00"), _t("2026-09-29T15:44-04:00")]
    out = _run(f"tradeDays({json.dumps(trades)}, '2026-09-30')")
    assert out["nToday"] == 1 and out["note"] == ""
    assert [r["first"] for r in out["rows"]] == [True, True]
    assert re.fullmatch(r"Today, Wed 30 Sept?", _run("dayLabel('2026-09-30', '2026-09-30')"))


def test_no_trades_at_all_has_no_note():
    assert _run("tradeDays([], '2026-09-30')") == {"rows": [], "nToday": 0, "note": ""}


def test_today_is_the_new_york_date_not_the_utc_one():
    # 02:30 UTC on 1 Oct is still 30 Sep in New York.
    assert _run("nyToday(new Date('2026-10-01T02:30:00Z'))") == "2026-09-30"


def test_a_replay_has_no_today():
    trades = [_t("2026-09-29T15:44-04:00"), _t("2026-09-29T10:01-04:00"), _t("2026-09-28T11:00-04:00")]
    out = _run(f"tradeDays({json.dumps(trades)}, null)")
    assert out["note"] == "" and out["nToday"] == 2  # the badge counts the latest day shown
    assert [r["first"] for r in out["rows"]] == [True, False, True]
    assert not _run("dayLabel('2026-09-29', null)").startswith("Today")
