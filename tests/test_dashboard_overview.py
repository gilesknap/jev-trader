import subprocess
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from trader import config, dashboard


@pytest.fixture(autouse=True)
def monorepo(monkeypatch):
    """Today's layout unless a test sets TRADER_DATA_ROOT (#169 split)."""
    monkeypatch.delenv("TRADER_DATA_ROOT", raising=False)


@pytest.mark.parametrize(
    "remote",
    [
        "github-trading:gilesknap/trading",
        "git@github-trading:gilesknap/trading",
        "git@github.com:gilesknap/trading.git",
        "https://github.com/gilesknap/trading.git",
        "https://github.com/gilesknap/trading/",
    ],
)
def test_github_repo_from_remote(remote):
    assert dashboard.github_repo(remote) == "gilesknap/trading"


@pytest.mark.parametrize("remote", [None, "", "not a remote"])
def test_github_repo_unparseable(remote):
    assert dashboard.github_repo(remote) is None


def test_links_build_github_urls_from_one_repo():
    groups = dashboard.links("o/r", "abc123")
    urls = [link["url"] for g in groups for link in g["links"]]
    gh = [u for u in urls if "github.com" in u]
    assert gh and all(u.startswith("https://github.com/o/r") for u in gh)
    assert "https://github.com/o/r/compare/abc123...main" in urls
    assert all(u.startswith("https://") for u in urls)
    assert all(link["label"] and link["hint"] for g in groups for link in g["links"])


def test_links_without_repo_or_commit():
    urls = [link["url"] for g in dashboard.links(None, None) for link in g["links"]]
    assert urls and not any("github.com" in u for u in urls)
    assert not any("/compare/" in link["url"] for g in dashboard.links("o/r", None) for link in g["links"])


def test_monorepo_github_links_are_unchanged():
    """The single-repo group exactly as before the split work (#169 A4 must be a no-op here)."""
    (gh,) = [g for g in dashboard.links("o/r", "abc123") if g["title"].startswith("GitHub")]
    assert gh["title"] == "GitHub (code and the strategist's notes)"
    assert gh["hint"] == "Your weekly job: merge the weekly PR and answer needs-human issues."
    assert [link["url"] for link in gh["links"]] == [
        "https://github.com/o/r",
        "https://github.com/o/r/blob/main/docs/how-to/daily-operations.md",
        "https://github.com/o/r/pulls",
        "https://github.com/o/r/issues?q=is%3Aissue+is%3Aopen+label%3Aneeds-human",
        "https://github.com/o/r/tree/strategist/journal/weekly",
        "https://github.com/o/r/blob/strategist/state/strategy.md",
        "https://github.com/o/r/compare/abc123...main",
    ]


def test_split_links_send_code_and_data_to_their_own_repos():
    groups = dashboard.links("pub/code", "c0de", {"repo": "me/data", "sha": "da7a"})
    by_title = {g["title"]: [link["url"] for link in g["links"]] for g in groups}
    code, data = by_title["GitHub: the code"], by_title["GitHub: your data (config and the strategist's notes)"]
    assert all(u.startswith("https://github.com/pub/code") for u in code)
    assert "https://github.com/pub/code/compare/c0de...main" in code
    assert any(u.endswith("/docs/how-to/daily-operations.md") for u in code)
    assert all(u.startswith("https://github.com/me/data") for u in data)
    for path in (
        "/tree/strategist/journal/weekly",
        "/blob/strategist/state/strategy.md",
        "/pulls",
        "/issues?q=is%3Aissue+is%3Aopen+label%3Aneeds-human",
        "/issues?q=is%3Aissue+label%3Aweekly",
        "/compare/da7a...main",
    ):
        assert f"https://github.com/me/data{path}" in data
    (hint,) = [g["hint"] for g in groups if g["title"].startswith("GitHub: your data")]
    assert "read the weekly issue" in hint and "merge" not in hint
    assert not any(g["title"] == "GitHub (code and the strategist's notes)" for g in groups)
    assert all(link["label"] and link["hint"] for g in groups for link in g["links"])


def test_split_links_without_code_remote_or_config_commit():
    groups = dashboard.links(None, None, {"repo": "me/data", "sha": None})
    urls = [link["url"] for g in groups for link in g["links"]]
    assert not any(g["title"] == "GitHub: the code" for g in groups)
    assert "https://github.com/me/data/tree/strategist/journal/weekly" in urls
    assert not any("/compare/" in u for u in urls)


def test_data_root_is_none_in_the_monorepo(tmp_path, monkeypatch):
    assert dashboard.data_root() is None
    monkeypatch.setattr(dashboard.config, "CODE_ROOT", tmp_path)
    monkeypatch.setenv("TRADER_DATA_ROOT", str(tmp_path))  # pointing at the code root is still the monorepo
    assert dashboard.data_root() is None
    monkeypatch.setenv("TRADER_DATA_ROOT", str(tmp_path / "config"))
    assert dashboard.data_root() == tmp_path / "config"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    return TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})


def _repo(path, remote, subject):
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(git + ["remote", "add", "origin", remote], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", subject], check=True)


def test_overview_reads_local_git(tmp_path, monkeypatch, client):
    _repo(tmp_path, "github-trading:owner/repo", "Deploy me")
    monkeypatch.setattr(dashboard.config, "CODE_ROOT", tmp_path)
    r = client.get("/api/overview").json()
    assert r["repo"] == "owner/repo" and r["data_repo"] == "owner/repo"
    assert r["split"] is False and r["config"] is None
    assert r["deployed"]["subject"] == "Deploy me" and len(r["deployed"]["short"]) == 7
    urls = [link["url"] for g in r["links"] for link in g["links"]]
    assert f"https://github.com/owner/repo/compare/{r['deployed']['sha']}...main" in urls


def test_overview_split_uses_the_data_repo_and_shows_the_config_commit(tmp_path, monkeypatch, client):
    _repo(tmp_path / "main", "https://github.com/pub/code.git", "Code")
    _repo(tmp_path / "config", "git@github-trading:me/data", "Config")
    monkeypatch.setattr(dashboard.config, "CODE_ROOT", tmp_path / "main")
    monkeypatch.setenv("TRADER_DATA_ROOT", str(tmp_path / "config"))
    settings = config.SETTINGS.model_copy(
        update={"owner": config.SETTINGS.owner.model_copy(update={"github_repo": "me/data"})}
    )
    monkeypatch.setattr(dashboard.config, "SETTINGS", settings)
    r = client.get("/api/overview").json()
    assert r["split"] is True and r["repo"] == "pub/code" and r["data_repo"] == "me/data"
    assert r["deployed"]["subject"] == "Code" and r["config"]["subject"] == "Config"
    urls = [link["url"] for g in r["links"] for link in g["links"]]
    assert f"https://github.com/pub/code/compare/{r['deployed']['sha']}...main" in urls
    assert f"https://github.com/me/data/compare/{r['config']['sha']}...main" in urls
    assert "https://github.com/me/data/blob/strategist/state/strategy.md" in urls


def test_overview_split_with_unreadable_config_checkout(tmp_path, monkeypatch, client):
    _repo(tmp_path / "main", "https://github.com/pub/code.git", "Code")
    monkeypatch.setattr(dashboard.config, "CODE_ROOT", tmp_path / "main")
    monkeypatch.setenv("TRADER_DATA_ROOT", str(tmp_path / "missing"))
    r = client.get("/api/overview").json()
    assert r["split"] is True and r["config"] is None and r["deployed"]["subject"] == "Code"
    assert r["data_repo"] == config.SETTINGS.owner.github_repo


def test_overview_without_git(tmp_path, monkeypatch, client):
    monkeypatch.setattr(dashboard.config, "CODE_ROOT", tmp_path / "missing")
    r = client.get("/api/overview").json()
    assert r["repo"] is None and r["deployed"] is None and r["links"]


def test_overview_requires_identity(client):
    assert (
        TestClient(dashboard.app, headers={"Tailscale-User-Login": "stranger@example.com"})
        .get("/api/overview")
        .status_code
        == 403
    )


def test_rules_fill_defaults_and_keep_invalid_specs(tmp_path, monkeypatch, client):
    f = tmp_path / "classifiers.yaml"
    f.write_text("""date: 2026-10-06
classifiers:
  - id: good
    family: novel
    symbols: [SPY]
    features: [vwap_dist_pct]
    context: why
    entry: {instructions: enter, criteria: {ENTER: a, WAIT: b}}
    exit: {instructions: hold, criteria: {HOLD: a, EXIT: b}}
  - id: bad
    symbols: [SPY]
""")
    monkeypatch.setattr(dashboard.config, "CLASSIFIERS_FILE", f)
    r = client.get("/api/rules").json()
    assert r["date"] == "2026-10-06"
    good, bad = r["classifiers"]
    assert good["id"] == "good" and good["stop_pct"] == 0.5 and good["window"] == ["09:45", "15:30"]
    assert bad["id"] == "bad" and "features" in bad["invalid"] and "entry" in bad["invalid"]
    assert any(p.startswith("bad:") for p in r["problems"])


def test_rules_report_file_level_problems(tmp_path, monkeypatch, client):
    spec = """  - id: {id}
    symbols: [{sym}]
    features: [vwap_dist_pct]
    entry: {{instructions: enter, criteria: {{ENTER: a, WAIT: b}}}}
    exit: {{instructions: hold, criteria: {{HOLD: a, EXIT: b}}}}
"""
    f = tmp_path / "classifiers.yaml"
    f.write_text("classifiers:\n" + spec.format(id="a", sym="SPY") + spec.format(id="a", sym="ZZZZ"))
    monkeypatch.setattr(dashboard.config, "CLASSIFIERS_FILE", f)
    monkeypatch.setattr(dashboard.config, "universe", lambda: ["SPY"])
    problems = client.get("/api/rules").json()["problems"]
    assert "a: the id is used twice" in problems
    assert "a: ZZZZ not in config/universe.yaml" in problems


@pytest.mark.parametrize("text", ["- id: x\n", "hello\n", "classifiers: nope\n"])
def test_rules_malformed_file(tmp_path, monkeypatch, client, text):
    f = tmp_path / "classifiers.yaml"
    f.write_text(text)
    monkeypatch.setattr(dashboard.config, "CLASSIFIERS_FILE", f)
    r = client.get("/api/rules")
    assert r.status_code == 200 and r.json()["error"] and r.json()["classifiers"] == []


def test_rules_without_file(tmp_path, monkeypatch, client):
    monkeypatch.setattr(dashboard.config, "CLASSIFIERS_FILE", tmp_path / "missing.yaml")
    r = client.get("/api/rules").json()
    assert r["classifiers"] == [] and "couldn't read" in r["error"]


def test_rules_broken_universe_is_a_problem(tmp_path, monkeypatch, client):
    f = tmp_path / "classifiers.yaml"
    f.write_text("classifiers: []\n")
    monkeypatch.setattr(dashboard.config, "CLASSIFIERS_FILE", f)
    monkeypatch.setattr(dashboard.config, "universe", lambda: set(cast(Any, 5)))  # TypeError, as from a broken universe
    r = client.get("/api/rules")
    assert r.status_code == 200 and "universe" in r.json()["problems"][0]


def test_equity_points_keep_a_week_of_detail_and_daily_closes_before_it():
    from trader.dashboard import _equity_points

    rows = [
        {"time": f"2026-09-{d:02d}T{h}-04:00", "equity": "250", "nav": "1.0"}
        for d in range(1, 30)
        for h in ("09:35", "12:00", "15:55")
    ]
    rows.append({"time": "junk", "equity": "x", "nav": "1"})  # malformed: skipped
    rows.append({"time": "junk", "equity": "250", "nav": "1.0"})  # an undatable mark: skipped too
    pts = _equity_points(rows)
    assert all(p[0] != "junk" for p in pts)
    days = [p[0][:10] for p in pts]
    assert days.count("2026-09-22") == 1 and pts[days.index("2026-09-22")][0].endswith("15:55-04:00")
    assert all(days.count(f"2026-09-{d}") == 3 for d in range(23, 30))
    assert _equity_points([]) == []
