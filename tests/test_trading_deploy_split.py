"""trading-deploy in the two-repo layout (#169, B2), and proof that the single-repo deploy is unchanged.

Two kinds of test. The helpers are sliced out of the script (like test_deploy_session_lock.py) and run
on temp dirs. Then the whole script runs end to end on throwaway git repos: its fixed paths are
rewritten to temp dirs, and `uv`, `systemctl`, `less` and the deployed `trader` are fakes that log
what they were asked. Nothing here touches /srv/trading.
"""

import getpass
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "trading-deploy").read_text()

pytestmark = pytest.mark.skipif(not (shutil.which("flock") and shutil.which("git")), reason="needs flock and git")
needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def block(start, end):
    i = SCRIPT.index(start)
    return SCRIPT[i:SCRIPT.index(end, i)]


def helpers(**vars_):
    """The function blocks, then variable overrides."""
    out = "set -euo pipefail\n" + block("# --- session lock", "# --- end session lock ---")
    out += block("# --- two-repo helpers", "# --- end two-repo helpers ---")
    out += block("# --- ownership pre-flight", "# --- end ownership pre-flight ---")
    return out + "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in vars_.items())


def bash(script, **kw):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, **kw)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=os.environ | GIT_ENV)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def write(root, files):
    for rel, text in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


# ---------------------------------------------------------------- mode detection

def test_layout_is_single_repo_without_the_config_dir(tmp_path):
    r = bash(helpers(CONFIG_DIR=tmp_path / "config") + "split_layout")
    assert r.returncode == 1 and r.stderr == ""


def test_layout_is_two_repo_with_a_config_checkout(tmp_path):
    (tmp_path / "config" / ".git").mkdir(parents=True)
    assert bash(helpers(CONFIG_DIR=tmp_path / "config") + "split_layout").returncode == 0


def test_layout_is_single_repo_with_the_empty_dir_setup_creates(tmp_path):
    (tmp_path / "config").mkdir()  # deploy/setup/1-host.sh makes it on every host
    r = bash(helpers(CONFIG_DIR=tmp_path / "config") + "split_layout")
    assert r.returncode == 1 and r.stderr == ""


@pytest.mark.parametrize("kind", ["non-empty dir", "file", "symlink"])
def test_layout_refuses_a_config_dir_that_is_not_a_checkout(tmp_path, kind):
    cfg = tmp_path / "config"
    if kind == "non-empty dir":
        cfg.mkdir()
        (cfg / "config.yaml").write_text("x")
    elif kind == "file":
        cfg.write_text("x")
    else:
        (tmp_path / "real" / ".git").mkdir(parents=True)
        cfg.symlink_to(tmp_path / "real")
    r = bash(helpers(CONFIG_DIR=cfg) + "split_layout")
    assert r.returncode == 2 and "REFUSING" in r.stderr and "neither empty nor a git checkout" in r.stderr


def test_config_dir_default_and_override():
    assert 'CONFIG_DIR="${TRADER_DEPLOY_CONFIG_DIR:-/srv/trading/config}"' in SCRIPT


def test_git_never_pages(tmp_path):
    """#170: under a pty `git diff --stat` opened less and the deploy hung."""
    pager = SCRIPT.index("export GIT_PAGER=cat")
    first_git = min(SCRIPT.index(s) for s in ("git fetch", "git -C", "git diff"))
    assert pager < first_git and pager < SCRIPT.index("# --- session lock")
    assert "less -R" in SCRIPT  # the deliberate review pager stays


# ---------------------------------------------------------------- unit sources (#169 item 6)

UNIT_SOURCE = {  # every unit file a deploy installs, and where the two-repo deploy installs it from
    "trader-dashboard.service": "code",
    "trader-dashboard-ssh.service": "code",
    "trader-runner.service": "code",
    "trader-watchdog.service": "code",
    "trader-watchdog.timer": "code",
    "trader-runner.timer": "data",  # rendered from the owner's config.yaml
}


def test_unit_source_table_covers_every_unit_the_code_ships():
    shipped = {p.name for p in (ROOT / "deploy" / "systemd").iterdir() if p.suffix in (".service", ".timer")}
    from_code = {name for name, src in UNIT_SOURCE.items() if src == "code"}
    assert shipped == from_code, "a unit was added or removed: say where it is installed from"


@pytest.mark.skipif((ROOT / "config.yaml").exists(), reason="a monorepo checkout carries its own data")
def test_the_code_ships_no_deployment_data():
    """Rendered files and config live only in each owner's data repo (and templates/data/), never in the code."""
    for rel in ("config.yaml", "config/mode.yaml", "state", "deploy/systemd/trader.env",
                *(f"deploy/systemd/{n}" for n, src in UNIT_SOURCE.items() if src == "data")):
        assert not (ROOT / rel).exists(), f"{rel} belongs in the data repo"
    assert not list((ROOT / "deploy" / "systemd-trader").glob("*.timer"))
    if (ROOT / ".git").exists():  # and git ignores a rendered copy by exact name, never the static watchdog timer
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "deploy/systemd/trader-runner.timer",
                                  "deploy/systemd/trader.env", "deploy/systemd-trader/trader-strategist-weekly.timer",
                                  "deploy/systemd/trader-watchdog.timer"], capture_output=True, text=True).stdout.split()
        assert ignored == ["deploy/systemd/trader-runner.timer", "deploy/systemd/trader.env",
                           "deploy/systemd-trader/trader-strategist-weekly.timer"]


def test_each_unit_comes_from_its_named_source_and_services_env_never(tmp_path):
    code, data = tmp_path / "code", tmp_path / "data"
    for name in UNIT_SOURCE:
        write(code, {f"deploy/systemd/{name}": f"code {name}\n"})
    write(code, {"deploy/systemd/trader.env": "code env\n"})
    write(data, {"deploy/systemd/trader-runner.timer": "data timer\n", "deploy/systemd/trader.env": "data env\n",
                 "deploy/systemd/evil.service": "a unit the data repo must not supply\n"})
    r = bash(helpers() + f'unit_sources {shlex.quote(str(code))} {shlex.quote(str(data))}\nprintf "%s\\n" "${{UNIT_SRC[@]}}"')
    assert r.returncode == 0, r.stderr
    got = {Path(p).name: ("code" if p.startswith(str(code) + "/") else "data") for p in r.stdout.split()}
    assert got == UNIT_SOURCE


def test_unit_sources_refuses_when_the_data_lacks_the_runner_timer(tmp_path):
    code, data = tmp_path / "code", tmp_path / "data"
    write(code, {"deploy/systemd/trader-runner.service": "x"})
    data.mkdir()
    r = bash(helpers() + f"unit_sources {code} {data}")
    assert r.returncode != 0 and "trader-runner.timer" in r.stderr


# ---------------------------------------------------------------- end to end on throwaway repos

FAKE_UV = r"""#!/bin/bash
mode=""; [[ -n "${TRADER_DATA_ROOT:-}" ]] && mode=$(cat "$TRADER_DATA_ROOT/config/mode.yaml" 2>/dev/null)
echo "uv $* | cwd=$PWD | TRADER_DATA_ROOT=${TRADER_DATA_ROOT:-} | mode=$mode" >> "$FAKE_LOG"
[[ "$1" == run ]] && env | grep '^TRADER_' | sort > "$FAKE_LOG.testenv"  # everything TRADER_* the tests see
case "$1" in
    sync) [[ "$PWD" == "${FAKE_CODE_DIR:-}" ]] && exit "${FAKE_SWITCH_SYNC_RC:-0}"  # the switch's sync
          mkdir -p .venv/bin && cp "$FAKE_TRADER" .venv/bin/trader ;;
    run) [[ -z "${FAKE_DURING_TESTS:-}" ]] || eval "$FAKE_DURING_TESTS"
         exit "${FAKE_PYTEST_RC:-0}" ;;
esac
"""
FAKE_TRADER = r"""#!/bin/bash
echo "trader $* | TRADER_DATA_ROOT=${TRADER_DATA_ROOT:-} | TRADER_STRATEGIST_ROOT=${TRADER_STRATEGIST_ROOT:-} | TRADER_RUNTIME=${TRADER_RUNTIME:-} | TRADER_SECRETS=${TRADER_SECRETS:-}" >> "$FAKE_LOG"
case "$1" in
    deploy-plan) [[ -n "${FAKE_PLAN_OUT:-}" ]] && echo "$FAKE_PLAN_OUT"; exit "${FAKE_PLAN_RC:-0}" ;;
    config) [[ "${FAKE_RENDER_RC:-0}" == 0 ]] || { echo "out of date with x/config.yaml: deploy/systemd/trader.env"; exit 1; }
            echo "deploy files match config.yaml" ;;
    validate) [[ "${FAKE_VALIDATE_RC:-0}" == 0 ]] || { echo "classifiers INVALID: unknown feature foo"; exit 1; }
              echo "classifiers OK" ;;
esac
"""
FAKE_SYSTEMCTL = r"""#!/bin/bash
if [[ "$*" == *ActiveState* ]]; then echo "${FAKE_RUNNER_STATE:-inactive}"; else echo "systemctl $*" >> "$FAKE_LOG"; fi
"""
FAKE_LESS = r"""#!/bin/bash
cat "${@: -1}" >> "$FAKE_REVIEW"
"""

CODE_FILES = {
    ".gitignore": ".venv/\n",
    "src/app.py": "VERSION = 1\n",
    **{f"deploy/systemd/{n}": f"code {n} v1\n" for n in UNIT_SOURCE},
    "deploy/systemd/trader.env": "code env\n",
    "deploy/systemd-trader/trader-strategist@.service": "strategist unit v1\n",
}
DATA_FILES = {
    "config.yaml": "owner: x\n",
    "config/mode.yaml": "mode: paper\n",
    "deploy/systemd/trader-runner.timer": "data runner timer v1\n",
    "deploy/systemd/trader.env": "data env\n",
    "deploy/systemd-trader/trader-strategist-premarket.timer": "premarket v1\n",
}


class Rig:
    def __init__(self, tmp: Path, split: bool):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, text in (("uv", FAKE_UV), ("trader", FAKE_TRADER), ("systemctl", FAKE_SYSTEMCTL), ("less", FAKE_LESS)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.log, self.review = tmp / "log", tmp / "review"
        self.home = tmp / "home"
        (self.home / ".config" / "systemd" / "user").mkdir(parents=True)
        (self.home / ".config" / "trading").mkdir(parents=True)
        self.units = self.home / ".config" / "systemd" / "user"
        self.runtime = tmp / "runtime"
        self.runtime.mkdir()
        self.strat = tmp / "strategist"
        write(self.strat, {"state/classifiers.yaml": "classifiers: []\n"})
        self.trader_units = tmp / "trader-units"  # absent unless a test installs copies
        self.lock = tmp / "trader-home" / "strategist.lock"
        self.scratch = tmp / "scratch"
        self.scratch.mkdir()
        # The single-repo layout is a monorepo: its code checkout carries the config too.
        self.code, self.code_origin = self.repo("code", CODE_FILES if split else CODE_FILES | {"config.yaml": "owner: x\n"})
        (self.code / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(self.bin / "trader", self.code / ".venv" / "bin" / "trader")
        self.config = tmp / "config"
        if split:
            self.config, self.data_origin = self.repo("config", DATA_FILES)
        self.script = tmp / "trading-deploy"
        self.script.write_text(self.rewrite(SCRIPT))

    def repo(self, name, files):
        work = self.tmp / f"{name}-work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        write(work, files)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "seed")
        origin = self.tmp / f"{name}-origin.git"
        git(self.tmp, "clone", "-q", "--bare", str(work), str(origin))
        git(work, "remote", "add", "origin", str(origin))
        deployed = self.tmp / name
        git(self.tmp, "clone", "-q", str(origin), str(deployed))
        return deployed, work

    def push(self, work, files, msg="change"):
        write(work, files)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", msg)
        git(work, "push", "-q", "origin", "main")
        return git(work, "rev-parse", "HEAD")

    def rewrite(self, text):
        subs = [
            ('[[ "$(id -un)" == runner ]] || { echo "run as: sudo -u runner $0" >&2; exit 1; }', ":"),
            ("[[ -t 0 || $DRY == 1 ]]", "[[ -n x || $DRY == 1 ]]"),
            ('export PATH="/usr/local/bin:/usr/bin:/bin"', f'export PATH="{self.bin}:/usr/local/bin:/usr/bin:/bin"'),
            ("/srv/trading/main", str(self.code)),
            ("/srv/trading/strategist", str(self.strat)),
            ("/srv/trading/runtime", str(self.runtime)),
            ("/home/trader/.config/systemd/user", str(self.trader_units)),
            ("-p /var/tmp", f"-p {self.scratch}"),
            ("OWNER=runner ", f"OWNER={getpass.getuser()} "),
        ]
        for old, new in subs:
            assert old in text, f"the script no longer contains {old!r}: update the test rig"
            text = text.replace(old, new)
        assert "/srv/trading" not in text.replace("/srv/trading/config", ""), "an unrewritten path"
        return text

    def run(self, *args, answer="yes", **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("TRADER_", "GIT_"))} | GIT_ENV | {
            "HOME": str(self.home), "FAKE_LOG": str(self.log), "FAKE_REVIEW": str(self.review),
            "FAKE_TRADER": str(self.bin / "trader"), "TRADER_DEPLOY_CONFIG_DIR": str(self.config),
            "TRADER_STRATEGIST_LOCK": str(self.lock), "FAKE_CODE_DIR": str(self.code), **{k: str(v) for k, v in env.items()}}
        return subprocess.run(["bash", str(self.script), *args], input=answer + "\n", env=e,
                              capture_output=True, text=True, timeout=60)

    def logged(self):
        return self.log.read_text() if self.log.exists() else ""

    def reviewed(self):
        return self.review.read_text() if self.review.exists() else ""


@pytest.fixture
def mono(tmp_path):
    return Rig(tmp_path, split=False)


@pytest.fixture
def split(tmp_path):
    return Rig(tmp_path, split=True)


# ---- single-repo layout: what it always did, and never a two-repo step

@needs_non_root
def test_single_repo_deploy_is_unchanged_and_never_enters_two_repo_code(mono):
    target = mono.push(mono.code_origin, {"src/app.py": "VERSION = 2\n"})
    mono.lock.parent.mkdir()
    mono.lock.parent.chmod(0)  # would refuse in the two-repo layout: the single-repo deploy never looks
    try:
        r = mono.run(FAKE_PLAN_OUT=f"PR {target[:10]} #7 a change")
    finally:
        mono.lock.parent.chmod(0o700)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(mono.code, "rev-parse", "HEAD") == target
    assert r.stdout.splitlines()[0] == f"=== files changed since last deploy ({git(mono.code, 'rev-parse', '--short', 'HEAD@{1}')} -> {target[:7]}) ==="
    assert "=== merged pull requests" in r.stdout and "code repo" not in r.stdout and "data repo" not in r.stdout
    assert mono.reviewed() == ""  # all merged PRs: no diff review, no prompt
    log = mono.logged()
    assert "config render-deploy" not in log and "trader validate" not in log
    assert all("TRADER_DATA_ROOT= " in ln for ln in log.splitlines() if ln.startswith("uv "))
    assert f"deploy-plan --repo {mono.code} --base HEAD --target {target} | TRADER_DATA_ROOT= |" in log
    # every unit from the code, the code's own runner timer included, as before
    for name in UNIT_SOURCE:
        assert (mono.units / name).read_text() == f"code {name} v1\n"
    assert not (mono.units / "trader.env").exists()
    assert r.stdout.rstrip().endswith("the runner picks it up at the next session start")


def test_single_repo_refuses_code_without_config(mono):
    """The public code repo deployed without a data checkout: no config.yaml, no runner timer."""
    git(mono.code_origin, "rm", "-q", "config.yaml")
    target = mono.push(mono.code_origin, {"src/app.py": "VERSION = 2\n"})
    for args in ((), ("--dry-run",)):
        r = mono.run(*args)
        assert r.returncode == 1 and "has no config.yaml" in r.stderr and "nothing was changed" in r.stderr
    assert git(mono.code, "rev-parse", "HEAD") != target and mono.logged() == ""


def test_single_repo_review_and_typed_yes_as_before(mono):
    target = mono.push(mono.code_origin, {"src/app.py": "VERSION = 2\n"}, msg="direct push")
    r = mono.run(answer="no", FAKE_PLAN_RC=3, FAKE_PLAN_OUT=f"REVIEW {target} not a PR merge commit: direct push")
    assert r.returncode == 1 and "aborted" in r.stdout
    assert "VERSION = 2" in mono.reviewed()
    assert git(mono.code, "rev-parse", "HEAD") != target and "uv run" not in mono.logged()


def test_single_repo_up_to_date(mono):
    r = mono.run()
    assert r.returncode == 0 and r.stdout.startswith("already at origin/main (")


# ---- the candidate tests' environment, in both layouts

LIVE_ENV = {  # what a shell with services.env loaded (or a developer's) might carry into the deploy
    "TRADER_DATA_ROOT": "/live/config", "TRADER_STRATEGIST_ROOT": "/live/strategist", "TRADER_RUNTIME": "/live/runtime",
    "TRADER_REPLAY_DIR": "/live/replays", "TRADER_SECRETS": "/live/env", "TRADER_CONFIG": "/live/config.yaml",
    "TRADER_CODE_ROOT": "/live/code", "TRADER_TEST_DATA_ROOT": "/live/data", "TRADER_STRATEGIST_STAMP": "/live/.last_run",
}


@pytest.mark.parametrize("two_repo", [False, True])
def test_candidate_tests_run_with_a_clean_trader_environment(tmp_path, two_repo):
    rig = Rig(tmp_path, split=two_repo)
    rig.push(rig.code_origin, {"src/app.py": "VERSION = 2\n"})
    r = rig.run(FAKE_PLAN_OUT="PR 0123456789 #9 merged", **LIVE_ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = (tmp_path / "log.testenv").read_text().splitlines()
    if two_repo:  # only the candidate data, set explicitly
        assert len(seen) == 1 and seen[0].startswith(f"TRADER_DATA_ROOT={rig.scratch}/")
    else:
        assert seen == []


# ---- two-repo layout

def test_two_repo_deploy_switches_both_and_installs_each_unit_from_its_source(split):
    code_t = split.push(split.code_origin, {"src/app.py": "VERSION = 2\n", "deploy/systemd/trader-runner.timer": "stale code timer\n"})
    data_t = split.push(split.data_origin, {"deploy/systemd/trader-runner.timer": "data runner timer v2\n"})
    r = split.run(FAKE_PLAN_OUT="PR 0123456789 #9 merged")  # both repos "all merged PRs"
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(split.code, "rev-parse", "HEAD") == code_t and git(split.config, "rev-parse", "HEAD") == data_t
    # the data diff is always reviewed in full and needs the yes, even from signed PRs; the code's isn't
    assert "data runner timer v2" in split.reviewed() and "VERSION = 2" not in split.reviewed()
    assert "always reviewed in full" in r.stdout
    assert (split.units / "trader-runner.timer").read_text() == "data runner timer v2\n"
    for name, src in UNIT_SOURCE.items():
        if src == "code":
            assert (split.units / name).read_text() == f"code {name} v1\n"
    assert not (split.units / "trader.env").exists()
    log = split.logged().splitlines()
    tests = next(ln for ln in log if ln.startswith("uv run --frozen pytest"))
    droot = tests.split("TRADER_DATA_ROOT=")[1].split(" |")[0]
    assert droot.startswith(str(split.scratch)) and "mode=mode: paper" in tests  # a worktree of TARGET_DATA
    assert any(ln.startswith(f"trader config render-deploy --check --data-root {droot} ") for ln in log)
    v = next(ln for ln in log if ln.startswith("trader validate"))
    assert f"--file {split.strat}/state/classifiers.yaml" in v and f"TRADER_DATA_ROOT={droot}" in v
    assert f"TRADER_STRATEGIST_ROOT={split.strat}" in v and f"TRADER_RUNTIME={split.runtime}" in v
    assert v.endswith(f"TRADER_SECRETS={split.home}/.config/trading/env")
    assert any(ln.startswith(f"trader deploy-plan --repo {split.config} ") for ln in log)
    # the deployed code reads the deployed config (it loads config.yaml at import); the code has none
    plans = [ln for ln in log if ln.startswith("trader deploy-plan")]
    assert len(plans) == 2 and all(f"TRADER_DATA_ROOT={split.config} |" in ln for ln in plans)
    # throwaway worktrees gone, both checkouts closed to group writes and others
    assert len(git(split.config, "worktree", "list").splitlines()) == 1
    assert len(git(split.code, "worktree", "list").splitlines()) == 1
    for d in (split.code, split.config):
        assert not os.stat(d / "deploy/systemd/trader-runner.timer").st_mode & (stat.S_IWGRP | stat.S_IRWXO)
    assert "deployed code " in r.stdout and " + data " in r.stdout
    assert "previous: code " in r.stdout
    # the pager headers name both ends by sha, never "HEAD"
    assert "..HEAD" not in split.reviewed() and f"{split.config}: " in split.reviewed()


def test_two_repo_data_change_needs_the_typed_yes(split):
    before = git(split.config, "rev-parse", "HEAD")
    split.push(split.data_origin, {"config/mode.yaml": "mode: live\n"})
    r = split.run(answer="no")
    assert r.returncode == 1 and "aborted" in r.stdout and "mode: live" in split.reviewed()
    assert git(split.config, "rev-parse", "HEAD") == before and "uv run" not in split.logged()


def test_two_repo_only_data_moved_still_tests_the_code_against_it(split):
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    r = split.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "code repo" not in r.stdout and "=== data repo files changed" in r.stdout
    assert "uv run --frozen pytest" in split.logged()


def test_two_repo_up_to_date(split):
    r = split.run()
    assert r.returncode == 0 and r.stdout.startswith("already at origin/main in both repos")


def test_two_repo_refuses_a_render_mismatch_naming_the_files(split):
    before = git(split.config, "rev-parse", "HEAD")
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    r = split.run(FAKE_RENDER_RC=1)
    assert r.returncode == 1 and "REFUSING" in r.stderr and "deploy/systemd/trader.env" in r.stderr
    assert git(split.config, "rev-parse", "HEAD") == before and "daemon-reload" not in split.logged()


def test_two_repo_refuses_data_without_mode_yaml(split):
    before = git(split.config, "rev-parse", "HEAD")
    git(split.data_origin, "rm", "-q", "config/mode.yaml")
    split.push(split.data_origin, {}, msg="drop mode")
    r = split.run()
    assert r.returncode == 1 and "no config/mode.yaml" in r.stderr
    assert git(split.config, "rev-parse", "HEAD") == before


def test_two_repo_invalid_strategist_specs_warn_but_deploy(split):
    target = split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    r = split.run(FAKE_VALIDATE_RC=1)
    assert r.returncode == 0 and "WARNING" in r.stdout and "unknown feature foo" in r.stdout
    assert git(split.config, "rev-parse", "HEAD") == target


def test_two_repo_failed_tests_change_nothing(split):
    before = git(split.config, "rev-parse", "HEAD")
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    r = split.run(FAKE_PYTEST_RC=1)
    assert r.returncode == 1 and "TESTS FAILED" in r.stderr and git(split.config, "rev-parse", "HEAD") == before


def test_two_repo_refuses_hidden_characters_in_the_data(split):
    split.push(split.data_origin, {"config.yaml": "owner: x" + chr(0x202E) + "\n"})
    r = split.run()
    assert r.returncode == 1 and "data repo diff contains invisible or bidi Unicode" in r.stderr


def test_two_repo_refuses_while_a_strategist_run_is_live(split):
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    split.lock.parent.mkdir()
    split.lock.touch()
    started = split.tmp / "started"
    p = subprocess.Popen(["flock", "-n", "-o", str(split.lock), "sh", "-c", f"touch {started}; sleep 30"])
    try:
        for _ in range(100):
            if started.exists():
                break
            subprocess.run(["sleep", "0.05"])
        r = split.run()
    finally:
        p.kill()
        p.wait()
    assert r.returncode == 1 and "a strategist run is live" in r.stderr
    assert "files changed" not in r.stdout, "it fails fast, before the review"


@needs_non_root
def test_two_repo_refuses_when_the_lock_is_unreadable(split):
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    split.lock.parent.mkdir()
    split.lock.touch()
    split.lock.chmod(0)
    r = split.run()
    assert r.returncode == 1 and "can't tell whether a strategist run is live" in r.stderr


def test_empty_config_dir_deploys_as_a_single_repo(mono):
    mono.config.mkdir()  # what setup leaves on every host before the cutover
    target = mono.push(mono.code_origin, {"src/app.py": "VERSION = 2\n"})
    r = mono.run()
    assert r.returncode == 0, r.stderr
    assert git(mono.code, "rev-parse", "HEAD") == target and "data repo" not in r.stdout
    assert (mono.units / "trader-runner.timer").read_text() == "code trader-runner.timer v1\n"


def test_two_repo_refuses_a_config_dir_that_is_not_a_checkout(mono):
    mono.config.mkdir()
    (mono.config / "config.yaml").write_text("x")  # e.g. files copied in by hand, no checkout
    target = mono.push(mono.code_origin, {"src/app.py": "VERSION = 2\n"})
    r = mono.run()
    assert r.returncode == 1 and "neither empty nor a git checkout" in r.stderr
    assert git(mono.code, "rev-parse", "HEAD") != target


def test_two_repo_dry_run_changes_nothing(split):
    before = git(split.config, "rev-parse", "HEAD")
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    r = split.run("--dry-run")
    assert r.returncode == 0 and "dry run: 1 diff(s) would need review" in r.stdout
    assert git(split.config, "rev-parse", "HEAD") == before and "uv run" not in split.logged()


def test_two_repo_hints_a_strategist_unit_reinstall(split):
    split.push(split.data_origin, {"deploy/systemd-trader/trader-strategist-premarket.timer": "premarket v2\n"})
    r = split.run()
    assert r.returncode == 0, r.stderr
    assert "trader-strategist-premarket.timer" in r.stdout and "2-strategist.sh" in r.stdout
    # once trader's installed copies match (and runner can read them), no note
    split.trader_units.mkdir()
    shutil.copy(split.config / "deploy/systemd-trader/trader-strategist-premarket.timer", split.trader_units)
    shutil.copy(split.code / "deploy/systemd-trader/trader-strategist@.service", split.trader_units)
    split.push(split.data_origin, {"config.yaml": "owner: z\n"})
    r = split.run()
    assert r.returncode == 0 and "reinstall them" not in r.stdout


@needs_non_root
def test_layout_refuses_an_unreadable_config_dir(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text("x")
    cfg.chmod(0)
    try:
        r = bash(helpers(CONFIG_DIR=cfg) + "split_layout")
    finally:
        cfg.chmod(0o700)
    assert r.returncode == 2 and "REFUSING" in r.stderr, "unreadable must never count as empty (single repo)"


@pytest.mark.parametrize("two_repo", [False, True])
def test_a_failed_switch_step_prints_the_rollback(tmp_path, two_repo):
    rig = Rig(tmp_path, split=two_repo)
    old_code, old_data = git(rig.code, "rev-parse", "HEAD"), ""
    rig.push(rig.code_origin, {"src/app.py": "VERSION = 2\n"})
    if two_repo:
        old_data = git(rig.config, "rev-parse", "HEAD")
        rig.push(rig.data_origin, {"config.yaml": "owner: y\n"})
    r = rig.run(FAKE_PLAN_OUT="PR 0123456789 #9 merged", FAKE_SWITCH_SYNC_RC=7)
    assert r.returncode == 7 and "PARTIAL SWITCH" in r.stderr and "exit 7" in r.stderr
    assert f"git -C {rig.code} reset -q --hard {old_code} && (cd {rig.code} && uv sync -q --frozen --extra dev)" in r.stderr
    assert (f"git -C {rig.config} reset -q --hard" in r.stderr) == two_repo
    if two_repo:
        assert f"git -C {rig.config} reset -q --hard {old_data}" in r.stderr
    assert "trading-deploy again" in r.stderr


def test_failed_tests_name_both_shas_in_two_repo_mode(split):
    old_code, old_data = git(split.code, "rev-parse", "HEAD"), git(split.config, "rev-parse", "HEAD")
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    r = split.run(FAKE_PYTEST_RC=1)
    assert f"keeps code {old_code[:7]} + data {old_data[:7]}" in r.stderr and "PARTIAL SWITCH" not in r.stderr


# ---------------------------------------------------------------- ownership pre-flight

def test_ownership_preflight_passes_when_the_owner_owns_everything(tmp_path):
    write(tmp_path / "code", {"a/b.txt": "x", ".venv/bin/python": "x"})
    write(tmp_path / "data", {"config.yaml": "x"})
    r = bash(helpers(OWNER=getpass.getuser()) + f"foreign_files {tmp_path / 'code'} {tmp_path / 'data'}")
    assert r.returncode == 0 and r.stdout == r.stderr == ""


@needs_non_root
def test_ownership_preflight_lists_every_foreign_file(tmp_path):
    """Files nobody but root can make foreign here, so the check is asked for another owner instead."""
    write(tmp_path / "code", {"a/b.txt": "x"})
    write(tmp_path / "data", {"config.yaml": "x"})
    r = bash(helpers(OWNER="root") + f"foreign_files {tmp_path / 'code'} {tmp_path / 'data'}")
    me = getpass.getuser()
    assert r.returncode == 1 and "REFUSING" in r.stderr and "nothing was changed" in r.stderr
    for p in ("code", "code/a", "code/a/b.txt", "data", "data/config.yaml"):
        assert f"  {me}  {tmp_path / p}\n" in r.stderr
    assert "sudo chown -h root" in r.stderr


@needs_non_root
def test_ownership_preflight_caps_the_list(tmp_path):
    write(tmp_path / "code", {f"f{i:03}": "x" for i in range(60)})
    r = bash(helpers(OWNER="root") + f"foreign_files {tmp_path / 'code'}")
    assert r.returncode == 1 and r.stderr.count(f"  {getpass.getuser()}  ") == 50 and "... and 11 more" in r.stderr


@needs_non_root
def test_ownership_preflight_refuses_what_it_cannot_check(tmp_path):
    write(tmp_path / "code", {"locked/f": "x"})
    (tmp_path / "code" / "locked").chmod(0)
    try:
        r = bash(helpers(OWNER=getpass.getuser()) + f"foreign_files {tmp_path / 'code'}")
    finally:
        (tmp_path / "code" / "locked").chmod(0o700)
    assert r.returncode == 1 and "REFUSING" in r.stderr and "Permission denied" in r.stderr


def test_ownership_preflight_runs_early_and_again_just_before_the_switch():
    body = SCRIPT[SCRIPT.index("# --- end ownership pre-flight ---"):]
    early = body.index('if ! foreign_files "${CHECKOUTS[@]}"')
    assert body.index("split_layout ||") < early < body.index("git fetch")
    late = body.index('foreign_files "${CHECKOUTS[@]}" || exit 1')
    assert body.index("uv run --frozen pytest") < late < body.index("lock_strategist || {") < body.index("lock_switch ||")
    assert 'CHECKOUTS=("$CODE_DIR"); (( SPLIT )) && CHECKOUTS+=("$CONFIG_DIR")' in body


@needs_non_root
@pytest.mark.parametrize("two_repo", [False, True])
def test_ownership_preflight_refuses_before_anything_changes(tmp_path, two_repo):
    rig = Rig(tmp_path, split=two_repo)
    rig.script.write_text(rig.script.read_text().replace(f"OWNER={getpass.getuser()} ", "OWNER=root ", 1))
    old_code = git(rig.code, "rev-parse", "HEAD")
    rig.push(rig.code_origin, {"src/app.py": "VERSION = 2\n"})
    r = rig.run("--dry-run")
    assert r.returncode == 0 and "the ownership pre-flight would refuse now" in r.stdout
    rig.log.unlink()  # the dry run's plan
    r = rig.run()
    assert r.returncode == 1 and "REFUSING" in r.stderr and f"  {getpass.getuser()}  {rig.code}\n" in r.stderr
    assert f"under {rig.code} {rig.config} aren't" in r.stderr if two_repo else f"under {rig.code} aren't" in r.stderr
    assert git(rig.code, "rev-parse", "HEAD") == old_code and rig.logged() == ""


@needs_non_root
def test_ownership_preflight_catches_a_file_that_appears_during_the_tests(split):
    """The second check, just before the switch: here a directory runner can't search turns up meanwhile."""
    old_code, old_data = git(split.code, "rev-parse", "HEAD"), git(split.config, "rev-parse", "HEAD")
    split.push(split.code_origin, {"src/app.py": "VERSION = 2\n"})
    split.push(split.data_origin, {"config.yaml": "owner: y\n"})
    sneak = split.config / "sneaked"
    try:
        r = split.run(FAKE_DURING_TESTS=f"mkdir {sneak} && chmod 0 {sneak}")
    finally:
        if sneak.exists():
            sneak.chmod(0o700)
    assert r.returncode == 1 and "REFUSING" in r.stderr and str(sneak) in r.stderr and "PARTIAL SWITCH" not in r.stderr
    assert git(split.code, "rev-parse", "HEAD") == old_code and git(split.config, "rev-parse", "HEAD") == old_data
    assert "uv run --frozen pytest" in split.logged()
