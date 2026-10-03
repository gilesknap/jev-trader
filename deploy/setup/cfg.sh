# Sourced by the setup scripts, which run before any venv exists: read one plain scalar from
# config.yaml at the root of the checkout these scripts are in. `cfg owner github_repo` prints
# gilesknap/trading. Only for two-level scalar keys; tests/test_config.py checks it agrees
# with the real loader (`trader config get`) for every key the scripts use.
CONFIG_YAML="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/config.yaml"
cfg() {
    local v
    v=$(awk -v s="$1:" -v k="$2:" '
        /^[^ #]/ { in_s = ($1 == s) }
        in_s && $1 == k { sub(/^[^:]*:[ \t]*/, ""); sub(/[ \t]+#.*$/, ""); gsub(/^["\047]|["\047]$/, ""); print; exit }
    ' "$CONFIG_YAML")
    [[ -n "$v" ]] || { echo "config.yaml: no value for $1.$2 ($CONFIG_YAML)" >&2; return 1; }
    printf '%s\n' "$v"
}
