#!/usr/bin/env bash
# Timer-run wrapper (trader-strategist@.service) for a headless strategist run. Usage: strategist.sh premarket|postclose|weekly|housekeeping
# Runs as `trader` on the Max subscription: ANTHROPIC_API_KEY is unset so billing can't switch to the API.
set -uo pipefail
KIND="${1:?premarket|postclose|weekly|housekeeping}"
REPO="${TRADER_STRATEGIST_ROOT:-$HOME/trading}"
SELF="$REPO/scripts/strategist.sh"
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/trader"
mkdir -p "$LOGDIR"
# Flags for the locked and re-exec'd passes below. Read them, then drop them so the Claude
# session and everything it starts can't inherit them.
LOG="${STRATEGIST_LOG:-$LOGDIR/$(date -u +%Y%m%d-%H%M)-$KIND.log}"
LOCKED="${STRATEGIST_LOCKED:-}"; SYNCED="${STRATEGIST_SYNCED:-}"
unset STRATEGIST_LOG STRATEGIST_LOCKED STRATEGIST_SYNCED
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO" || exit 1

skip() { echo "$(date -u +%FT%TZ) $KIND: $1" >>"$LOG"; exit 0; }
alert() { uv run python -c "from trader.alerts import notify; import sys; notify('urgent', sys.argv[1])" "$1"; }

# One run at a time: a premarket tick fires every 15 min but a run may take 25. flock holds
# the lock itself and runs this script as its child with the lock fd closed (-o), so a stray
# background process left by a run can't keep the lock and silently skip later runs.
if [[ -z "$LOCKED" ]]; then
    STRATEGIST_LOCKED=1 STRATEGIST_LOG="$LOG" flock -n -o -E 201 "$LOGDIR/strategist.lock" "$SELF" "$@"
    rc=$?
    [[ $rc -eq 201 ]] && skip "another run holds the lock"
    exit $rc
fi

# Session gating via Alpaca's calendar (holidays, half-days, UK/US DST offsets).
if [[ "$KIND" == premarket || "$KIND" == postclose ]]; then
    SESSION=$(uv run trader session 2>>"$LOG"); rc=$?
    [[ $rc -eq 2 ]] && skip "market closed today"
    [[ $rc -ne 0 ]] && { alert "strategist $KIND: session lookup failed"; exit 1; }
    MTO=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['minutes_to_open'])" "$SESSION")
    MTC=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['minutes_to_close'])" "$SESSION")
    STAMP="$LOGDIR/done-$KIND-$(date -u +%F)"
    [[ -e "$STAMP" ]] && skip "already ran today"
    if [[ "$KIND" == premarket ]]; then (( MTO >= 30 && MTO <= 75 )) || skip "outside window (${MTO} min to open)"; fi
    if [[ "$KIND" == postclose ]]; then (( MTC <= -20 )) || skip "too early (${MTC} min to close)"; fi
fi

# Deploys update only the runner's checkout of main; this checkout catches up here, after
# gating. The merge can rewrite this script while bash is still reading it, so re-exec the
# merged copy: everything below (and the gating above, re-run) uses the new code. Bash parses
# this whole `if` before running it, so the merge can't disturb it.
if [[ -z "$SYNCED" ]]; then
    if [[ "$KIND" == housekeeping && ( "$(git branch --show-current)" != strategist || -n "$(git status --porcelain --untracked-files=no)" ) ]]; then
        # Housekeeping runs daily without gating: don't pull the checkout out from under a session.
        echo "$(date -u +%FT%TZ) $KIND: checkout is off strategist or dirty; not syncing" >>"$LOG"
    else
        git checkout -q strategist >>"$LOG" 2>&1
        if ! git pull -q --rebase origin strategist >>"$LOG" 2>&1; then
            git rebase --abort >>"$LOG" 2>&1 || true   # never leave the checkout mid-rebase
            alert "strategist $KIND: could not pull origin/strategist (continuing on local state)"
        fi
        BEFORE=$(git rev-parse HEAD)
        git fetch -q origin main >>"$LOG" 2>&1 && git merge -q --no-edit origin/main >>"$LOG" 2>&1 \
            || { git merge --abort 2>/dev/null; alert "strategist $KIND: could not merge main into strategist"; }
        # Publish the merge now rather than leaving it for the end of some later run.
        if [[ "$(git rev-parse HEAD)" != "$BEFORE" ]]; then
            git push -q origin strategist >>"$LOG" 2>&1 || alert "strategist $KIND: push after merging main failed"
        fi
    fi
    if ! bash -n "$SELF" >>"$LOG" 2>&1 || [[ ! -x "$SELF" ]]; then
        alert "strategist $KIND: scripts/strategist.sh doesn't parse or isn't executable after the merge; fix main, then merge it by hand"
        exit 1
    fi
    STRATEGIST_LOCKED=1 STRATEGIST_SYNCED=1 STRATEGIST_LOG="$LOG" exec "$SELF" "$@"
fi

if [[ "$KIND" == housekeeping ]]; then
    uv run trader housekeeping >>"$LOG" 2>&1
    exit $?
fi
[[ "$KIND" != premarket ]] && uv run trader archive >>"$LOG" 2>&1
# Score probes for the strategist and the dashboard. Research only: a failure (or no probes) never stops the run.
[[ "$KIND" == postclose ]] && { uv run trader probe-report --out logs/probe_report.json >>"$LOG" 2>&1 || true; }
[[ "$KIND" == weekly ]] && uv run trader compact >>"$LOG" 2>&1

PROMPT="$(cat "prompts/$KIND.md")
Today (UTC): $(date -u '+%A %F %H:%M'). Session: ${SESSION:-n/a}"
PRE=$(git rev-parse HEAD)
LIMIT=50m; [[ "$KIND" == premarket ]] && LIMIT=25m
if ! MODEL=$(uv run trader config get models.strategist 2>>"$LOG") || [[ -z "$MODEL" ]]; then
    alert "strategist $KIND: can't read models.strategist from config.yaml — see $LOG"
    exit 1
fi
timeout "$LIMIT" claude -p "$PROMPT" --model "$MODEL" --output-format text >>"$LOG" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
    alert "strategist $KIND run failed (exit $rc) — see $LOG"
    exit $rc
fi

# The strategist branch may only change these paths; code changes go via proposal/* PRs.
git checkout -q strategist >>"$LOG" 2>&1 || alert "strategist $KIND: left repo off the strategist branch"
ALLOWED='^(state/|journal/|features/custom/|logs/)'
POST=$(git rev-parse HEAD)
# Undo any commits the run made itself, so everything goes through the path check below.
[[ "$POST" != "$PRE" ]] && git reset -q --soft "$PRE"
git reset -q
OUTSIDE=$(git status --porcelain | awk '{print $NF}' | grep -Ev "$ALLOWED" || true)
if [[ -n "$OUTSIDE" ]]; then
    alert "strategist $KIND touched non-strategy paths (reverted): $(echo $OUTSIDE | head -c 300)"
    echo "$OUTSIDE" | xargs -r git checkout -q -- 2>/dev/null
    echo "$OUTSIDE" | xargs -r git clean -qfd -- 2>/dev/null
fi
# Only existing dirs: a missing pathspec makes `git add` add nothing at all (and none would add everything).
DIRS=$(ls -d state journal features/custom logs 2>/dev/null)
[[ -n "$DIRS" ]] && git add -A -- $DIRS >>"$LOG" 2>&1
# The index now holds the path-checked tree. If the run pushed its own commits, build on the
# newest of them (BASE) so the publish is a fast-forward: the commit below carries the
# path-checked tree, reverting anything disallowed the run pushed. Never force-push.
git fetch -q origin strategist >>"$LOG" 2>&1
REMOTE=$(git rev-parse -q --verify refs/remotes/origin/strategist) || REMOTE=$PRE
BASE=$(git merge-base "$POST" "$REMOTE" 2>/dev/null) || BASE=$PRE
git merge-base --is-ancestor "$PRE" "$BASE" 2>/dev/null || BASE=$PRE
PUSHED_OUTSIDE=$(git diff --name-only --no-renames "$PRE" "$BASE" | grep -Ev "$ALLOWED" || true)
[[ -n "$PUSHED_OUTSIDE" ]] && alert "strategist $KIND pushed non-strategy paths to origin/strategist (reverted): $(echo $PUSHED_OUTSIDE | head -c 300)"
TREE=$(git write-tree)
if [[ "$TREE" != "$(git rev-parse "$BASE^{tree}")" ]]; then
    BASE=$(git commit-tree "$TREE" -p "$BASE" -m "strategist: $KIND $(date -u +%F)")
fi
git reset -q --soft "$BASE"
# Someone else pushed to strategist during the run: replay this run's commit on top.
if ! git merge-base --is-ancestor "$REMOTE" "$BASE" && ! git pull -q --rebase origin strategist >>"$LOG" 2>&1; then
    git rebase --abort >>"$LOG" 2>&1 || true
    alert "strategist $KIND: could not rebase onto origin/strategist; not pushed"
elif ! git push -q origin strategist >>"$LOG" 2>&1; then
    alert "strategist $KIND: git push failed"
fi
# Invariant: strategist differs from the run's start only under the allowed paths. The rebase
# above trusts origin, so a pushed commit the run later amended, reset away or force-pushed
# over would otherwise stay there unnoticed.
LEAKED=$(git diff --name-only --no-renames "$PRE" HEAD | grep -Ev "$ALLOWED" || true)
[[ -n "$LEAKED" ]] && alert "strategist $KIND: strategist now differs from the run's start outside strategy paths (NOT reverted; a human push during the run, or the run pushed then rewrote its history): $(echo $LEAKED | head -c 300)"
touch "$REPO/.last_run"
[[ "$KIND" == postclose ]] && touch "$REPO/.last_postclose"
[[ "$KIND" != weekly ]] && touch "$STAMP"
find "$LOGDIR" -name '*.log' -mtime +14 -delete
find "$LOGDIR" -name 'done-*' -mtime +3 -delete
