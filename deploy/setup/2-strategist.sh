#!/usr/bin/env bash
# Step 2 (as trader, from a fresh login so the trading group applies):  bash deploy/setup/2-strategist.sh
# Checks out the strategist branch at /srv/trading/strategist, creates .env (paper keys only),
# installs the strategist's systemd timers and points ~/trading at the checkout. Idempotent.
#
# Split layout (#169), chosen when /srv/trading/config is a checkout of the data repo's main (made by
# `3-runner.sh install`, so run that first): the checkout is the DATA repo's strategist branch;
# trader gets its own venv of the deployed code plus the `trader`, `trader-python` and `trader-test`
# helpers in ~/.local/bin; the unit comes from the code (/srv/trading/main) and the timers from the
# config checkout; and Claude Code deny rules, the charter-import check and runner's access to the
# run lock are set up. Without that checkout everything below runs exactly as before the split.
set -euo pipefail
# Everything this writes into the checkout must be writable by trader only: trader's login umask can
# be 0002, and group trading includes runner, which must never be able to plant files trader runs.
umask 022
# shellcheck source=deploy/setup/lib.sh
. "$(dirname "$0")/lib.sh"
SPLIT=""
if split_layout; then
    SPLIT=1
    CODE=/srv/trading/main
    # The data repo's slug comes from the deployed config unless the caller points elsewhere.
    export TRADER_DATA_ROOT="${TRADER_DATA_ROOT:-$CONFIG_CHECKOUT}"
fi
# shellcheck source=deploy/setup/cfg.sh
. "$(dirname "$0")/cfg.sh"
GH_REPO=$(cfg_repo)
REPO=https://github.com/$GH_REPO.git
S=/srv/trading/strategist
[[ "$(id -un)" == trader ]] || { echo "run as trader" >&2; exit 1; }
id -nG | grep -qw trading || { echo "this login lacks the trading group: log in again" >&2; exit 1; }

if [[ ! -d $S/.git ]]; then
    if [[ -n $SPLIT ]]; then
        # The data repo's strategist branch (0-data.sh made it). Never create it here: a missing
        # branch means the wrong repo, and a branch cut from data main would hold the config.
        git clone -q -b strategist "$REPO" "$S"
    else
        git clone -q "$REPO" "$S"
        cd "$S"
        if git ls-remote --exit-code --heads origin strategist >/dev/null; then
            git checkout -q strategist
        else
            git checkout -q -b strategist && git push -q -u origin strategist
        fi
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

if [[ -n $SPLIT ]]; then
    # trader's own venv of the deployed code (#169 section 6.2): runner's venv isn't readable by
    # trader. uv needs no write access to $CODE for this (checked: --frozen leaves uv.lock alone,
    # and UV_PROJECT_ENVIRONMENT keeps it from creating $CODE/.venv). The editable install's .pth
    # points at $CODE/src, so trader always runs exactly the deployed code. --no-dev leaves out the
    # dev tools; the test group (pytest) is for trader-test (proposal clones, section 14 item 4).
    VENV=$HOME/.local/share/trader/venv
    mkdir -p "$(dirname "$VENV")"
    chmod 750 "$(dirname "$VENV")"   # nobody outside trader's group needs it once runner can enter ~
    UV_PROJECT_ENVIRONMENT=$VENV uv sync -q --frozen --no-dev --group test --project "$CODE"
    mkdir -p ~/.local/bin
    install -m 755 "$CODE/scripts/trader-shim" ~/.local/bin/trader
    install -m 755 "$CODE/scripts/trader-python" ~/.local/bin/trader-python
    install -m 755 "$CODE/scripts/trader-test" ~/.local/bin/trader-test
    UNIT_SRC=$CODE/deploy/systemd-trader
    TIMER_SRC=$CONFIG_CHECKOUT/deploy/systemd-trader
else
    uv sync -q --no-dev --group test
    UNIT_SRC=deploy/systemd-trader
    TIMER_SRC=deploy/systemd-trader
fi
if [[ -n $SPLIT ]]; then
    # Before any timer is (re)enabled, so no run starts without these.
    # Keep Claude off the public code repo's issues and PRs (#169 section 6.6, section 14 item 9).
    # Merged into trader's settings: defaultMode and everything else stay as they are.
    mkdir -p ~/.claude
    CODE_NAME=$(repo_name "$(git -C "$CODE" -c safe.directory="$CODE" remote get-url origin 2>/dev/null || true)")
    # A gh rule whose name the data repo's name contains is skipped (with a warning): it would
    # block gh on the data repo itself.
    mapfile -t RULES < <(strategist_deny_rules "$CODE_NAME" "$(repo_name "$REPO")")
    claude_deny_merge ~/.claude/settings.json "${RULES[@]}"
    # The wrapper passes the charter with --append-system-prompt-file; a user-scope import as
    # well would load it twice (section 14 item 10).
    if claude_md_imports_charter ~/.claude/CLAUDE.md; then
        echo "WARNING: ~/.claude/CLAUDE.md imports a CLAUDE.md (an @...CLAUDE.md line). In the split layout" \
             "the charter comes from $CODE/CLAUDE.md through the wrapper: remove the import." >&2
    fi
    # trading-deploy (as runner) takes the strategist's run lock to refuse a deploy while a run is
    # live, so runner needs to reach it: traverse (x) only on the directories, read on the lock.
    # Adding runner:x leaves each directory's ACL mask equal to its group bits, so nobody else
    # gains anything.
    # The default ACL gives files created there later the same modes the unit's UMask=0022 gives
    # today (a default ACL replaces the umask), plus read for runner.
    command -v setfacl >/dev/null || { echo "setfacl is missing (apt install acl)" >&2; exit 1; }
    # Traverse on ~ lets runner reach anything world-readable below it. ~/.claude holds Claude's
    # session history, tool results and memory (and is 755 by default): close it first. Split mode
    # only, because runner gets no traverse in the monorepo layout, and an admin with an ACL on ~
    # may rely on reading it there today.
    chmod o-rwx "$HOME/.claude"
    LOCKDIR=$HOME/.local/state/trader   # the wrapper's $LOGDIR (the unit sets no XDG_STATE_HOME)
    mkdir -p "$LOCKDIR"
    touch "$LOCKDIR/strategist.lock"
    for d in "$HOME" "$HOME/.local" "$HOME/.local/state" "$LOCKDIR"; do setfacl -m u:runner:x "$d"; done
    setfacl -d -m u::rwx,g::rx,o::rx,u:runner:r "$LOCKDIR"
    setfacl -n -m u:runner:r "$LOCKDIR/strategist.lock"   # -n: leave the group's rights as they are
fi

# A unit that points the code at /srv/trading/config on a host without that checkout would stop
# every strategist run (#169 section 14 item 3).
if unit_needs_missing_config "$UNIT_SRC/trader-strategist@.service"; then
    echo "$UNIT_SRC/trader-strategist@.service names $CONFIG_CHECKOUT, which holds no config.yaml here;" \
         "not installing it (clone the data repo's main there first: sudo bash deploy/setup/3-runner.sh install)" >&2
    exit 1
fi
# The schedule: systemd user timers (they honour config.yaml's local_tz; cron's CRON_TZ didn't, #152).
# Re-run this script after a schedule change in config.yaml to reinstall them.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p ~/.config/systemd/user
cp "$UNIT_SRC/trader-strategist@.service" "$TIMER_SRC"/trader-strategist-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trader-strategist-{premarket,postclose,weekly,housekeeping}.timer
systemctl --user restart trader-strategist-{premarket,postclose,weekly,housekeeping}.timer  # re-arm a changed OnCalendar
crontab -r 2>/dev/null || true   # an older install scheduled these from cron

if [[ -L ~/trading ]]; then :
elif [[ -e ~/trading ]]; then mv ~/trading ~/trading.pre-srv && ln -s "$S" ~/trading
else ln -s "$S" ~/trading; fi

echo "strategist ready (branch $(git branch --show-current), timers installed):"
systemctl --user list-timers 'trader-strategist-*' --no-pager
if [[ -n $SPLIT ]]; then
    # The first install ran before $S/.env existed, so it couldn't give runner its env yet.
    echo "Next, as admin: sudo bash $CODE/deploy/setup/3-runner.sh install   (again: it copies $S/.env to runner)"
    echo "then:           sudo bash $CODE/deploy/setup/check.sh"
else
    echo "Next, as admin: sudo bash $S/deploy/setup/3-runner.sh key"
fi
