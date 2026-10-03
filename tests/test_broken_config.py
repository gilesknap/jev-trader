"""A malformed config.yaml must not take the last resorts with it (#146): a fresh `trader stop` still
flags and flattens, the watchdog still runs (and says the file is broken), and notify still pushes.
Strict validation stays where it matters: `trader validate`, every other command, and the runner's
session start, which must never trade on a wrong start date."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_alerts_robustness import _watchdog_env, et
from trader import alerts, config, runner

ROOT = Path(__file__).resolve().parents[1]
SECRET_VARS = ("ALPACA_", "OPENROUTER_API_KEY", "NTFY_TOPIC")
UNCLOSED = b"owner: [unclosed\n"
LATIN_1 = "owner:\n  name: Ren\u00e9e\n".encode("latin-1")  # not UTF-8: a UnicodeDecodeError, not a YAMLError


@pytest.fixture
def broken(tmp_path, monkeypatch):
    """This process's config.yaml is malformed and not yet loaded, as in a fresh process. Undoing puts
    the loaded settings back."""
    bad = tmp_path / "config.yaml"
    bad.write_text("owner: [unclosed\n")
    monkeypatch.setattr(config, "SETTINGS_FILE", bad)
    monkeypatch.delattr(config, "SETTINGS")
    return bad


def _fresh(tmp_path, *args, script=None, content=UNCLOSED, **env):
    """A fresh interpreter on a malformed TRADER_CONFIG, with its own runtime dir, HOME and no secrets."""
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(content)
    base = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_VARS) and k != "TRADER_SECRETS"}
    base |= {"HOME": str(tmp_path), "TRADER_CONFIG": str(bad), "TRADER_RUNTIME": str(tmp_path / "runtime")}
    cmd = [sys.executable, "-c", script] if script else [sys.executable, "-m", "trader.cli", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=base | env, timeout=120)


FRESH = """
import json, httpx
from trader import config, runner
posts = []
httpx.post = lambda url, **k: posts.append(url)
runner.AlpacaBroker = None  # no keys: nothing may try the broker but the calendar, which fails
from trader import cli
for argv in (["stop"], ["watchdog"]):
    cli.main(argv)
from trader.alerts import notify
notify("urgent", "strategist postclose: run failed")
print(json.dumps(posts))
"""


@pytest.mark.parametrize("content", [UNCLOSED, LATIN_1], ids=["yaml", "latin-1"])
def test_fresh_stop_watchdog_and_notify_work_with_a_malformed_config(tmp_path, content):
    (tmp_path / "runtime" / "books" / "paper").mkdir(parents=True)
    r = _fresh(tmp_path, script=FRESH, content=content, NTFY_TOPIC="t")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.splitlines()[-1]) == [f"{alerts.DEFAULT_NTFY_SERVER}/t"] * 4  # stop, 2 watchdog, notify
    assert json.loads((tmp_path / "runtime" / "books" / "paper" / "stop.json").read_text())["stop_on"]
    log = (tmp_path / "runtime" / "alerts.log").read_text()
    assert "STOP flagged for ['paper']" in log
    assert "WATCHDOG: config.yaml unreadable" in log and "bad.yaml: can't read deployment settings" in log
    assert "strategist postclose: run failed" in log


@pytest.mark.parametrize("argv", [["validate"], ["run", "--decider", "stub"], ["golive"], ["config", "get", "owner"]])
def test_other_commands_still_refuse_a_malformed_config(tmp_path, argv):
    (tmp_path / "runtime").mkdir()
    r = _fresh(tmp_path, *argv)
    assert r.returncode != 0
    assert "bad.yaml: can't read deployment settings" in r.stderr and "Traceback" not in r.stderr
    assert not (tmp_path / "runtime" / "status.json").exists()


def test_stop_flattens_directly_with_a_malformed_config(broken, tmp_path, monkeypatch):
    posts, flattened = [], []
    monkeypatch.setattr(runner, "BOOKS_DIR", tmp_path / "books")
    (tmp_path / "books" / "live").mkdir(parents=True)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)  # no status.json: the daemon looks dead
    monkeypatch.setattr(
        config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s", "NTFY_TOPIC": "t"}
    )

    class Broker:
        def __init__(self, key, secret, paper):
            self.client = self
            self.paper = paper

        def close_all_positions(self, cancel_orders):
            flattened.append(self.paper)

    monkeypatch.setattr(runner, "AlpacaBroker", Broker)
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **k: posts.append((url, k["content"])))
    msg = runner.request_stop()
    assert "flattened paper directly" in msg and flattened == [True]
    assert (tmp_path / "books" / "live" / "stop.json").exists()
    assert posts == [(f"{alerts.DEFAULT_NTFY_SERVER}/t", msg.encode())]


def test_run_session_refuses_a_malformed_config_before_anything_else(broken, monkeypatch):
    monkeypatch.setattr(config, "load_secrets", lambda: pytest.fail("got past the settings check"))
    with pytest.raises(config.SettingsError, match="can't read deployment settings"):
        runner.run_session(decider_name="stub")


def test_watchdog_alerts_hourly_on_a_malformed_config_and_skips_the_deadline(broken, tmp_path, monkeypatch):
    fri = et(2026, 10, 9, 16)
    sent = _watchdog_env(tmp_path, monkeypatch, close=fri)
    t = et(2026, 10, 10, 9)  # Friday's post-close run is overdue, but its deadline needs schedule.*
    out = runner.watchdog(t)
    assert "config.yaml unreadable" in out and "no post-close" not in out
    runner.watchdog(t.replace(minute=30))
    assert len(sent) == 1 and str(broken) in sent[0] and "\n" not in sent[0]
    runner.watchdog(t.replace(hour=10, minute=1))
    assert len(sent) == 2


def test_a_config_that_is_not_utf8_is_a_settings_error(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_bytes(LATIN_1)
    with pytest.raises(config.SettingsError, match="can't read deployment settings.*utf-8"):
        config.load_settings(bad)


def test_notify_pushes_to_the_default_server_whatever_breaks_the_settings(monkeypatch):
    posts = []
    monkeypatch.setattr(config, "load_secrets", lambda: {"NTFY_TOPIC": "t"})
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **k: posts.append(url))
    monkeypatch.delattr(config, "SETTINGS")
    monkeypatch.setattr(config, "get_settings", lambda: 1 / 0)  # not even a SettingsError
    alerts.notify("urgent", "kill switch")
    assert posts == [f"{alerts.DEFAULT_NTFY_SERVER}/t"]


def test_settings_load_on_first_use_and_a_failure_is_not_cached(tmp_path):
    loaded = config.get_settings()
    bad, good = tmp_path / "bad.yaml", tmp_path / "good.yaml"
    bad.write_text("owner: [unclosed\n")
    good.write_text(config.SETTINGS_FILE.read_text())
    mp = pytest.MonkeyPatch()
    mp.delattr(config, "SETTINGS")  # as in a fresh process: not loaded yet
    mp.setattr(config, "SETTINGS_FILE", bad)
    with pytest.raises(config.SettingsError):
        config.SETTINGS  # noqa: B018
    mp.setattr(config, "SETTINGS_FILE", good)  # fixed: the next use loads it
    assert config.SETTINGS == config.load_settings(good) and config.SETTINGS is config.get_settings()  # then cached
    mp.undo()
    assert config.SETTINGS is loaded  # what was loaded before, not the copy cached during the test


def test_a_monkeypatched_settings_is_undone_to_the_loaded_one():
    loaded = config.get_settings()
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "SETTINGS", loaded.model_copy(update={"models": config.Models(jev="x", strategist="y")}))
    assert config.get_settings().models.jev == "x" and config.setting("models.jev") == "x"
    mp.undo()
    assert config.get_settings() is loaded and config.SETTINGS is loaded
