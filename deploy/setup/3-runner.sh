#!/usr/bin/env bash
# Step 3 (as admin):  sudo bash deploy/setup/3-runner.sh key      -> prints a deploy key to add on GitHub (read-only)
#                     sudo bash deploy/setup/3-runner.sh install  -> checks out main, installs services + dashboard
# Idempotent: safe to re-run either step.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
. "$(dirname "$0")/cfg.sh"
GH_REPO=$(cfg owner github_repo)
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
    if [[ ! -f $R/.config/trading/env ]]; then
        # Start from the strategist's paper-only secrets; add ALPACA_LIVE_* here later.
        install -o runner -g runner -m 600 /srv/trading/strategist/.env $R/.config/trading/env
    fi
    [[ -d /srv/trading/main/.git ]] || as_runner bash -c "umask 027 && git clone -q $REPO /srv/trading/main"
    as_runner bash -c 'cd /srv/trading/main && git pull -q --ff-only && uv sync -q --frozen --extra dev && uv run pytest -q tests'
    install -m 755 /srv/trading/main/deploy/trading-deploy /usr/local/bin/trading-deploy
    [[ -f $R/.config/trading/services.env ]] || as_runner cp /srv/trading/main/deploy/systemd/trader.env $R/.config/trading/services.env
    as_runner bash -c 'cp /srv/trading/main/deploy/systemd/*.service /srv/trading/main/deploy/systemd/*.timer ~/.config/systemd/user/'
    as_runner systemctl --user daemon-reload
    as_runner systemctl --user enable trader-dashboard.service trader-dashboard-ssh.service trader-runner.timer trader-watchdog.timer
    as_runner systemctl --user restart trader-dashboard.service trader-dashboard-ssh.service
    as_runner systemctl --user start trader-runner.timer trader-watchdog.timer
    tailscale serve --https=$PORT off 2>/dev/null || true
    tailscale serve --bg --https=$PORT unix:/run/user/$RUID/trader-dashboard.sock
    echo "runner ready. Dashboard: https://$(tailscale status --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):$PORT"
    echo "Verify everything: sudo bash /srv/trading/main/deploy/setup/check.sh"
    ;;
*) echo "usage: sudo bash $0 key|install" >&2; exit 1 ;;
esac
