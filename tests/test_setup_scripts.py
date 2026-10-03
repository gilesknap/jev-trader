"""The split layout's (#169) setup pieces: deploy/setup/lib.sh helpers, cfg.sh's data root,
scripts/trader-shim, trader-python and trader-test, and 0-data.sh against fixtures (a bare local
remote, a fake gh, and a fake uv that renders with this tree's real code).

The setup scripts themselves act on the host (users, units, ACLs), so they aren't run here; their
split-only logic lives in lib.sh, which is tested function by function.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from trader import config

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "deploy" / "setup"
LIB = SETUP / "lib.sh"

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def sh(script, *args, env=None, cwd=None):
    """Run a bash snippet with lib.sh sourced; args are $1..."""
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {script}', "bash", *map(str, args)],
                          capture_output=True, text=True, env=env, cwd=cwd)


@pytest.mark.parametrize("path", sorted(SETUP.glob("*.sh")) + [ROOT / "scripts" / n for n in ("trader-shim", "trader-python", "trader-test")])
def test_scripts_parse(path):
    shell = "bash" if path.suffix == ".sh" else "sh"
    assert subprocess.run([shell, "-n", str(path)], capture_output=True).returncode == 0


# ---- cfg.sh ------------------------------------------------------------------------------

def test_cfg_reads_the_data_root_config_when_set(tmp_path):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text()) if (ROOT / "config.yaml").exists() else None
    if raw is None:
        pytest.skip("no config.yaml at the code root")
    raw["owner"]["github_repo"] = "someone/their-data"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    env = {**os.environ, "TRADER_DATA_ROOT": str(tmp_path)}
    r = subprocess.run(["bash", "-c", f'. "{SETUP}/cfg.sh" && cfg owner github_repo'], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "someone/their-data"


def test_cfg_falls_back_to_the_checkout_root():
    env = {k: v for k, v in os.environ.items() if k != "TRADER_DATA_ROOT"}
    r = subprocess.run(["bash", "-c", f'. "{SETUP}/cfg.sh" && echo "$CONFIG_YAML"'], capture_output=True, text=True, env=env)
    assert r.stdout.strip() == str(ROOT / "config.yaml")


def cfg_repo_in(tmp_path, slug):
    (tmp_path / "config.yaml").write_text(f"owner:\n  name: x\n  github_repo: {slug}   # comment\n")
    env = {**os.environ, "TRADER_DATA_ROOT": str(tmp_path)}
    return subprocess.run(["bash", "-c", f'. "{SETUP}/cfg.sh" && cfg_repo'], capture_output=True, text=True, env=env)


def test_cfg_repo_reads_a_real_slug(tmp_path):
    r = cfg_repo_in(tmp_path, "someone/their-data")
    assert r.returncode == 0 and r.stdout.strip() == "someone/their-data" and r.stderr == ""


def test_cfg_repo_refuses_the_template_placeholder(tmp_path):
    """Rehearsal F1: `3-runner.sh key` without TRADER_DATA_ROOT read the public code's placeholder
    config.yaml and printed a deploy-key page for your-github-user/your-repo."""
    r = cfg_repo_in(tmp_path, "your-github-user/your-data-repo")
    assert r.returncode != 0 and r.stdout == ""
    assert "placeholder" in r.stderr and "TRADER_DATA_ROOT" in r.stderr


def test_cfg_repo_refuses_the_shipped_template():
    tpl = ROOT / "templates" / "data" / "main"
    if not (tpl / "config.yaml").exists():
        pytest.skip("no data template in this tree")
    env = {**os.environ, "TRADER_DATA_ROOT": str(tpl)}
    r = subprocess.run(["bash", "-c", f'. "{SETUP}/cfg.sh" && cfg_repo'], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "placeholder" in r.stderr


@pytest.mark.parametrize("script", ["2-strategist.sh", "3-runner.sh", "check.sh"])
def test_setup_scripts_read_the_repo_through_cfg_repo(script):
    text = (SETUP / script).read_text()
    assert "cfg_repo" in text
    # a raw read only to report the placeholder itself (check.sh)
    raw = [ln for ln in text.splitlines() if "cfg owner github_repo" in ln]
    assert len(raw) <= 1 and (not raw or script == "check.sh")
    if raw:
        nxt = text.splitlines()[text.splitlines().index(raw[0]) + 1]
        assert "your-github-user/*" in nxt and "bad " in nxt


# ---- lib.sh ------------------------------------------------------------------------------

def test_split_layout_needs_a_checkout_not_just_the_directory(tmp_path):
    assert sh('split_layout "$1"', tmp_path).returncode != 0   # 1-host.sh makes the empty dir everywhere
    (tmp_path / ".git").mkdir()
    assert sh('split_layout "$1"', tmp_path).returncode == 0
    assert sh('echo "$CONFIG_CHECKOUT"').stdout.strip() == "/srv/trading/config"


def test_a_unit_naming_a_missing_config_checkout_is_refused(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    unit = tmp_path / "u.service"
    unit.write_text(f"[Service]\nEnvironment=TRADER_DATA_ROOT={cfg_dir}\nExecStart=/srv/trading/main/scripts/strategist.sh %i\n")
    assert sh('unit_needs_missing_config "$1" "$2"', unit, cfg_dir).returncode == 0   # refuse
    (cfg_dir / "config.yaml").write_text("owner: {}\n")
    assert sh('unit_needs_missing_config "$1" "$2"', unit, cfg_dir).returncode != 0
    today = ROOT / "deploy" / "systemd-trader" / "trader-strategist@.service"
    assert sh('unit_needs_missing_config "$1" "$2"', today, tmp_path / "absent").returncode != 0


@pytest.mark.parametrize("text, imports", [
    ("@/srv/trading/main/CLAUDE.md\n", True),
    ("# Env\n- see the charter:\n  @~/charter/CLAUDE.md\n", True),
    ("The charter is `/srv/trading/main/CLAUDE.md` (not an import).\n", False),
    ("Charter: `@/srv/trading/main/CLAUDE.md`\n", False),
    ("mail me at someone@example.com about CLAUDE.md\n", False),
    ("@notes.md\n", False),
])
def test_charter_import_detection(tmp_path, text, imports):
    f = tmp_path / "CLAUDE.md"
    f.write_text(text)
    assert (sh('claude_md_imports_charter "$1"', f).returncode == 0) is imports


def test_charter_import_detection_without_a_file(tmp_path):
    assert sh('claude_md_imports_charter "$1"', tmp_path / "CLAUDE.md").returncode != 0


def test_deny_merge_keeps_every_other_setting(tmp_path):
    f = tmp_path / "settings.json"
    before = {"permissions": {"defaultMode": "auto", "deny": ["Bash(rm -rf /*)"], "allow": ["Read"]},
              "skipDangerousModePermissionPrompt": True, "statusLine": {"type": "command", "command": "x"}}
    f.write_text(json.dumps(before))
    f.chmod(0o640)
    r = sh('claude_deny_merge "$@"', f, "WebFetch(domain:github.com)", "Bash(rm -rf /*)", "Bash(gh *jev-trader*)")
    assert r.returncode == 0, r.stderr
    after = json.loads(f.read_text())
    assert after["permissions"]["deny"] == ["Bash(rm -rf /*)", "WebFetch(domain:github.com)", "Bash(gh *jev-trader*)"]
    assert {k: v for k, v in after.items() if k != "permissions"} == {k: v for k, v in before.items() if k != "permissions"}
    assert after["permissions"]["defaultMode"] == "auto" and after["permissions"]["allow"] == ["Read"]
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    assert "WebFetch(domain:github.com)" in r.stdout
    # idempotent: nothing to add, nothing printed, file untouched
    mtime = f.stat().st_mtime_ns
    r = sh('claude_deny_merge "$@"', f, "WebFetch(domain:github.com)")
    assert r.returncode == 0 and r.stdout == "" and f.stat().st_mtime_ns == mtime
    assert list(tmp_path.iterdir()) == [f]   # no temp files left


def test_deny_merge_creates_a_private_file(tmp_path):
    f = tmp_path / "settings.json"
    assert sh('claude_deny_merge "$@"', f, "WebFetch(domain:github.com)").returncode == 0
    assert json.loads(f.read_text()) == {"permissions": {"deny": ["WebFetch(domain:github.com)"]}}
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["{not json", "[]", '{"permissions": []}', '{"permissions": {"deny": "x"}}'])
def test_deny_merge_refuses_what_it_cannot_merge(tmp_path, content):
    f = tmp_path / "settings.json"
    f.write_text(content)
    r = sh('claude_deny_merge "$@"', f, "WebFetch(domain:github.com)")
    assert r.returncode != 0 and "nothing changed" in r.stderr
    assert f.read_text() == content


def test_deny_rules_close_the_named_routes_to_public_github():
    rules = sh('strategist_deny_rules ""').stdout.splitlines()
    for rule in ("WebFetch(domain:github.com)", "WebFetch(domain:raw.githubusercontent.com)",
                 "Bash(curl *github.com*)", "Bash(wget *github.com*)", "Bash(gh *jev-trader*)"):
        assert rule in rules
    assert not any("trading" in r for r in rules)   # gh against the data repo stays allowed
    fork = sh('strategist_deny_rules my-fork').stdout.splitlines()
    assert "Bash(gh *my-fork*)" in fork and "Bash(gh *jev-trader*)" in fork
    same = sh('strategist_deny_rules jev-trader').stdout.splitlines()
    assert same.count("Bash(gh *jev-trader*)") == 1


@pytest.mark.parametrize("code, data, kept, skipped", [
    ("trading", "trading", ["Bash(gh *jev-trader*)"], ["Bash(gh *trading*)"]),
    ("trader", "my-trader-data", ["Bash(gh *jev-trader*)"], ["Bash(gh *trader*)"]),
    ("my-fork", "jev-trader-data", ["Bash(gh *my-fork*)"], ["Bash(gh *jev-trader*)"]),
    ("my-fork", "trading", ["Bash(gh *my-fork*)", "Bash(gh *jev-trader*)"], []),
])
def test_deny_rules_never_block_gh_on_the_data_repo(code, data, kept, skipped):
    r = sh('strategist_deny_rules "$1" "$2"', code, data)
    rules = r.stdout.splitlines()
    assert all(k in rules for k in kept) and not any(k in rules for k in skipped)
    assert all(f"no '{k}' deny rule" in r.stderr for k in skipped)
    assert ("WARNING" in r.stderr) is bool(skipped)


@pytest.mark.parametrize("url, name", [
    ("https://github.com/gilesknap/jev-trader.git", "jev-trader"),
    ("https://github.com/someone/fork", "fork"),
    ("git@github-trading:gilesknap/trading", "trading"),
    ("git@github.com:a/b.git", "b"),
    ("", ""),
])
def test_repo_name(url, name):
    assert sh('repo_name "$1"', url).stdout.strip() == name


# ---- check.sh's verdicts (lib.sh) ------------------------------------------------------

def verdict(script, *args):
    """Run SCRIPT under check.sh's own shell options; prints its output then fails=N."""
    return sh(f'set -uo pipefail; fails=0; {script}; echo "fails=$fails"', *args)


def test_nchk_fails_on_a_tree_with_many_group_writable_files(tmp_path):
    """Rehearsal F3: under pipefail, `find ... | grep -q .` lost its find to SIGPIPE (141) once grep
    matched, so nchk passed exactly when there WERE group-writable files. Enough files that find's
    output overflows the pipe after grep has quit."""
    tree = tmp_path / "t"
    tree.mkdir()
    for i in range(3000):
        f = tree / f"group-writable-file-with-a-longish-name-{i:05d}"
        f.write_text("")
        f.chmod(0o664)
    expr = f"find {tree} ! -type l -perm -g=w | grep -q ."
    r = verdict('nchk "no group write" "$1"', expr)
    assert "FAIL" in r.stdout and "PASS" not in r.stdout and r.stdout.strip().endswith("fails=1"), r.stdout
    r = verdict('chk "has group write" "$1"', expr)
    assert "PASS" in r.stdout and r.stdout.strip().endswith("fails=0"), r.stdout
    for f in tree.iterdir():
        f.chmod(0o644)
    tree.chmod(0o755)
    r = verdict('nchk "no group write" "$1"', expr)
    assert "PASS" in r.stdout and r.stdout.strip().endswith("fails=0"), r.stdout


def test_check_verdicts_judge_the_last_command_and_count_failures():
    r = verdict('chk a true; chk b false; nchk c false; nchk d true; wchk e false; wchk f true')
    lines = r.stdout.splitlines()
    got = {ln.split()[-1]: ("PASS" if "PASS" in ln else "FAIL" if "FAIL" in ln else "WARN") for ln in lines[:-1]}
    assert got == {"a": "PASS", "b": "FAIL", "c": "PASS", "d": "FAIL", "e": "WARN", "f": "PASS"}
    assert lines[-1] == "fails=2"   # a WARN isn't counted
    # a failing left-hand side is no input to grep, not a verdict of its own
    r = verdict('nchk x "false | grep -q ."; chk y "printf a | grep -q a"; nchk z "(echo m; exit 3) | grep -q m"')
    got = {ln.split()[-1]: "PASS" if "PASS" in ln else "FAIL" for ln in r.stdout.splitlines()[:-1]}
    assert got == {"x": "PASS", "y": "PASS", "z": "FAIL"} and r.stdout.strip().endswith("fails=1")


def test_check_sh_uses_the_lib_verdicts():
    """check.sh must not grow its own eval-based chk again: every verdict goes through check_eval."""
    text = (SETUP / "check.sh").read_text()
    assert not re.search(r"\beval\b", text)
    assert not any(line.startswith(("chk()", "nchk()", "wchk()")) for line in text.splitlines())


def test_check_sh_checks_the_strategist_tree_and_gpg():
    text = (SETUP / "check.sh").read_text()
    assert 'GWL=$(find /srv/trading/strategist ! -type l -perm -g=w 2>/dev/null)' in text
    assert 'nchk "strategist tree has no group write"         "[[ -n \\$GWL ]]"' in text
    assert "command -v gpg" in text   # rehearsal F6: trading-deploy's plan verifies signatures with it


# ---- the umask and the next-step hints (rehearsal F4, F9) --------------------------------

@pytest.mark.parametrize("script", ["2-strategist.sh", "3-runner.sh"])
def test_setup_scripts_set_umask_022_before_writing(script):
    """Trader's login umask can be 0002, and group trading includes runner: a checkout cloned under it
    is writable by runner (rehearsal F4)."""
    lines = (SETUP / script).read_text().splitlines()
    umask = lines.index("umask 022")
    first_write = next(i for i, ln in enumerate(lines) if not ln.lstrip().startswith("#")
                       and any(w in ln for w in ("git clone", "install ", "mkdir", "cp ", "> ")))
    assert umask < first_write


def test_2_strategist_split_hint_names_the_second_runner_install():
    text = (SETUP / "2-strategist.sh").read_text()
    split_hint = text[text.rindex("if [[ -n $SPLIT ]]; then"):]
    split_hint = split_hint[:split_hint.index("else")]
    assert split_hint.index("3-runner.sh install") < split_hint.index("check.sh")


# ---- the strategist's helpers ------------------------------------------------------------

def fake_venv(home, *names):
    """A fake ~/.local/share/trader/venv/bin with programs that print their environment and args."""
    bin_ = home / ".local" / "share" / "trader" / "venv" / "bin"
    bin_.mkdir(parents=True)
    for name in names:
        p = bin_ / name
        p.write_text(f'#!/bin/sh\necho "PROG={name}"\necho "ARGS=$*"\nenv | grep -E "^(TRADER_|PYTHONPATH=)" | sort\n')
        p.chmod(0o755)
    return bin_


def run_env(cmd, home, cwd=None, **extra):
    base = {"PATH": os.environ["PATH"], "HOME": str(home), **extra}
    r = subprocess.run(cmd, capture_output=True, text=True, env=base, cwd=cwd)
    out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return r, out


SPLIT_ENV = {
    "TRADER_CODE_ROOT": "/srv/trading/main", "TRADER_DATA_ROOT": "/srv/trading/config",
    "TRADER_STRATEGIST_ROOT": "/srv/trading/strategist", "TRADER_RUNTIME": "/srv/trading/runtime",
    "TRADER_REPLAY_DIR": "/srv/trading/strategist/replays", "TRADER_SECRETS": "/srv/trading/strategist/.env",
}


@pytest.mark.parametrize("script, prog", [("trader-shim", "trader"), ("trader-python", "python")])
def test_shims_run_trader_venv_against_the_deployed_layout(tmp_path, script, prog):
    fake_venv(tmp_path, "trader", "python")
    r, out = run_env([str(ROOT / "scripts" / script), "validate", "--file", "a b.yaml"], tmp_path,
                     TRADER_DATA_ROOT="/elsewhere", TRADER_CODE_ROOT="/elsewhere")
    assert r.returncode == 0, r.stderr
    assert out["PROG"] == prog and out["ARGS"] == "validate --file a b.yaml"
    assert {k: out[k] for k in SPLIT_ENV} == SPLIT_ENV   # fixed paths win over the caller's


def test_trader_test_puts_the_clone_first_and_drops_the_deployed_paths(tmp_path):
    """The unit's deployed paths, TRADER_DATA_ROOT included, are dropped: a public-code clone's
    conftest then uses its own data template. TRADER_TEST_DATA_ROOT is the opt-in to real data."""
    home, clone = tmp_path / "home", tmp_path / "clone"
    fake_venv(home, "python")
    (clone / "src" / "trader").mkdir(parents=True)
    r, out = run_env([str(ROOT / "scripts" / "trader-test"), "-q", "tests/test_engine.py"], home, cwd=clone,
                     PYTHONPATH="/x", TRADER_TEST_DATA_ROOT="/d", **SPLIT_ENV,
                     TRADER_STRATEGIST_STAMP="/s", TRADER_CONFIG="/c")
    assert r.returncode == 0, r.stderr
    assert out["PROG"] == "python" and out["ARGS"] == "-m pytest -q tests/test_engine.py"
    assert out["PYTHONPATH"] == f"{clone}/src:/x"
    assert out["TRADER_TEST_DATA_ROOT"] == "/d"
    for gone in ("TRADER_CODE_ROOT", "TRADER_DATA_ROOT", "TRADER_STRATEGIST_ROOT", "TRADER_RUNTIME", "TRADER_REPLAY_DIR",
                 "TRADER_SECRETS", "TRADER_STRATEGIST_STAMP", "TRADER_CONFIG"):
        assert gone not in out
    assert r.stderr == ""


def test_trader_test_outside_a_clone_warns_and_leaves_pythonpath(tmp_path):
    home = tmp_path / "home"
    fake_venv(home, "python")
    r, out = run_env([str(ROOT / "scripts" / "trader-test")], home, cwd=tmp_path)
    assert r.returncode == 0 and "PYTHONPATH" not in out and "DEPLOYED trader" in r.stderr


def test_trader_test_import_order_really_prefers_the_clone(tmp_path):
    """With a real interpreter: a clone's src/trader shadows the installed one."""
    home, clone = tmp_path / "home", tmp_path / "clone"
    bin_ = home / ".local" / "share" / "trader" / "venv" / "bin"
    bin_.mkdir(parents=True)
    (bin_ / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_ / "python").chmod(0o755)
    (clone / "src" / "trader").mkdir(parents=True)
    (clone / "src" / "trader" / "__init__.py").write_text("")
    (clone / "test_which.py").write_text(
        "import trader\n\ndef test_which():\n    assert trader.__file__.startswith(%r)\n" % str(clone))
    r, _ = run_env([str(ROOT / "scripts" / "trader-test"), "-q", "-p", "no:cacheprovider", "test_which.py"], home, cwd=clone)
    assert r.returncode == 0, r.stdout + r.stderr


# ---- 0-data.sh ---------------------------------------------------------------------------

FAKE_GH = '#!/bin/sh\necho "$*" >> "$GH_LOG"\n'
# `uv run -q trader ARGS` -> this tree's real CLI, with this tree's templates.
FAKE_UV = f"""#!/bin/sh
[ "$1" = run ] || exit 9
shift; [ "$1" = -q ] && shift; [ "$1" = trader ] || exit 9; shift
TRADER_CODE_ROOT={ROOT} exec {sys.executable} -m trader.cli "$@"
"""


def template_config():
    """The data template's config.yaml: templates/data/main's when this tree has it, else this
    tree's config.yaml with the starter's placeholders (as sync-starter.sh writes them)."""
    tpl = ROOT / "templates" / "data" / "main" / "config.yaml"
    if tpl.exists():
        return tpl.read_text()
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["owner"].update(name="Your Name", github_repo="your-github-user/your-data-repo")
    raw["dashboard"]["users"] = []
    raw["experiment"]["start_date"] = "2099-01-05"
    raw["schedule"]["runner_start"] = str(raw["schedule"]["runner_start"])
    return "# TEMPLATE: set owner, dashboard.users and experiment.start_date\n" + yaml.safe_dump(raw, sort_keys=False)


@pytest.fixture
def data_env(tmp_path):
    code = tmp_path / "code"
    (code / "deploy" / "setup").mkdir(parents=True)
    shutil.copy(SETUP / "0-data.sh", code / "deploy" / "setup")
    main = code / "templates" / "data" / "main"
    (main / "config").mkdir(parents=True)
    (main / "config.yaml").write_text(template_config())
    (main / "config" / "mode.yaml").write_text("mode: paper\n")
    (main / "README.md").write_text("data\n")
    strat = code / "templates" / "data" / "strategist"
    for d in ("state", "journal/daily", "logs", "features/custom", "proposals"):
        (strat / d).mkdir(parents=True)
    (strat / "CLAUDE.md.template").write_text("stub: the charter is `/srv/trading/main/CLAUDE.md`\n")
    (strat / ".gitignore").write_text(".env\nreplays/\n")
    (strat / "state" / "strategy.md").write_text("# Strategy\n")
    for d in ("journal/daily", "logs", "proposals"):
        (strat / d / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "main", str(code)], check=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("gh", FAKE_GH), ("uv", FAKE_UV)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}", "HOME": str(tmp_path), "GH_LOG": str(tmp_path / "gh.log"),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "o", "GIT_AUTHOR_EMAIL": "o@o", "GIT_COMMITTER_NAME": "o", "GIT_COMMITTER_EMAIL": "o@o"}

    def run(*args):
        return subprocess.run(["bash", str(code / "deploy" / "setup" / "0-data.sh"), *args, "--remote", str(remote)],
                              capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return run, remote, tmp_path


def git_out(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def heads(remote):
    return git_out(remote, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")


FULL = ["you/your-data", "--name", "Ada Lovelace", "--start-date", "2026-11-02", "--users", "ada@example.com, b@example.com"]


@needs_git
def test_0_data_builds_both_branches_and_the_labels(data_env):
    run, remote, tmp = data_env
    r = run(*FULL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert heads(remote).split()[::2] == ["main", "strategist"]
    # main: the owner's config, rendered files, nothing of the strategist's
    files = set(git_out(remote, "ls-tree", "-r", "--name-only", "main").split())
    assert {"config.yaml", "config/mode.yaml", "README.md", "deploy/systemd/trader.env", "deploy/systemd/trader-runner.timer",
            "deploy/systemd-trader/trader-strategist-weekly.timer"} <= files
    assert not any(f.startswith(("state/", "journal/", "CLAUDE.md")) for f in files)
    cfg_text = git_out(remote, "show", "main:config.yaml")
    assert "# TEMPLATE:" not in cfg_text
    cfg_file = tmp / "check.yaml"
    cfg_file.write_text(cfg_text)
    s = config.load_settings(cfg_file)
    assert (s.owner.name, s.owner.github_repo, s.dashboard.users, str(s.experiment.start_date)) == \
        ("Ada Lovelace", "you/your-data", ["ada@example.com", "b@example.com"], "2026-11-02")
    assert "TRADER_DASHBOARD_USERS" not in git_out(remote, "show", "main:deploy/systemd/trader.env")  # config.yaml alone
    # strategist: an orphan holding the template, no config
    sfiles = set(git_out(remote, "ls-tree", "-r", "--name-only", "strategist").split())
    assert {"CLAUDE.md", ".gitignore", "state/strategy.md", "journal/daily/.gitkeep", "proposals/.gitkeep"} <= sfiles
    assert "config.yaml" not in sfiles
    assert "CLAUDE.md.template" not in sfiles
    assert git_out(remote, "show", "strategist:CLAUDE.md").startswith("stub:")
    assert subprocess.run(["git", "-C", str(remote), "merge-base", "main", "strategist"], capture_output=True).returncode != 0
    gh = (tmp / "gh.log").read_text()
    assert "label create needs-human -R you/your-data --force" in gh and "label create weekly -R you/your-data --force" in gh
    assert "api -X PATCH repos/you/your-data -f default_branch=main" in gh


@needs_git
def test_0_data_rerun_touches_nothing_but_the_labels(data_env):
    run, remote, tmp = data_env
    assert run(*FULL).returncode == 0
    before = heads(remote)
    (tmp / "gh.log").unlink()
    r = run("you/your-data")   # no values needed: nothing is built
    assert r.returncode == 0, r.stderr
    assert heads(remote) == before and "already has main and strategist" in r.stdout
    gh = (tmp / "gh.log").read_text()
    assert gh.count("label create") == 2 and "api -X PATCH repos/you/your-data -f default_branch=main" in gh


@needs_git
def test_0_data_refuses_a_repo_with_anything_else(data_env, tmp_path):
    run, remote, tmp = data_env
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("hi\n")
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "o", "GIT_AUTHOR_EMAIL": "o@o", "GIT_COMMITTER_NAME": "o", "GIT_COMMITTER_EMAIL": "o@o"}
    for args in (["add", "-A"], ["commit", "-qm", "init"], ["push", "-q", str(remote), "main"]):
        subprocess.run(["git", "-C", str(seed), *args], check=True, env=git_env, capture_output=True)
    before = heads(remote)
    r = run(*FULL)
    assert r.returncode != 0 and "not empty" in r.stderr
    assert heads(remote) == before and not (tmp / "gh.log").exists()


@needs_git
@pytest.mark.parametrize("args, why", [
    (["you/your-data", "--start-date", "2026-11-02"], "Your name"),
    (["you/your-data", "--name", "Ada"], "start date"),
    (["you/your-data", "--name", "Ada", "--start-date", "2/11/2026"], "YYYY-MM-DD"),
    (["you/your-data", "--name", "A|da", "--start-date", "2026-11-02"], "name"),
    (["you/your-data", "--name", "Ada", "--start-date", "2026-11-02", "--users", "a b'c"], "users"),
    (["not-a-slug", "--name", "Ada", "--start-date", "2026-11-02"], "usage"),
])
def test_0_data_refuses_bad_or_missing_values_before_pushing(data_env, args, why):
    run, remote, tmp = data_env
    r = run(*args)
    assert r.returncode != 0 and why.lower() in r.stderr.lower()
    assert heads(remote) == "" and not (tmp / "gh.log").exists()


@needs_git
def test_0_data_pushes_nothing_when_the_config_does_not_load(data_env):
    run, remote, tmp = data_env
    r = run("you/your-data", "--name", "Ada", "--start-date", "2026-02-30")   # passes the pattern, not the loader
    assert r.returncode != 0 and "render-deploy failed" in r.stderr
    assert heads(remote) == "" and not (tmp / "gh.log").exists()


def test_host_step_names_the_next_step_for_both_layouts():
    # A fresh split install goes 1-host -> 3-runner key -> 3-runner install --split -> 2-strategist (#169
    # section 15); a monorepo install goes on to 2-strategist. 1-host can't tell which yet, so it names both.
    text = (SETUP / "1-host.sh").read_text()
    assert '3-runner.sh key"' in text and "split layout" in text
    assert "monorepo layout: as trader, bash deploy/setup/2-strategist.sh" in text


# ---- the push probe (#186) ---------------------------------------------------------------

@pytest.mark.parametrize("url, canonical", [
    ("https://github.com/gilesknap/jev-trader.git", "https://github.com/gilesknap/jev-trader.git"),
    ("https://github.com/gilesknap/jev-trader", "https://github.com/gilesknap/jev-trader.git"),
    ("https://github.com/someone/fork/", "https://github.com/someone/fork.git"),
    ("git@github-trading:gilesknap/trading", None),
    ("git@github.com:gilesknap/trading.git", None),
    ("ssh://git@github.com/gilesknap/trading.git", None),
    ("http://github.com/gilesknap/trading.git", None),
    ("https://gitlab.com/gilesknap/trading.git", None),
    ("https://github.com.evil.example/a/b.git", None),
    ("https://user:tok@github.com/a/b.git", None),
    ("https://github.com/a/b/c.git", None),
    ("https://github.com/a/b.git?x=1", None),
    ("https://github.com/../b.git", None),
    ("https://github.com/a/.git", None),
    ("https://github.com/a/b.git\nhttps://github.com/c/d.git", None),
    ("", None),
])
def test_github_https_url_accepts_only_https_github(url, canonical):
    r = sh('github_https_url "$1"', url)
    assert (r.stdout.strip() or None) == canonical and (r.returncode == 0) is (canonical is not None)


FAKE_GIT = """#!/bin/sh
echo "$* PROMPT=$GIT_TERMINAL_PROMPT" >> "$GIT_LOG"
case "$*" in *" push "*) exit "$PUSH_RC" ;; esac
exec "$REAL_GIT" "$@"
"""


@pytest.fixture
def probe_env(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(FAKE_GIT)
    (bin_ / "git").chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "GIT_LOG": str(tmp_path / "git.log"),
           "REAL_GIT": shutil.which("git"), "TMPDIR": str(scratch), "GIT_CONFIG_GLOBAL": "/dev/null"}

    def probe(url, push_rc):
        r = sh('push_probe "$1"', url, env={**env, "PUSH_RC": str(push_rc)})
        log = (tmp_path / "git.log").read_text() if (tmp_path / "git.log").exists() else ""
        return r, log, list(scratch.iterdir())
    return probe


@needs_git
@pytest.mark.parametrize("push_rc, rc", [(0, 0), (128, 1), (1, 1)])
def test_push_probe_is_a_dry_run_from_a_throwaway_repo(probe_env, push_rc, rc):
    r, log, left = probe_env("https://github.com/gilesknap/jev-trader", push_rc)
    assert r.returncode == rc, r.stderr
    (push,) = [ln for ln in log.splitlines() if " push " in ln]
    assert "push --dry-run --quiet https://github.com/gilesknap/jev-trader.git HEAD:refs/heads/probe" in push
    assert "core.hooksPath=/dev/null" in push and push.endswith("PROMPT=0")
    assert left == []   # the throwaway repo is gone


@needs_git
@pytest.mark.parametrize("url", ["git@github-trading:gilesknap/trading", "http://github.com/a/b.git",
                                 "https://example.com/a/b.git", ""])
def test_push_probe_refuses_anything_but_https_github(probe_env, url):
    r, log, left = probe_env(url, 0)
    assert r.returncode == 2 and "not an https GitHub URL" in r.stderr
    assert log == "" and left == []   # git never ran


@needs_git
def test_push_probe_reports_not_probed_when_it_cannot_make_the_repo(probe_env, tmp_path):
    (tmp_path / "scratch").chmod(0o500)   # mktemp -d fails
    try:
        r, log, _ = probe_env("https://github.com/a/b.git", 0)
    finally:
        (tmp_path / "scratch").chmod(0o700)
    assert r.returncode == 2 and " push " not in log


@needs_git
@pytest.mark.parametrize("push_rc, rc", [(0, 0), (1, 1), (124, 2)])
def test_run_push_probe_passes_the_probe_result_through(probe_env, tmp_path, push_rc, rc):
    probe_env("https://github.com/a/b.git", 0)   # builds the fake git and its env
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "GIT_LOG": str(tmp_path / "git.log"),
           "REAL_GIT": shutil.which("git"), "TMPDIR": str(tmp_path / "scratch"), "GIT_CONFIG_GLOBAL": "/dev/null",
           "PUSH_RC": str(push_rc)}
    r = sh('run_push_probe "$1" "$2" env', LIB, "https://github.com/a/b.git", env=env)
    assert r.returncode == rc, r.stderr


@pytest.mark.parametrize("runner", ["", "false", "sh -c 'exit 1'"])
def test_run_push_probe_never_reports_cant_push_when_nothing_ran(tmp_path, runner):
    """An unreadable lib, or a runner (sudo) that fails with 1, is 'not probed' (2), never 1."""
    missing = tmp_path / "absent.sh"
    r = sh(f'run_push_probe "$1" "$2" {runner}', missing if not runner else LIB, "https://github.com/a/b.git")
    assert r.returncode == 2


def test_run_push_probe_with_an_unreadable_lib_is_not_probed(tmp_path):
    lib = tmp_path / "lib.sh"
    lib.write_text("echo x\n")
    lib.chmod(0)
    if os.access(lib, os.R_OK):
        pytest.skip("running as root")
    assert sh('run_push_probe "$1" "$2"', lib, "https://github.com/a/b.git").returncode == 2
