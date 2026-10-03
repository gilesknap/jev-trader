"""The Today page's "Strategist's read on today": the latest daily journal entry's Pre-market and
Post-close sections, and a clear "no entry for today" when the pre-market run hasn't written one."""

import os

import pytest
from fastapi.testclient import TestClient

from trader import config, dashboard
from trader.dashboard import journal_section

ENTRY = """# 2026-10-02 (Fri)

## Pre-market
Futures down; no change.

## Post-close
Quiet day.
### Detail
More.
```
# not a heading
```

## Replay log
- replays
"""


def test_sections_are_cut_at_the_next_section():
    assert journal_section(ENTRY, "Pre-market") == "Futures down; no change."
    assert journal_section(ENTRY, "Post-close") == "Quiet day.\n### Detail\nMore.\n```\n# not a heading\n```"
    assert journal_section(ENTRY, "Weekly") is None


@pytest.mark.parametrize("heading", ["## pre-market", "## PRE-MARKET (09:05 ET)", "## Premarket", "##  Pre market read"])
def test_headings_match_case_insensitively_by_prefix(heading):
    assert journal_section(f"# Day\n{heading}\nES -0.6%.\n# Next\nx", "Pre-market") == "ES -0.6%."


def test_a_deeper_heading_or_a_hashtag_is_not_a_section():
    assert journal_section("### Pre-market\nx\n#Pre-market\ny", "Pre-market") is None


@pytest.fixture
def daily(tmp_path, monkeypatch):
    d = tmp_path / "strategist" / "journal" / "daily"
    d.mkdir(parents=True)
    monkeypatch.setattr(config, "STRATEGIST_ROOT", tmp_path / "strategist")
    monkeypatch.setattr(dashboard, "_ny_today", lambda: "2026-10-02")
    return d


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    return TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})


def test_todays_entry(daily, client):
    (daily / "2026-10-01.md").write_text("## Pre-market\nold\n")
    (daily / "2026-10-02.md").write_text(ENTRY)
    r = client.get("/api/today-read").json()
    assert r["name"] == "2026-10-02.md" and r["is_today"]
    assert r["sections"] == {"premarket": "Futures down; no change.", "postclose": journal_section(ENTRY, "Post-close")}


def test_no_entry_today_shows_the_latest_and_says_so(daily, client):
    (daily / "2026-10-01.md").write_text("## Post-close\nyesterday\n")
    (daily / "2026-10-05.md").write_text("## Pre-market\nmisdated\n")  # a future date is never "the latest"
    r = client.get("/api/today-read").json()
    assert r["name"] == "2026-10-01.md" and not r["is_today"]
    assert r["sections"] == {"premarket": None, "postclose": "yesterday"}


def test_no_entries_at_all(daily, client):
    assert client.get("/api/today-read").json() == {"today": "2026-10-02", "name": None, "is_today": False, "sections": {}}


def test_a_symlinked_entry_is_never_followed(daily, client, tmp_path):
    (tmp_path / "secret.md").write_text("## Pre-market\nSECRET\n")
    os.symlink(tmp_path / "secret.md", daily / "2026-10-02.md")
    r = client.get("/api/today-read").json()
    assert "SECRET" not in str(r)


def test_prompts_pin_the_headings():
    pre = (config.CODE_ROOT / "prompts" / "premarket.md").read_text()
    post = (config.CODE_ROOT / "prompts" / "postclose.md").read_text()
    assert "`## Pre-market`" in pre
    assert "`## Post-close`" in post and "Keep the `## Pre-market` section" in post
