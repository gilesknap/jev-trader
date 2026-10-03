"""The undeployed check, in today's monorepo and the split layout (#169: public code, private data repo)."""

import importlib
import subprocess
import time

import pytest

from trader import housekeeping

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _git(*args, cwd=None):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _origin(path):
    """A bare 'GitHub' repo with one commit on main."""
    _git("init", "-q", "--bare", "-b", "main", str(path))
    seed = path.parent / f"{path.name}-seed"
    _git("clone", "-q", str(path), str(seed))
    _git("checkout", "-q", "-b", "main", cwd=seed)
    _git("commit", "-q", "--allow-empty", "-m", "first", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    return path


def _merge(origin):
    """A new commit on origin's main (a merged PR the runner hasn't deployed)."""
    seed = origin.parent / f"{origin.name}-seed"
    _git("commit", "-q", "--allow-empty", "-m", "merged", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    return _git("rev-parse", "HEAD", cwd=seed)


def _clone(origin, path):
    _git("clone", "-q", "-b", "main", str(origin), str(path))
    return path


def _age(state, key, hours):
    state[key]["t"] = time.time() - hours * 3600


@pytest.fixture
def mono(tmp_path, monkeypatch):
    """Today: /srv/trading/main and the strategist checkout share one origin."""
    monkeypatch.delenv("TRADER_DATA_ROOT", raising=False)
    origin = _origin(tmp_path / "trading.git")
    monkeypatch.setattr(housekeeping, "MAIN", _clone(origin, tmp_path / "main"))
    monkeypatch.setattr(housekeeping.config, "STRATEGIST_ROOT", _clone(origin, tmp_path / "strategist"))
    return origin


def test_main_defaults_to_the_deployed_checkout_not_code_root(monkeypatch):
    try:
        monkeypatch.delenv("TRADER_CODE_ROOT", raising=False)
        assert str(importlib.reload(housekeeping).MAIN) == "/srv/trading/main"
        monkeypatch.setenv("TRADER_CODE_ROOT", "/elsewhere/code")
        assert str(importlib.reload(housekeeping).MAIN) == "/elsewhere/code"
    finally:
        monkeypatch.undo()
        importlib.reload(housekeeping)


def test_monorepo_in_sync(mono):
    state = {}
    assert housekeeping.check_undeployed(state) == [] and state == {}


def test_monorepo_alerts_after_two_days_with_todays_message(mono):
    sha = _merge(mono)
    state = {}
    assert housekeeping.check_undeployed(state) == []
    assert state["undeployed_since"]["sha"] == sha
    _age(state, "undeployed_since", 49)
    assert housekeeping.check_undeployed(state) == [
        (
            "undeployed",
            "main has changes merged 2 days ago that aren't deployed. Run: sudo -u runner trading-deploy",
            False,
        )
    ]


def test_monorepo_does_not_read_trader_data_root_pointing_at_main(mono, monkeypatch):
    monkeypatch.setenv("TRADER_DATA_ROOT", str(housekeeping.MAIN))
    assert housekeeping._data_root() is None
    assert housekeeping.check_undeployed({}) == []


def test_monorepo_ls_remote_times_out_like_any_other_failure(mono, monkeypatch):
    real = subprocess.run

    def slow(cmd, *a, **k):
        if "ls-remote" in cmd:
            assert k["timeout"] == 60
            raise subprocess.TimeoutExpired(cmd, k["timeout"])
        return real(cmd, *a, **k)

    monkeypatch.setattr(housekeeping.subprocess, "run", slow)
    ((pid, msg, urgent),) = housekeeping.check_undeployed({})
    assert pid == "deploy_check" and msg.startswith("Couldn't compare deployed code with main:") and "60" in msg
    assert not urgent


def test_monorepo_unreadable_checkout(mono, monkeypatch, tmp_path):
    monkeypatch.setattr(housekeeping, "MAIN", tmp_path / "missing")
    ((pid, msg, urgent),) = housekeeping.check_undeployed({})
    assert pid == "deploy_check" and msg.startswith("Couldn't compare deployed code with main:") and not urgent


# The public code repo's https URL. Port 9 on localhost refuses at once, so a lookup that isn't redirected to
# the local bare repo fails fast and offline.
CODE_URL = "https://127.0.0.1:9/jev-trader.git"


@pytest.fixture
def split(tmp_path, monkeypatch):
    """Split: MAIN from the public code repo; /srv/trading/config and the strategist checkout from the data repo."""
    code = _origin(tmp_path / "jev-trader.git")
    data = _origin(tmp_path / "data.git")
    main = _clone(code, tmp_path / "main")
    _git("remote", "set-url", "origin", CODE_URL, cwd=main)
    # Only command-line config reaches the anonymous lookup, so that is how the test redirects https to the bare repo.
    monkeypatch.setattr(housekeeping, "_ANON_GIT_CONFIG", ("-c", f"url.{code}.insteadOf={CODE_URL}"))
    monkeypatch.setattr(housekeeping, "MAIN", main)
    monkeypatch.setenv("TRADER_DATA_ROOT", str(_clone(data, tmp_path / "config")))
    monkeypatch.setattr(housekeeping.config, "STRATEGIST_ROOT", _clone(data, tmp_path / "strategist"))
    return code, data


def test_split_in_sync(split):
    state = {}
    assert housekeeping.check_undeployed(state) == [] and state == {}


def test_split_undeployed_code(split):
    code, _ = split
    _merge(code)
    state = {}
    assert housekeeping.check_undeployed(state) == []
    _age(state, "undeployed_since", 49)
    ((pid, msg, _),) = housekeeping.check_undeployed(state)
    assert pid == "undeployed" and "code repo's main" in msg and "trading-deploy" in msg
    assert "undeployed_config_since" not in state


def test_split_undeployed_config(split):
    _, data = split
    sha = _merge(data)
    state = {}
    assert housekeeping.check_undeployed(state) == []
    assert state["undeployed_config_since"]["sha"] == sha and "undeployed_since" not in state
    _age(state, "undeployed_config_since", 49)
    ((pid, msg, _),) = housekeeping.check_undeployed(state)
    assert pid == "undeployed_config" and "data repo's main" in msg and "trading-deploy" in msg


def test_split_compares_config_with_data_main_not_the_strategist_branch(split):
    _, data = split
    strategist = housekeeping.config.STRATEGIST_ROOT
    _git("checkout", "-q", "-b", "strategist", cwd=strategist)
    _git("commit", "-q", "--allow-empty", "-m", "journal", cwd=strategist)
    _git("push", "-q", "origin", "strategist", cwd=strategist)
    assert housekeeping.check_undeployed({}) == []


def test_split_reads_the_code_repo_anonymously(split, monkeypatch):
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        calls.append((cmd, k))
        return real(cmd, *a, **k)

    monkeypatch.setattr(housekeeping.subprocess, "run", spy)
    assert housekeeping.check_undeployed({}) == []
    ((cmd, kw),) = [(c, k) for c, k in calls if "ls-remote" in c and CODE_URL in c]
    assert cmd[:3] == ["git", "-c", "credential.helper="]
    # Run from / with no global or system config and no -C: neither the code checkout's nor any repo's
    # config (credential helpers, insteadOf, sshCommand) applies, only the command line.
    assert "-C" not in cmd and kw["cwd"] == "/"
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kw["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null" and kw["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


def test_split_ignores_insteadof_outside_the_command_line(split, monkeypatch, tmp_path):
    """A rewrite in the code checkout's, the working directory's or the global git config never redirects
    the anonymous lookup (to an SSH remote with trader's identity, say)."""
    code, _ = split
    rewrite = f"url.{code}.insteadOf"
    _git("config", rewrite, CODE_URL, cwd=housekeeping.MAIN)
    here = tmp_path / "cwd-repo"
    _git("init", "-q", str(here))
    _git("config", rewrite, CODE_URL, cwd=here)
    monkeypatch.chdir(here)
    glob = tmp_path / "gitconfig"
    glob.write_text(f'[url "{code}"]\n\tinsteadOf = {CODE_URL}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    monkeypatch.setattr(housekeeping, "_ANON_GIT_CONFIG", ())
    problems = housekeeping.check_undeployed({})
    assert [p[0] for p in problems] == ["deploy_check"]  # it went to the real URL, which refuses


def test_split_refuses_a_non_https_code_remote(split):
    _git("remote", "set-url", "origin", "git@github-trading:someone/jev-trader", cwd=housekeeping.MAIN)
    ((pid, msg, _),) = housekeeping.check_undeployed({})
    assert pid == "deploy_check" and "git@github-trading:someone/jev-trader" in msg and "https" in msg


def test_split_code_check_failure_still_checks_config(split, monkeypatch, tmp_path):
    _, data = split
    monkeypatch.setattr(housekeeping, "_ANON_GIT_CONFIG", ("-c", f"url.{tmp_path / 'gone.git'}.insteadOf={CODE_URL}"))
    _merge(data)
    state = {}
    problems = housekeeping.check_undeployed(state)
    assert [p[0] for p in problems] == ["deploy_check"]
    assert "code repo's main" in problems[0][1]
    assert "undeployed_config_since" in state


def test_split_unreadable_config_checkout(split, monkeypatch, tmp_path):
    monkeypatch.setenv("TRADER_DATA_ROOT", str(tmp_path / "missing"))
    ((pid, msg, _),) = housekeeping.check_undeployed({})
    assert pid == "config_deploy_check" and "deployed config" in msg
