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
    block = SCRIPT[start : SCRIPT.index("# --- end session lock ---", start)]
    return (
        f"set -euo pipefail\n{block}\nLOCK={shlex.quote(str(lock))}\n"
        f"systemctl() {{ {'return 1' if state is None else f'echo {shlex.quote(state)}'}; }}\n"
    )


def runner_argv(lock, marker):
    """The unit's ExecStart, with the lock path swapped and `uv run trader run` faked."""
    line = next(ln for ln in UNIT.splitlines() if ln.startswith("ExecStart="))
    argv = shlex.split(line.removeprefix("ExecStart="))
    assert LOCK in argv, "the runner unit must take the session lock"
    argv = [str(lock) if a == LOCK else a for a in argv]
    uv = argv.index("/usr/local/bin/uv")
    return argv[:uv] + ["sh", "-c", f"touch {shlex.quote(str(marker))}; sleep 30"]


def deploy(lock, body, state="inactive"):
    return subprocess.run(
        ["bash", "-c", deploy_functions(lock, state) + body], capture_output=True, text=True, timeout=20
    )


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


@pytest.mark.parametrize(
    "state, idle",
    [
        ("inactive", True),
        ("failed", True),
        ("active", False),
        ("activating", False),
        ("deactivating", False),
        ("reloading", False),
        ("", False),
        (None, False),
    ],
)
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
    assert "lock_switch" not in SCRIPT[main : at("uv run --frozen pytest", main)]
    for step in ("less -R", "read -r -p", "uv run --frozen pytest"):
        assert at(step, main) < at("lock_switch ||", main)
    switch = at("lock_switch ||", main)
    assert (
        switch
        < at("git reset -q --hard", main)
        < at("uv sync -q --frozen --extra dev\n", switch)
        < at("daemon-reload", switch)
        < at("unlock_switch", switch)
        < at("restart trader-dashboard", switch)
    )


# ---- the strategist-run interlock (#169 6.1, items 19-20): in the same block, so sliced the same way ----

STRATEGIST_DEFAULT = "/home/trader/.local/state/trader/strategist.lock"
needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")


def interlock(tmp_path, strategist_lock, body):
    """The block with the strategist lock pointed at a temp path."""
    script = (
        deploy_functions(tmp_path / "session.lock") + f"STRATEGIST_LOCK={shlex.quote(str(strategist_lock))}\n" + body
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)


def hold(path, marker):
    """A strategist run as the wrapper takes it: flock -n -o on the lock for the whole run."""
    return subprocess.Popen(
        ["flock", "-n", "-o", str(path), "sh", "-c", f"touch {shlex.quote(str(marker))}; sleep 30"],
        start_new_session=True,
    )


def test_interlock_default_path_is_the_wrappers_lock():
    start = SCRIPT.index("# --- session lock")
    block = SCRIPT[start : SCRIPT.index("# --- end session lock ---", start)]
    assert f'STRATEGIST_LOCK="${{TRADER_STRATEGIST_LOCK:-{STRATEGIST_DEFAULT}}}"' in block
    wrapper = (ROOT / "scripts" / "strategist.sh").read_text()
    assert 'LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/trader"' in wrapper and '"$LOGDIR/strategist.lock"' in wrapper


def test_interlock_missing_lock_file_means_no_run(tmp_path):
    assert interlock(tmp_path, tmp_path / "state" / "trader" / "strategist.lock", "lock_strategist").returncode == 0
    assert interlock(tmp_path, tmp_path / "strategist.lock", "lock_strategist").returncode == 0


def test_interlock_free_lock_is_taken_and_blocks_a_run_until_released(tmp_path):
    lock = tmp_path / "strategist.lock"
    lock.touch(mode=0o640)
    ran = tmp_path / "ran"
    body = (
        f"lock_strategist\n"
        f"flock -n {shlex.quote(str(lock))} touch {shlex.quote(str(ran))} || echo run-skipped\n"
        f"unlock_strategist\n"
        f"flock -n {shlex.quote(str(lock))} touch {shlex.quote(str(ran))}\n"
    )
    r = interlock(tmp_path, lock, body)
    assert r.returncode == 0, r.stderr
    assert "run-skipped" in r.stdout and ran.exists()


def test_interlock_refuses_while_a_run_holds_the_lock(tmp_path):
    lock = tmp_path / "strategist.lock"
    lock.touch()
    started = tmp_path / "started"
    p = hold(lock, started)
    try:
        assert wait_for(started)
        r = interlock(tmp_path, lock, "lock_strategist")
        assert r.returncode != 0 and "a strategist run is live" in r.stderr and "nothing was changed" in r.stderr
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
    assert interlock(tmp_path, lock, "lock_strategist").returncode == 0  # a dead run leaves nothing stale


@needs_non_root
def test_interlock_fails_closed_on_an_unreadable_lock_file(tmp_path):
    lock = tmp_path / "strategist.lock"
    lock.touch(mode=0o600)
    lock.chmod(0)
    r = interlock(tmp_path, lock, "lock_strategist")
    assert r.returncode != 0 and "can't tell whether a strategist run is live" in r.stderr
    assert "Grant runner read access" in r.stderr


@needs_non_root
def test_interlock_fails_closed_when_a_directory_on_the_way_is_closed(tmp_path):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "strategist.lock").touch()
    home.chmod(0o600)  # readable listing, but not searchable: the file's existence can't be known
    try:
        r = interlock(tmp_path, home / "state" / "strategist.lock", "lock_strategist")
        assert r.returncode != 0 and "can't reach it" in r.stderr
        home.chmod(0)
        r = interlock(tmp_path, home / "missing-too" / "strategist.lock", "lock_strategist")
        assert r.returncode != 0, "an unsearchable home must not pass for a missing lock"
    finally:
        home.chmod(0o700)


def test_interlock_refuses_a_symlink_or_non_file(tmp_path):
    target = tmp_path / "session.lock"
    target.touch()
    link = tmp_path / "strategist.lock"
    link.symlink_to(target)
    r = interlock(tmp_path, link, "lock_strategist")
    assert r.returncode != 0 and "isn't a regular file" in r.stderr
    d = tmp_path / "dir.lock"
    d.mkdir()
    assert interlock(tmp_path, d, "lock_strategist").returncode != 0


def test_unlock_strategist_is_a_noop_when_nothing_was_taken(tmp_path):
    assert (
        interlock(
            tmp_path, tmp_path / "absent.lock", "lock_strategist\nunlock_strategist\nunlock_strategist"
        ).returncode
        == 0
    )


def test_strategist_lock_is_taken_only_around_the_switch_and_only_in_the_two_repo_layout():
    main = SCRIPT.index("# --- end two-repo helpers ---")
    body = SCRIPT[main:]
    take = body.index("lock_strategist || {")
    assert (
        body.index("uv run --frozen pytest") < take < body.index("lock_switch ||") < body.index("git reset -q --hard")
    )
    assert body.rfind("if (( SPLIT )); then", 0, take) > body.rfind("fi\n", 0, take), (
        "the switch-time take is split-only"
    )
    assert body.index("unlock_switch") < body.index("if (( SPLIT )); then unlock_strategist; fi")
    early = body.index("if lock_strategist; then unlock_strategist")
    assert body.index("if (( SPLIT )); then") < early < body.index("git fetch")
