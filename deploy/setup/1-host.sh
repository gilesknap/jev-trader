#!/usr/bin/env bash
# Step 1 (as an admin, with sudo):  sudo bash deploy/setup/1-host.sh
# Creates the `trading` group, the no-sudo `runner` user and /srv/trading. Idempotent.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
id trader >/dev/null || { echo "user trader must exist first" >&2; exit 1; }

getent group trading >/dev/null || groupadd trading
id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
usermod -aG trading trader
usermod -aG trading runner
loginctl enable-linger runner
loginctl enable-linger trader   # the strategist's systemd timers run without a login

install -d -o root   -g trading -m 0750 /srv/trading
install -d -o trader -g trading -m 2750 /srv/trading/strategist
install -d -o runner -g trading -m 2750 /srv/trading/main
install -d -o runner -g trading -m 2750 /srv/trading/runtime
# The split layout's (#169) checkout of the private data repo's main, cloned by 3-runner.sh. Empty
# and unused on a monorepo host: the other scripts key the split layout on a checkout (.git) here,
# never on the directory alone.
install -d -o runner -g trading -m 2750 /srv/trading/config

# The SSH-tunnel dashboard (trader-dashboard-ssh.service): its socket lives in a directory only
# the dashview group can enter. Add the admin running this, never trader or runner.
getent group dashview >/dev/null || groupadd dashview
if [[ -n "${SUDO_USER:-}" && ! "$SUDO_USER" =~ ^(root|trader|runner)$ ]]; then usermod -aG dashview "$SUDO_USER"; fi
install -d -o runner -g dashview -m 0750 /srv/trading-dashview

echo "host ready. Next, as trader: bash deploy/setup/2-strategist.sh"
echo "(trader must log in again, or restart Claude from a fresh login, to pick up the trading group)"
