#!/usr/bin/env bash
# Timer-run wrapper (trader-strategist@.service) for a headless strategist run. Usage: strategist.sh premarket|postclose|weekly|housekeeping
# Runs as `trader` on the Max subscription: ANTHROPIC_API_KEY is unset so billing can't switch to the API.
set -uo pipefail
KIND="${1:?premarket|postclose|weekly|housekeeping}"
REPO="${TRADER_STRATEGIST_ROOT:-$HOME/trading}"
# Two layouts (#169). Monorepo (TRADER_DATA_ROOT unset, today): REPO is the strategist branch with
# the code on it; the wrapper merges main into it and runs `uv run trader` there. Split
# (TRADER_DATA_ROOT set): REPO holds data only, the code is the deployed checkout CODE, `trader`
# is the shim on PATH, and this script runs from CODE rather than from REPO.
if [[ -n ${TRADER_DATA_ROOT:-} ]]; then
    SPLIT=1
    CODE="${TRADER_CODE_ROOT:-/srv/trading/main}"
    SELF=$(readlink -f -- "${BASH_SOURCE[0]}")
    TRADER_BIN=(trader); PY_BIN=(trader-python)
else
    SPLIT=""
    CODE="$REPO"
    SELF="$REPO/scripts/strategist.sh"
    TRADER_BIN=(uv run trader); PY_BIN=(uv run python)
fi
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/trader"
mkdir -p "$LOGDIR"
# Flags for the copied, locked and re-exec'd passes below. Read them, then drop them so the Claude
# session and everything it starts can't inherit them.
LOG="${STRATEGIST_LOG:-$LOGDIR/$(date -u +%Y%m%d-%H%M)-$KIND.log}"
LOCKED="${STRATEGIST_LOCKED:-}"; SYNCED="${STRATEGIST_SYNCED:-}"; COPIED="${STRATEGIST_COPIED:-}"
unset STRATEGIST_LOG STRATEGIST_LOCKED STRATEGIST_SYNCED STRATEGIST_COPIED
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO" || exit 1

skip() { echo "$(date -u +%FT%TZ) $KIND: $1" >>"$LOG"; exit 0; }
alert() { "${PY_BIN[@]}" -c "from trader.alerts import notify; import sys; notify('urgent', sys.argv[1])" "$1"; }

# Split mode: a deploy can replace this file while a run is still reading it (bash reads
# scripts as it goes), so run a private copy instead. The copy is made before the lock, and
# every later pass (the locked child included) is that copy. Bash parses this whole `if`
# before running it, so the exec is the last thing read from the deployed file. The syntax
# check catches a copy taken while a deploy was half-way through writing the file.
if [[ -n "$SPLIT" && -z "$COPIED" && -z "$LOCKED" ]]; then
    COPY="$LOGDIR/strategist.sh.$$"
    if ! { cp -- "$SELF" "$COPY" && chmod 700 "$COPY" && bash -n "$COPY" >>"$LOG" 2>&1; }; then
        rm -f -- "$COPY"
        alert "strategist $KIND: could not copy $SELF to $COPY, or the copy doesn't parse; not running"
        exit 1
    fi
    STRATEGIST_COPIED=1 STRATEGIST_LOG="$LOG" exec "$COPY" "$@"
fi

# One run at a time: a premarket tick fires every 15 min but a run may take 25. flock holds
# the lock itself and runs this script as its child with the lock fd closed (-o), so a stray
# background process left by a run can't keep the lock and silently skip later runs.
# The lock is held for the whole run, so a deploy can test it to refuse while a run is live.
if [[ -z "$LOCKED" ]]; then
    STRATEGIST_LOCKED=1 STRATEGIST_LOG="$LOG" flock -n -o -E 201 "$LOGDIR/strategist.lock" "$SELF" "$@"
    rc=$?
    # Split mode: SELF is the private copy made above; nothing runs it any more.
    if [[ -n "$SPLIT" && "$SELF" == "$LOGDIR"/strategist.sh.* ]]; then rm -f -- "$SELF"; fi
    [[ $rc -eq 201 ]] && skip "another run holds the lock"
    exit $rc
fi

# Split mode: bring trader's venv (the one the `trader` shim runs) in line with the deployed
# uv.lock before anything uses it, the session lookup included (#169 section 6.2). The command is
# 2-strategist.sh's, exactly; it's a quick no-op when uv.lock hasn't changed. It runs under the
# run lock, which a deploy takes too, so the code can't change under it. On failure the venv may
# not match the deployed code, so nothing runs (the note in the log is in case the alert, which
# uses that venv, can't be sent).
if [[ -n "$SPLIT" ]]; then
    if ! UV_PROJECT_ENVIRONMENT="$HOME/.local/share/trader/venv" uv sync -q --frozen --extra dev --project "$CODE" >>"$LOG" 2>&1; then
        echo "$(date -u +%FT%TZ) $KIND: trader's venv sync failed; not running" >>"$LOG"
        alert "strategist $KIND: could not sync trader's venv with $CODE (uv sync failed; see $LOG); not running"
        exit 1
    fi
fi

# Session gating via Alpaca's calendar (holidays, half-days, UK/US DST offsets).
if [[ "$KIND" == premarket || "$KIND" == postclose ]]; then
    SESSION=$("${TRADER_BIN[@]}" session 2>>"$LOG"); rc=$?
    [[ $rc -eq 2 ]] && skip "market closed today"
    [[ $rc -ne 0 ]] && { alert "strategist $KIND: session lookup failed"; exit 1; }
    MTO=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['minutes_to_open'])" "$SESSION")
    MTC=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['minutes_to_close'])" "$SESSION")
    STAMP="$LOGDIR/done-$KIND-$(date -u +%F)"
    [[ -e "$STAMP" ]] && skip "already ran today"
    if [[ "$KIND" == premarket ]]; then (( MTO >= 30 && MTO <= 75 )) || skip "outside window (${MTO} min to open)"; fi
    if [[ "$KIND" == postclose ]]; then (( MTC <= -20 )) || skip "too early (${MTC} min to close)"; fi
fi

# Monorepo: deploys update only the runner's checkout of main; this checkout catches up here,
# after gating. The merge can rewrite this script while bash is still reading it, so re-exec
# the merged copy: everything below (and the gating above, re-run) uses the new code. Bash
# parses this whole `if` before running it, so the merge can't disturb it.
# Split: the data branch has no code, so there is nothing to merge and nothing to re-exec;
# only the pull of origin/strategist runs.
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
        if [[ -z "$SPLIT" ]]; then
            BEFORE=$(git rev-parse HEAD)
            git fetch -q origin main >>"$LOG" 2>&1 && git merge -q --no-edit origin/main >>"$LOG" 2>&1 \
                || { git merge --abort 2>/dev/null; alert "strategist $KIND: could not merge main into strategist"; }
            # Publish the merge now rather than leaving it for the end of some later run.
            if [[ "$(git rev-parse HEAD)" != "$BEFORE" ]]; then
                git push -q origin strategist >>"$LOG" 2>&1 || alert "strategist $KIND: push after merging main failed"
            fi
        fi
    fi
    if [[ -z "$SPLIT" ]]; then
        if ! bash -n "$SELF" >>"$LOG" 2>&1 || [[ ! -x "$SELF" ]]; then
            alert "strategist $KIND: scripts/strategist.sh doesn't parse or isn't executable after the merge; fix main, then merge it by hand"
            exit 1
        fi
        STRATEGIST_LOCKED=1 STRATEGIST_SYNCED=1 STRATEGIST_LOG="$LOG" exec "$SELF" "$@"
    fi
fi

# Split mode: the charter (CLAUDE.md) and the prompt come from the deployed code; without
# them the run would be unguided, so refuse before anything below changes the checkout. The
# charter is checked for every kind (a missing one means a broken deploy); housekeeping has
# no prompt. In the monorepo the charter is the project CLAUDE.md, which Claude loads itself
# (the flag would load it twice).
CHARTER=()
if [[ -n "$SPLIT" ]]; then
    if [[ ! -s "$CODE/CLAUDE.md" ]] || [[ "$KIND" != housekeeping && ! -s "$CODE/prompts/$KIND.md" ]]; then
        alert "strategist $KIND: $CODE/CLAUDE.md or $CODE/prompts/$KIND.md is missing or empty; not running"
        exit 1
    fi
    CHARTER=(--append-system-prompt-file "$CODE/CLAUDE.md")
fi

if [[ "$KIND" == housekeeping ]]; then
    "${TRADER_BIN[@]}" housekeeping >>"$LOG" 2>&1
    exit $?
fi
[[ "$KIND" != premarket ]] && "${TRADER_BIN[@]}" archive >>"$LOG" 2>&1
# Score probes for the strategist and the dashboard. Research only: a failure (or no probes) never stops the run.
[[ "$KIND" == postclose ]] && { "${TRADER_BIN[@]}" probe-report --out logs/probe_report.json >>"$LOG" 2>&1 || true; }
[[ "$KIND" == weekly ]] && "${TRADER_BIN[@]}" compact >>"$LOG" 2>&1

PROMPT="$(cat "$CODE/prompts/$KIND.md")
Today (UTC): $(date -u '+%A %F %H:%M'). Session: ${SESSION:-n/a}"
PRE=$(git rev-parse HEAD)
LIMIT=50m; [[ "$KIND" == premarket ]] && LIMIT=25m
if ! MODEL=$("${TRADER_BIN[@]}" config get models.strategist 2>>"$LOG") || [[ -z "$MODEL" ]]; then
    alert "strategist $KIND: can't read models.strategist from config.yaml — see $LOG"
    exit 1
fi
timeout "$LIMIT" claude -p "$PROMPT" ${CHARTER[@]+"${CHARTER[@]}"} --model "$MODEL" --output-format text >>"$LOG" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
    alert "strategist $KIND run failed (exit $rc) — see $LOG"
    exit $rc
fi

# The strategist branch may only change these paths; code changes go via proposal/* PRs
# (split mode: as patches under proposals/).
git checkout -q strategist >>"$LOG" 2>&1 || alert "strategist $KIND: left repo off the strategist branch"
ALLOWED='^(state/|journal/|features/custom/|logs/)'
if [[ -n "$SPLIT" ]]; then ALLOWED='^(state/|journal/|features/custom/|logs/|proposals/)'; fi
# Human-owned files inside the allowed dirs: the strategist reads them but may not change them, so
# the path check treats them as outside (#201). state/steering.md is the human's steering.
HUMAN_OWNED='^state/steering\.md$'
is_allowed() { [[ $1 =~ $ALLOWED && ! $1 =~ $HUMAN_OWNED ]]; }
# Filter a newline-separated path list on stdin down to the paths outside the strategy paths.
outside_only() { local p; while IFS= read -r p || [[ -n $p ]]; do is_allowed "$p" || printf '%s\n' "$p"; done; }
POST=$(git rev-parse HEAD)
# Undo any commits the run made itself, so everything goes through the path check below.
[[ "$POST" != "$PRE" ]] && git reset -q --soft "$PRE"
git reset -q
# Changed paths outside ALLOWED, from `git status -z` (NUL-separated and unquoted, so spaces and
# non-ASCII parse exactly, #177): untracked ones in OUT_UNTRACKED, the rest in OUT_TRACKED, all
# of them in OUT_ALL. A rename or copy entry carries its source path as a second field. Untracked
# files are listed one by one, whatever status.showUntrackedFiles says: `no` would hide them, and
# the default names only the top untracked directory, which can hold allowed paths too. Returns
# non-zero if git can't list them.
outside_paths() {
    OUT_TRACKED=(); OUT_UNTRACKED=(); OUT_ALL=()
    local list rec xy path orig p rc=0
    list=$(mktemp "$LOGDIR/status.XXXXXX") || return 1
    git status --porcelain -z --untracked-files=all >"$list" 2>>"$LOG" || rc=1
    while IFS= read -r -d '' rec; do
        xy=${rec:0:2}; path=${rec:3}; orig=""
        if [[ $xy == *[RC]* ]]; then IFS= read -r -d '' orig || rc=1; fi
        for p in "$path" ${orig:+"$orig"}; do
            is_allowed "$p" && continue
            OUT_ALL+=("$p")
            if [[ $xy == '??' ]]; then OUT_UNTRACKED+=("$p"); else OUT_TRACKED+=("$p"); fi
        done
    done <"$list"
    rm -f -- "$list"
    return $rc
}
joined() { local out="" p; for p in "$@"; do out+="${out:+, }$p"; done; printf '%s' "${out:0:300}"; }
# For the alerts that refuse to publish: those exits skip the publish below, which is what reverts
# anything outside ALLOWED the run pushed to origin/strategist itself, and the next run starts past
# it. Name every outside path origin/strategist now differs by from the run's start, so a human
# can check them: the run's own pushes, or a human push during the run.
origin_note() {
    local diff out
    if git fetch -q origin strategist >>"$LOG" 2>&1 \
        && diff=$(git diff --name-only --no-renames "$PRE" refs/remotes/origin/strategist 2>>"$LOG"); then
        mapfile -t out < <(printf '%s' "$diff" | outside_only)
        (( ${#out[@]} )) && printf ' Also, origin/strategist differs from the run'\''s start outside strategy paths (NOT reverted; pushed by the run, or by a human during it): %s' "$(joined "${out[@]}")"
    else
        printf ' Could not check origin/strategist for pushed non-strategy paths.'
    fi
    return 0
}
# Revert them one path at a time, so one failure can't skip the rest (F5: a single `git checkout`
# of every path failed as a whole on any untracked one). Untracked first: a tracked file replaced
# by a directory comes back only once that directory is gone, so empty directories an untracked
# file leaves behind go too (rmdir removes only empty ones; a parent of an outside path is outside
# too). Literal pathspecs: a name like `*` must not match anything else. Then look again, and
# refuse to publish if anything survived.
if ! outside_paths; then
    alert "strategist $KIND: could not list changed paths for the path check; not publishing — see $LOG.$(origin_note)"
    exit 1
fi
if (( ${#OUT_ALL[@]} )); then
    TOUCHED=$(joined "${OUT_ALL[@]}")
    for p in ${OUT_UNTRACKED[@]+"${OUT_UNTRACKED[@]}"}; do
        git --literal-pathspecs clean -qfd -- "$p" >>"$LOG" 2>&1
        d=$(dirname -- "$p")
        while [[ $d != . && $d != / ]] && rmdir -- "$d" 2>/dev/null; do d=$(dirname -- "$d"); done
    done
    for p in ${OUT_TRACKED[@]+"${OUT_TRACKED[@]}"}; do
        git --literal-pathspecs checkout -q HEAD -- "$p" >>"$LOG" 2>&1
    done
    if ! outside_paths || (( ${#OUT_ALL[@]} )); then
        # Stop today's later ticks re-running a whole session on the same broken checkout.
        [[ -n ${STAMP:-} ]] && touch "$STAMP"
        alert "strategist $KIND touched non-strategy paths and could NOT revert all of them; not publishing, fix the checkout by hand (no retry today). Still changed: $(joined ${OUT_ALL[@]+"${OUT_ALL[@]}"}). Touched: $TOUCHED.$(origin_note)"
        exit 1
    fi
    alert "strategist $KIND touched non-strategy paths (reverted): $TOUCHED"
fi
# Only existing dirs: a missing pathspec makes `git add` add nothing at all (and none would add everything).
DIRS=$(ls -d state journal features/custom logs ${SPLIT:+proposals} 2>/dev/null)
[[ -n "$DIRS" ]] && git add -A -- $DIRS >>"$LOG" 2>&1
# The index now holds the path-checked tree. If the run pushed its own commits, build on the
# newest of them (BASE) so the publish is a fast-forward: the commit below carries the
# path-checked tree, reverting anything disallowed the run pushed. Never force-push.
git fetch -q origin strategist >>"$LOG" 2>&1
REMOTE=$(git rev-parse -q --verify refs/remotes/origin/strategist) || REMOTE=$PRE
BASE=$(git merge-base "$POST" "$REMOTE" 2>/dev/null) || BASE=$PRE
git merge-base --is-ancestor "$PRE" "$BASE" 2>/dev/null || BASE=$PRE
PUSHED_OUTSIDE=$(git diff --name-only --no-renames "$PRE" "$BASE" | outside_only)
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
LEAKED=$(git diff --name-only --no-renames "$PRE" HEAD | outside_only)
[[ -n "$LEAKED" ]] && alert "strategist $KIND: strategist now differs from the run's start outside strategy paths (NOT reverted; a human push during the run, or the run pushed then rewrote its history): $(echo $LEAKED | head -c 300)"
touch "$REPO/.last_run"
[[ "$KIND" == postclose ]] && touch "$REPO/.last_postclose"
[[ "$KIND" != weekly ]] && touch "$STAMP"
# Split mode: private copies left by runs that were killed before they could remove theirs.
if [[ -n "$SPLIT" ]]; then find "$LOGDIR" -name 'strategist.sh.*' -mmin +180 -delete; fi
find "$LOGDIR" -name '*.log' -mtime +14 -delete
find "$LOGDIR" -name 'done-*' -mtime +3 -delete
