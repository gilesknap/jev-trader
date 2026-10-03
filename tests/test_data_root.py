"""DATA_ROOT (#169, part A1): where the human-owned deployment data lives.

Unset, every path resolves exactly as before (one checkout holds code and data). Set to another
directory ("split mode"), it moves config.yaml, config/mode.yaml, the .env fallback and the
rendered deploy files, plus STRATEGIST_ROOT's default; nothing else. config.py resolves its paths
at import (and caches SETTINGS once loaded), so those checks run in a fresh interpreter with a
scrubbed environment: nothing leaks into, or in from, the other tests.

CODE is the code root those subprocesses run with: this tree when it holds config.yaml (the monorepo),
otherwise (the public code repo, whose data lives elsewhere) a temp code tree built from this one's
templates and universe plus the conftest's data-root config, passed as TRADER_CODE_ROOT. The trader
package itself always imports from this tree.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from trader import config

ROOT = Path(__file__).resolve().parents[1]  # the tree under test
CODE: Path = ROOT  # CODE_ROOT in the subprocesses: set by the code_root fixture below
CODE_ENV: dict = {}  # what puts it there ({} when CODE is ROOT)


@pytest.fixture(autouse=True, scope="module")
def code_root(tmp_path_factory):
    """A code root holding config.yaml, config/mode.yaml and up-to-date rendered deploy files, as this tree
    does in the monorepo. In the public layout (no ROOT/config.yaml) build one: the templates and universe
    from this tree, config.yaml and mode.yaml from the data root this process loaded (tests/conftest.py's)."""
    global CODE, CODE_ENV
    if (ROOT / "config.yaml").exists():
        CODE, CODE_ENV = ROOT, {}
    else:
        CODE = tmp_path_factory.mktemp("code")
        shutil.copytree(ROOT / "deploy" / "templates", CODE / "deploy" / "templates")
        (CODE / "config").mkdir()
        shutil.copy(ROOT / "config" / "universe.yaml", CODE / "config" / "universe.yaml")
        shutil.copy(config.SETTINGS_FILE, CODE / "config.yaml")
        mode = config.MODE_FILE.read_text() if config.MODE_FILE.exists() else "mode: auto\n"
        (CODE / "config" / "mode.yaml").write_text(mode)
        for rel, text in config.render_deploy(CODE, CODE).items():
            (CODE / rel).parent.mkdir(parents=True, exist_ok=True)
            (CODE / rel).write_text(text)
        CODE_ENV = {"TRADER_CODE_ROOT": str(CODE)}
    yield CODE
    CODE, CODE_ENV = ROOT, {}


SHOW = """
import json
from trader import config as c
print(json.dumps({
    "CODE_ROOT": str(c.CODE_ROOT), "DATA_ROOT": str(c.DATA_ROOT), "STRATEGIST_ROOT": str(c.STRATEGIST_ROOT),
    "RUNTIME_DIR": str(c.RUNTIME_DIR), "REPLAY_DIR": str(c.REPLAY_DIR),
    "SECRETS_FILES": [str(p) for p in c.SECRETS_FILES],
    "STRATEGIST_STAMP": str(c.STRATEGIST_STAMP), "POSTCLOSE_STAMP": str(c.POSTCLOSE_STAMP),
    "STRATEGIST_ALERTS": str(c.STRATEGIST_ALERTS), "CLASSIFIERS_FILE": str(c.CLASSIFIERS_FILE),
    "CUSTOM_FEATURES_DIR": str(c.CUSTOM_FEATURES_DIR), "MODE_FILE": str(c.MODE_FILE),
    "UNIVERSE_FILE": str(c.UNIVERSE_FILE), "SETTINGS_FILE": str(c.SETTINGS_FILE),
    "split": c.split_mode(), "repo": c.SETTINGS.owner.github_repo,
}))
"""


def _env(home: Path, **env) -> dict:
    """The current environment minus every TRADER_* variable and every secret load_secrets() lets the
    environment override, with HOME moved (no services.env, no ~/.config/trading/env), plus CODE_ENV and
    `env` (a None value drops that variable)."""
    secrets = ("ALPACA_", "OPENROUTER_API_KEY", "NTFY_TOPIC")
    base = {k: v for k, v in os.environ.items() if not k.startswith(("TRADER_", *secrets))}
    return {k: v for k, v in (base | {"HOME": str(home)} | CODE_ENV | env).items() if v is not None}


def _run(args: list[str], home: Path, cwd: Path | str = ROOT, **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=cwd, env=_env(home, **env))


def _paths(home: Path, cwd: Path | str = ROOT, **env) -> dict:
    r = _run(["-c", SHOW], home, cwd, **env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _data_root(tmp_path: Path, mode: str | None = "paper", repo: str = "someone/their-data") -> Path:
    """A data checkout: config.yaml (the tree's own, with another repo slug so we can tell which file
    loaded) and, unless mode is None, config/mode.yaml."""
    d = tmp_path / "data"
    (d / "config").mkdir(parents=True)
    raw = yaml.safe_load((CODE / "config.yaml").read_text())
    raw["owner"]["github_repo"] = repo
    (d / "config.yaml").write_text(yaml.safe_dump(raw))
    if mode:
        (d / "config" / "mode.yaml").write_text(f"mode: {mode}\n")
    return d


def _old_layout(code: Path) -> dict:
    """What every path was before DATA_ROOT existed, all derived from CODE_ROOT."""
    return {
        "CODE_ROOT": str(code),
        "STRATEGIST_ROOT": str(code),
        "RUNTIME_DIR": str(code / "runtime"),
        "REPLAY_DIR": str(code / "runtime" / "replay"),
        "STRATEGIST_STAMP": str(code / ".last_run"),
        "POSTCLOSE_STAMP": str(code / ".last_postclose"),
        "STRATEGIST_ALERTS": str(code / "strategist-alerts.log"),
        "CLASSIFIERS_FILE": str(code / "state" / "classifiers.yaml"),
        "CUSTOM_FEATURES_DIR": str(code / "features" / "custom"),
        "MODE_FILE": str(code / "config" / "mode.yaml"),
        "UNIVERSE_FILE": str(code / "config" / "universe.yaml"),
        "SETTINGS_FILE": str(code / "config.yaml"),
    }


# ---- no-op in today's layout --------------------------------------------------------------


@pytest.mark.parametrize("data_env", [None, "", "same"], ids=["unset", "empty", "equal-to-code-root"])
def test_unset_data_root_changes_no_path(tmp_path, data_env):
    env = {} if data_env is None else {"TRADER_DATA_ROOT": str(CODE) if data_env == "same" else ""}
    got = _paths(tmp_path, **env)
    code = Path(got["CODE_ROOT"])
    assert code == CODE
    assert {k: got[k] for k in _old_layout(code)} == _old_layout(code)
    assert got["SECRETS_FILES"] == [str(code / ".env"), str(tmp_path / ".config" / "trading" / "env")]
    assert Path(got["DATA_ROOT"]).resolve() == code.resolve()
    assert got["split"] is False
    assert got["repo"] == config.load_settings(CODE / "config.yaml").owner.github_repo


def test_existing_overrides_still_win_without_data_root(tmp_path):
    got = _paths(
        tmp_path,
        TRADER_STRATEGIST_ROOT=str(tmp_path / "s"),
        TRADER_RUNTIME=str(tmp_path / "rt"),
        TRADER_SECRETS=str(tmp_path / "sec"),
        TRADER_CONFIG=str(CODE / "config.yaml"),
    )
    assert got["STRATEGIST_ROOT"] == str(tmp_path / "s")
    assert got["CLASSIFIERS_FILE"] == str(tmp_path / "s" / "state" / "classifiers.yaml")
    assert got["RUNTIME_DIR"] == str(tmp_path / "rt") and got["REPLAY_DIR"] == str(tmp_path / "rt" / "replay")
    assert got["SECRETS_FILES"][:2] == [str(tmp_path / "sec"), str(CODE / ".env")]
    assert got["MODE_FILE"] == str(CODE / "config" / "mode.yaml") and got["split"] is False


# ---- what TRADER_DATA_ROOT moves ----------------------------------------------------------


def test_data_root_moves_config_mode_and_env_fallback_only(tmp_path):
    data = _data_root(tmp_path)
    got = _paths(tmp_path, TRADER_DATA_ROOT=str(data))
    old = _old_layout(CODE)
    assert got["DATA_ROOT"] == str(data) and got["split"] is True
    assert got["SETTINGS_FILE"] == str(data / "config.yaml")
    assert got["repo"] == "someone/their-data"  # SETTINGS really loaded from the data root
    assert got["MODE_FILE"] == str(data / "config" / "mode.yaml")
    assert got["SECRETS_FILES"][0] == str(data / ".env")
    # Unchanged: the code, the universe (it must match the allocator's buckets), the runtime, replays.
    for k in ("CODE_ROOT", "UNIVERSE_FILE", "RUNTIME_DIR", "REPLAY_DIR"):
        assert got[k] == old[k], k
    # STRATEGIST_ROOT defaults to DATA_ROOT (a single data checkout on a laptop)...
    assert got["STRATEGIST_ROOT"] == str(data)
    assert got["CLASSIFIERS_FILE"] == str(data / "state" / "classifiers.yaml")
    # ...but its own variable still wins (the VPS: a runner-owned config checkout, a trader-owned strategist one).
    got = _paths(tmp_path, TRADER_DATA_ROOT=str(data), TRADER_STRATEGIST_ROOT=str(tmp_path / "s"))
    assert got["STRATEGIST_ROOT"] == str(tmp_path / "s") and got["MODE_FILE"] == str(data / "config" / "mode.yaml")
    assert got["STRATEGIST_ALERTS"] == str(tmp_path / "s" / "strategist-alerts.log")


def test_code_root_defaults_to_the_packages_own_tree(tmp_path):
    """Without TRADER_CODE_ROOT, CODE_ROOT is the tree the package imports from, in either layout (a data
    root supplies the config, since the public code tree has none)."""
    data = _data_root(tmp_path)
    got = _paths(tmp_path, TRADER_DATA_ROOT=str(data), TRADER_CODE_ROOT=None)
    assert got["CODE_ROOT"] == str(ROOT) and got["split"] is True
    assert got["UNIVERSE_FILE"] == str(ROOT / "config" / "universe.yaml")
    assert got["RUNTIME_DIR"] == str(ROOT / "runtime") and got["SETTINGS_FILE"] == str(data / "config.yaml")


def test_a_relative_data_root_is_made_absolute_once_at_import(tmp_path):
    data = _data_root(tmp_path)
    got = _paths(tmp_path, cwd=tmp_path, TRADER_DATA_ROOT="data")
    assert got["DATA_ROOT"] == str(data) and got["SETTINGS_FILE"] == str(data / "config.yaml")
    assert got["MODE_FILE"] == str(data / "config" / "mode.yaml") and got["repo"] == "someone/their-data"
    code = (
        "import os; from trader import config as c; os.chdir('/'); "
        "print(c.DATA_ROOT, c.split_mode(), c.load_settings().owner.github_repo)"
    )
    r = _run(["-c", code], tmp_path, cwd=tmp_path, TRADER_DATA_ROOT="data")
    assert r.returncode == 0 and r.stdout.split() == [str(data), "True", "someone/their-data"], r.stderr


def test_trader_config_still_overrides_the_data_root(tmp_path):
    data = _data_root(tmp_path)
    got = _paths(tmp_path, TRADER_DATA_ROOT=str(data), TRADER_CONFIG=str(CODE / "config.yaml"))
    assert got["SETTINGS_FILE"] == str(CODE / "config.yaml") and got["MODE_FILE"] == str(data / "config" / "mode.yaml")


def test_secrets_fall_back_to_the_data_roots_env(tmp_path):
    data = _data_root(tmp_path)
    # A key load_secrets() never takes from the environment, so an exported value can't mask the file.
    (data / ".env").write_text("A1_PROBE_KEY=from-data-root\n")
    code = "from trader import config; print(config.load_secrets().get('A1_PROBE_KEY'))"
    r = _run(["-c", code], tmp_path, TRADER_DATA_ROOT=str(data))
    assert r.returncode == 0 and r.stdout.strip() == "from-data-root", r.stderr


def test_missing_config_names_trader_data_root(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run(["-c", "import trader.config as c; c.SETTINGS"], tmp_path, TRADER_DATA_ROOT=str(empty))
    assert r.returncode != 0
    assert str(empty / "config.yaml") in r.stderr and "TRADER_DATA_ROOT" in r.stderr


# ---- split mode: the mode file is mandatory -----------------------------------------------


def test_split_mode_requires_the_mode_file(tmp_path, monkeypatch):
    absent = tmp_path / "config" / "mode.yaml"
    monkeypatch.setattr(config, "MODE_FILE", absent)
    monkeypatch.setattr(config, "DATA_ROOT", config.CODE_ROOT)
    assert not config.split_mode()
    config.require_mode_file()  # single checkout: a missing file still reads as auto, as before
    config.require_mode_file(tmp_path, tmp_path)  # the rule decides from its arguments
    with pytest.raises(config.SettingsError, match="mode.yaml is missing"):
        config.require_mode_file(tmp_path, config.CODE_ROOT)
    from trader import golive

    assert golive.override() == "auto"
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    assert config.split_mode()
    with pytest.raises(config.SettingsError, match="mode.yaml is missing"):
        config.require_mode_file()
    with pytest.raises(config.SettingsError, match="mode.yaml is missing"):
        config.account_mode()
    absent.parent.mkdir()
    absent.write_text("mode: paper\n")
    config.require_mode_file()
    assert config.account_mode() == "paper"


def test_golive_command_refuses_a_split_data_root_without_mode_file(tmp_path):
    data = _data_root(tmp_path, mode=None)
    rt = tmp_path / "runtime"
    rt.mkdir()
    r = _run(["-m", "trader.cli", "golive"], tmp_path, TRADER_DATA_ROOT=str(data), TRADER_RUNTIME=str(rt))
    assert r.returncode != 0 and "mode.yaml is missing" in r.stderr
    (data / "config" / "mode.yaml").write_text("mode: paper\n")
    r = _run(["-m", "trader.cli", "golive"], tmp_path, TRADER_DATA_ROOT=str(data), TRADER_RUNTIME=str(rt))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["override"] == "paper" and out["effective"] == "paper"


# ---- render-deploy: templates from code, config and output under data ---------------------


def test_render_deploy_reads_templates_from_code_and_config_from_data(tmp_path):
    data = _data_root(tmp_path)
    raw = yaml.safe_load((data / "config.yaml").read_text())
    raw["schedule"]["runner_start"] = "07:07"
    (data / "config.yaml").write_text(yaml.safe_dump(raw))
    files = config.render_deploy(CODE, data)
    assert set(files) == set(config.render_deploy(CODE, CODE))  # same relative paths
    assert "OnCalendar=Mon..Fri 07:07 " in files["deploy/systemd/trader-runner.timer"]


def test_render_deploy_into_a_separate_data_root_needs_its_mode_file(tmp_path):
    data = _data_root(tmp_path, mode=None)
    with pytest.raises(config.SettingsError, match="mode.yaml is missing"):
        config.render_deploy(CODE, data)


def _snapshot(paths) -> dict:
    return {p: (CODE / p).read_text() for p in paths}


@pytest.mark.parametrize("how", ["option", "env"])
def test_cli_render_deploy_writes_and_checks_under_the_data_root(tmp_path, how):
    data = _data_root(tmp_path)
    before = _snapshot(config.DEPLOY_TEMPLATES.values())
    # "option": config.SETTINGS would be the code root's config.yaml; --data-root must win.
    flag, env = (["--data-root", str(data)], {}) if how == "option" else ([], {"TRADER_DATA_ROOT": str(data)})
    cmd = ["-m", "trader.cli", "config", "render-deploy"]

    r = _run([*cmd, "--check", *flag], tmp_path, **env)
    assert r.returncode != 0 and "out of date" in r.stderr  # nothing rendered there yet
    r = _run([*cmd, *flag], tmp_path, **env)
    assert r.returncode == 0 and "updated:" in r.stdout, r.stderr
    assert sorted(str(p.relative_to(data)) for p in data.rglob("*.timer")) == sorted(
        p for p in config.DEPLOY_TEMPLATES.values() if p.endswith(".timer")
    )
    assert (data / "deploy" / "systemd" / "trader.env").exists()
    r = _run([*cmd, "--check", *flag], tmp_path, **env)
    assert r.returncode == 0 and "match" in r.stdout, r.stderr

    raw = yaml.safe_load((data / "config.yaml").read_text())
    raw["schedule"]["runner_start"] = "07:07"
    (data / "config.yaml").write_text(yaml.safe_dump(raw))
    r = _run([*cmd, "--check", *flag], tmp_path, **env)
    assert r.returncode != 0 and "deploy/systemd/trader-runner.timer" in r.stderr

    assert _snapshot(config.DEPLOY_TEMPLATES.values()) == before  # the code tree's files untouched
    r = _run([*cmd, "--check"], tmp_path)  # and the code root still checks clean on its own
    assert r.returncode == 0, r.stderr


def test_cli_render_deploy_refuses_a_data_root_without_mode_file(tmp_path):
    data = _data_root(tmp_path, mode=None)
    r = _run(["-m", "trader.cli", "config", "render-deploy", "--check", "--data-root", str(data)], tmp_path)
    assert r.returncode != 0 and "mode.yaml is missing" in r.stderr
    assert not (data / "deploy").exists()
