#!/usr/bin/env bash
# Detect the test command for the current project.
# Prints the detected command to stdout. Exits 1 if none found.
# Priority: CLAUDE.md → Makefile → package.json → pyproject.toml → go.mod → Cargo.toml

set -euo pipefail

detect_test_command() {
    local dir="${1:-.}"

    # 1. Check CLAUDE.md for test command hints
    if [[ -f "$dir/CLAUDE.md" ]]; then
        local cmd
        cmd=$(grep -iE '^\s*(test command|run tests|testing):?\s+' "$dir/CLAUDE.md" 2>/dev/null | head -1 | sed 's/^[^:]*:\s*//' || true)
        if [[ -n "$cmd" ]]; then
            echo "$cmd"
            return 0
        fi
        # Look for backtick-wrapped test commands (must start with a known test runner)
        cmd=$(grep -oP '`(pytest|jest|vitest|npm test|yarn test|bun test|pnpm test|make test|cargo test|go test)[^`]*`' "$dir/CLAUDE.md" 2>/dev/null | head -1 | tr -d '`' || true)
        if [[ -n "$cmd" ]]; then
            echo "$cmd"
            return 0
        fi
    fi

    # 2. Makefile with test target
    if [[ -f "$dir/Makefile" ]]; then
        if grep -qE '^test\s*:' "$dir/Makefile" 2>/dev/null; then
            echo "make test"
            return 0
        fi
    fi

    # 3. package.json with test script
    if [[ -f "$dir/package.json" ]]; then
        local test_script
        test_script=$(python3 -c "
import json, sys
try:
    d = json.load(open('$dir/package.json'))
    s = d.get('scripts', {}).get('test', '')
    if s and 'no test specified' not in s:
        print(s)
except: pass
" 2>/dev/null || true)
        if [[ -n "$test_script" ]]; then
            # Use the appropriate package manager
            if [[ -f "$dir/bun.lockb" ]] || [[ -f "$dir/bun.lock" ]]; then
                echo "bun test"
            elif [[ -f "$dir/pnpm-lock.yaml" ]]; then
                echo "pnpm test"
            elif [[ -f "$dir/yarn.lock" ]]; then
                echo "yarn test"
            else
                echo "npm test"
            fi
            return 0
        fi
    fi

    # 4. pyproject.toml or pytest
    if [[ -f "$dir/pyproject.toml" ]]; then
        if grep -qE '\[tool\.pytest' "$dir/pyproject.toml" 2>/dev/null; then
            echo "pytest"
            return 0
        fi
        if grep -qE 'pytest' "$dir/pyproject.toml" 2>/dev/null; then
            echo "pytest"
            return 0
        fi
    fi
    if [[ -f "$dir/setup.cfg" ]] && grep -qE '\[tool:pytest\]' "$dir/setup.cfg" 2>/dev/null; then
        echo "pytest"
        return 0
    fi
    if [[ -d "$dir/tests" ]] || [[ -d "$dir/test" ]]; then
        if command -v pytest &>/dev/null && [[ -f "$dir/requirements.txt" || -f "$dir/pyproject.toml" || -f "$dir/setup.py" ]]; then
            echo "pytest"
            return 0
        fi
    fi

    # 5. Go
    if [[ -f "$dir/go.mod" ]]; then
        echo "go test ./..."
        return 0
    fi

    # 6. Rust
    if [[ -f "$dir/Cargo.toml" ]]; then
        echo "cargo test"
        return 0
    fi

    return 1
}

# When run directly, detect and print
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    detect_test_command "${1:-.}"
fi
