#!/usr/bin/env bash
# Step 3 (as admin):  sudo bash deploy/setup/3-runner.sh key      -> prints a deploy key to add on GitHub (read-only)
#                     sudo bash deploy/setup/3-runner.sh install  -> checks out main, installs services + dashboard
# Idempotent: safe to re-run either step.
#
# Split layout (#169):  sudo TRADER_DATA_ROOT=<a data checkout> bash deploy/setup/3-runner.sh key
#                       sudo TRADER_DATA_ROOT=<a data checkout> bash deploy/setup/3-runner.sh install --split [--code-repo OWNER/NAME]
#   clones the public code (default gilesknap/jev-trader) over https to /srv/trading/main and the data
#   repo's main (with the deploy key, on the data repo) to /srv/trading/config, tests the code against
#   that config, and installs the services and trader-watchdog.timer from the code and
#   trader-runner.timer from the config. TRADER_DATA_ROOT (where config.yaml is read from) is needed
#   only before /srv/trading/config exists; without it the code's placeholder config.yaml is read,
#   and its your-github-user/... slug is refused. Once that checkout exists, a re-run is in split mode
#   without the flag; without it and without --split, everything runs exactly as before the split.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
# Nothing written below may be group-writable: group trading holds both trader and runner. (The
# runner-side commands get at least 022 anyway: sudo adds its own umask to this one.)
umask 022
# shellcheck source=deploy/setup/lib.sh
. "$(dirname "$0")/lib.sh"
if split_layout; then export TRADER_DATA_ROOT="${TRADER_DATA_ROOT:-$CONFIG_CHECKOUT}"; fi
# shellcheck source=deploy/setup/cfg.sh
. "$(dirname "$0")/cfg.sh"
GH_REPO=$(cfg_repo)
PORT=$(cfg dashboard tailscale_port)
REPO=git@github-trading:$GH_REPO
R=/home/runner
RUID=$(id -u runner)
as_runner() { sudo -u runner -H env XDG_RUNTIME_DIR=/run/user/$RUID "$@"; }

case "${1:-}" in
key)
    as_runner mkdir -p $R/.ssh $R/.config/trading $R/.config/systemd/user
    chmod 700 $R/.ssh
    [[ -f $R/.ssh/trading_deploy ]] || as_runner ssh-keygen -q -t ed25519 -f $R/.ssh/trading_deploy -N '' -C 'runner trading deploy'
    if ! grep -qs github-trading $R/.ssh/config; then
        printf 'Host github-trading\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/trading_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' \
            | as_runner tee -a $R/.ssh/config >/dev/null
    fi
    chmod 600 $R/.ssh/config
    echo "Add this as a READ-ONLY deploy key at https://github.com/$GH_REPO/settings/keys :"
    cat $R/.ssh/trading_deploy.pub
    ;;
install)
    shift
    SPLIT=""; CODE_REPO=gilesknap/jev-trader
    while (( $# )); do
        case "$1" in
        --split) SPLIT=1 ;;
        --code-repo) CODE_REPO="${2:?--code-repo OWNER/NAME}"; SPLIT=1; shift ;;
        *) echo "usage: sudo bash $0 install [--split] [--code-repo OWNER/NAME]" >&2; exit 1 ;;
        esac
        shift
    done
    split_layout && SPLIT=1
    [[ "$CODE_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "--code-repo: expected OWNER/NAME" >&2; exit 1; }
    if [[ ! -f $R/.config/trading/env ]]; then
        if [[ -n $SPLIT && ! -f /srv/trading/strategist/.env ]]; then
            # Split installs run this step before 2-strategist.sh, which reads the config checkout made here.
            echo "no /srv/trading/strategist/.env yet: after 2-strategist.sh, re-run this to copy it to $R/.config/trading/env" >&2
        else
            # Start from the strategist's paper-only secrets; add ALPACA_LIVE_* here later.
            install -o runner -g runner -m 600 /srv/trading/strategist/.env $R/.config/trading/env
        fi
    fi
    if [[ -n $SPLIT ]]; then
        # The public code needs no key; the deploy key is for the private data repo.
        [[ -d /srv/trading/main/.git ]] || as_runner bash -c "umask 027 && git clone -q https://github.com/$CODE_REPO.git /srv/trading/main"
        [[ -d $CONFIG_CHECKOUT/.git ]] || as_runner bash -c "umask 027 && git clone -q -b main $REPO $CONFIG_CHECKOUT"
        as_runner bash -c "cd $CONFIG_CHECKOUT && git pull -q --ff-only"
        as_runner chmod -R g-w,o-rwx "$CONFIG_CHECKOUT"   # trader reads it; only runner writes it
        as_runner bash -c "cd /srv/trading/main && git pull -q --ff-only && uv sync -q --frozen --extra dev && TRADER_DATA_ROOT=$CONFIG_CHECKOUT uv run pytest -q tests"
        UNIT_FILES="/srv/trading/main/deploy/systemd/*.service /srv/trading/main/deploy/systemd/trader-watchdog.timer $CONFIG_CHECKOUT/deploy/systemd/trader-runner.timer"
        SERVICES_ENV=$CONFIG_CHECKOUT/deploy/systemd/trader.env
    else
        [[ -d /srv/trading/main/.git ]] || as_runner bash -c "umask 027 && git clone -q $REPO /srv/trading/main"
        as_runner bash -c 'cd /srv/trading/main && git pull -q --ff-only && uv sync -q --frozen --extra dev && uv run pytest -q tests'
        UNIT_FILES="/srv/trading/main/deploy/systemd/*.service /srv/trading/main/deploy/systemd/*.timer"
        SERVICES_ENV=/srv/trading/main/deploy/systemd/trader.env
    fi
    install -m 755 /srv/trading/main/deploy/trading-deploy /usr/local/bin/trading-deploy
    [[ -f $R/.config/trading/services.env ]] || as_runner cp "$SERVICES_ENV" $R/.config/trading/services.env
    # services.env is never overwritten, so one from before the split lacks the data root.
    if [[ -n $SPLIT ]] && ! grep -qx "TRADER_DATA_ROOT=$CONFIG_CHECKOUT" $R/.config/trading/services.env; then
        echo "WARNING: $R/.config/trading/services.env has no TRADER_DATA_ROOT=$CONFIG_CHECKOUT line; add it" \
             "(compare $SERVICES_ENV), or the runner reads config from the code checkout" >&2
    fi
    as_runner bash -c "cp $UNIT_FILES ~/.config/systemd/user/"
    as_runner systemctl --user daemon-reload
    as_runner systemctl --user enable trader-dashboard.service trader-dashboard-ssh.service trader-runner.timer trader-watchdog.timer
    as_runner systemctl --user restart trader-dashboard.service trader-dashboard-ssh.service
    as_runner systemctl --user start trader-runner.timer trader-watchdog.timer
    tailscale serve --https=$PORT off 2>/dev/null || true
    tailscale serve --bg --https=$PORT unix:/run/user/$RUID/trader-dashboard.sock
    echo "runner ready. Dashboard: https://$(tailscale status --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):$PORT"
    [[ -n $SPLIT ]] && echo "Next, as trader (if not done yet): bash /srv/trading/main/deploy/setup/2-strategist.sh"
    echo "Verify everything: sudo bash /srv/trading/main/deploy/setup/check.sh"
    ;;
*) echo "usage: sudo bash $0 key|install" >&2; exit 1 ;;
esac
