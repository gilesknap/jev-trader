#!/usr/bin/env bash
# Step 0 (as the owner, on a laptop or the VPS, from a clone of the code; not as trader or runner):
#   bash deploy/setup/0-data.sh OWNER/DATA-REPO [--name "Your Name"] [--start-date YYYY-MM-DD]
#                                               [--users a@b.com,c@d.com] [--remote URL]
# Creates your private data repo (#169) from the code's templates/data/: branch `main` (config.yaml
# with your values, config/mode.yaml, and the deploy files rendered from them) and an orphan
# branch `strategist` (the strategist's starting state), pushed together; then the `needs-human`
# and `weekly` labels. Create the repo on GitHub first, private and EMPTY (no README or licence).
# Values not given as flags are asked for. --remote overrides the push URL (default
# https://github.com/OWNER/DATA-REPO.git, authenticated through gh: `gh auth setup-git`).
# Idempotent: it refuses a repo holding anything else, and on a repo it already set up it only
# re-checks the default branch and the labels.
set -euo pipefail
die() { echo "0-data.sh: $*" >&2; exit 1; }
CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TPL=$CODE/templates/data

SLUG="" NAME="" START="" USERS="" USERS_SET="" REMOTE=""
while (( $# )); do
    case "$1" in
    --name) NAME="${2?--name NAME}"; shift ;;
    --start-date) START="${2?--start-date YYYY-MM-DD}"; shift ;;
    --users) USERS="${2?--users a@b.com,...}"; USERS_SET=1; shift ;;
    --remote) REMOTE="${2?--remote URL}"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [[ -z $SLUG ]] || die "one OWNER/DATA-REPO only"; SLUG=$1 ;;
    esac
    shift
done
[[ "$SLUG" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "usage: bash $0 OWNER/DATA-REPO [options] (see --help)"
[[ -f $TPL/main/config.yaml && -f $TPL/main/config/mode.yaml && -d $TPL/strategist ]] \
    || die "$TPL/{main,strategist} not found: run this from a checkout of the code"
for t in git gh uv; do command -v "$t" >/dev/null || die "$t is not installed"; done
REMOTE=${REMOTE:-https://github.com/$SLUG.git}

finish() {  # idempotent: the default branch and the labels
    gh api -X PATCH "repos/$SLUG" -f default_branch=main >/dev/null   # whichever branch GitHub saw first
    gh label create needs-human -R "$SLUG" --force --color D93F0B \
        --description "The strategist needs the human (a ticker, data, an API, a code proposal)" >/dev/null
    gh label create weekly -R "$SLUG" --force --color 0E8A16 \
        --description "The strategist's weekly retrospective" >/dev/null
    echo "default branch main, labels needs-human and weekly are in place on $SLUG"
}

# What's there already? Only an empty repo is filled; one holding exactly the two branches this
# script pushes (atomically, so never just one of them) was set up by an earlier run.
REFS=$(git ls-remote --heads "$REMOTE") || die "can't read $REMOTE (does the repo exist, and can you reach it?)"
HEADS=$(awk '{sub("refs/heads/", "", $2); print $2}' <<<"$REFS" | sort | tr '\n' ' ')
if [[ "$HEADS" == "main strategist " ]]; then
    echo "$SLUG already has main and strategist: not touching them"
    finish
    exit 0
fi
[[ -z "$REFS" ]] || die "$REMOTE is not empty (branches: $HEADS); refusing to overwrite it"

ask() {  # ask VAR PROMPT: read a value from the terminal, or fail when there's none
    [[ -t 0 ]] || die "$2: not given, and no terminal to ask on (pass it as a flag; see --help)"
    read -r -p "$2: " "$1"
}
[[ -n $NAME ]] || ask NAME "Your name (owner.name)"
[[ -n $START ]] || ask START "Experiment start date, observe day 1 (YYYY-MM-DD)"
if [[ -z $USERS_SET && -t 0 ]]; then
    read -r -p "Tailscale logins allowed into the dashboard, comma-separated (blank = nobody): " USERS
fi
# Values go into YAML through sed: refuse anything that could break either.
[[ -n $NAME && ! "$NAME" =~ [\"\\\|\&#] ]] || die "name: required, without \" \\ | & #"
[[ "$START" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "start date: expected YYYY-MM-DD, got '$START'"
USERS=${USERS// /}
[[ "$USERS" =~ ^[A-Za-z0-9._%+@,-]*$ ]] || die "users: comma-separated logins, got '$USERS'"
USERS_YAML="[${USERS//,/, }]"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
D=$WORK/data
git init -q -b main "$D"
cp -a "$TPL/main/." "$D/"
# Replace each placeholder's value, keeping its comment; each key must appear exactly once.
sub() {
    [[ $(grep -c -E "^  $1:" "$D/config.yaml") -eq 1 ]] || die "templates/data/main/config.yaml: expected one '  $1:' line"
    sed -i -E "s|^(  $1:)[^#]*|\1 $2   |" "$D/config.yaml"
}
sub name "\"$NAME\""
sub github_repo "$SLUG"
sub users "$USERS_YAML"
sub start_date "$START"
sed -i '/^# TEMPLATE:/d' "$D/config.yaml"
# Render with this checkout's templates (it also proves config.yaml loads). TRADER_DATA_ROOT
# because the settings load at import, from the data root.
(cd "$CODE" && env -u TRADER_CODE_ROOT -u TRADER_CONFIG -u TRADER_STRATEGIST_ROOT TRADER_DATA_ROOT="$D" \
    uv run -q --no-dev trader config render-deploy --data-root "$D") || die "render-deploy failed (see above)"
CODE_SHA=$(git -C "$CODE" rev-parse --short HEAD 2>/dev/null || echo unknown)
git -C "$D" add -A
git -C "$D" commit -q -m "Data repo main from the code's templates/data/main ($CODE_SHA)"

# The strategist's branch shares no history or tree with main, and is never merged with it.
git -C "$D" checkout -q --orphan strategist
git -C "$D" rm -rfq .
cp -a "$TPL/strategist/." "$D/"
# The template's stub is CLAUDE.md.template, so Claude sessions in the code repo don't load it.
if [[ -f $D/CLAUDE.md.template ]]; then mv "$D/CLAUDE.md.template" "$D/CLAUDE.md"; fi
[[ -f $D/CLAUDE.md ]] || die "templates/data/strategist has no CLAUDE.md.template"
git -C "$D" add -A
git -C "$D" commit -q -m "Strategist branch from the code's templates/data/strategist ($CODE_SHA)"

git -C "$D" remote add origin "$REMOTE"
git -C "$D" push -q --atomic origin main strategist
finish
echo "$SLUG is ready: main (config) and strategist (data). Review config.yaml there; after editing it,"
echo "re-render with: TRADER_DATA_ROOT=<checkout> uv run trader config render-deploy --data-root <checkout>"
