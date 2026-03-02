#!/usr/bin/env bash
# quality-pipeline.sh — Multi-round automated code quality pipeline
# Orchestrates sequential `claude -p` invocations, each with a focused objective,
# test verification, and a clean git commit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROUNDS_DIR="$PLUGIN_DIR/rounds"

# Defaults
BRANCH_PREFIX="quality"
DRY_RUN=false
START_FROM=1
TEST_COMMAND=""
REQUESTED_ROUNDS=()
CONFIG_FILE=""
PROJECT_DIR=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()   { echo -e "${BLUE}[pipeline]${NC} $*"; }
ok()    { echo -e "${GREEN}[pipeline]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pipeline]${NC} $*"; }
err()   { echo -e "${RED}[pipeline]${NC} $*" >&2; }

# --- Resource monitoring ---

GPU_TYPE="none"
MONITOR_PID=""
MONITOR_INTERVAL=60  # seconds between periodic reports
PIPELINE_START_EPOCH=0

detect_gpu() {
    if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
        GPU_TYPE="nvidia"
    elif command -v rocm-smi &>/dev/null; then
        GPU_TYPE="rocm"
    else
        GPU_TYPE="none"
    fi
}

format_duration() {
    local secs="$1"
    if [[ $secs -ge 3600 ]]; then
        printf "%dh %dm %ds" $((secs / 3600)) $((secs % 3600 / 60)) $((secs % 60))
    elif [[ $secs -ge 60 ]]; then
        printf "%dm %ds" $((secs / 60)) $((secs % 60))
    else
        printf "%ds" "$secs"
    fi
}

get_resource_snapshot() {
    local cpu_info mem_info gpu_info=""

    # CPU: load average (instant, cross-platform)
    local ncpu loadavg
    ncpu=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo "?")
    if [[ -f /proc/loadavg ]]; then
        loadavg=$(awk '{print $1}' /proc/loadavg)
    else
        loadavg=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{print $1}')
    fi
    cpu_info="load ${loadavg:-?} (${ncpu} cores)"

    # Memory
    case "$(uname)" in
        Darwin)
            local mem_total_mb page_size used_mb pct
            local pages_active=0 pages_wired=0 pages_compressed=0
            mem_total_mb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1048576 ))
            page_size=$(sysctl -n hw.pagesize 2>/dev/null || echo 4096)
            eval "$(vm_stat 2>/dev/null | awk '
                /Pages active/            {gsub(/\./,"",$NF); print "pages_active="$NF}
                /Pages wired/             {gsub(/\./,"",$NF); print "pages_wired="$NF}
                /occupied by compressor/  {gsub(/\./,"",$NF); print "pages_compressed="$NF}
            ')"
            used_mb=$(( (pages_active + pages_wired + pages_compressed) * page_size / 1048576 ))
            if [[ $mem_total_mb -gt 0 ]]; then
                pct=$(( used_mb * 100 / mem_total_mb ))
                mem_info="${used_mb}/${mem_total_mb} MB (${pct}%)"
            else
                mem_info="?"
            fi
            ;;
        Linux)
            if command -v free &>/dev/null; then
                mem_info=$(free -m 2>/dev/null \
                    | awk '/Mem:/ {if ($2>0) printf "%d/%d MB (%.0f%%)", $3, $2, $3*100/$2; else print "?"}')
            elif [[ -f /proc/meminfo ]]; then
                # Fallback for minimal containers without procps
                mem_info=$(awk '
                    /MemTotal:/     {total=$2}
                    /MemAvailable:/ {avail=$2}
                    END {
                        if (total>0) {
                            used=total-avail;
                            printf "%d/%d MB (%.0f%%)", used/1024, total/1024, used*100/total
                        } else print "?"
                    }
                ' /proc/meminfo)
            else
                mem_info="?"
            fi
            ;;
        *)
            mem_info="?"
            ;;
    esac

    # GPU — only report when utilization > 0
    case "${GPU_TYPE:-none}" in
        nvidia)
            # Query all GPUs; summarize as "GPU0: X% VRAM Y/Z MB, GPU1: ..."
            local gpu_lines
            gpu_lines=$(nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
                --format=csv,noheader,nounits 2>/dev/null) || true
            if [[ -n "$gpu_lines" ]]; then
                local parts=() any_active=false
                while IFS= read -r line; do
                    local idx util mem_used mem_total
                    idx=$(echo "$line" | awk -F', ' '{print $1}' | tr -d ' ')
                    util=$(echo "$line" | awk -F', ' '{print $2}' | tr -d ' ')
                    mem_used=$(echo "$line" | awk -F', ' '{print $3}' | tr -d ' ')
                    mem_total=$(echo "$line" | awk -F', ' '{print $4}' | tr -d ' ')
                    if [[ -n "$util" && "$util" =~ ^[0-9]+$ && "$util" -gt 0 ]]; then
                        any_active=true
                    fi
                    parts+=("GPU${idx}: ${util}% VRAM ${mem_used}/${mem_total} MB")
                done <<< "$gpu_lines"
                if $any_active; then
                    gpu_info="${parts[0]}"
                    for (( gi=1; gi<${#parts[@]}; gi++ )); do
                        gpu_info="${gpu_info}, ${parts[$gi]}"
                    done
                fi
            fi
            ;;
        rocm)
            # Try modern rocm-smi first (ROCm 6+), fall back to legacy
            local rocm_out
            rocm_out=$(rocm-smi --showgpuuse 2>/dev/null || rocm-smi --showuse 2>/dev/null) || true
            if [[ -n "$rocm_out" ]]; then
                local gpu_util
                gpu_util=$(echo "$rocm_out" \
                    | awk '/GPU use|GPU Utilization/ {gsub(/%/,""); for(i=1;i<=NF;i++) if($i+0==$i && $i>0) {print $i; exit}}')
                if [[ -n "$gpu_util" && "$gpu_util" =~ ^[0-9]+$ && "$gpu_util" -gt 0 ]]; then
                    gpu_info="GPU: ${gpu_util}%"
                fi
            fi
            ;;
    esac

    local report="CPU: ${cpu_info} | Mem: ${mem_info}"
    [[ -n "$gpu_info" ]] && report="${report} | ${gpu_info}"
    echo "$report"
}

start_resource_monitor() {
    local round_start_epoch="$1"
    local round_name="$2"
    (
        set +e  # don't exit on errors in monitor subshell
        while true; do
            sleep "$MONITOR_INTERVAL"
            local now elapsed snapshot
            now=$(date +%s)
            elapsed=$(( now - round_start_epoch ))
            snapshot=$(get_resource_snapshot 2>/dev/null) || snapshot="(unavailable)"
            echo -e "${BLUE}[pipeline]${NC}   ⏱ $(format_duration $elapsed) | ${snapshot}"
        done
    ) &
    MONITOR_PID=$!
}

stop_resource_monitor() {
    if [[ -n "${MONITOR_PID:-}" ]]; then
        kill "$MONITOR_PID" 2>/dev/null || true
        wait "$MONITOR_PID" 2>/dev/null || true
        MONITOR_PID=""
    fi
}

log_round_finish() {
    local start_epoch="$1" name="$2" status="$3"
    local elapsed=$(( $(date +%s) - start_epoch ))
    local snapshot
    snapshot=$(get_resource_snapshot 2>/dev/null) || snapshot="(unavailable)"
    log "Round ${BOLD}${name}${NC} ${status} in $(format_duration $elapsed) | ${snapshot}"
}

cleanup_monitor() {
    stop_resource_monitor
}
trap cleanup_monitor EXIT

usage() {
    cat <<'EOF'
Usage: quality-pipeline.sh [OPTIONS]

Options:
  --project-dir DIR        Run in DIR instead of current directory
  --rounds "r1 r2 ..."    Rounds to run (default: all in rounds/ dir)
  --config FILE            Path to pipeline.yaml config
  --start-from N           Start from round N (1-indexed, for resuming)
  --dry-run                Show plan without executing
  --test-command "CMD"     Override auto-detected test command
  -h, --help               Show this help

Examples:
  quality-pipeline.sh
  quality-pipeline.sh --project-dir ~/myproject
  quality-pipeline.sh --rounds "add-tests refactor"
  quality-pipeline.sh --start-from 3
  quality-pipeline.sh --dry-run
  quality-pipeline.sh --test-command "pytest tests/"
EOF
}

# --- Frontmatter parsing ---

# Extract a YAML field from a round file's frontmatter
frontmatter_field() {
    local file="$1" field="$2"
    sed -n '/^---$/,/^---$/p' "$file" | grep -E "^${field}:" | head -1 | sed "s/^${field}:\s*//" | tr -d '"' | tr -d "'"
}

# Extract the prompt body (everything after the closing --- of frontmatter)
round_prompt() {
    local file="$1"
    # Find the line number of the second --- (closing frontmatter delimiter)
    local end_line
    end_line=$(awk '/^---$/ { count++; if (count == 2) { print NR; exit } }' "$file")
    if [[ -n "$end_line" ]]; then
        tail -n +"$((end_line + 1))" "$file"
    else
        cat "$file"
    fi
}

# --- Round discovery ---

# Find all round files, sorted by filename (which encodes order via prefix)
discover_rounds() {
    local -a found=()
    for f in "$ROUNDS_DIR"/*.md; do
        [[ -f "$f" ]] || continue
        found+=("$f")
    done
    # Sort by filename
    IFS=$'\n' sorted=($(sort <<<"${found[*]}")); unset IFS
    echo "${sorted[@]}"
}

# Resolve a round name (e.g. "add-tests") to its file path
resolve_round_file() {
    local name="$1"
    for f in "$ROUNDS_DIR"/*.md; do
        [[ -f "$f" ]] || continue
        local file_name
        file_name=$(frontmatter_field "$f" "name")
        if [[ "$file_name" == "$name" ]]; then
            echo "$f"
            return 0
        fi
    done
    # Try matching by filename pattern
    for f in "$ROUNDS_DIR"/*-"${name}".md "$ROUNDS_DIR"/*"${name}"*.md; do
        [[ -f "$f" ]] && echo "$f" && return 0
    done
    return 1
}

# --- Config loading ---

load_config() {
    local config="$1"
    [[ -f "$config" ]] || return 1

    # Parse YAML config with python (available everywhere, no dependencies)
    eval "$(python3 -c "
import yaml, sys, shlex
try:
    c = yaml.safe_load(open('$config'))
    if c.get('test_command'):
        print(f'CONFIG_TEST_COMMAND={shlex.quote(c[\"test_command\"])}')
    if c.get('rounds'):
        print(f'CONFIG_ROUNDS=({\" \".join(shlex.quote(r) for r in c[\"rounds\"])})')
    if c.get('branch_prefix'):
        print(f'CONFIG_BRANCH_PREFIX={shlex.quote(c[\"branch_prefix\"])}')
    if c.get('max_budget_usd'):
        print(f'CONFIG_MAX_BUDGET={c[\"max_budget_usd\"]}')
    overrides = c.get('overrides', {})
    for name, ov in overrides.items():
        safe = name.replace('-', '_').upper()
        if ov.get('max_budget_usd'):
            print(f'CONFIG_OVERRIDE_{safe}_BUDGET={ov[\"max_budget_usd\"]}')
        if ov.get('append_prompt'):
            print(f'CONFIG_OVERRIDE_{safe}_APPEND={shlex.quote(ov[\"append_prompt\"])}')
except Exception as e:
    print(f'echo \"Warning: failed to parse config: {e}\"', file=sys.stderr)
" 2>/dev/null)" 2>/dev/null || true
}

# --- Test command detection ---

detect_test_command() {
    source "$SCRIPT_DIR/detect-test-command.sh"
    detect_test_command "$(pwd)"
}

# --- Core pipeline ---

run_tests() {
    local test_cmd="$1"
    log "Running tests: $test_cmd"
    if eval "$test_cmd"; then
        ok "Tests passed"
        return 0
    else
        err "Tests failed"
        return 1
    fi
}

run_round() {
    local round_file="$1" round_num="$2" total_rounds="$3" test_cmd="$4"
    local round_start_epoch
    round_start_epoch=$(date +%s)

    local name commit_prefix max_budget max_turns prompt
    name=$(frontmatter_field "$round_file" "name")
    commit_prefix=$(frontmatter_field "$round_file" "commit_message_prefix")
    max_budget=$(frontmatter_field "$round_file" "max_budget_usd")
    max_turns=$(frontmatter_field "$round_file" "max_turns")
    prompt=$(round_prompt "$round_file")

    # Apply config overrides
    local safe_name
    safe_name="$(echo "${name//-/_}" | tr '[:lower:]' '[:upper:]')"
    local override_budget_var="CONFIG_OVERRIDE_${safe_name}_BUDGET"
    local override_append_var="CONFIG_OVERRIDE_${safe_name}_APPEND"
    [[ -n "${!override_budget_var:-}" ]] && max_budget="${!override_budget_var}"
    [[ -n "${!override_append_var:-}" ]] && prompt="$prompt"$'\n\n'"${!override_append_var}"

    # Defaults
    max_budget="${max_budget:-5.00}"
    max_turns="${max_turns:-20}"
    commit_prefix="${commit_prefix:-chore: }"

    echo ""
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "${BOLD}Round $round_num/$total_rounds: $name${NC}"
    log "Budget: \$$max_budget | Max turns: $max_turns"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if $DRY_RUN; then
        log "[DRY RUN] Would run claude -p with ${#prompt} chars of prompt"
        log "[DRY RUN] Would run tests: $test_cmd"
        log "[DRY RUN] Would commit with prefix: ${commit_prefix}"
        return 0
    fi

    # Build system context for this round
    local system_context="You are running as part of an automated quality pipeline.
This is round $round_num of $total_rounds.
The test command for this project is: $test_cmd
After making changes, run the tests to verify nothing is broken.
Do not commit your changes — the pipeline handles commits.
Focus exclusively on the task described in the prompt. Do not do work that belongs to other rounds."

    # Snapshot untracked files before this round (for safe rollback)
    local pre_round_untracked
    pre_round_untracked=$(mktemp)
    git ls-files --others --exclude-standard > "$pre_round_untracked"

    # Log initial resource state and start periodic monitor
    log "Resources: $(get_resource_snapshot)"
    start_resource_monitor "$round_start_epoch" "$name"

    # Run claude -p
    log "Invoking claude..."
    local claude_exit=0
    claude -p "$prompt" \
        --append-system-prompt "$system_context" \
        --dangerously-skip-permissions \
        --max-budget-usd "$max_budget" \
        --max-turns "$max_turns" \
        --output-format json \
        2>&1 | tee /tmp/quality-pipeline-round-${round_num}.log || claude_exit=$?

    # Stop periodic resource monitor
    stop_resource_monitor

    if [[ $claude_exit -ne 0 ]]; then
        err "Claude exited with code $claude_exit in round $round_num ($name)"
        log_round_finish "$round_start_epoch" "$name" "failed"
        return 1
    fi

    # Check if any files changed (tracked modifications or new untracked files)
    if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
        warn "No changes made in round $round_num ($name) — skipping commit"
        log_round_finish "$round_start_epoch" "$name" "no changes"
        return 0
    fi

    # Stage all changes (including new files) before testing
    git add -A

    # Run tests before committing
    if ! run_tests "$test_cmd"; then
        err "Tests failed after round $round_num ($name)"
        err "Rolling back changes from this round..."
        git reset HEAD -- . 2>/dev/null || true
        git checkout -- . 2>/dev/null || true
        # Only remove files that are untracked AND were not present before this round
        if [[ -f "$pre_round_untracked" ]]; then
            git ls-files --others --exclude-standard | while IFS= read -r f; do
                if ! grep -qxF "$f" "$pre_round_untracked"; then
                    rm -f "$f"
                fi
            done
        fi
        log_round_finish "$round_start_epoch" "$name" "tests failed"
        return 1
    fi

    # Commit (already staged above)
    local commit_msg="${commit_prefix}${name} (round ${round_num}/${total_rounds})"
    git commit -m "$commit_msg" --no-gpg-sign 2>/dev/null || git commit -m "$commit_msg"
    ok "Committed: $commit_msg"
    log_round_finish "$round_start_epoch" "$name" "passed"
}

main() {
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --rounds)
                shift
                read -ra REQUESTED_ROUNDS <<< "$1"
                ;;
            --config)
                shift
                CONFIG_FILE="$1"
                ;;
            --start-from)
                shift
                START_FROM="$1"
                ;;
            --project-dir)
                shift
                PROJECT_DIR="$1"
                ;;
            --dry-run)
                DRY_RUN=true
                ;;
            --test-command)
                shift
                TEST_COMMAND="$1"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                err "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
        shift
    done

    # Change to project directory if specified
    if [[ -n "$PROJECT_DIR" ]]; then
        if [[ ! -d "$PROJECT_DIR" ]]; then
            err "Project directory does not exist: $PROJECT_DIR"
            exit 1
        fi
        cd "$PROJECT_DIR"
        log "Working in: $PROJECT_DIR"
    fi

    # Ensure we're in a git repo
    if ! git rev-parse --is-inside-work-tree &>/dev/null; then
        err "Not inside a git repository. Please run from a project directory."
        exit 1
    fi

    # Load config if present
    if [[ -n "$CONFIG_FILE" ]]; then
        load_config "$CONFIG_FILE"
    elif [[ -f ".claude/pipeline.yaml" ]]; then
        log "Found .claude/pipeline.yaml — loading config"
        load_config ".claude/pipeline.yaml"
    fi

    # Apply config values (CLI args take precedence)
    [[ -z "$TEST_COMMAND" && -n "${CONFIG_TEST_COMMAND:-}" ]] && TEST_COMMAND="$CONFIG_TEST_COMMAND"
    [[ ${#REQUESTED_ROUNDS[@]} -eq 0 && -n "${CONFIG_ROUNDS[*]:-}" ]] && REQUESTED_ROUNDS=("${CONFIG_ROUNDS[@]}")
    [[ -n "${CONFIG_BRANCH_PREFIX:-}" ]] && BRANCH_PREFIX="$CONFIG_BRANCH_PREFIX"

    # Detect test command
    if [[ -z "$TEST_COMMAND" ]]; then
        log "Auto-detecting test command..."
        if TEST_COMMAND=$(detect_test_command); then
            ok "Detected test command: $TEST_COMMAND"
        else
            err "Could not auto-detect test command."
            err "Specify with --test-command or add to .claude/pipeline.yaml"
            exit 1
        fi
    else
        log "Using test command: $TEST_COMMAND"
    fi

    # Detect GPU availability
    detect_gpu
    if [[ "$GPU_TYPE" != "none" ]]; then
        log "GPU monitoring: $GPU_TYPE"
    fi

    # Resolve round files
    local -a round_files=()
    if [[ ${#REQUESTED_ROUNDS[@]} -gt 0 ]]; then
        for name in "${REQUESTED_ROUNDS[@]}"; do
            local f
            if f=$(resolve_round_file "$name"); then
                round_files+=("$f")
            else
                err "Unknown round: $name"
                err "Available rounds:"
                for rf in "$ROUNDS_DIR"/*.md; do
                    [[ -f "$rf" ]] && err "  - $(frontmatter_field "$rf" "name")"
                done
                exit 1
            fi
        done
    else
        read -ra round_files <<< "$(discover_rounds)"
    fi

    local total=${#round_files[@]}
    if [[ $total -eq 0 ]]; then
        err "No rounds found."
        exit 1
    fi

    # Create branch
    local branch_name="${BRANCH_PREFIX}/$(date +%Y-%m-%d)-$(git rev-parse --short HEAD)"
    if ! $DRY_RUN; then
        if git show-ref --verify --quiet "refs/heads/$branch_name" 2>/dev/null; then
            log "Branch $branch_name already exists — using it"
            git checkout "$branch_name"
        else
            git checkout -b "$branch_name"
            ok "Created branch: $branch_name"
        fi
    else
        log "[DRY RUN] Would create branch: $branch_name"
    fi

    # Print plan
    echo ""
    log "${BOLD}Quality Pipeline Plan${NC}"
    log "Branch: $branch_name"
    log "Test command: $TEST_COMMAND"
    log "Rounds: $total (starting from $START_FROM)"
    for i in "${!round_files[@]}"; do
        local n=$((i + 1))
        local name
        name=$(frontmatter_field "${round_files[$i]}" "name")
        local budget
        budget=$(frontmatter_field "${round_files[$i]}" "max_budget_usd")
        local marker=""
        [[ $n -lt $START_FROM ]] && marker=" (skip)"
        [[ $n -eq $START_FROM ]] && marker=" ← start"
        log "  $n. $name [\$${budget:-5.00}]$marker"
    done
    echo ""

    if $DRY_RUN; then
        ok "Dry run complete. No changes made."
        exit 0
    fi

    # Run rounds
    PIPELINE_START_EPOCH=$(date +%s)
    local passed=0 failed=0 skipped=0
    for i in "${!round_files[@]}"; do
        local n=$((i + 1))
        if [[ $n -lt $START_FROM ]]; then
            skipped=$((skipped + 1))
            continue
        fi

        if run_round "${round_files[$i]}" "$n" "$total" "$TEST_COMMAND"; then
            passed=$((passed + 1))
        else
            failed=$((failed + 1))
            err "Pipeline stopped at round $n."
            if [[ $n -lt $total ]]; then
                warn "Resume with: quality-pipeline.sh --start-from $((n + 1))"
            fi
            break
        fi
    done

    # Summary
    local pipeline_elapsed=$(( $(date +%s) - PIPELINE_START_EPOCH ))
    echo ""
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "${BOLD}Pipeline Summary${NC}"
    log "Branch: $branch_name"
    log "Total time: $(format_duration $pipeline_elapsed)"
    log "Resources: $(get_resource_snapshot)"
    ok "Passed: $passed"
    [[ $skipped -gt 0 ]] && warn "Skipped: $skipped"
    [[ $failed -gt 0 ]] && err "Failed: $failed"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [[ $failed -gt 0 ]]; then
        exit 1
    fi

    ok "Pipeline complete. Review commits with: git log --oneline ${branch_name}"
}

main "$@"
