#!/usr/bin/env bash
#
# run-workflow-loop.sh - scheduled benchmark loop using the Python workflow driver.
#
# OpenCode sibling of run-workflow-loop-cursor.sh. Reuses benchmark/benchmark.env
# and benchmark/requirements/*.md, but drives the Assurance Workflow through the
# deterministic Python driver (`aa workflow run`) instead of asking a single
# main-agent to follow aa-workflow/SKILL.md prose. The driver owns the state
# machine (explore -> report); OpenCode only executes one bounded agent per phase.
#
# One tick:
#   1. Seed intake inputs for each item (.qa.yaml + proposal.md). The driver's
#      full scope starts at `explore` and has NO interactive intake phase, so the
#      requirement must be materialized on disk before the driver runs.
#   2. `aa workflow run --scope full` drives the change to a terminal state,
#      dispatching each phase to OpenCode (opencode adapter, default) or to a
#      spawned `opencode run` per phase (headless adapter).
#   3. The script verifies completion with deterministic `aa status`.
#   4. Completed changes are archived through OpenCode + aa-archive.
#   5. (Optional) retro-nightly collect — meta loop via skills repo driver.
#
# Retro is NOT inlined here by default. Set DO_RETRO=true to restore the legacy
# end-of-loop `aa retro` + agent proposals path (do not enable both DO_RETRO
# and DO_NIGHTLY_COLLECT).
#
# Important: driver/agent exit code is not treated as workflow success. A change
# is archived only when `aa status --change <id> --next --json` reports
# terminal.kind == "completed".
#
# Adapter selection (DRIVER_ADAPTER):
#   opencode  (default) — driver talks to a running OpenCode server over HTTP and
#              dispatches phases to the bounded aa-* agents. Requires a live
#              server at OPENCODE_SERVER (this is the path validated end-to-end).
#   headless           — driver spawns `opencode run` per phase (no server, no
#              bounded agents; uses --dangerously-skip-permissions). Matches the
#              eval wrapper.
#
# Usage:
#   ./benchmark/run-workflow-loop.sh
#   OPENCODE_MODEL=anthropic/claude-sonnet-4 ./benchmark/run-workflow-loop.sh
#   DRIVER_ADAPTER=headless ./benchmark/run-workflow-loop.sh
#   OPENCODE_SERVER=http://127.0.0.1:4096 ./benchmark/run-workflow-loop.sh
#   OPENCODE_MAX_WORKFLOW_ATTEMPTS=4 ./benchmark/run-workflow-loop.sh
#   DO_NIGHTLY_COLLECT=false ./benchmark/run-workflow-loop.sh
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Python migration: skills are synced INTO the SUT project by `aa skill refresh`
# (M7), so archive/retro prompts point at $PROJECT_ROOT/skills, not a TS repo.
AA_SKILLS_ROOT="${AA_SKILLS_ROOT:-$PROJECT_ROOT/skills}"
AA_REPO_ROOT="${AA_REPO_ROOT:-/Users/lvqingquan/agent/assurance-agent}"   # for uv-based aa install
NIGHTLY_CLI="${NIGHTLY_CLI:-aa retro nightly}"
cd "$PROJECT_ROOT"

CONFIG_FILE="${BENCHMARK_ENV:-$SCRIPT_DIR/benchmark.env}"
# shellcheck disable=SC1090
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

RUN_MODE="${RUN_MODE:-full}"
RUN_TESTS="${RUN_TESTS:-true}"
FORCE_CONTINUE="${FORCE_CONTINUE:-false}"
DO_ARCHIVE="${DO_ARCHIVE:-true}"
DO_RETRO="${DO_RETRO:-false}"
DO_RETRO_PROPOSALS="${DO_RETRO_PROPOSALS:-true}"
DO_NIGHTLY_COLLECT="${DO_NIGHTLY_COLLECT:-true}"
RETRO_SINCE_DAYS="${RETRO_SINCE_DAYS:-7}"
STEP_TIMEOUT="${STEP_TIMEOUT:-2700}"

# Clean case/change artifacts before the loop starts so each benchmark run is a
# fresh slate. Only qa/cases + qa/changes by default (preserve qa/archive,
# qa/retro, tests/). Cleaned ONCE at loop start (never between items) so this
# run's archives and unarchived terminal changes survive for retro-nightly.
CLEAN_ARTIFACTS="${CLEAN_ARTIFACTS:-true}"
CLEAN_TARGETS="${CLEAN_TARGETS:-qa/cases qa/changes}"

# Python workflow driver -----------------------------------------------------
AA_BIN="${AA_BIN:-aa}"                          # deterministic CLI (owns the driver)
DRIVER_ADAPTER="${DRIVER_ADAPTER:-opencode}"    # opencode | headless
DRIVER_SCOPE="${DRIVER_SCOPE:-full}"            # full | execute
TEST_TYPES="${TEST_TYPES:-api,e2e}"             # comma-separated layers to cover
MAX_HEALING_ATTEMPTS="${MAX_HEALING_ATTEMPTS:-3}"
OPENCODE_SERVER="${OPENCODE_SERVER:-http://127.0.0.1:4096}"  # opencode adapter only

OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
OPENCODE_MODEL="${OPENCODE_MODEL:-}"
OPENCODE_MAX_WORKFLOW_ATTEMPTS="${OPENCODE_MAX_WORKFLOW_ATTEMPTS:-3}"
# Agent command for retro-nightly: driver appends the prompt as the final argv.
# Override with OPENCODE_NIGHTLY_AGENT if needed.
if [ -z "${OPENCODE_NIGHTLY_AGENT:-}" ]; then
  OPENCODE_NIGHTLY_AGENT="$OPENCODE_BIN run --format json --dangerously-skip-permissions --dir $PROJECT_ROOT"
  [ -n "$OPENCODE_MODEL" ] && OPENCODE_NIGHTLY_AGENT="$OPENCODE_NIGHTLY_AGENT --model $OPENCODE_MODEL"
fi

if [ -z "${BENCHMARK_ITEMS+x}" ] || [ "${#BENCHMARK_ITEMS[@]}" -eq 0 ]; then
  BENCHMARK_ITEMS=(
    "RET-dept-management:requirements/dept-management.md"
    "RET-user-management:requirements/user-management.md"
    "RET-api-management:requirements/api-management.md"
    "RET-role-management:requirements/role-management.md"
    "RET-menu-management:requirements/menu-management.md"
  )
fi

RUNSTAMP="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$SCRIPT_DIR/runs/$RUNSTAMP"
mkdir -p "$RUN_DIR"
LOOP_LOG="$RUN_DIR/loop.log"
SUMMARY="$RUN_DIR/loop-summary.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOOP_LOG"; }

if command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD=(gtimeout "$STEP_TIMEOUT")
elif command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD=(timeout "$STEP_TIMEOUT")
else
  TIMEOUT_CMD=()
  log "WARN: no timeout/gtimeout found - steps run without a wall-clock cap"
fi

clean_generated_artifacts() {
  if [ "$CLEAN_ARTIFACTS" != "true" ]; then
    log "clean: disabled (CLEAN_ARTIFACTS=$CLEAN_ARTIFACTS)"
    return 0
  fi
  local target abspath
  for target in $CLEAN_TARGETS; do
    case "$target" in
      /*|*..*|"")
        log "clean: WARN refusing unsafe target '$target'"
        continue
        ;;
    esac
    abspath="$PROJECT_ROOT/$target"
    if [ -e "$abspath" ]; then
      log "clean: removing $target/ (generated benchmark artifacts)"
      rm -rf "$abspath"
    else
      log "clean: $target/ absent - nothing to remove"
    fi
  done
}

iso_days_ago() {
  local days="$1"
  if date -u -v-1d +%Y >/dev/null 2>&1; then
    date -u -v-"${days}"d +%Y-%m-%dT%H:%M:%S.000Z
  else
    date -u -d "${days} days ago" +%Y-%m-%dT%H:%M:%S.000Z
  fi
}

run_with_timeout() {
  if [ ${#TIMEOUT_CMD[@]} -gt 0 ]; then
    "${TIMEOUT_CMD[@]}" "$@"
  else
    "$@"
  fi
}

# Seed intake inputs for one change. The driver's full scope starts at `explore`
# and has no interactive intake phase, so the requirement must exist on disk as
# proposal.md (+ an autonomous-mode .qa.yaml) before the driver runs. This is the
# deterministic equivalent of what aa-intake writes in the interactive flow.
# $1=change_id $2=base_id (requirement id) $3=requirement text
seed_change() {
  local change_id="$1" base_id="$2" requirement="$3"
  local cdir="$PROJECT_ROOT/qa/changes/$change_id"
  local feature="${base_id#RET-}"
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
  mkdir -p "$cdir"

  cat >"$cdir/.qa.yaml" <<YAML
schema_version: "1.0"
schema: case-driven
created_at: "$now"

change:
  change_id: $change_id
  requirement_id: $base_id
  feature_name: $feature
  status: draft

approval:
  mode: autonomous
  approved_by: aa-workflow
  approved_approach: API + E2E
  approved_at: "$now"
YAML

  {
    echo "# $feature — QA Proposal (benchmark seed)"
    echo
    echo "> Autonomous benchmark seed: intake inputs for the TS driver full-scope"
    echo "> run. The driver starts at \`explore\`; there is no interactive intake"
    echo "> phase, so the requirement is materialized here for the workflow to read."
    echo
    echo "## Requirement"
    echo
    printf '%s\n' "$requirement"
    echo
    echo "## Test Types Considered"
    echo "- API: selected"
    echo "- E2E: selected"
    echo "- Fuzz: declined (benchmark scope)"
    echo "- Performance: declined (benchmark scope)"
    echo
    echo "## Layer Rationale"
    echo "Benchmark autonomous run — API + E2E coverage for $feature."
    echo
    echo "generation_mode: autonomous"
  } >"$cdir/proposal.md"

  log "[$change_id] seeded intake inputs (.qa.yaml + proposal.md, feature=$feature)"
}

# Build the runtime params JSON for the driver (robust quoting via python3).
driver_params_json() {
  TEST_TYPES="$TEST_TYPES" RUN_MODE="$RUN_MODE" RUN_TESTS="$RUN_TESTS" \
  FORCE_CONTINUE="$FORCE_CONTINUE" MAX_HEALING="$MAX_HEALING_ATTEMPTS" \
  python3 -c '
import os, json
print(json.dumps({
    "run_mode": os.environ["RUN_MODE"],
    "test_types": [t for t in os.environ["TEST_TYPES"].split(",") if t],
    "run_tests": os.environ["RUN_TESTS"] == "true",
    "force_continue": os.environ["FORCE_CONTINUE"] == "true",
    "max_healing_attempts": int(os.environ["MAX_HEALING"]),
    "auto_archive": False,
}))'
}

# Run one driver attempt for a change. The driver resumes from the
# workflow-state breakpoint, so a retry after a timeout continues rather than
# restarting (stale driver.lock is auto-recycled when its pid is dead).
# $1=logfile $2=change_id
run_driver() {
  local logf="$1" change_id="$2"
  local params
  params="$(driver_params_json)"

  local -a cmd=(
    "$AA_BIN" workflow run
    --change "$change_id"
    --scope "$DRIVER_SCOPE"
    --adapter "$DRIVER_ADAPTER"
    --params "$params"
  )

  if [ "$DRIVER_ADAPTER" = "opencode" ]; then
    cmd+=(--server "$OPENCODE_SERVER" --directory "$PROJECT_ROOT")
    [ -n "$OPENCODE_MODEL" ] && cmd+=(--model "$OPENCODE_MODEL")
  else
    local agent_cmd="$OPENCODE_BIN run --dir $PROJECT_ROOT --format json --dangerously-skip-permissions"
    [ -n "$OPENCODE_MODEL" ] && agent_cmd="$agent_cmd --model $OPENCODE_MODEL"
    cmd+=(--agent-cmd "$agent_cmd")
  fi

  run_with_timeout "${cmd[@]}" >"$logf" 2>&1
}

# Run one headless OpenCode prompt (archive / retro-proposals stages only).
# $1=logfile, $2=prompt.
run_opencode() {
  local logf="$1" prompt="$2"
  local -a cmd=(
    "$OPENCODE_BIN"
    run
    "$prompt"
    --format json
    --dangerously-skip-permissions
    --dir "$PROJECT_ROOT"
  )
  [ -n "$OPENCODE_MODEL" ] && cmd+=(--model "$OPENCODE_MODEL")

  run_with_timeout "${cmd[@]}" >"$logf" 2>&1
}

status_json_path() {
  local change_id="$1"
  echo "$RUN_DIR/${change_id}.status.json"
}

# `aa status`: 0 running/completed, 20 stopped, 30 needs_human_review,
# 40 command/data error. Terminal control uses JSON, but rc=40 must fail closed.
write_status_snapshot() {
  local change_id="$1"
  local rc=0
  "$AA_BIN" status --change "$change_id" --next --json >"$(status_json_path "$change_id")" 2>>"$LOOP_LOG" || rc=$?
  case "$rc" in 0|20|30) return 0 ;; *) rm -f "$(status_json_path "$change_id")"; return "$rc" ;; esac
}

terminal_kind() {
  local change_id="$1"
  write_status_snapshot "$change_id"
  python3 - "$RUN_DIR/${change_id}.status.json" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("missing")
    raise SystemExit(0)

terminal = data.get("terminal")
if isinstance(terminal, dict):
    print(terminal.get("kind") or "running")
elif terminal:
    print(str(terminal))
else:
    print("running")
PY
}

terminal_reason() {
  local change_id="$1"
  local status_file="$RUN_DIR/${change_id}.status.json"
  [ -f "$status_file" ] || {
    echo ""
    return 0
  }
  python3 - "$status_file" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("")
    raise SystemExit(0)

terminal = data.get("terminal")
if isinstance(terminal, dict):
    print(terminal.get("reason") or "")
else:
    print("")
PY
}

snapshot_unarchived_evidence() {
  local retro_id="$1" change_id="$2"
  local src="qa/changes/${change_id}"
  local dst="qa/retro/${retro_id}/evidence/${change_id}"
  [ -d "$src" ] || return 0
  [ -d "qa/archive/${change_id}" ] && return 0

  mkdir -p "$dst"
  for rel in "events.jsonl" "workflow-state.yaml" "inspect/failure-analysis.json" "healing"; do
    if [ -e "$src/$rel" ]; then
      mkdir -p "$dst/$(dirname "$rel")"
      cp -R "$src/$rel" "$dst/$rel"
    fi
  done
}

execution_final_status() {
  local change_id="$1"
  local manifest="qa/changes/${change_id}/execution/execution-manifest.yaml"
  if [ -f "$manifest" ]; then
    sed -n 's/^final_status:[[:space:]]*//p' "$manifest" | sed -n '1p'
  else
    echo "UNKNOWN"
  fi
}

archive_prompt() {
  local change_id="$1"
  cat <<EOF
Archive benchmark change ${change_id}.

Instructions:
1. Load and follow:
   ${AA_SKILLS_ROOT}/aa-archive/SKILL.md
2. Only archive if the change satisfies the archive contract. If not eligible,
   report the missing phases and do not fabricate archive artifacts.
3. Before ending, confirm whether qa/archive/${change_id}/ exists.
EOF
}

retro_proposals_prompt() {
  local retro_id="$1"
  cat <<EOF
Generate retro proposals for benchmark retro id ${retro_id}.

Instructions:
1. Load and follow:
   ${AA_SKILLS_ROOT}/aa-retro/SKILL.md
2. Read qa/retro/${retro_id}/context.json.
3. Write qa/retro/${retro_id}/proposals.json and
   qa/retro/${retro_id}/retro-summary.md.
4. Do not modify skill files or .aa/memory files.
EOF
}

run_nightly_collect() {
  local collect_log="$RUN_DIR/nightly-collect.log"
  local collect_exit=0
  log "stage 3/3 retro-nightly collect --sut $PROJECT_ROOT ..."
  set +e
  if command -v "$AA_BIN" >/dev/null 2>&1 && "$AA_BIN" retro nightly --help >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    "$AA_BIN" retro nightly collect \
      --sut "$PROJECT_ROOT" \
      --agent "$OPENCODE_NIGHTLY_AGENT" \
      >"$collect_log" 2>&1
    collect_exit=$?
  else
    log "nightly collect: aa retro nightly not available"
    collect_exit=40
  fi
  set -e
  cat "$collect_log" >>"$LOOP_LOG"
  return "$collect_exit"
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
if ! command -v "$OPENCODE_BIN" >/dev/null 2>&1; then
  log "ERROR: opencode binary not found: $OPENCODE_BIN"
  exit 1
fi

# uv-based bootstrap (replaces the TS npm build/link path): install the aa CLI
# from the assurance-agent repo if missing, then materialize skills into the SUT
# so aa-archive/aa-retro prompts can Read $AA_SKILLS_ROOT/<skill>/SKILL.md.
if ! command -v "$AA_BIN" >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1 && [ -d "$AA_REPO_ROOT" ]; then
    log "aa CLI not found - installing via uv from $AA_REPO_ROOT"
    uv tool install --from "$AA_REPO_ROOT" assurance-agent || {
      log "ERROR: uv tool install failed for assurance-agent"; exit 1; }
  fi
fi
if ! command -v "$AA_BIN" >/dev/null 2>&1; then
  log "ERROR: aa CLI not found: $AA_BIN (install with 'uv tool install .' in $AA_REPO_ROOT)"
  exit 1
fi

# Ensure the aa-* skills are present in the SUT for archive/retro prompt Reads.
if [ ! -d "$AA_SKILLS_ROOT" ]; then
  log "materializing aa skills into $AA_SKILLS_ROOT via aa skill refresh"
  ( cd "$PROJECT_ROOT" && "$AA_BIN" skill refresh >/dev/null 2>&1 ) || \
    log "WARN: aa skill refresh failed - archive/retro prompts may miss SKILL.md"
fi

# opencode adapter needs a live server; fail fast with a clear message rather
# than letting every driver phase error out mid-run.
if [ "$DRIVER_ADAPTER" = "opencode" ]; then
  if ! curl -sf -o /dev/null "$OPENCODE_SERVER" 2>/dev/null; then
    log "ERROR: opencode adapter selected but no server reachable at $OPENCODE_SERVER"
    log "       start one (e.g. 'opencode serve --port 4096'), set OPENCODE_SERVER,"
    log "       or run with DRIVER_ADAPTER=headless."
    exit 1
  fi
fi

if [ "$DO_RETRO" = "true" ] && [ "$DO_NIGHTLY_COLLECT" = "true" ]; then
  log "WARN: DO_RETRO and DO_NIGHTLY_COLLECT both true — using legacy DO_RETRO only"
  DO_NIGHTLY_COLLECT="false"
fi

log "opencode benchmark loop start - runstamp=$RUNSTAMP items=${#BENCHMARK_ITEMS[@]}"
log "project_root=$PROJECT_ROOT run_mode=$RUN_MODE run_tests=$RUN_TESTS test_types=$TEST_TYPES"
log "driver: adapter=$DRIVER_ADAPTER scope=$DRIVER_SCOPE max_healing=$MAX_HEALING_ATTEMPTS server=${OPENCODE_SERVER}"
log "opencode=$OPENCODE_BIN model=${OPENCODE_MODEL:-default} max_attempts=$OPENCODE_MAX_WORKFLOW_ATTEMPTS"
log "do_archive=$DO_ARCHIVE do_nightly_collect=$DO_NIGHTLY_COLLECT do_retro=$DO_RETRO"

clean_generated_artifacts

declare -a ROW_RESULTS=()
declare -a RETRO_CHANGE_IDS=()

for item in "${BENCHMARK_ITEMS[@]}"; do
  base_id="${item%%:*}"
  req_rel="${item#*:}"
  req_file="$req_rel"
  [ -f "$req_file" ] || req_file="$SCRIPT_DIR/$req_rel"

  change_id="${base_id}-${RUNSTAMP}"
  if [ ! -f "$req_file" ]; then
    log "SKIP $change_id - requirement file not found: $req_rel"
    ROW_RESULTS+=("$change_id|SKIP|requirement file missing|archived=no")
    continue
  fi

  requirement="$(cat "$req_file")"
  workflow_kind="running"
  workflow_reason=""
  attempt=1

  # Materialize intake inputs once; driver attempts below resume from breakpoint.
  seed_change "$change_id" "$base_id" "$requirement"

  while [ "$attempt" -le "$OPENCODE_MAX_WORKFLOW_ATTEMPTS" ]; do
    wf_log="$RUN_DIR/${change_id}.workflow.attempt-${attempt}.log"
    log "[$change_id] stage 1/2 driver workflow attempt $attempt/$OPENCODE_MAX_WORKFLOW_ATTEMPTS (adapter=$DRIVER_ADAPTER) ..."
    if run_driver "$wf_log" "$change_id"; then
      log "[$change_id] driver attempt $attempt exited 0 (completed)"
    else
      log "[$change_id] driver attempt $attempt exited non-zero (see $(basename "$wf_log"))"
    fi

    workflow_kind="$(terminal_kind "$change_id")"
    workflow_reason="$(terminal_reason "$change_id")"
    log "[$change_id] aa status terminal=$workflow_kind${workflow_reason:+ reason=$workflow_reason}"

    if [ "$workflow_kind" = "completed" ] || [ "$workflow_kind" = "stopped" ]; then
      break
    fi

    attempt=$((attempt + 1))
  done

  final_status="$(execution_final_status "$change_id")"
  archived="no"

  if [ "$workflow_kind" != "completed" ]; then
    log "[$change_id] workflow not complete - skip archive"
    ROW_RESULTS+=("$change_id|$workflow_kind|${workflow_reason:-final_status=$final_status}|archived=no")
    if [ "$workflow_kind" = "stopped" ] && [ "$DO_RETRO" = "true" ]; then
      RETRO_CHANGE_IDS+=("$change_id")
    fi
    continue
  fi

  if [ "$DO_ARCHIVE" = "true" ]; then
    ar_log="$RUN_DIR/${change_id}.archive.jsonl"
    log "[$change_id] stage 2/2 opencode archive ..."
    if run_opencode "$ar_log" "$(archive_prompt "$change_id")"; then
      [ -d "qa/archive/${change_id}" ] && archived="yes"
      log "[$change_id] archive done (archived=$archived)"
    else
      log "[$change_id] archive exited non-zero (see $(basename "$ar_log"))"
    fi
  fi

  ROW_RESULTS+=("$change_id|completed|final_status=$final_status|archived=$archived")
  if [ "$DO_RETRO" = "true" ]; then
    RETRO_CHANGE_IDS+=("$change_id")
  fi
done

retro_id=""
signal_count=""
change_count=""
nightly_collect_exit=""
nightly_review_queue=""
if [ "$DO_NIGHTLY_COLLECT" = "true" ]; then
  run_nightly_collect
  nightly_collect_exit=$?
  if [ "$nightly_collect_exit" = "0" ] || [ "$nightly_collect_exit" = "10" ]; then
    latest_retro="$(ls -1d qa/retro/retro-* 2>/dev/null | sort | tail -1 || true)"
    if [ -n "$latest_retro" ]; then
      retro_id="$(basename "$latest_retro")"
      if [ -f "$latest_retro/context.json" ]; then
        signal_count="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("signal_count",""))' "$latest_retro/context.json" 2>/dev/null || true)"
        change_count="$(python3 -c 'import json,sys; w=json.load(open(sys.argv[1])).get("window",{}); print(w.get("change_count",""))' "$latest_retro/context.json" 2>/dev/null || true)"
      fi
      [ -f "$latest_retro/proposals.json" ] && cp "$latest_retro/proposals.json" "$RUN_DIR/proposals.json"
      [ -f "$latest_retro/retro-summary.md" ] && cp "$latest_retro/retro-summary.md" "$RUN_DIR/retro-summary.md"
      if [ -f "$latest_retro/review-queue.md" ]; then
        nightly_review_queue="$latest_retro/review-queue.md"
        cp "$latest_retro/review-queue.md" "$RUN_DIR/review-queue.md"
      fi
    fi
    if [ "$nightly_collect_exit" = "10" ]; then
      log "nightly collect: no-op (exit 10)"
    else
      log "nightly collect complete: retro_id=${retro_id:-unknown}"
    fi
  else
    log "nightly collect failed (exit $nightly_collect_exit, see nightly-collect.log)"
  fi
elif [ "$DO_RETRO" = "true" ]; then
  retro_id="retro-${RUNSTAMP}"
  retro_json="$RUN_DIR/retro.json"
  if [ "${#RETRO_CHANGE_IDS[@]}" -eq 0 ]; then
    log "legacy retro skipped - no terminal changes"
    retro_id=""
  else
    retro_cmd=("$AA_BIN" retro --retro-id "$retro_id" --json)
    for cid in "${RETRO_CHANGE_IDS[@]}"; do
      snapshot_unarchived_evidence "$retro_id" "$cid"
      retro_cmd+=(--change "$cid")
    done
    log "stage 3/4 aa retro --retro-id $retro_id --change ${RETRO_CHANGE_IDS[*]} ..."
    if "${retro_cmd[@]}" >"$retro_json" 2>>"$LOOP_LOG"; then
      retro_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("retro_id",""))' "$retro_json" 2>/dev/null)"
      signal_count="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("signal_count",""))' "$retro_json" 2>/dev/null)"
      change_count="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("change_count",""))' "$retro_json" 2>/dev/null)"
      log "retro aggregated: retro_id=$retro_id change_count=$change_count signal_count=$signal_count"
    else
      log "aa retro failed (see loop.log)"
    fi
  fi

  if [ "$DO_RETRO_PROPOSALS" = "true" ] && [ -n "$retro_id" ]; then
    rp_log="$RUN_DIR/retro-proposals.jsonl"
    log "stage 4/4 opencode aa-retro proposals for $retro_id ..."
    if run_opencode "$rp_log" "$(retro_proposals_prompt "$retro_id")"; then
      [ -f "qa/retro/${retro_id}/proposals.json" ] && cp "qa/retro/${retro_id}/proposals.json" "$RUN_DIR/proposals.json"
      [ -f "qa/retro/${retro_id}/retro-summary.md" ] && cp "qa/retro/${retro_id}/retro-summary.md" "$RUN_DIR/retro-summary.md"
      log "retro proposals generated (see qa/retro/${retro_id}/)"
    else
      log "opencode retro proposals exited non-zero (see $(basename "$rp_log"))"
    fi
  fi
fi

{
  echo "# OpenCode benchmark loop - $RUNSTAMP"
  echo
  echo "- project: \`$PROJECT_ROOT\`"
  echo "- engine: \`aa workflow run\` (Python driver, adapter=\`$DRIVER_ADAPTER\`)"
  echo "- run_mode: \`$RUN_MODE\` run_tests: \`$RUN_TESTS\` test_types: \`$TEST_TYPES\` force_continue: \`$FORCE_CONTINUE\`"
  echo "- driver scope: \`$DRIVER_SCOPE\` max healing attempts: \`$MAX_HEALING_ATTEMPTS\`"
  echo "- max workflow attempts: \`$OPENCODE_MAX_WORKFLOW_ATTEMPTS\`"
  echo "- nightly collect: \`$DO_NIGHTLY_COLLECT\` (exit: \`${nightly_collect_exit:-n/a}\`)"
  echo "- legacy retro: \`$DO_RETRO\`"
  echo
  echo "## Workflow results"
  echo
  echo "| change_id | terminal | detail | archive |"
  echo "|---|---|---|---|"
  for row in "${ROW_RESULTS[@]}"; do
    IFS='|' read -r cid term detail archive <<<"$row"
    echo "| \`$cid\` | $term | $detail | $archive |"
  done
  echo
  echo "## Retro"
  echo
  if [ -n "$retro_id" ]; then
    echo "- retro_id: \`$retro_id\`"
    echo "- change_count (window): \`$change_count\`"
    echo "- signal_count: \`$signal_count\`"
    [ -f "$RUN_DIR/proposals.json" ] && echo "- proposals: \`benchmark/runs/$RUNSTAMP/proposals.json\`"
    [ -f "$RUN_DIR/retro-summary.md" ] && echo "- summary: \`benchmark/runs/$RUNSTAMP/retro-summary.md\`"
    [ -n "$nightly_review_queue" ] && echo "- review queue: \`benchmark/runs/$RUNSTAMP/review-queue.md\`"
  else
    echo "- (retro disabled, no-op, or failed)"
  fi
  if [ "$DO_NIGHTLY_COLLECT" = "true" ] && [ -n "$nightly_collect_exit" ] && [ "$nightly_collect_exit" != "0" ] && [ "$nightly_collect_exit" != "10" ]; then
    echo "- nightly collect log: \`benchmark/runs/$RUNSTAMP/nightly-collect.log\`"
  fi
  echo
  echo "## Artifacts"
  echo
  echo "- driver logs: \`benchmark/runs/$RUNSTAMP/*.workflow.attempt-*.log\`"
  echo "- opencode logs: \`benchmark/runs/$RUNSTAMP/*.jsonl\`"
  echo "- status snapshots: \`benchmark/runs/$RUNSTAMP/*.status.json\`"
  echo "- loop log: \`benchmark/runs/$RUNSTAMP/loop.log\`"
} >"$SUMMARY"

log "opencode benchmark loop done - summary: $SUMMARY"
echo
cat "$SUMMARY"
