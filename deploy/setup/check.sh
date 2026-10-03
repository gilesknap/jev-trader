#!/usr/bin/env bash
# Verify the whole installation (as admin):  sudo bash /srv/trading/main/deploy/setup/check.sh
# Read-only apart from creating/removing a temp file to prove write permissions are denied.
# The split layout's checks (#169) run only when /srv/trading/config is a checkout of the data
# repo's main; without it every check is the monorepo's.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
# shellcheck source=deploy/setup/lib.sh
. "$(dirname "$0")/lib.sh"
SPLIT=""; split_layout && SPLIT=1
C=$CONFIG_CHECKOUT
fails=0
ok()   { printf '  \e[32mPASS\e[0m %s\n' "$1"; }
bad()  { printf '  \e[31mFAIL\e[0m %s\n' "$1"; fails=$((fails + 1)); }
chk()  { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
nchk() { if eval "$2" >/dev/null 2>&1; then bad "$1"; else ok "$1"; fi; }
RUID=$(id -u runner 2>/dev/null || echo 0)
as_runner() { sudo -u runner -H env XDG_RUNTIME_DIR=/run/user/$RUID "$@"; }
as_trader() { sudo -u trader -H "$@"; }
TUID=$(id -u trader 2>/dev/null || echo 0)
as_trader_sd() { sudo -u trader -H env XDG_RUNTIME_DIR=/run/user/$TUID "$@"; }

echo "Users and groups"
chk  "runner exists and is in trading"            "id -nG runner | grep -qw trading"
chk  "trader is in trading"                       "id -nG trader | grep -qw trading"
nchk "runner has no sudo"                         "sudo -l -U runner | grep -q 'may run'"
nchk "trader has no sudo"                         "sudo -l -U trader | grep -q 'may run'"
chk  "runner lingers (services run without login)" "loginctl show-user runner -p Linger | grep -q yes"

echo "Directories"
chk  "/srv/trading is root:trading 750"           "[[ \$(stat -c %U:%G:%a /srv/trading) == root:trading:750 ]]"
chk  "strategist checkout owned by trader"        "[[ \$(stat -c %U /srv/trading/strategist) == trader ]]"
chk  "main checkout owned by runner"              "[[ \$(stat -c %U /srv/trading/main) == runner ]]"
nchk "trader cannot write main"                   "as_trader touch /srv/trading/main/.w"
nchk "trader cannot write runtime"                "as_trader touch /srv/trading/runtime/.w"
chk  "runner can read strategist state"           "as_runner cat /srv/trading/strategist/state/classifiers.yaml"
# the strategist logs its alerts here (it can't write runtime/); runner and the human read it via group trading
chk  "runner can read strategist alerts (if any)" "[[ ! -e /srv/trading/strategist/strategist-alerts.log ]] || as_runner cat /srv/trading/strategist/strategist-alerts.log >/dev/null"
# runner is in trading, so a group-writable strategist tree would let it plant code trader runs
nchk "runner cannot write strategist"             "as_runner touch /srv/trading/strategist/.w || as_runner touch /srv/trading/strategist/state/.w"
nchk "strategist tree has no group write"         "find /srv/trading/strategist ! -type l -perm -g=w | grep -q ."
nchk "strategist git is not group-shared"         "git -C /srv/trading/strategist -c safe.directory='*' config core.sharedRepository"
rm -f /srv/trading/main/.w /srv/trading/runtime/.w /srv/trading/strategist/.w /srv/trading/strategist/state/.w

echo "Secrets"
chk  "strategist .env is 600"                     "[[ \$(stat -c %a /srv/trading/strategist/.env) == 600 ]]"
nchk "strategist .env holds no live keys"         "grep -q '^ALPACA_LIVE_' /srv/trading/strategist/.env"
chk  "runner env is 600 runner-owned"             "[[ \$(stat -c %U:%a /home/runner/.config/trading/env) == runner:600 ]]"
nchk "trader cannot read runner env"              "as_trader cat /home/runner/.config/trading/env"
nchk "runner env has no duplicated keys"          "grep -o '^[A-Z_]*=' /home/runner/.config/trading/env | sort | uniq -d | grep -q ."

echo "Services"
chk  "dashboard service active"                   "as_runner systemctl --user is-active trader-dashboard.service"
chk  "runner timer active"                        "as_runner systemctl --user is-active trader-runner.timer"
chk  "watchdog timer active"                      "as_runner systemctl --user is-active trader-watchdog.timer"
SOCK=/run/user/$RUID/trader-dashboard.sock
for _ in $(seq 15); do [[ -S $SOCK ]] && break; sleep 1; done   # a just-restarted dashboard takes a few seconds
chk  "dashboard socket exists"                    "[[ -S $SOCK ]]"
chk  "runner can reach dashboard socket"          "as_runner curl -s --max-time 3 --unix-socket $SOCK -o /dev/null http://x/api/health"
nchk "nothing listens on TCP 8321"                "ss -ltn | grep -q ':8321 '"
nchk "trader cannot reach dashboard socket"       "as_trader curl -s --max-time 3 --unix-socket $SOCK http://x/api/health"
chk  "tailscale serve -> dashboard socket"            "tailscale serve status | grep -q trader-dashboard.sock"
# tailscale serve sends no identity header for tagged devices (this VPS itself included), so
# the served dashboard must never trust a request without one.
# Behavioural, so ALLOW_LOCAL set anywhere (a drop-in, services.env) is caught: no header must mean 403.
nchk "served dashboard refuses a request with no Tailscale header" "as_runner curl -sf --max-time 3 --unix-socket $SOCK -o /dev/null http://x/api/health"
if [[ -d /srv/trading-dashview ]]; then
    DV=/srv/trading-dashview/dashboard.sock
    for _ in $(seq 15); do [[ -S $DV ]] && break; sleep 1; done
    chk  "SSH dashboard dir is runner:dashview 750"   "[[ \$(stat -c %U:%G:%a /srv/trading-dashview) == runner:dashview:750 ]]"
    chk  "SSH dashboard answers on its socket"        "as_runner curl -s --max-time 3 --unix-socket $DV -o /dev/null http://x/api/health"
    nchk "trader cannot reach the SSH dashboard"      "as_trader curl -s --max-time 3 --unix-socket $DV http://x/api/health"
    nchk "trader is not in dashview"                  "id -nG trader | grep -qw dashview"
fi
chk  "trading-deploy installed"                   "[[ -x /usr/local/bin/trading-deploy ]]"
nchk "no sudoers rule lets trader deploy"         "sudo -l -U trader | grep -q trading-deploy"

echo "Feature sandbox"
chk  "bubblewrap works for runner (no network inside)" "as_runner bwrap --ro-bind / / --unshare-all --die-with-parent true"
nchk "sandbox hides runner secrets"               "as_runner bwrap --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/bin /bin --unshare-all --die-with-parent /usr/bin/cat /home/runner/.config/trading/env"

echo "Strategist"
chk  "trader lingers (strategist timers run without login)" "loginctl show-user trader -p Linger | grep -q yes"
TIMERS=/srv/trading/main/deploy/systemd-trader; TFROM=main
[[ -n $SPLIT ]] && { TIMERS=$C/deploy/systemd-trader; TFROM=config; }   # rendered into the data repo
for k in premarket postclose weekly housekeeping; do
    chk  "strategist $k timer active"               "as_trader_sd systemctl --user is-active trader-strategist-$k.timer"
    chk  "strategist $k timer matches $TFROM"       "diff -q /home/trader/.config/systemd/user/trader-strategist-$k.timer $TIMERS/trader-strategist-$k.timer"
done
chk  "strategist service unit matches main"       "diff -q /home/trader/.config/systemd/user/trader-strategist@.service /srv/trading/main/deploy/systemd-trader/trader-strategist@.service"
nchk "no leftover strategist crontab"             "crontab -l -u trader 2>/dev/null | grep -q strategist.sh"
chk  "claude CLI available to trader"             "as_trader bash -lc 'command -v claude || [[ -x ~/.local/bin/claude ]]'"
chk  "strategist on branch strategist"            "[[ \$(git -C /srv/trading/strategist -c safe.directory='*' branch --show-current) == strategist ]]"
# An OAuth login is the operator's own identity: it reaches every repo they can, and bypasses
# the public repo's ruleset. The strategist's token must be a fine-grained PAT scoped to the data
# repo (#169 section 14 item 8). Only the prefix is tested; the token is never printed.
chk  "trader's gh token is a fine-grained PAT"   "as_trader gh auth token 2>/dev/null | grep -q '^github_pat_'"

if [[ -n $SPLIT ]]; then
    echo "Split layout (#169)"
    chk  "config checkout is runner:trading"          "[[ \$(stat -c %U:%G $C) == runner:trading ]]"
    nchk "trader cannot write config"                 "as_trader touch $C/.w || as_trader touch $C/config/.w"
    nchk "config tree has no group or other write"    "find $C ! -type l -perm /g=w,o=w | grep -q ."
    rm -f "$C/.w" "$C/config/.w"
    chk  "config checkout holds config/mode.yaml"     "[[ -f $C/config/mode.yaml ]]"
    chk  "trader's venv imports trader from main/src" "as_trader /home/trader/.local/share/trader/venv/bin/python -I -c 'import sys, trader; sys.exit(not trader.__file__.startswith(\"/srv/trading/main/src/\"))'"
    chk  "trader shim and helpers installed"          "as_trader bash -c '[[ -x ~/.local/bin/trader && -x ~/.local/bin/trader-python && -x ~/.local/bin/trader-test ]]'"
    chk  "strategist unit runs from main"             "grep -qE '^ExecStart=/srv/trading/main/' /home/trader/.config/systemd/user/trader-strategist@.service"
    chk  "trader's Claude settings deny GitHub WebFetch" "python3 -c 'import json, sys; sys.exit(\"WebFetch(domain:github.com)\" not in json.load(open(sys.argv[1]))[\"permissions\"][\"deny\"])' /home/trader/.claude/settings.json"
    nchk "runner cannot read trader's ~/.claude"      "as_runner ls /home/trader/.claude"
    chk  "runner services.env names the config checkout" "grep -qx 'TRADER_DATA_ROOT=$C' /home/runner/.config/trading/services.env"
    nchk "trader's ~/.claude/CLAUDE.md imports no CLAUDE.md" "claude_md_imports_charter /home/trader/.claude/CLAUDE.md"
    # Claude Code loads CLAUDE.md from parent directories too; /srv/trading is root-owned.
    nchk "no /srv/trading/CLAUDE.md"                  "[[ -e /srv/trading/CLAUDE.md ]]"
    chk  "runner can reach the strategist run lock"   "as_runner test -x /home/trader/.local/state/trader"
    L=/home/trader/.local/state/trader/strategist.lock
    chk  "runner can read the lock (if any)"          "[[ ! -e $L ]] || as_runner test -r $L"
fi

if [[ -n $SPLIT ]]; then
    echo "Mode: $(grep -E '^mode:' "$C/config/mode.yaml" 2>/dev/null || echo unknown)"
    echo "Deployed: code $(git -C /srv/trading/main -c safe.directory='*' log --oneline -1 2>/dev/null)"
    echo "          config $(git -C "$C" -c safe.directory='*' log --oneline -1 2>/dev/null)"
else
    echo "Mode: $(grep -E '^mode:' /srv/trading/main/config/mode.yaml 2>/dev/null || echo unknown)"
    echo "Deployed: $(git -C /srv/trading/main -c safe.directory='*' log --oneline -1 2>/dev/null)"
fi
if (( fails )); then echo "$fails check(s) FAILED"; exit 1; else echo "all checks passed"; fi
