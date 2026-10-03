"""notify(): where an alert is logged when the runtime alerts.log isn't writable (the strategist as trader)."""

import os
import stat

import pytest

from trader import alerts, config


@pytest.fixture
def paths(tmp_path, monkeypatch):
    posts = []
    monkeypatch.setattr(config, "load_secrets", lambda: {"NTFY_TOPIC": "t"})
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **k: posts.append((url, k["content"])))
    runtime, fallback = tmp_path / "runtime", tmp_path / "strategist" / "strategist-alerts.log"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "STRATEGIST_ALERTS", fallback)
    return runtime, fallback, posts


def _unwritable(runtime):
    # A file where the directory should be: mkdir/open fail with an OSError whoever runs the tests (root included).
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("")


def test_writable_runtime_log_is_used_and_fallback_untouched(paths, capsys):
    runtime, fallback, posts = paths
    alerts.notify("urgent", "kill switch")
    assert "URGENT kill switch" in (runtime / "alerts.log").read_text()
    assert not fallback.exists()
    assert capsys.readouterr().err == ""
    assert posts == [(f"{config.SETTINGS.alerts.ntfy_server.rstrip('/')}/t", b"kill switch")]


def test_unwritable_runtime_log_falls_back_to_strategist_log_readable_by_group_only(paths, capsys):
    runtime, fallback, posts = paths
    _unwritable(runtime)
    alerts.notify("urgent", "strategist premarket: git push failed")
    alerts.notify("info", "second")
    lines = fallback.read_text().splitlines()
    assert lines[0].endswith("URGENT strategist premarket: git push failed") and lines[1].endswith("INFO second")
    mode = stat.S_IMODE(fallback.stat().st_mode)
    assert not mode & (stat.S_IWGRP | stat.S_IRWXO)  # never group-writable (check.sh), never world-readable
    err = capsys.readouterr().err
    assert "git push failed (alerts.log not writable; logged to" in err  # the journal still has it
    assert len(posts) == 2  # ntfy unchanged


def test_both_logs_unwritable_still_goes_to_stderr_and_ntfy(paths, capsys):
    runtime, fallback, posts = paths
    _unwritable(runtime)
    fallback.parent.mkdir(parents=True)
    fallback.mkdir()  # a directory where the file should be
    alerts.notify("urgent", "disk full")
    assert "URGENT disk full (alerts.log not writable)" in capsys.readouterr().err
    assert len(posts) == 1


def test_fallback_defaults_into_the_strategist_checkout_and_is_gitignored():
    if "TRADER_STRATEGIST_ALERTS" in os.environ:
        pytest.skip("default overridden")
    assert config.STRATEGIST_ALERTS.parent == config.STRATEGIST_ROOT
    ignored = (config.CODE_ROOT / ".gitignore").read_text().split()
    assert config.STRATEGIST_ALERTS.name in ignored  # else the wrapper's non-strategy-path check would delete it
