# shellcheck shell=bash
# Sourced by the setup scripts: helpers for the split layout (#169), where the code is a public
# repo checked out at /srv/trading/main and the human-owned config is a checkout of the private
# data repo's main at /srv/trading/config. Every helper takes its paths as arguments, so
# tests/test_setup_scripts.py can run each one against a fixture. Nothing here acts on its own.

# The data repo's main, checked out by 3-runner.sh. The split layout is keyed on this checkout
# existing, not on the bare directory: 1-host.sh creates the (empty) directory on every host, so
# re-running it on a monorepo host must not switch anything into split mode.
CONFIG_CHECKOUT=/srv/trading/config

# split_layout [DIR]: true when DIR (default CONFIG_CHECKOUT) is a checkout of data main.
split_layout() { [[ -e "${1:-$CONFIG_CHECKOUT}/.git" ]]; }

# unit_needs_missing_config UNIT [DIR]: true (so the caller refuses to install UNIT) when the unit
# names DIR but DIR holds no config.yaml. A unit with TRADER_DATA_ROOT=/srv/trading/config on a
# host without that checkout would make every trader command fail (#169 section 14 item 3).
unit_needs_missing_config() {
    local dir="${2:-$CONFIG_CHECKOUT}"
    grep -qF -- "$dir" "$1" && [[ ! -f "$dir/config.yaml" ]]
}

# claude_md_imports_charter FILE: true when FILE (trader's user-scope ~/.claude/CLAUDE.md) has an
# @-import of a CLAUDE.md. In split mode the wrapper passes the charter with
# --append-system-prompt-file, so an import as well would load it twice, and an import of a stale
# copy would contradict it (#169 section 14 item 10). A path in backticks isn't an import.
claude_md_imports_charter() {
    [[ -f "$1" ]] && grep -qE '(^|[[:space:]])@[^[:space:]`]*CLAUDE\.md' "$1"
}

# claude_deny_merge FILE RULE...: add each RULE to permissions.deny in the Claude Code settings
# FILE, keeping every other setting (defaultMode, the status line, ...) and any deny rules already
# there. Creates FILE (mode 600) if it's missing. Refuses, changing nothing, when FILE isn't a JSON
# object, or its permissions or permissions.deny has the wrong type. Prints what it added.
claude_deny_merge() {
    python3 - "$@" <<'PY'
import json, os, sys, tempfile
path, rules = sys.argv[1], sys.argv[2:]
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except (OSError, ValueError) as e:
    sys.exit(f"{path}: can't read it as JSON ({e}); fix it by hand, nothing changed")
if not isinstance(data, dict):
    sys.exit(f"{path}: not a JSON object; nothing changed")
perms = data.setdefault("permissions", {})
if not isinstance(perms, dict) or not isinstance(perms.setdefault("deny", []), list):
    sys.exit(f"{path}: permissions or permissions.deny has the wrong type; nothing changed")
added = [r for r in dict.fromkeys(rules) if r not in perms["deny"]]
if not added:
    sys.exit(0)
perms["deny"].extend(added)
mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".settings.")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp)
    raise
for r in added:
    print(f"deny rule added: {r}")
PY
}

# strategist_deny_rules CODE_REPO_NAME [DATA_REPO_NAME]: the Claude Code deny rules for the strategist in split mode
# (#169 section 6.6 and section 14 item 9), one per line. The public code repo's issues and PRs
# are attacker-writable prose, so the named routes to GitHub's public content are closed: WebFetch
# of GitHub, curl and wget against it, and gh naming the code repo (jev-trader, and the name of
# the repo this host deploys from, if that's a fork). gh against the data repo stays allowed: the
# strategist needs it, so a gh rule whose name DATA_REPO_NAME contains (e.g. a data repo called
# jev-trader-data) is left out, with a warning on stderr. Pattern rules are porous (python's urllib still works), so the charter
# carries the rule too; these close the obvious routes.
strategist_deny_rules() {
    local name host data="${2:-}"
    for host in github.com api.github.com raw.githubusercontent.com gist.github.com codeload.github.com; do
        echo "WebFetch(domain:$host)"
    done
    for host in github.com githubusercontent.com; do
        echo "Bash(curl *$host*)"
        echo "Bash(wget *$host*)"
    done
    for name in jev-trader ${1:+"$1"}; do
        if [[ -n $data && $data == *"$name"* ]]; then
            echo "WARNING: no 'Bash(gh *$name*)' deny rule: it would block gh on the data repo $data" >&2
            continue
        fi
        echo "Bash(gh *$name*)"
    done | sort -u
}

# repo_name URL: the repository name from a git remote URL (https or scp-style), without .git.
repo_name() {
    local n="${1%/}"
    n="${n##*/}"; n="${n##*:}"
    printf '%s\n' "${n%.git}"
}
