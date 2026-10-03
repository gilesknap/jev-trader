"""scripts/strategist.sh in a sandbox: a bare origin, a clone on `strategist`, fake uv/trader/claude.

Two layouts (#169). Monorepo mode (TRADER_DATA_ROOT unset, as deployed today): the strategist branch
carries the code, and the wrapper merges main into it and re-execs the merged copy. Split mode
(TRADER_DATA_ROOT set): the branch is data only, and the wrapper, prompts and charter come from the
code checkout at TRADER_CODE_ROOT, with `trader` from PATH.
"""

import fcntl
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "strategist.sh"

pytestmark = pytest.mark.skipif(not (shutil.which("flock") and shutil.which("git")), reason="needs flock and git")

FAKE_UV = """#!/bin/bash
case "$*" in
  sync\\ *) echo "$UV_PROJECT_ENVIRONMENT $*" >> "$HOME/uv_sync"; echo sync >> "$HOME/seq"
           flock -n -s "$XDG_STATE_HOME/trader/strategist.lock" true && echo free >> "$HOME/sync_lock" || echo held >> "$HOME/sync_lock"
           [[ -e "$HOME/sync_fails" ]] && exit 1; exit 0 ;;
  "run trader session") echo '{"minutes_to_open": 45, "minutes_to_close": 400}' ;;
  "run trader config get models.strategist") echo cfg-model ;;
  run\\ python*) echo "${@: -1}" >> "$HOME/alerts" ;;
  *) echo "$*" >> "$HOME/uv_calls" ;;
esac
"""
FAKE_TRADER = """#!/bin/bash
case "$*" in
  "session") echo session >> "$HOME/seq"; echo '{"minutes_to_open": 45, "minutes_to_close": 400}' ;;
  "config get models.strategist") echo cfg-model ;;
  *) echo "$*" >> "$HOME/trader_calls" ;;
esac
"""
FAKE_TRADER_PYTHON = """#!/bin/bash
echo "${@: -1}" >> "$HOME/alerts"
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


TRACKED_ODD = "docs/réad me.md"  # a tracked path git quotes in plain `git status --porcelain`


def make_sandbox(tmp_path, split=False, code=ROOT):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    fakes = [("uv", FAKE_UV), ("claude", FAKE_CLAUDE), ("trader", FAKE_TRADER), ("trader-python", FAKE_TRADER_PYTHON)]
    for name, body in fakes:
        f = home / ".local" / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "TRADER_STRATEGIST_ROOT": str(tmp_path / "repo"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    if split:
        (tmp_path / "config").mkdir()
        env |= {"TRADER_DATA_ROOT": str(tmp_path / "config"), "TRADER_CODE_ROOT": str(code)}
    git(tmp_path, "init", "-q", "--bare", "origin.git", env=env)
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", "origin.git", "seed", env=env)
    (seed / "state").mkdir()
    (seed / "state" / "s.md").write_text("s")
    # As in the real checkout. (Without a tracked file under features/, git reports a new
    # features/custom/x.py as the untracked dir `features/`, which the path check reverts.)
    (seed / "features" / "custom").mkdir(parents=True)
    (seed / "features" / "custom" / "README.md").write_text("r")
    # The real .gitignore, so a test fails if it ever starts hiding new strategy files (#169 section 14.1).
    shutil.copy(ROOT / ".gitignore", seed / ".gitignore")
    # Tracked files outside the allowed paths, for the revert tests (F5, #177).
    (seed / "CLAUDE.md").write_text("charter\n")
    (seed / "docs").mkdir()
    (seed / TRACKED_ODD).write_text("odd\n")
    if not split:  # monorepo: the branch carries the wrapper and the prompts
        for d in ("scripts", "prompts"):
            (seed / d).mkdir()
        shutil.copy(WRAPPER, seed / "scripts" / "strategist.sh")
        for kind in ("premarket", "postclose", "weekly"):
            (seed / "prompts" / f"{kind}.md").write_text(kind)
    git(seed, "add", "-A", env=env)
    git(seed, "commit", "-qm", "init", env=env)
    git(seed, "push", "-q", "origin", "HEAD:main", "HEAD:strategist", env=env)
    git(tmp_path, "clone", "-q", "-b", "strategist", "origin.git", "repo", env=env)

    class S:
        pass

    s = S()
    s.tmp, s.home, s.env, s.seed, s.repo, s.code = tmp_path, home, env, seed, tmp_path / "repo", code
    s.logdir = tmp_path / "state" / "trader"
    s.wrapper = (code if split else s.repo) / "scripts" / "strategist.sh"

    def run(kind):
        return subprocess.run(["bash", str(s.wrapper), kind], env=env, capture_output=True, text=True, timeout=60)

    def push_main(edit):
        edit(s.seed / "scripts" / "strategist.sh")
        git(s.seed, "commit", "-qam", "change wrapper", env=env)
        git(s.seed, "push", "-q", "origin", "HEAD:main", env=env)

    def read(name):
        p = home / name
        return p.read_text() if p.exists() else ""

    s.run, s.push_main, s.read = run, push_main, read
    return s


@pytest.fixture
def sandbox(tmp_path):
    return make_sandbox(tmp_path)


@pytest.fixture
def split(tmp_path):
    return make_sandbox(tmp_path, split=True)


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
    assert sandbox.read("marker") == "new\n"  # the merged script ran, not the old one
    assert sandbox.read("claude_runs") == "run\n"
    assert "--model cfg-model" in sandbox.read("claude_args")  # from config.yaml models.strategist
    assert sandbox.read("claude_env") == ""  # internal flags don't reach the session
    assert sandbox.read("alerts") == ""
    assert "change wrapper" in git(sandbox.tmp / "origin.git", "log", "--oneline", "strategist")
    assert len(list(sandbox.logdir.glob("*-premarket.log"))) == 1


def test_stray_child_does_not_hold_the_lock(sandbox):
    assert sandbox.run("weekly").returncode == 0  # its fake claude leaves `sleep 3` running
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
    (sandbox.home / ".local" / "bin" / "uv").write_text(
        FAKE_UV.replace('"minutes_to_close": 400', '"minutes_to_close": -30').replace(
            "  *) echo", '  *probe-report*) echo "$*" >> "$HOME/uv_calls"; exit 1 ;;\n  *) echo'
        )
    )
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
    fake_claude(
        sandbox,
        """echo note >> state/note.md
mkdir -p scripts && echo evil > scripts/evil.sh
git add -A && git commit -qm "model commit" && git push -q origin strategist
echo later >> journal.md; mkdir -p journal && echo later > journal/d.md
""",
    )
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
    assert git(sandbox.repo, "rev-parse", "HEAD").strip() == log[0].split()[0]  # not diverged
    assert git(sandbox.repo, "status", "--porcelain") == ""


def test_run_that_pushes_everything_adds_no_commit(sandbox):
    fake_claude(
        sandbox,
        'echo note >> state/note.md\ngit add -A && git commit -qm "model commit" && git push -q origin strategist\n',
    )
    assert sandbox.run("premarket").returncode == 0
    assert sandbox.read("alerts") == ""
    log = origin_log(sandbox)
    assert log[0].endswith("model commit")
    assert git(sandbox.repo, "rev-parse", "HEAD").strip() == log[0].split()[0]


def test_someone_else_pushing_during_the_run_is_rebased_onto(sandbox):
    fake_claude(
        sandbox,
        f"""echo note >> state/note.md
git add -A && git commit -qm "model commit" && git push -q origin strategist
echo more >> state/note2.md
cd {sandbox.seed} && git fetch -q origin && git checkout -q -B strategist origin/strategist
echo human > state/human.md && git add -A && git commit -qm "human commit" && git push -q origin strategist
""",
    )
    assert sandbox.run("premarket").returncode == 0
    assert sandbox.read("alerts") == ""
    subjects = [line.split(" ", 1)[1] for line in origin_log(sandbox)]
    assert subjects[:3] == [subjects[0], "human commit", "model commit"] and subjects[0].startswith("strategist: ")
    files = git(sandbox.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert {"state/note.md", "state/note2.md", "state/human.md"} <= set(files)


def test_run_that_pushes_then_amends_alerts_that_origin_is_not_reverted(sandbox):
    fake_claude(
        sandbox,
        """echo note >> state/note.md
mkdir -p scripts && echo evil > scripts/evil.sh
git add -A && git commit -qm "model commit" && git push -q origin strategist
git commit -q --amend -m amended
""",
    )
    assert sandbox.run("premarket").returncode == 0
    alerts = sandbox.read("alerts")
    assert "NOT reverted" in alerts and "scripts/evil.sh" in alerts.split("NOT reverted")[1]


# ---- both modes ----

NEW_FILES = [
    "journal/daily/2026-10-05.md",
    "journal/weekly/2026-W41.md",
    "logs/new_equity.csv",
    "state/new.md",
    "features/custom/new_feature.py",
]


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_new_strategy_files_are_committed_and_published(tmp_path, mode):
    # #169 section 14.1: a .gitignore entry for these dirs would make `git add -A -- state journal ...`
    # silently skip new files. The sandbox uses the real .gitignore.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(s, "".join(f"mkdir -p $(dirname {f}) && echo x > {f}\n" for f in NEW_FILES))
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert s.read("alerts") == ""
    files = git(s.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert set(NEW_FILES) <= set(files)
    assert git(s.repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_run_lock_is_held_for_the_whole_run(tmp_path, mode):
    # A deploy (#169 section 6.1) refuses while the existing strategist.lock is held; it must be held
    # while claude runs, not just at the start.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(
        s,
        'flock -n -s "$XDG_STATE_HOME/trader/strategist.lock" true && echo free >> "$HOME/probe" '
        '|| echo held >> "$HOME/probe"\n',
    )
    assert s.run("weekly").returncode == 0
    assert s.read("probe") == "held\n"


def test_monorepo_reverts_proposals(sandbox):
    fake_claude(sandbox, "mkdir -p proposals/x && echo p > proposals/x/README.md\necho n > state/n.md\n")
    assert sandbox.run("weekly").returncode == 0
    assert "touched non-strategy paths (reverted): proposals/" in sandbox.read("alerts")
    files = git(sandbox.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert "state/n.md" in files and "proposals/x/README.md" not in files


# ---- split mode ----


def test_split_runs_deployed_code_without_merging_main(split):
    # Something on origin/main must not reach the data branch: there is no merge of main.
    (split.seed / "code.py").write_text("x")
    git(split.seed, "add", "-A", env=split.env)
    git(split.seed, "commit", "-qm", "code on main", env=split.env)
    git(split.seed, "push", "-q", "origin", "HEAD:main", env=split.env)
    r = split.run("premarket")
    assert r.returncode == 0, r.stderr
    assert split.read("alerts") == ""
    assert split.read("uv_calls") == ""  # no `uv run`: trader is the shim on PATH
    assert split.read("claude_runs") == "run\n"
    args = split.read("claude_args")
    assert args.startswith("-p " + (ROOT / "prompts" / "premarket.md").read_text()[:200])
    assert f"--append-system-prompt-file {ROOT / 'CLAUDE.md'} --model cfg-model" in args
    assert split.read("claude_env") == ""  # internal flags (STRATEGIST_COPIED too) don't leak
    files = git(split.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert "state/note.md" in files and "code.py" not in files
    assert "code on main" not in git(split.tmp / "origin.git", "log", "--oneline", "strategist")
    assert list(split.logdir.glob("strategist.sh.*")) == []  # the private copy is removed


def test_split_pulls_origin_strategist_first(split):
    (split.seed / "state" / "remote.md").write_text("r")
    git(split.seed, "add", "-A", env=split.env)
    git(split.seed, "commit", "-qm", "remote change", env=split.env)
    git(split.seed, "push", "-q", "origin", "HEAD:strategist", env=split.env)
    fake_claude(split, 'cat state/remote.md >> "$HOME/seen"\n')
    assert split.run("weekly").returncode == 0
    assert split.read("seen") == "r"
    assert split.read("trader_calls") == "archive\ncompact\n"


def test_split_publishes_proposals_and_reverts_code(split):
    fake_claude(
        split, "mkdir -p proposals/x scripts && echo p > proposals/x/0001-a.patch && echo e > scripts/evil.sh\n"
    )
    assert split.run("weekly").returncode == 0
    alerts = split.read("alerts")
    assert "touched non-strategy paths (reverted): scripts/" in alerts and "proposals" not in alerts
    files = git(split.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert "proposals/x/0001-a.patch" in files and "scripts/evil.sh" not in files
    assert not (split.repo / "scripts").exists()


def code_copy(tmp_path, charter=True):
    code = tmp_path / "code"
    shutil.copytree(ROOT / "prompts", code / "prompts")
    (code / "scripts").mkdir()
    shutil.copy(WRAPPER, code / "scripts" / "strategist.sh")
    if charter:
        shutil.copy(ROOT / "CLAUDE.md", code / "CLAUDE.md")
    return code


@pytest.mark.parametrize("charter", ["missing", "empty"])
def test_split_without_a_charter_alerts_and_does_not_run(tmp_path, charter):
    code = code_copy(tmp_path, charter=False)
    if charter == "empty":
        (code / "CLAUDE.md").write_text("")
    s = make_sandbox(tmp_path, split=True, code=code)
    r = s.run("weekly")
    assert r.returncode == 1
    assert "CLAUDE.md or" in s.read("alerts") and "not running" in s.read("alerts")
    assert s.read("claude_runs") == ""
    assert s.read("trader_calls") == ""  # refused before archive/compact could touch journal/ or logs/
    assert git(s.repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert s.run("housekeeping").returncode == 1  # the charter is checked for every kind
    assert s.read("trader_calls") == ""


def test_split_housekeeping_needs_no_prompt(tmp_path):
    code = code_copy(tmp_path)
    shutil.rmtree(code / "prompts")
    s = make_sandbox(tmp_path, split=True, code=code)
    assert s.run("housekeeping").returncode == 0
    assert s.read("trader_calls") == "housekeeping\n" and s.read("alerts") == ""
    assert s.run("weekly").returncode == 1  # but a strategist run does
    assert "prompts/weekly.md is missing" in s.read("alerts") and "archive" not in s.read("trader_calls")


def test_split_copy_that_does_not_parse_alerts_and_does_not_run(tmp_path):
    # As if the copy were taken while a deploy was half-way through writing the script: the top
    # runs, but the file as a whole doesn't parse.
    code = code_copy(tmp_path)
    with open(code / "scripts" / "strategist.sh", "a") as f:
        f.write("\nif then fi (\n")
    s = make_sandbox(tmp_path, split=True, code=code)
    r = s.run("weekly")
    assert r.returncode == 1
    assert "doesn't parse; not running" in s.read("alerts")
    assert s.read("claude_runs") == "" and s.read("trader_calls") == ""
    assert list(s.logdir.glob("strategist.sh.*")) == []


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_exit_status_is_the_final_log_sweeps(tmp_path, mode):
    # As on main, a run's exit status is that of its last command, the done-* sweep.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_find = s.home / ".local" / "bin" / "find"
    fake_find.write_text('#!/bin/bash\ncase "$*" in *done-*) exit 3 ;; *) exit 0 ;; esac\n')
    fake_find.chmod(0o755)
    assert s.run("weekly").returncode == 3


def test_split_survives_its_script_being_replaced_mid_run(tmp_path):
    # A deploy can rewrite the deployed wrapper in place while a run is reading it; the run must carry
    # on with the copy it started from (path check and publish included).
    code = code_copy(tmp_path)
    s = make_sandbox(tmp_path, split=True, code=code)
    fake_claude(
        s,
        f"""echo note >> state/note.md
printf '%s\\n' 'echo HIJACK >> "$HOME/hijack"; exit 7' "$(head -c 20000 /dev/zero | tr '\\0' '#')" > {code}/scripts/strategist.sh
""",
    )
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert s.read("hijack") == "" and s.read("alerts") == ""
    files = git(s.tmp / "origin.git", "ls-tree", "-r", "--name-only", "strategist").split()
    assert "state/note.md" in files
    assert list(s.logdir.glob("strategist.sh.*")) == []


def test_split_held_lock_skips_and_removes_its_copy(split):
    split.logdir.mkdir(parents=True)
    with open(split.logdir / "strategist.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        r = split.run("weekly")
    assert r.returncode == 0
    assert split.read("claude_runs") == ""
    assert "another run holds the lock" in "".join(p.read_text() for p in split.logdir.glob("*.log"))
    assert list(split.logdir.glob("strategist.sh.*")) == []


def test_split_housekeeping_leaves_a_busy_checkout_alone(split):
    git(split.repo, "checkout", "-q", "-b", "proposal/x", env=split.env)
    (split.repo / "state" / "s.md").write_text("edited")
    r = split.run("housekeeping")
    assert r.returncode == 0, r.stderr
    assert git(split.repo, "branch", "--show-current", env=split.env).strip() == "proposal/x"
    assert split.read("trader_calls") == "housekeeping\n"
    assert split.read("uv_calls") == ""


# ---- trader's venv re-sync (split mode, #169 section 6.2 / C2) ----


def test_split_syncs_trader_venv_first_under_the_lock(split):
    # Exactly 2-strategist.sh's command, against the deployed code, before the session lookup uses
    # the venv, while the run lock is held (a deploy takes it too).
    assert split.run("premarket").returncode == 0
    venv = split.home / ".local" / "share" / "trader" / "venv"
    assert split.read("uv_sync") == f"{venv} sync -q --frozen --extra dev --project {ROOT}\n"
    assert split.read("seq") == "sync\nsession\n"
    assert split.read("sync_lock") == "held\n"
    assert split.read("claude_runs") == "run\n" and split.read("alerts") == ""
    setup = (ROOT / "deploy" / "setup" / "2-strategist.sh").read_text()
    assert 'UV_PROJECT_ENVIRONMENT=$VENV uv sync -q --frozen --extra dev --project "$CODE"' in setup
    assert "VENV=$HOME/.local/share/trader/venv" in setup


@pytest.mark.parametrize("kind", ["weekly", "housekeeping"])
def test_split_failed_venv_sync_alerts_and_does_not_run(split, kind):
    (split.home / "sync_fails").write_text("")
    r = split.run(kind)
    assert r.returncode == 1
    assert "could not sync trader's venv" in split.read("alerts") and "not running" in split.read("alerts")
    assert split.read("claude_runs") == "" and split.read("trader_calls") == ""
    assert git(split.repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert "venv sync failed" in "".join(p.read_text() for p in split.logdir.glob("*.log"))
    assert list(split.logdir.glob("strategist.sh.*")) == []


def test_monorepo_does_not_sync_trader_venv(sandbox):
    assert sandbox.run("weekly").returncode == 0
    assert sandbox.read("uv_sync") == ""


# ---- the charter and prompts for the split layout (#169 C2) ----


def test_charter_and_prompts_fit_the_split_layout():
    charter = (ROOT / "CLAUDE.md").read_text()
    prompts = {k: (ROOT / "prompts" / f"{k}.md").read_text() for k in ("premarket", "postclose", "weekly")}
    for name, text in {"CLAUDE.md": charter, **prompts}.items():
        assert "uv run trader" not in text, name  # `trader` is the shim on PATH
        assert "gh pr" not in text, name  # no code PRs, and the weekly is an issue
        assert "Read CLAUDE.md" not in text, name  # the charter comes in the system prompt
    for text in prompts.values():
        assert "Your charter is in your system prompt" in text
    assert "This charter is in your system prompt" in charter
    assert "`state/`, `journal/`, `features/custom/`, `logs/` and `proposals/`" in charter
    # A local clone checks ownership on the gitdir: safe.directory must name /srv/trading/main/.git.
    assert (
        "git -c safe.directory=/srv/trading/main/.git clone -q --no-hardlinks /srv/trading/main ~/work/<topic>"
        in charter
    )
    assert "safe.directory=/srv/trading/main " not in charter and "`trader-test`" in charter
    assert "other than this week's" in prompts["weekly"]
    assert "Never interact with `gilesknap/jev-trader` or any other public GitHub repository" in charter
    assert "untrusted" in charter and "`trader-python`" in charter
    assert "gh issue create --label weekly" in prompts["weekly"]
    assert "proposals/<topic>/" in prompts["postclose"]


# ---- the path check's revert (F5 from the split rehearsal, #169; #177) ----


def origin_files(s):
    return set(git(s.tmp / "origin.git", "ls-tree", "-r", "--name-only", "-z", "strategist").split("\0")) - {""}


def origin_show(s, path):
    return git(s.tmp / "origin.git", "show", f"strategist:{path}")


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_edited_tracked_and_new_untracked_outside_files_are_both_reverted(tmp_path, mode):
    # F5 exactly: the run edited CLAUDE.md and created evil.txt. One `git checkout` of both paths failed
    # as a whole on the untracked one, so only evil.txt went and CLAUDE.md kept its edit, yet the alert
    # said "(reverted)".
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(s, "echo evil >> CLAUDE.md\necho evil > evil.txt\necho n > state/n.md\n")
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert (s.repo / "CLAUDE.md").read_text() == "charter\n"
    assert not (s.repo / "evil.txt").exists()
    alerts = s.read("alerts")
    assert "touched non-strategy paths (reverted): CLAUDE.md, evil.txt" in alerts
    assert "could NOT" not in alerts
    assert git(s.repo, "status", "--porcelain") == ""
    files = origin_files(s)
    assert "state/n.md" in files and "evil.txt" not in files
    assert origin_show(s, "CLAUDE.md") == "charter\n"


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_paths_with_spaces_and_non_ascii_are_checked_exactly(tmp_path, mode):
    # #177: plain porcelain quotes these paths and `awk '{print $NF}'` took only their last word.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(
        s,
        f"""echo evil >> "{TRACKED_ODD}"
mkdir -p scripts && echo evil > "scripts/evil file.sh"
echo evil > "naïve évil.txt"
echo keep > "state/my note.md"
mkdir -p journal && echo keep > "journal/café ☕.md"
""",
    )
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert (s.repo / TRACKED_ODD).read_text() == "odd\n"
    assert not (s.repo / "scripts" / "evil file.sh").exists() and not (s.repo / "naïve évil.txt").exists()
    alerts = s.read("alerts")
    assert "could NOT" not in alerts
    (line,) = [a for a in alerts.splitlines() if "touched non-strategy paths (reverted): " in a]
    assert TRACKED_ODD in line and "naïve évil.txt" in line and "scripts/" in line
    assert "state/" not in line and "journal/" not in line  # allowed paths aren't named
    assert git(s.repo, "status", "--porcelain") == ""
    files = origin_files(s)
    assert {"state/my note.md", "journal/café ☕.md"} <= files
    assert not {"scripts/evil file.sh", "naïve évil.txt"} & files
    assert origin_show(s, TRACKED_ODD) == "odd\n"


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_renames_across_the_allowed_boundary(tmp_path, mode):
    # Out of an allowed dir (the outside copy goes; the deletion inside is a strategy change) and into
    # one (the tracked outside file comes back; its copy inside is a strategy file). Committed, so the
    # wrapper's reset turns them into a deletion plus an untracked file.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(
        s,
        """mkdir -p scripts && git mv state/s.md scripts/s.md
git mv CLAUDE.md state/charter.md
git commit -qm renames
""",
    )
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert not (s.repo / "scripts" / "s.md").exists()
    assert (s.repo / "CLAUDE.md").read_text() == "charter\n"
    alerts = s.read("alerts")
    assert "touched non-strategy paths (reverted): CLAUDE.md, scripts/" in alerts and "could NOT" not in alerts
    assert git(s.repo, "status", "--porcelain") == ""
    files = origin_files(s)
    assert "CLAUDE.md" in files and "state/charter.md" in files
    assert "scripts/s.md" not in files and "state/s.md" not in files


SURVIVORS = {
    # A stale index lock: the wrapper's reset and every checkout fail, so the edit stays.
    "index lock": ("echo evil >> CLAUDE.md\ntouch .git/index.lock\n", "CLAUDE.md"),
    # The same with a staged rename out of an allowed dir: a rename entry, two paths in one record.
    "staged rename": (
        "mkdir -p scripts && git mv state/s.md scripts/moved.md\ntouch .git/index.lock\n",
        "scripts/moved.md",
    ),
    # `git clean -fd` won't remove a nested repository.
    "nested repo": ("mkdir -p tools/x && git -C tools/x init -q && echo x > tools/x/f\n", "tools/"),
}


@pytest.mark.parametrize("mode", ["monorepo", "split"])
@pytest.mark.parametrize("case", list(SURVIVORS))
def test_an_outside_path_that_survives_the_revert_alerts_and_is_not_published(tmp_path, mode, case):
    s = make_sandbox(tmp_path, split=mode == "split")
    body, survivor = SURVIVORS[case]
    fake_claude(s, "echo n > state/n.md\n" + body)
    before = origin_log(s)
    r = s.run("weekly")
    assert r.returncode == 1
    alerts = s.read("alerts")
    assert "could NOT revert" in alerts and "not publishing" in alerts
    assert survivor in alerts.split("Still changed: ")[1].split(". Touched")[0]
    assert "(reverted)" not in alerts
    assert "differs from the run" not in alerts and "Could not check" not in alerts  # the run pushed nothing
    assert origin_log(s) == before  # nothing published, not even state/n.md
    assert not list(s.logdir.glob("status.*"))  # the status scratch file is removed


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_show_untracked_files_no_does_not_hide_outside_files(tmp_path, mode):
    s = make_sandbox(tmp_path, split=mode == "split")
    git(s.repo, "config", "status.showUntrackedFiles", "no", env=s.env)
    fake_claude(
        s, "mkdir -p scripts/deep && echo evil > scripts/deep/evil.sh && echo evil > evil.txt\necho n > state/n.md\n"
    )
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    alerts = s.read("alerts")
    assert (
        "touched non-strategy paths (reverted): evil.txt, scripts/deep/evil.sh" in alerts and "could NOT" not in alerts
    )
    assert not (s.repo / "evil.txt").exists() and not (s.repo / "scripts" / "deep").exists()  # no empty dir left
    assert git(s.repo, "status", "--porcelain", "--untracked-files=all") == ""
    files = origin_files(s)
    assert "state/n.md" in files and not {"evil.txt", "scripts/deep/evil.sh"} & files


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_tracked_file_replaced_by_a_directory_is_restored(tmp_path, mode):
    # The untracked file inside goes first, then its now-empty directory, so the file can come back.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(s, "rm CLAUDE.md && mkdir -p CLAUDE.md/sub && echo evil > CLAUDE.md/sub/x\n")
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert "could NOT" not in s.read("alerts")
    assert (s.repo / "CLAUDE.md").read_text() == "charter\n"
    assert git(s.repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert origin_show(s, "CLAUDE.md") == "charter\n"


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_survivor_alert_names_what_the_run_pushed_and_stops_same_day_retries(tmp_path, mode):
    # The refusal skips the publish that would revert the run's own pushes on origin: name them.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(
        s,
        """echo n > state/n.md
mkdir -p scripts && echo evil > scripts/evil.sh
git add -A && git commit -qm "model commit" && git push -q origin strategist
mkdir -p tools/x && git -C tools/x init -q && echo x > tools/x/f
""",
    )
    r = s.run("premarket")
    assert r.returncode == 1
    (line,) = [a for a in s.read("alerts").splitlines() if "could NOT revert" in a]
    assert "no retry today" in line
    assert (
        "differs from the run's start outside strategy paths (NOT reverted; pushed by the run, or by a human during it): scripts/evil.sh"
        in line
    )
    assert "state/n.md" not in line.split("during it): ")[1]  # allowed paths aren't named
    r = s.run("premarket")  # a later tick the same day
    assert r.returncode == 0 and s.read("alerts").count("could NOT revert") == 1
    assert "already ran today" in "".join(p.read_text() for p in s.logdir.glob("*.log"))


def test_survivor_alert_says_when_origin_cannot_be_checked(sandbox):
    fake_claude(
        sandbox,
        f"mkdir -p tools/x && git -C tools/x init -q && echo x > tools/x/f\nmv {sandbox.tmp}/origin.git {sandbox.tmp}/gone.git\n",
    )
    assert sandbox.run("weekly").returncode == 1
    alerts = sandbox.read("alerts")
    assert "could NOT revert" in alerts and "Could not check origin/strategist" in alerts


# ---- human-owned files inside the allowed dirs (#201) ----


def push_steering(s, text="S1 human\n"):
    git(s.seed, "fetch", "-q", "origin", env=s.env)
    git(s.seed, "checkout", "-q", "-B", "strategist", "origin/strategist", env=s.env)
    (s.seed / "state" / "steering.md").write_text(text)
    git(s.seed, "add", "-A", env=s.env)
    git(s.seed, "commit", "-qm", "steering", env=s.env)
    git(s.seed, "push", "-q", "origin", "strategist", env=s.env)


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_steering_edit_is_reverted_and_the_human_merge_is_kept(tmp_path, mode):
    # The human's steering arrives between runs; the run's edit to it is reverted, its other strategy
    # changes publish.
    s = make_sandbox(tmp_path, split=mode == "split")
    push_steering(s)
    fake_claude(s, "echo mine >> state/steering.md\necho n > state/n.md\n")
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert "touched non-strategy paths (reverted): state/steering.md" in s.read("alerts")
    assert (s.repo / "state" / "steering.md").read_text() == "S1 human\n"
    assert git(s.repo, "status", "--porcelain") == ""
    assert origin_show(s, "state/steering.md") == "S1 human\n"
    assert "state/n.md" in origin_files(s)


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_steering_created_or_deleted_by_the_run_is_reverted(tmp_path, mode):
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(s, "echo mine > state/steering.md\n")
    assert s.run("weekly").returncode == 0
    assert "(reverted): state/steering.md" in s.read("alerts")
    assert "state/steering.md" not in origin_files(s)

    s2 = make_sandbox(tmp_path / "b", split=mode == "split")
    push_steering(s2)
    fake_claude(s2, "rm state/steering.md\n")
    assert s2.run("weekly").returncode == 0
    assert "(reverted): state/steering.md" in s2.read("alerts")
    assert origin_show(s2, "state/steering.md") == "S1 human\n"


def test_steering_edit_the_run_pushed_itself_is_reverted_on_origin(sandbox):
    push_steering(sandbox)
    fake_claude(
        sandbox,
        """echo mine >> state/steering.md
echo n > state/n.md
git add -A && git commit -qm "model commit" && git push -q origin strategist
""",
    )
    assert sandbox.run("premarket").returncode == 0
    alerts = sandbox.read("alerts")
    assert "pushed non-strategy paths to origin/strategist (reverted): state/steering.md" in alerts
    assert "NOT reverted" not in alerts
    assert origin_show(sandbox, "state/steering.md") == "S1 human\n"
    assert "state/n.md" in origin_files(sandbox)


def test_human_steering_pushed_during_a_run_is_kept_and_named(sandbox):
    # Merging during a run is against the how-to; the wrapper keeps the human's commit and says so.
    fake_claude(
        sandbox,
        f"""echo n > state/n.md
cd {sandbox.seed} && git fetch -q origin && git checkout -q -B strategist origin/strategist
echo human > state/steering.md && git add -A && git commit -qm "human steering" && git push -q origin strategist
""",
    )
    assert sandbox.run("premarket").returncode == 0
    alerts = sandbox.read("alerts")
    assert "NOT reverted" in alerts and "state/steering.md" in alerts
    assert origin_show(sandbox, "state/steering.md") == "human\n"


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_renaming_the_last_state_file_onto_steering_keeps_state(tmp_path, mode):
    # The cleanup of the reverted steering.md must not rmdir the (now empty) allowed state/ dir,
    # or the deletion of state/s.md is never staged and the checkout is left dirty.
    s = make_sandbox(tmp_path, split=mode == "split")
    fake_claude(s, "git mv state/s.md state/steering.md\n")
    r = s.run("weekly")
    assert r.returncode == 0, r.stderr
    assert "(reverted): state/steering.md" in s.read("alerts")
    assert git(s.repo, "status", "--porcelain") == ""
    files = origin_files(s)
    assert "state/steering.md" not in files and "state/s.md" not in files


# ---- the trial ledger is append-only ----

LEDGER_TEXT = "time,kind\n2026-10-01T10:00:00+00:00,replay\n"


def push_ledger(s, text=LEDGER_TEXT):
    git(s.seed, "fetch", "-q", "origin", env=s.env)
    git(s.seed, "checkout", "-q", "-B", "strategist", "origin/strategist", env=s.env)
    (s.seed / "logs").mkdir(exist_ok=True)
    (s.seed / "logs" / "trials.csv").write_text(text)
    git(s.seed, "add", "-A", env=s.env)
    git(s.seed, "commit", "-qm", "ledger", env=s.env)
    git(s.seed, "push", "-q", "origin", "strategist", env=s.env)


@pytest.mark.parametrize("mode", ["monorepo", "split"])
def test_rows_added_to_the_ledger_are_published(tmp_path, mode):
    s = make_sandbox(tmp_path, split=mode == "split")
    push_ledger(s)
    fake_claude(s, "echo 2026-10-02T10:00:00+00:00,replay >> logs/trials.csv\n")
    assert s.run("weekly").returncode == 0
    assert "trials" not in s.read("alerts")
    assert origin_show(s, "logs/trials.csv") == LEDGER_TEXT + "2026-10-02T10:00:00+00:00,replay\n"


@pytest.mark.parametrize(
    "change",
    [
        "sed -i 's/replay/probe_report/' logs/trials.csv",  # an edited row
        "head -c 20 logs/trials.csv > t && mv t logs/trials.csv",  # truncated
        "rm logs/trials.csv",  # removed
        "printf 'x\\n' > ~/x && rm logs/trials.csv && ln -s ~/x logs/trials.csv",  # a symlink
        "rm logs/trials.csv && mkdir logs/trials.csv",  # a directory
        "echo new > logs/trials.csv && git add -A && git commit -qm c && git push -q origin strategist",
    ],
)
def test_any_other_change_to_the_ledger_is_reverted_and_alerted(sandbox, change):
    push_ledger(sandbox)
    fake_claude(sandbox, change + "\necho n > state/n.md\n")
    assert sandbox.run("weekly").returncode == 0
    assert "logs/trials.csv, which is append-only: reverted" in sandbox.read("alerts")
    assert (sandbox.repo / "logs" / "trials.csv").read_text() == LEDGER_TEXT
    assert origin_show(sandbox, "logs/trials.csv") == LEDGER_TEXT
    assert "state/n.md" in origin_files(sandbox)
    assert git(sandbox.repo, "status", "--porcelain") == ""


def test_a_ledger_tampered_in_an_earlier_failed_run_is_caught_by_the_next(sandbox):
    push_ledger(sandbox)
    fake_claude(sandbox, "echo edited > logs/trials.csv\nexit 1\n")
    assert sandbox.run("weekly").returncode == 1
    fake_claude(sandbox, "true\n")
    assert sandbox.run("weekly").returncode == 0
    assert "append-only: reverted" in sandbox.read("alerts")
    assert origin_show(sandbox, "logs/trials.csv") == LEDGER_TEXT


def test_the_first_ledger_is_published(sandbox):
    fake_claude(sandbox, "mkdir -p logs && printf 'time,kind\\n' > logs/trials.csv\n")
    assert sandbox.run("weekly").returncode == 0
    assert "trials" not in sandbox.read("alerts")
    assert origin_show(sandbox, "logs/trials.csv") == "time,kind\n"


# ---- the trading day is the New York date ----


def ny_dates():
    """Today's New York date, and the dates either side in case the run straddles midnight there."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    return {(today + dt.timedelta(days=d)).isoformat() for d in (-1, 0, 1)}


def test_trading_day_is_the_new_york_date(sandbox):
    before = ny_dates()
    assert sandbox.run("premarket").returncode == 0
    dates = before | ny_dates()
    stamps = [p.name for p in sandbox.logdir.glob("done-premarket-*")]
    assert len(stamps) == 1 and stamps[0].removeprefix("done-premarket-") in dates
    assert origin_log(sandbox)[0].split(" strategist: premarket ")[1] in dates
    assert any(f"the trading day (New York date) is {d}" in sandbox.read("claude_args") for d in dates)
