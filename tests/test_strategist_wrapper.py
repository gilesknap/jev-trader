"""scripts/strategist.sh in a sandbox: a bare origin, a clone on `strategist`, fake uv/claude."""

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "strategist.sh"

pytestmark = pytest.mark.skipif(not (shutil.which("flock") and shutil.which("git")), reason="needs flock and git")

FAKE_UV = """#!/bin/bash
case "$*" in
  "run trader session") echo '{"minutes_to_open": 45, "minutes_to_close": 400}' ;;
  "run trader config get models.strategist") echo cfg-model ;;
  run\\ python*) echo "${@: -1}" >> "$HOME/alerts" ;;
  *) echo "$*" >> "$HOME/uv_calls" ;;
esac
"""
FAKE_CLAUDE = """#!/bin/bash
env | grep ^STRATEGIST_ >> "$HOME/claude_env"
echo "$*" >> "$HOME/claude_args"
echo run >> "$HOME/claude_runs"
sleep 3 >/dev/null 2>&1 &   # a stray background process outliving the run
echo note >> state/note.md
"""


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    for name, body in (("uv", FAKE_UV), ("claude", FAKE_CLAUDE)):
        f = home / ".local" / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TRADER_STRATEGIST_ROOT": str(tmp_path / "repo"),
           "XDG_STATE_HOME": str(tmp_path / "state")}
    git(tmp_path, "init", "-q", "--bare", "origin.git", env=env)
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", "origin.git", "seed", env=env)
    for d in ("scripts", "prompts", "state"):
        (seed / d).mkdir()
    shutil.copy(WRAPPER, seed / "scripts" / "strategist.sh")
    for kind in ("premarket", "postclose", "weekly"):
        (seed / "prompts" / f"{kind}.md").write_text(kind)
    (seed / "state" / "s.md").write_text("s")
    (seed / ".gitignore").write_text(".last_run\n.last_postclose\n")
    git(seed, "add", "-A", env=env)
    git(seed, "commit", "-qm", "init", env=env)
    git(seed, "push", "-q", "origin", "HEAD:main", "HEAD:strategist", env=env)
    git(tmp_path, "clone", "-q", "-b", "strategist", "origin.git", "repo", env=env)

    class S:
        pass

    s = S()
    s.tmp, s.home, s.env, s.seed, s.repo = tmp_path, home, env, seed, tmp_path / "repo"
    s.logdir = tmp_path / "state" / "trader"

    def run(kind):
        return subprocess.run(["bash", str(s.repo / "scripts" / "strategist.sh"), kind], env=env,
                              capture_output=True, text=True, timeout=60)

    def push_main(edit):
        edit(s.seed / "scripts" / "strategist.sh")
        git(s.seed, "commit", "-qam", "change wrapper", env=env)
        git(s.seed, "push", "-q", "origin", "HEAD:main", env=env)

    def read(name):
        p = home / name
        return p.read_text() if p.exists() else ""

    s.run, s.push_main, s.read = run, push_main, read
    return s


def add_marker(path):
    text = path.read_text()
    anchor = 'if [[ "$KIND" == housekeeping ]]; then'
    path.write_text(text.replace(anchor, 'echo new >> "$HOME/marker"\n' + anchor, 1))


def test_wrapper_parses():
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


def test_reexecs_merged_wrapper_and_pushes_merge(sandbox):
    sandbox.push_main(add_marker)
    r = sandbox.run("premarket")
    assert r.returncode == 0, r.stderr
    assert sandbox.read("marker") == "new\n"          # the merged script ran, not the old one
    assert sandbox.read("claude_runs") == "run\n"
    assert "--model cfg-model" in sandbox.read("claude_args")  # from config.yaml models.strategist
    assert sandbox.read("claude_env") == ""           # internal flags don't reach the session
    assert sandbox.read("alerts") == ""
    assert "change wrapper" in git(sandbox.tmp / "origin.git", "log", "--oneline", "strategist")
    assert len(list(sandbox.logdir.glob("*-premarket.log"))) == 1


def test_stray_child_does_not_hold_the_lock(sandbox):
    assert sandbox.run("weekly").returncode == 0      # its fake claude leaves `sleep 3` running
    assert sandbox.run("weekly").returncode == 0
    assert sandbox.read("claude_runs") == "run\nrun\n"


def test_held_lock_skips_with_a_log_line(sandbox):
    sandbox.logdir.mkdir(parents=True)
    with open(sandbox.logdir / "strategist.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        r = sandbox.run("weekly")
    assert r.returncode == 0
    assert sandbox.read("claude_runs") == ""
    assert "another run holds the lock" in "".join(p.read_text() for p in sandbox.logdir.glob("*.log"))


def test_broken_wrapper_on_main_alerts_instead_of_running(sandbox):
    sandbox.push_main(lambda p: p.write_text(p.read_text().replace("then\n", "then then\n", 1)))
    r = sandbox.run("premarket")
    assert r.returncode == 1
    assert "doesn't parse" in sandbox.read("alerts")
    assert sandbox.read("claude_runs") == ""


def test_housekeeping_leaves_a_busy_checkout_alone(sandbox):
    git(sandbox.repo, "checkout", "-q", "-b", "proposal/x", env=sandbox.env)
    (sandbox.repo / "state" / "s.md").write_text("edited")
    sandbox.push_main(add_marker)
    r = sandbox.run("housekeeping")
    assert r.returncode == 0, r.stderr
    assert git(sandbox.repo, "branch", "--show-current", env=sandbox.env).strip() == "proposal/x"
    assert sandbox.read("marker") == ""
    assert sandbox.read("uv_calls") == "run trader housekeeping\n"


def test_postclose_writes_the_probe_report_and_survives_its_failure(sandbox):
    (sandbox.home / ".local" / "bin" / "uv").write_text(FAKE_UV.replace('"minutes_to_close": 400', '"minutes_to_close": -30')
                                                        .replace('  *) echo', '  *probe-report*) echo "$*" >> "$HOME/uv_calls"; exit 1 ;;\n  *) echo'))
    r = sandbox.run("postclose")
    assert r.returncode == 0, r.stderr
    calls = sandbox.read("uv_calls").splitlines()
    assert calls.index("run trader probe-report --out logs/probe_report.json") == calls.index("run trader archive") + 1
    assert sandbox.read("claude_runs") == "run\n"  # the strategist still ran after the report failed


def fake_claude(sandbox, body):
    f = sandbox.home / ".local" / "bin" / "claude"
    f.write_text("#!/bin/bash\nset -e\n" + body)


def origin_log(sandbox):
    return git(sandbox.tmp / "origin.git", "log", "--format=%H %s", "strategist").splitlines()


def test_run_that_pushes_itself_is_fast_forwarded_and_path_checked(sandbox):
    # The model commits and pushes strategist itself, including a path it may not change.
    fake_claude(sandbox, """echo note >> state/note.md
mkdir -p scripts && echo evil > scripts/evil.sh
git add -A && git commit -qm "model commit" && git push -q origin strategist
echo later >> journal.md; mkdir -p journal && echo later > journal/d.md
""")
    r = sandbox.run("premarket")
    assert r.returncode == 0, r.stderr
    alerts = sandbox.read("alerts")
    assert "git push failed" not in alerts and "rebase" not in alerts and "NOT reverted" not in alerts
    assert "pushed non-strategy paths to origin/strategist (reverted): scripts/evil.sh" in alerts
    log = origin_log(sandbox)
    assert log[1].endswith(" model commit") and " strategist: premarket " in log[0]
    files = git(sandbox.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert "state/note.md" in files and "journal/d.md" in files
    assert "scripts/evil.sh" not in files and "journal.md" not in files
    assert git(sandbox.repo, "rev-parse", "HEAD").strip() == log[0].split()[0]   # not diverged
    assert git(sandbox.repo, "status", "--porcelain") == ""


def test_run_that_pushes_everything_adds_no_commit(sandbox):
    fake_claude(sandbox, 'echo note >> state/note.md\ngit add -A && git commit -qm "model commit" && git push -q origin strategist\n')
    assert sandbox.run("premarket").returncode == 0
    assert sandbox.read("alerts") == ""
    log = origin_log(sandbox)
    assert log[0].endswith("model commit")
    assert git(sandbox.repo, "rev-parse", "HEAD").strip() == log[0].split()[0]


def test_someone_else_pushing_during_the_run_is_rebased_onto(sandbox):
    fake_claude(sandbox, f"""echo note >> state/note.md
git add -A && git commit -qm "model commit" && git push -q origin strategist
echo more >> state/note2.md
cd {sandbox.seed} && git fetch -q origin && git checkout -q -B strategist origin/strategist
echo human > state/human.md && git add -A && git commit -qm "human commit" && git push -q origin strategist
""")
    assert sandbox.run("premarket").returncode == 0
    assert sandbox.read("alerts") == ""
    subjects = [line.split(" ", 1)[1] for line in origin_log(sandbox)]
    assert subjects[:3] == [subjects[0], "human commit", "model commit"] and subjects[0].startswith("strategist: ")
    files = git(sandbox.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert {"state/note.md", "state/note2.md", "state/human.md"} <= set(files)


def test_run_that_pushes_then_amends_alerts_that_origin_is_not_reverted(sandbox):
    fake_claude(sandbox, """echo note >> state/note.md
mkdir -p scripts && echo evil > scripts/evil.sh
git add -A && git commit -qm "model commit" && git push -q origin strategist
git commit -q --amend -m amended
""")
    assert sandbox.run("premarket").returncode == 0
    alerts = sandbox.read("alerts")
    assert "NOT reverted" in alerts and "scripts/evil.sh" in alerts.split("NOT reverted")[1]
