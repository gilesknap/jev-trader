"""The dashboard reads the strategist's checkout as the runner does (safeio): a symlink planted
there gives an empty view or a clean error, never another of runner's files, and never a 500."""

import shutil

import pytest
from fastapi.testclient import TestClient

from trader import config, dashboard

SECRET = "ALPACA_LIVE_KEY=hunter2secret"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A strategist checkout with every file the dashboard shows, and a runner secret beside it."""
    root = tmp_path / "strategist"
    for d in ("state", "logs", "journal/daily", "journal/weekly"):
        (root / d).mkdir(parents=True)
    (root / "state" / "strategy.md").write_text("# Strategy\n")
    (root / "state" / "watchlist.md").write_text("# Ideas\n")
    (root / "state" / "classifiers.yaml").write_text("classifiers: []\n")
    (root / "logs" / "probe_report.json").write_text('{"probes": {}}')
    (root / "journal" / "daily" / "2026-10-02.md").write_text("# Day\n")
    (tmp_path / "secret.env").write_text(f"# runner secrets\n{SECRET}\n")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.md").write_text(SECRET)
    monkeypatch.setattr(config, "STRATEGIST_ROOT", root)
    monkeypatch.setattr(config, "CLASSIFIERS_FILE", root / "state" / "classifiers.yaml")
    return root


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    return TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})


def _swap_for_link(path, target):
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    path.symlink_to(target)


def test_real_files_are_shown(checkout, client):
    docs = client.get("/api/docs").json()
    assert docs["strategy"] == "# Strategy\n" and docs["watchlist"] == "# Ideas\n"
    assert docs["classifiers"] == "classifiers: []\n"
    assert docs["journal"]["daily"] == ["2026-10-02.md"] and docs["journal"]["monthly"] == []
    assert client.get("/api/journal/daily/2026-10-02.md").json()["text"] == "# Day\n"
    assert client.get("/api/probes").json()["report"] == {"probes": {}}
    r = client.get("/api/rules").json()
    assert r["classifiers"] == [] and "error" not in r


@pytest.mark.parametrize("rel", ["state/strategy.md", "state/watchlist.md", "state/classifiers.yaml"])
def test_symlinked_doc_is_empty(checkout, client, tmp_path, rel):
    _swap_for_link(checkout / rel, tmp_path / "secret.env")
    r = client.get("/api/docs")
    assert r.status_code == 200 and "hunter2" not in r.text
    assert r.json()[rel.split("/")[1].split(".")[0]] is None


def test_symlinked_state_directory_empties_every_state_view(checkout, client, tmp_path):
    (tmp_path / "elsewhere" / "classifiers.yaml").write_text("classifiers: [{id: leaked}]\n")
    _swap_for_link(checkout / "state", tmp_path / "elsewhere")
    for path in ("/api/docs", "/api/rules", "/api/scoreboard"):
        r = client.get(path)
        assert r.status_code == 200 and "leaked" not in r.text
    assert "symlink" in client.get("/api/rules").json()["error"]


def test_symlinked_classifiers_file_is_a_clean_rules_error(checkout, client, tmp_path):
    _swap_for_link(checkout / "state" / "classifiers.yaml", tmp_path / "secret.env")
    r = client.get("/api/rules")
    assert r.status_code == 200 and "hunter2" not in r.text
    assert r.json()["classifiers"] == [] and "symlink" in r.json()["error"]
    assert dashboard._classifiers_file() == []


def test_symlinked_probe_report_is_no_report(checkout, client, tmp_path):
    (tmp_path / "report.json").write_text('{"probes": {"leaked": 1}}')
    _swap_for_link(checkout / "logs" / "probe_report.json", tmp_path / "report.json")
    r = client.get("/api/probes")
    assert r.status_code == 200 and r.json() == {"report": None, "age_s": None}


def test_symlinked_journal_entry_is_listed_but_not_read(checkout, client, tmp_path):
    (checkout / "journal" / "daily" / "2026-10-03.md").symlink_to(tmp_path / "elsewhere" / "secret.md")
    r = client.get("/api/journal/daily/2026-10-03.md")
    assert r.status_code == 200 and r.json()["text"] is None and "hunter2" not in r.text


@pytest.mark.parametrize("link", ["journal", "journal/weekly"])
def test_symlinked_journal_directory_lists_nothing(checkout, client, tmp_path, link):
    (tmp_path / "elsewhere" / "weekly").mkdir()
    (tmp_path / "elsewhere" / "weekly" / "secret.md").write_text(SECRET)  # reached via a journal/ link
    _swap_for_link(checkout / link, tmp_path / "elsewhere" / ("" if link == "journal" else "weekly"))
    r = client.get("/api/docs")
    assert r.status_code == 200 and r.json()["journal"]["weekly"] == []
    assert "secret.md" not in r.text
    assert client.get("/api/journal/weekly/secret.md").status_code == 404


def test_fifo_and_non_utf8_files_are_empty_not_a_hang_or_500(checkout, client):
    import os

    (checkout / "state" / "strategy.md").unlink()
    os.mkfifo(checkout / "state" / "strategy.md")
    (checkout / "state" / "watchlist.md").write_bytes(b"\xff\xfe")
    (checkout / "state" / "classifiers.yaml").write_bytes(b"\xff\xfe")
    docs = client.get("/api/docs").json()
    assert docs["strategy"] is None and docs["watchlist"] is None
    assert client.get("/api/rules").status_code == 200
