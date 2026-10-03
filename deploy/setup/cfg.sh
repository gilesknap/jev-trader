# Sourced by the setup scripts, which run before any venv exists: read one plain scalar from
# config.yaml. `cfg owner github_repo` prints gilesknap/trading. Only for two-level scalar keys;
# tests/test_config.py checks it agrees with the real loader (`trader config get`) for every key
# the scripts use. The file is the one in $TRADER_DATA_ROOT (a data checkout, #169) when that's
# set, else the one at the root of the checkout these scripts are in (the monorepo).
CONFIG_YAML="${TRADER_DATA_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/config.yaml"
cfg() {
    local v
    v=$(awk -v s="$1:" -v k="$2:" '
        /^[^ #]/ { in_s = ($1 == s) }
        in_s && $1 == k { sub(/^[^:]*:[ \t]*/, ""); sub(/[ \t]+#.*$/, ""); gsub(/^["\047]|["\047]$/, ""); print; exit }
    ' "$CONFIG_YAML")
    [[ -n "$v" ]] || { echo "config.yaml: no value for $1.$2 ($CONFIG_YAML)" >&2; return 1; }
    printf '%s\n' "$v"
}

# cfg_repo: owner.github_repo, refusing the code template's placeholder (your-github-user/...). That
# is what the public code's own config.yaml holds, so reading it means TRADER_DATA_ROOT wasn't set
# to the data checkout, and a clone URL or deploy-key page built from it would name nobody's repo.
cfg_repo() {
    local v
    v=$(cfg owner github_repo) || return 1
    if [[ $v == your-github-user/* ]]; then
        echo "config.yaml: owner.github_repo is the template placeholder '$v' ($CONFIG_YAML)." >&2
        echo "Point TRADER_DATA_ROOT at a checkout of your data repo's main, e.g." \
             "sudo TRADER_DATA_ROOT=/path/to/data-main bash deploy/setup/3-runner.sh key (or install --split);" \
             "if $CONFIG_YAML is already your data checkout, set owner.github_repo there" >&2
        return 1
    fi
    printf '%s\n' "$v"
}
