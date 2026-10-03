"""trading-deploy vs a running session (#50): the runner unit holds a shared lock for the whole
session; the deploy refuses rather than waits, and locks only around the switch."""

import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "trading-deploy").read_text()
UNIT = (ROOT / "deploy" / "systemd" / "trader-runner.service").read_text()
LOCK = "/srv/trading/runtime/session.lock"

pytestmark = pytest.mark.skipif(not shutil.which("flock"), reason="needs flock")


def deploy_functions(lock, state="inactive"):
    """The script's session-lock block, pointed at a temp lock, with a fake systemctl."""
    start = SCRIPT.index("# --- session lock")
    block = SCRIPT[start:SCRIPT.index("# --- end session lock ---", start)]
    return (f"set -euo pipefail\n{block}\nLOCK={shlex.quote(str(lock))}\n"
            f"systemctl() {{ {'return 1' if state is None else f'echo {shlex.quote(state)}'}; }}\n")


def runner_argv(lock, marker):
    """The unit's ExecStart, with the lock path swapped and `uv run trader run` faked."""
    line = next(ln for ln in UNIT.splitlines() if ln.startswith("ExecStart="))
    argv = shlex.split(line.removeprefix("ExecStart="))
    assert LOCK in argv, "the runner unit must take the session lock"
    argv = [str(lock) if a == LOCK else a for a in argv]
    uv = argv.index("/usr/local/bin/uv")
    return argv[:uv] + ["sh", "-c", f"touch {shlex.quote(str(marker))}; sleep 30"]


def deploy(lock, body, state="inactive"):
    return subprocess.run(["bash", "-c", deploy_functions(lock, state) + body], capture_output=True, text=True, timeout=20)


def wait_for(path, secs=5.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def runner(tmp_path):
    procs = []

    def start():
        marker = tmp_path / f"started{len(procs)}"
        p = subprocess.Popen(runner_argv(tmp_path / "session.lock", marker), start_new_session=True)
        procs.append(p)
        return p, marker

    yield start
    for p in procs:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()


def test_switch_refuses_while_a_session_runs_and_not_after_the_runner_dies(tmp_path, runner):
    lock = tmp_path / "session.lock"
    p, started = runner()
    assert wait_for(started)
    r = deploy(lock, "lock_switch")
    assert r.returncode != 0 and "session is running" in r.stderr
    os.killpg(p.pid, signal.SIGKILL)  # a crashed runner: the kernel drops its lock, nothing stale
    p.wait()
    assert deploy(lock, "lock_switch").returncode == 0


def test_runner_start_waits_out_the_switch_then_runs(tmp_path, runner):
    lock = tmp_path / "session.lock"
    go = tmp_path / "go"
    body = f"lock_switch\nwhile [[ ! -e {shlex.quote(str(go))} ]]; do sleep 0.05; done\nunlock_switch\nsleep 5\n"
    d = subprocess.Popen(["bash", "-c", deploy_functions(lock) + body])
    try:
        time.sleep(0.5)  # the deploy is mid-switch
        p, started = runner()
        assert not wait_for(started, 1.0), "the runner must not start mid-switch"
        assert p.poll() is None, "the runner must wait for the switch, not fail"
        go.touch()
        assert wait_for(started), "the runner starts once the switch is done (the deploy is still running)"
    finally:
        d.kill()
        d.wait()


def test_lock_switch_refuses_when_systemd_says_the_runner_is_up_even_without_the_lock(tmp_path):
    # e.g. the first deploy of this change (the old unit takes no lock), or the restart gap after a crash
    r = deploy(tmp_path / "session.lock", "lock_switch", state="activating")
    assert r.returncode != 0 and "trader-runner is activating" in r.stderr


@pytest.mark.parametrize("state, idle", [("inactive", True), ("failed", True), ("active", False), ("activating", False),
                                         ("deactivating", False), ("reloading", False), ("", False), (None, False)])
def test_runner_idle_only_when_systemd_says_so(tmp_path, state, idle):
    r = deploy(tmp_path / "session.lock", "runner_idle", state=state)
    assert (r.returncode == 0) == idle
    if not idle:
        assert "REFUSING" in r.stderr and "12:50 London" in r.stderr


def test_lock_is_held_only_around_the_switch():
    """#82's trap: holding the lock through the pager, the prompt or the tests delayed the session."""
    def at(s, frm=0):
        return SCRIPT.index(s, frm)
    main = at("# --- end session lock ---")
    assert at("runner_idle || exit 1", main) < at("git fetch", main)  # fail fast, holding nothing
    assert "lock_switch" not in SCRIPT[main:at("uv run --frozen pytest", main)]
    for step in ("less -R", 'read -r -p', "uv run --frozen pytest"):
        assert at(step, main) < at("lock_switch ||", main)
    switch = at("lock_switch ||", main)
    assert switch < at("git reset -q --hard", main) < at("uv sync -q --frozen --extra dev\n", switch) \
        < at("daemon-reload", switch) < at("unlock_switch", switch) < at("restart trader-dashboard", switch)
