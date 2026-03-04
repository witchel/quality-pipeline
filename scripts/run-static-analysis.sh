#!/usr/bin/env bash
# run-static-analysis.sh — Run static analysis tools relevant to a round
# Usage: run-static-analysis.sh <round_name> <project_dir> [analyzers_override]
#
# Returns analyzer output on stdout (capped at 4000 chars).
# Exits 0 always — analyzer failures are non-fatal.

set -uo pipefail

ROUND_NAME="${1:-}"
PROJECT_DIR="${2:-.}"
ANALYZERS_OVERRIDE="${3:-}"
MAX_OUTPUT=4000

cd "$PROJECT_DIR" 2>/dev/null || exit 0

# --- Default round → analyzer mapping ---

default_analyzers() {
    case "$ROUND_NAME" in
        security)       echo "bandit semgrep" ;;
        type-safety)    echo "mypy pyright tsc" ;;
        dead-code)      echo "vulture" ;;
        *)              echo "" ;;
    esac
}

# --- Per-analyzer functions ---

run_bandit() {
    command -v bandit &>/dev/null || return 0
    # Only run for Python projects
    [[ -f pyproject.toml || -f setup.py || -f setup.cfg || -f requirements.txt ]] || return 0
    bandit -r . -f txt --severity-filter medium 2>/dev/null || true
}

run_semgrep() {
    command -v semgrep &>/dev/null || return 0
    semgrep --config auto --quiet --no-git-ignore 2>/dev/null || true
}

run_mypy() {
    command -v mypy &>/dev/null || return 0
    [[ -f pyproject.toml || -f setup.py || -f setup.cfg || -f mypy.ini ]] || return 0
    mypy . --no-error-summary 2>/dev/null || true
}

run_pyright() {
    command -v pyright &>/dev/null || return 0
    [[ -f pyproject.toml || -f setup.py || -f setup.cfg || -f pyrightconfig.json ]] || return 0
    pyright . 2>/dev/null || true
}

run_tsc() {
    command -v tsc &>/dev/null || return 0
    [[ -f tsconfig.json ]] || return 0
    tsc --noEmit 2>/dev/null || true
}

run_vulture() {
    command -v vulture &>/dev/null || return 0
    [[ -f pyproject.toml || -f setup.py || -f setup.cfg || -f requirements.txt ]] || return 0
    vulture . 2>/dev/null || true
}

# --- Main ---

analyzers="${ANALYZERS_OVERRIDE:-$(default_analyzers)}"
[[ -z "$analyzers" ]] && exit 0

output=""
for analyzer in $analyzers; do
    fn="run_${analyzer}"
    if declare -f "$fn" &>/dev/null; then
        result=$("$fn" 2>/dev/null) || true
        if [[ -n "$result" ]]; then
            output+="### ${analyzer}"$'\n'"${result}"$'\n\n'
        fi
    fi
done

# Cap output and emit
if [[ -n "$output" ]]; then
    echo "$output" | head -c "$MAX_OUTPUT"
fi

exit 0
