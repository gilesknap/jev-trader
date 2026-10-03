#!/usr/bin/env bash
# Step 2 (as trader, from a fresh login so the trading group applies):  bash deploy/setup/2-strategist.sh
# Checks out the strategist branch at /srv/trading/strategist, creates .env (paper keys only),
# installs the strategist's systemd timers and points ~/trading at the checkout. Idempotent.
set -euo pipefail
. "$(dirname "$0")/cfg.sh"
REPO=https://github.com/$(cfg owner github_repo).git
S=/srv/trading/strategist
[[ "$(id -un)" == trader ]] || { echo "run as trader" >&2; exit 1; }
id -nG | grep -qw trading || { echo "this login lacks the trading group: log in again" >&2; exit 1; }

if [[ ! -d $S/.git ]]; then
    git clone -q "$REPO" "$S"
    cd "$S"
    if git ls-remote --exit-code --heads origin strategist >/dev/null; then
        git checkout -q strategist
    else
        git checkout -q -b strategist && git push -q -u origin strategist
    fi
fi
cd "$S"
mkdir -p replays
if [[ ! -f .env ]]; then
    install -m 600 /dev/null .env
    cat > .env <<ENV
# Strategist secrets: PAPER keys only. Never put live keys in this file.
ALPACA_PAPER_KEY=
ALPACA_PAPER_SECRET=
OPENROUTER_API_KEY=
# ntfy.sh push topic: subscribe to this exact name in the ntfy app
NTFY_TOPIC=trader-$(openssl rand -hex 12)
ENV
    echo "created $S/.env: fill in the keys"
fi
chmod 600 .env
uv sync -q --extra dev
# The schedule: systemd user timers (they honour config.yaml's local_tz; cron's CRON_TZ didn't, #152).
# Re-run this script after a schedule change in config.yaml to reinstall them.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p ~/.config/systemd/user
cp deploy/systemd-trader/trader-strategist@.service deploy/systemd-trader/trader-strategist-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trader-strategist-{premarket,postclose,weekly,housekeeping}.timer
systemctl --user restart trader-strategist-{premarket,postclose,weekly,housekeeping}.timer  # re-arm a changed OnCalendar
crontab -r 2>/dev/null || true   # an older install scheduled these from cron

if [[ -L ~/trading ]]; then :
elif [[ -e ~/trading ]]; then mv ~/trading ~/trading.pre-srv && ln -s "$S" ~/trading
else ln -s "$S" ~/trading; fi

echo "strategist ready (branch $(git branch --show-current), timers installed):"
systemctl --user list-timers 'trader-strategist-*' --no-pager
echo "Next, as admin: sudo bash $S/deploy/setup/3-runner.sh key"
