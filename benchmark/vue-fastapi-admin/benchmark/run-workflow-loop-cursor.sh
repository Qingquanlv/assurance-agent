#!/usr/bin/env bash
#
# run-workflow-loop-cursor.sh - scheduled benchmark loop using Cursor Agent.
#
# Cursor sibling of run-workflow-loop.sh. Same GraphRuntime driver
# (`aa workflow run` / `resume`), but task handlers are dispatched through
# headless `cursor-agent --print` instead of OpenCode.
#
# One tick:
#   1. Seed intake inputs for each item (.qa.yaml + proposal.md).
#   2. `aa workflow run --entrypoint full|… --adapter headless --agent-cmd …`
#      drives the change to a terminal state (one cursor-agent spawn per task).
#   3. Verify completion with deterministic `aa workflow status`.
#   4. Fold `aa trace --json` and adjudicate `aa verify --json`; persist both
#      outputs and require a pass verdict for benchmark acceptance.
#   5. Archive via GraphRuntime: `aa workflow run --entrypoint archive`
#      (skill:aa-archive + archive-gate; not a free-form agent prompt).
#   6. After every item settles, run one explicit Batch Retro through
#      `aa retro --batch-manifest ...`. Failed/stopped/timed-out items remain
#      members and become typed evidence gaps instead of blocking analysis.
#      Artifacts: context.json, proposal-candidates.json, accept-status.json,
#      retro-summary.md, review-queue.md, plus qa/improvements/*.
#      Legacy `aa retro nightly` / free-form proposals.json prompts are gone.
#
# Process-group hard timeout (run_with_hard_timeout.py) wraps driver / archive /
# retro runs so leftover SUT dev servers started by cursor-agent do not strand
# the loop. Status-poll early kill is retained as a safety net if the driver
# process lingers after a terminal state is already recorded.
#
# Usage:
#   ./benchmark/run-workflow-loop-cursor.sh
#   CURSOR_MODEL=cursor-grok-4.5-high-fast ./benchmark/run-workflow-loop-cursor.sh
#   CURSOR_MAX_WORKFLOW_ATTEMPTS=4 ./benchmark/run-workflow-loop-cursor.sh
#   PROJECT_ROOT=/path/to/vue-fastapi-admin ./benchmark/run-workflow-loop-cursor.sh
#   DO_TRACE_VERIFY=false                   # skip trace/verify collection + gate
#   DO_SPECIALTY_REPORT=false               # skip architecture-specific report + policy replay
#   USE_WORKFLOW_ARCHIVE=false              # legacy free-form archive prompt
#   RESUME_RUNSTAMP=20260713-113457 ./benchmark/run-workflow-loop-cursor.sh
#   DAEMON=1 ./benchmark/run-workflow-loop-cursor.sh   # detach + write PID/log symlinks
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOOP_HELPERS="$SCRIPT_DIR/cursor-loop-helpers.sh"
SPECIALTY_REPORT_PY="$SCRIPT_DIR/benchmark_specialty_report.py"
if [ ! -f "$LOOP_HELPERS" ]; then
  printf 'ERROR: missing %s\n' "$LOOP_HELPERS" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$LOOP_HELPERS"
PROJECT_ROOT="$(resolve_cursor_project_root "$SCRIPT_DIR" "${PROJECT_ROOT:-}")" || {
  printf 'ERROR: invalid PROJECT_ROOT override: %s\n' "${PROJECT_ROOT:-}" >&2
  exit 1
}
# Python migration: skills are synced INTO the SUT project by `aa skill refresh`.
AA_SKILLS_ROOT="${AA_SKILLS_ROOT:-$PROJECT_ROOT/skills}"
# SCRIPT_DIR = <aa-repo>/benchmark/vue-fastapi-admin/benchmark → repo root is ../../..
AA_REPO_ROOT="${AA_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
AA_PYTHON_BIN="${AA_PYTHON_BIN:-}"
RESUME_LOG_DIR="${RESUME_LOG_DIR:-$SCRIPT_DIR/resume-logs}"
AUTO_DECIDE_BENCHMARK="${AUTO_DECIDE_BENCHMARK:-true}"
# When set, log non-terminal / needs-human stalls (does not mutate state).
RECOVER_HEALING_DEADLOCK="${RECOVER_HEALING_DEADLOCK:-true}"
cd "$PROJECT_ROOT"

CONFIG_FILE="${BENCHMARK_ENV:-$SCRIPT_DIR/benchmark.env}"
# shellcheck disable=SC1090
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"

RUN_MODE="${RUN_MODE:-full}"
RUN_TESTS="${RUN_TESTS:-true}"
FORCE_CONTINUE="${FORCE_CONTINUE:-false}"
DO_ARCHIVE="${DO_ARCHIVE:-true}"
# Prefer the GraphRuntime archive entrypoint. Retro has one canonical CLI path.
USE_WORKFLOW_ARCHIVE="${USE_WORKFLOW_ARCHIVE:-true}"
ARCHIVE_ENTRYPOINT="${ARCHIVE_ENTRYPOINT:-archive}"
RETRO_ID="${RETRO_ID:-}"
RETRO_DRY_RUN="${RETRO_DRY_RUN:-false}"
# Benchmark-local deterministic Eval metrics. New names take precedence while
# the legacy regression names remain accepted during configuration migration.
DO_BENCHMARK_EVAL="$(benchmark_eval_setting "${DO_BENCHMARK_EVAL-}" "${DO_EVAL_REGRESSION-}" true)"
BENCHMARK_EVAL_SUITES="$(benchmark_eval_setting \
  "${BENCHMARK_EVAL_SUITES-}" \
  "${EVAL_REGRESSION_SUITES-}" \
  "workflow-run,classification-unit,safety-lite,eval-smoke,case-generation,workflow-case,workflow-api-codegen,workflow-e2e-codegen,workflow-fuzz-codegen,workflow-performance-codegen,workflow-full")"
# Exercise the current traceability/evidence read side for every completed item.
# Both JSON artifacts are retained in RUN_DIR and verify is part of the final
# benchmark acceptance gate (0/pass succeeds; 30/needs_human and 40/fail fail).
DO_TRACE_VERIFY="${DO_TRACE_VERIFY:-true}"
DO_SPECIALTY_REPORT="${DO_SPECIALTY_REPORT:-true}"
EVAL_ENGINE_ROOT="${EVAL_ENGINE_ROOT:-$AA_REPO_ROOT}"   # holds eval/suites + eval/baselines
STEP_TIMEOUT="${STEP_TIMEOUT:-5400}"
STATUS_POLL_INTERVAL="${STATUS_POLL_INTERVAL:-120}"

CLEAN_ARTIFACTS="${CLEAN_ARTIFACTS:-true}"
CLEAN_TARGETS="${CLEAN_TARGETS:-qa/cases qa/changes}"

# Python workflow driver -----------------------------------------------------
AA_BIN="${AA_BIN:-aa}"
# macOS ships /usr/bin/aa (Apple Archive). Prefer the assurance-agent CLI on PATH.
if [ -x "$AA_BIN" ]; then
  AA_BIN_DIR="$(cd "$(dirname "$AA_BIN")" && pwd)"
  export PATH="$AA_BIN_DIR:$PATH"
elif [ -x "$AA_REPO_ROOT/.venv/bin/aa" ]; then
  AA_BIN="$AA_REPO_ROOT/.venv/bin/aa"
  export PATH="$AA_REPO_ROOT/.venv/bin:$PATH"
fi
export AA_BIN
DRIVER_ENTRYPOINT="${DRIVER_ENTRYPOINT:-full}"
TEST_TYPES="${TEST_TYPES:-api,e2e}"
MAX_HEALING_ATTEMPTS="${MAX_HEALING_ATTEMPTS:-3}"

CURSOR_AGENT_BIN="${CURSOR_AGENT_BIN:-cursor-agent}"
CURSOR_MODEL="${CURSOR_MODEL:-}"
CURSOR_MAX_WORKFLOW_ATTEMPTS="${CURSOR_MAX_WORKFLOW_ATTEMPTS:-3}"
CURSOR_AGENT_FORCE="${CURSOR_AGENT_FORCE:-true}"
CURSOR_OUTPUT_FORMAT="${CURSOR_OUTPUT_FORMAT:-stream-json}"

# QA test-runtime endpoints (inherited by the driver → operation:run-tests → pytest).
# The isolated task sandbox excludes db.sqlite3 from tree capture, so the fuzz/api
# isolated_worker would otherwise fall back to an empty DB ("no such table"). Pin
# QA_SQLITE_FILE to the live SUT DB by absolute path so workers read the migrated DB.
export QA_SQLITE_FILE="${QA_SQLITE_FILE:-$PROJECT_ROOT/db.sqlite3}"
export BASE_URL="${BASE_URL:-http://127.0.0.1:9999}"
export E2E_FRONTEND_URL="${E2E_FRONTEND_URL:-http://127.0.0.1:3100}"
# Fuzz schema acquisition: hit the LIVE SUT (from_url) instead of importing the
# app in-process (from_asgi). from_asgi boots the app lifespan → aerich migrate →
# writes migrations/** inside the task sandbox (forbidden_write) AND fuzzes an
# in-process app bound to the sandbox DB, inconsistent with the real-DB seeds.
# Both env names are set because generated fuzz files vary in which they read.
export QA_FUZZ_SCHEMA_MODE="${QA_FUZZ_SCHEMA_MODE:-uri}"
export FUZZ_SCHEMA_MODE="${FUZZ_SCHEMA_MODE:-uri}"

# Default: one item only, so a first end-to-end smoke can finish before scaling up.
# Override with e.g.:
#   BENCHMARK_ITEMS=(
#     "RET-dept-management:requirements/dept-management.md"
#     "RET-user-management:requirements/user-management.md"
#   ) ./benchmark/run-workflow-loop-cursor.sh
if [ -z "${BENCHMARK_ITEMS+x}" ] || [ "${#BENCHMARK_ITEMS[@]}" -eq 0 ]; then
  BENCHMARK_ITEMS=(
    "RET-dept-management:requirements/dept-management.md"
  )
fi

SESSION_STAMP="$(date +%Y%m%d-%H%M%S)"
RUNSTAMP="${RESUME_RUNSTAMP:-$SESSION_STAMP}"
RUN_DIR="$SCRIPT_DIR/runs/$RUNSTAMP-cursor"
mkdir -p "$RUN_DIR" "$RESUME_LOG_DIR"
RETRO_ID="${RETRO_ID:-retro-${RUNSTAMP}-cursor}"
BATCH_MANIFEST="$RUN_DIR/batch-manifest.json"
LOOP_LOG="$RUN_DIR/loop.log"
SUMMARY="$RUN_DIR/loop-summary.md"
TRACK_LOG="$RESUME_LOG_DIR/cursor-loop-${SESSION_STAMP}.log"
TRACK_PID_FILE="$RESUME_LOG_DIR/cursor-loop-latest.pid"
TRACK_LATEST_LOG="$RESUME_LOG_DIR/cursor-loop-latest.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  local line
  line="$(printf '[%s] %s' "$(date +%H:%M:%S)" "$*")"
  printf '%s\n' "$line" | tee -a "$LOOP_LOG" >>"$TRACK_LOG"
}

setup_run_tracking() {
  : >"$TRACK_LOG"
  ln -sfn "$(basename "$TRACK_LOG")" "$TRACK_LATEST_LOG"
  echo "$$" >"$TRACK_PID_FILE"
  log "tracking: $TRACK_LOG (latest → cursor-loop-latest.log)"
}

# Auto-resume the first pending GraphRuntime interrupt (benchmark headless path).
# Also best-effort materializes .aa/data-knowledge.yaml from a proposal if missing.
maybe_auto_decide() {
  local change_id="$1"
  [ "$AUTO_DECIDE_BENCHMARK" = "true" ] || return 1
  if [ ! -f ".aa/data-knowledge.yaml" ]; then
    mkdir -p .aa
    cat >".aa/data-knowledge.yaml" <<'EOF'
version: 1
accounts: {}
auth: {}
entities: {}
capabilities:
  domain_factories: {}
  adapters:
    api: {}
    e2e: {}
    fuzz: {}
    performance: {}
  cleanup: {}
EOF
    log "[$change_id] scaffolded empty .aa/data-knowledge.yaml"
  fi
  if compgen -G "qa/changes/$change_id/plans/data-knowledge.proposal.*.yaml" >/dev/null; then
    if "$AA_BIN" knowledge promote --change "$change_id" --yes; then
      log "[$change_id] promoted data-knowledge proposals into .aa/data-knowledge.yaml"
    fi
  fi
  local status_json interrupt_id action reason
  status_json="$("$AA_BIN" workflow status --change "$change_id" --json 2>/dev/null || true)"
  [ -n "$status_json" ] || return 1
  interrupt_id="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); ints=d.get("pending_interrupts") or []; print((ints[0].get("interrupt_id") or ints[0].get("id") or "") if ints else "")' "$status_json")"
  [ -n "$interrupt_id" ] || return 1
  action="accept_risk"
  reason="benchmark auto resume interrupt $interrupt_id so workflow can complete"
  log "[$change_id] auto resume interrupt=$interrupt_id action=$action"
  "$AA_BIN" workflow resume --change "$change_id" --interrupt "$interrupt_id" --action "$action" --reason "$reason"
}

# Diagnostic only: log stalls that need operator attention. Does not mutate ledger.
recover_dead_end() {
  local change_id="$1"
  [ "$RECOVER_HEALING_DEADLOCK" = "true" ] || return 1
  local kind reason
  kind="$(terminal_kind "$change_id")"
  reason="$(terminal_reason "$change_id")"
  if [ "$kind" = "needs_human_review" ]; then
    log "[$change_id] workflow needs human review${reason:+: $reason}; use aa workflow resume --interrupt …"
  elif [ -z "$kind" ]; then
    log "[$change_id] no terminal yet; preserve evidence and inspect driver error / pending interrupts"
  fi
  return 1
}

HARD_TIMEOUT_PY="$SCRIPT_DIR/run_with_hard_timeout.py"
if [ ! -f "$HARD_TIMEOUT_PY" ]; then
  log "ERROR: missing $HARD_TIMEOUT_PY"
  exit 1
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
      if ! remove_generated_artifact_tree "$abspath"; then
        log "ERROR: clean failed for $target/ after restoring owner write permissions"
        exit 1
      fi
    else
      log "clean: $target/ absent - nothing to remove"
    fi
  done
}

# The codegen/execution phases assume the shared pytest scaffold already exists in
# the SUT repo (tests/config.py, tests/conftest.py, tests/schema_validation.py).
# Verify they are present up front so codegen does not STOP and tests can run.
ensure_test_infra() {
  local missing=()
  local f
  for f in tests/config.py tests/conftest.py tests/schema_validation.py; do
    [ -f "$PROJECT_ROOT/$f" ] || missing+=("$f")
  done
  if [ ${#missing[@]} -ne 0 ]; then
    log "ERROR: missing test infra: ${missing[*]}"
    log "       restore tests/config.py, tests/conftest.py, tests/schema_validation.py"
    log "       (codegen phases STOP and tests cannot run without them)"
    exit 1
  fi
  if [ ! -f "$PROJECT_ROOT/.aa/config.yaml" ]; then
    log "ERROR: missing .aa/config.yaml — run: (cd $PROJECT_ROOT && aa init --yes)"
    log "       (aa run / execution phases fail without it; macOS /usr/bin/aa is unrelated)"
    exit 1
  fi
  log "test-infra: scaffold present"
}

kill_pgid_file() {
  local pgid_file="$1"
  local supervisor_pid="${2:-}"
  [ -f "$pgid_file" ] || return 0
  local pgid
  pgid="$(cat "$pgid_file" 2>/dev/null)"
  [ -n "$pgid" ] || return 0
  kill -TERM "-$pgid" 2>/dev/null
  local waited=0
  while [ "$waited" -lt 10 ] && kill -0 "-$pgid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
  done
  kill -KILL "-$pgid" 2>/dev/null || true
  if [ -n "$supervisor_pid" ] && kill -0 "$supervisor_pid" 2>/dev/null; then
    kill -TERM "$supervisor_pid" 2>/dev/null
    waited=0
    while [ "$waited" -lt 5 ] && kill -0 "$supervisor_pid" 2>/dev/null; do
      sleep 1
      waited=$((waited + 1))
    done
    kill -KILL "$supervisor_pid" 2>/dev/null || true
  fi
}

force_kill_agent_run() {
  local pgid_file="$1" supervisor_pid="$2" reason="$3"
  log "$reason"
  kill_pgid_file "$pgid_file" "$supervisor_pid"
}

# Seed intake inputs for one change (GraphRuntime full entrypoint has no
# interactive intake). Params are passed on `aa workflow run --params`; do not
# pre-write a v1 workflow-state.yaml — the runtime owns the ledger/projection.
# $1=change_id $2=base_id $3=requirement text
seed_change() {
  local change_id="$1" base_id="$2" requirement="$3"
  local cdir="$PROJECT_ROOT/qa/changes/$change_id"
  local feature="${base_id#RET-}"
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
  mkdir -p "$cdir"

  # Derive selected layers from TEST_TYPES so the seed proposal stays a single
  # source of truth with the driver params. Hardcoding all four layers while
  # TEST_TYPES only covers api,e2e made case-design emit no Fuzz/Performance
  # cases, which then hard-rejected at the fuzz/performance plan-review gate.
  local -a _layer_names=(API E2E Fuzz Performance)
  local -a _layer_keys=(api e2e fuzz performance)
  local approach="" i
  for i in "${!_layer_names[@]}"; do
    if [[ ",$TEST_TYPES," == *",${_layer_keys[$i]},"* ]]; then
      approach+="${approach:+ + }${_layer_names[$i]}"
    fi
  done
  [[ -n "$approach" ]] || approach="API + E2E"

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
  approved_approach: $approach
  approved_at: "$now"
YAML

  {
    echo "# $feature — QA Proposal (benchmark seed)"
    echo
    echo "> Autonomous benchmark seed for GraphRuntime \`aa workflow run\`."
    echo "> Intake is non-interactive; the requirement is materialized here for"
    echo "> explore / case-design nodes to read."
    echo
    echo "## Requirement"
    echo
    printf '%s\n' "$requirement"
    echo
    echo "## Test Types Considered"
    for i in "${!_layer_names[@]}"; do
      if [[ ",$TEST_TYPES," == *",${_layer_keys[$i]},"* ]]; then
        echo "- ${_layer_names[$i]}: selected"
      else
        echo "- ${_layer_names[$i]}: declined"
      fi
    done
    echo
    echo "## Layer Rationale"
    echo "Benchmark autonomous run — $approach coverage for $feature."
    echo
    echo "generation_mode: autonomous"
  } >"$cdir/proposal.md"

  log "[$change_id] seeded intake inputs (.qa.yaml + proposal.md, feature=$feature)"
}

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

cursor_agent_cmd_prefix() {
  # Do NOT pin --workspace to PROJECT_ROOT here. HeadlessAdapter.invoke rewrites
  # or injects --workspace to the task-private materialized root so freeze/repair
  # see agent writes. Legacy archive prompt / `aa retro` show still use PROJECT_ROOT.
  local cmd="$CURSOR_AGENT_BIN --print --output-format $CURSOR_OUTPUT_FORMAT --trust"
  [ "$CURSOR_AGENT_FORCE" = "true" ] && cmd="$cmd --force"
  [ -n "$CURSOR_MODEL" ] && cmd="$cmd --model $CURSOR_MODEL"
  printf '%s' "$cmd"
}

# Run one command under hard process-group timeout, optionally polling
# aa status for early kill when poll_change_id is set.
# Args after -- are the command.
run_hard_timeout() {
  local logf="$1"
  local poll_change_id="${2:-}"
  shift 2
  # remaining: command argv

  if [ -z "$poll_change_id" ]; then
    python3 "$HARD_TIMEOUT_PY" "$STEP_TIMEOUT" "$logf" -- "$@"
    return $?
  fi

  local pgid_file="${logf}.pgid"
  rm -f "$pgid_file"

  python3 "$HARD_TIMEOUT_PY" "$STEP_TIMEOUT" "$logf" --pgid-file "$pgid_file" -- "$@" &
  local bg_pid=$!
  local started_at
  started_at=$(date +%s)
  local poll_elapsed=0 check_every=15 kill_reason="" wall_timed_out=false

  while kill -0 "$bg_pid" 2>/dev/null; do
    sleep "$check_every"
    poll_elapsed=$((poll_elapsed + check_every))
    local wall_elapsed=$(( $(date +%s) - started_at ))

    if [ "$wall_elapsed" -ge "$STEP_TIMEOUT" ]; then
      wall_timed_out=true
      kill_reason="[$poll_change_id] bash wall-clock cap STEP_TIMEOUT=${STEP_TIMEOUT}s reached - force killing driver/agent group"
      force_kill_agent_run "$pgid_file" "$bg_pid" "$kill_reason"
      break
    fi

    if [ "$poll_elapsed" -ge "$STATUS_POLL_INTERVAL" ]; then
      poll_elapsed=0
      local kind
      kind="$(terminal_kind "$poll_change_id" 2>/dev/null || echo running)"
      if [ "$kind" = "completed" ] || [ "$kind" = "stopped" ]; then
        kill_reason="[$poll_change_id] status poll: terminal=$kind - stopping driver early (safety net)"
        force_kill_agent_run "$pgid_file" "$bg_pid" "$kill_reason"
        break
      fi
    fi
  done

  wait "$bg_pid"
  local exit_code=$?
  if [ "$wall_timed_out" = true ] && [ "$exit_code" -eq 0 ]; then
    return 124
  fi
  return "$exit_code"
}

# $1=logfile $2=change_id
run_driver() {
  local logf="$1" change_id="$2"
  local params agent_cmd result_json root_state root_id root_entrypoint
  params="$(driver_params_json)"
  agent_cmd="$(cursor_agent_cmd_prefix)"
  result_json="$RUN_DIR/${change_id}.workflow-result.json"
  if root_state="$(read_workflow_root_state "$RUN_DIR" "$change_id" 2>/dev/null)"; then
    IFS='|' read -r root_id root_entrypoint <<<"$root_state"
    run_hard_timeout "$logf" "$change_id" \
      "$AA_BIN" workflow resume \
      --change "$change_id" \
      --invocation "$root_id" \
      --entrypoint "$root_entrypoint" \
      --adapter headless \
      --agent-cmd "$agent_cmd"
    return $?
  fi
  run_hard_timeout "$logf" "$change_id" \
    "$AA_BIN" workflow run \
    --change "$change_id" \
    --entrypoint "$DRIVER_ENTRYPOINT" \
    --adapter headless \
    --params "$params" \
    --agent-cmd "$agent_cmd" \
    --result-json "$result_json"
  local driver_exit=$?
  pin_workflow_root_from_result "$RUN_DIR" "$change_id" "$result_json" "$DRIVER_ENTRYPOINT" \
    || log "[$change_id] WARN: workflow root not pinned from $(basename "$result_json")"
  return "$driver_exit"
}

# One-shot cursor-agent prompt (legacy archive path only).
# $1=logfile $2=prompt
run_cursor_agent() {
  local logf="$1" prompt="$2"
  local -a cmd=(
    "$CURSOR_AGENT_BIN"
    --print
    --output-format "$CURSOR_OUTPUT_FORMAT"
    --workspace "$PROJECT_ROOT"
    --trust
  )
  [ "$CURSOR_AGENT_FORCE" = "true" ] && cmd+=(--force)
  [ -n "$CURSOR_MODEL" ] && cmd+=(--model "$CURSOR_MODEL")
  cmd+=("$prompt")

  run_hard_timeout "$logf" "" "${cmd[@]}"
}

archive_params_json() {
  # Entrypoint archive also injects with.auto_archive=true; keep params explicit.
  python3 -c 'import json; print(json.dumps({"auto_archive": True}))'
}

# GraphRuntime entrypoint run for per-Change operations such as archive.
run_workflow_entrypoint() {
  local logf="$1" change_id="$2" entrypoint="$3" params="$4"
  local agent_cmd
  agent_cmd="$(cursor_agent_cmd_prefix)"
  run_hard_timeout "$logf" "$change_id" \
    "$AA_BIN" workflow run \
    --change "$change_id" \
    --entrypoint "$entrypoint" \
    --adapter headless \
    --params "$params" \
    --agent-cmd "$agent_cmd"
}

# Archive one completed change. Prefer --entrypoint archive; optional legacy prompt.
# $1=change_id  → sets archived=yes|no via caller check of qa/archive/
run_archive_stage() {
  local change_id="$1"
  local ar_log rc=0
  ARCHIVE_LAST_STATUS="ok"
  if [ -d "qa/archive/${change_id}" ]; then
    return 0
  fi
  if [ "$USE_WORKFLOW_ARCHIVE" = "true" ]; then
    ar_log="$RUN_DIR/${change_id}.archive.workflow.log"
    log "[$change_id] archive via workflow --entrypoint $ARCHIVE_ENTRYPOINT ..."
    run_workflow_entrypoint "$ar_log" "$change_id" "$ARCHIVE_ENTRYPOINT" "$(archive_params_json)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    # 20 = graph stopped, i.e. archive-gate refused this change (FAIL execution,
    # unresolved healing, stale failure analysis). A refusal is a verdict, not an
    # infrastructure error, and must not read like a crash in the log.
    if [ "$rc" -eq 20 ]; then
      ARCHIVE_LAST_STATUS="gate-stop"
      log "[$change_id] archive refused by archive-gate (exit 20, see $(basename "$ar_log"))"
      return "$rc"
    fi
    ARCHIVE_LAST_STATUS="error"
    log "[$change_id] archive entrypoint exited $rc (see $(basename "$ar_log"))"
    return "$rc"
  fi
  ar_log="$RUN_DIR/${change_id}.archive.cursor.jsonl"
  log "[$change_id] archive via legacy cursor prompt ..."
  run_cursor_agent "$ar_log" "$(archive_prompt "$change_id")"
}

# Capture artifacts from the exact Retro ID bound to this benchmark Batch.
# Sets: retro_id signal_count change_count retro_review_queue improvement_count.
capture_retro_artifacts() {
  local want_id="$1"
  local latest_retro="qa/retro/$want_id"
  retro_artifacts_complete "$latest_retro" "$RETRO_DRY_RUN" || return 1
  retro_id="$(basename "$latest_retro")"
  if [ -f "$latest_retro/context.json" ]; then
    signal_count="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("signal_count",""))' "$latest_retro/context.json" 2>/dev/null || true)"
    change_count="$(python3 -c 'import json,sys; w=json.load(open(sys.argv[1])).get("window") or {}; ids=w.get("change_ids") or []; print(len(ids) if isinstance(ids,list) else "")' "$latest_retro/context.json" 2>/dev/null || true)"
  fi
  [ -f "$latest_retro/proposal-candidates.json" ] && cp "$latest_retro/proposal-candidates.json" "$RUN_DIR/proposal-candidates.json"
  [ -f "$latest_retro/accept-status.json" ] && cp "$latest_retro/accept-status.json" "$RUN_DIR/accept-status.json"
  [ -f "$latest_retro/retro-status.json" ] && cp "$latest_retro/retro-status.json" "$RUN_DIR/retro-status.json"
  [ -f "$latest_retro/auto-review-summary.json" ] && cp "$latest_retro/auto-review-summary.json" "$RUN_DIR/auto-review-summary.json"
  [ -f "$latest_retro/retro-summary.md" ] && cp "$latest_retro/retro-summary.md" "$RUN_DIR/retro-summary.md"
  if [ -f "$latest_retro/review-queue.md" ]; then
    retro_review_queue="$latest_retro/review-queue.md"
    cp "$latest_retro/review-queue.md" "$RUN_DIR/review-queue.md"
  fi
  if [ -f "qa/improvements/improvements.json" ]; then
    cp "qa/improvements/improvements.json" "$RUN_DIR/improvements.json"
    improvement_count="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get("improvements") or {}))' "$RUN_DIR/improvements.json" 2>/dev/null || true)"
  fi
  [ -f "qa/improvements/review-queue.json" ] && cp "qa/improvements/review-queue.json" "$RUN_DIR/improvement-review-queue.json"
  return 0
}

status_json_path() {
  local change_id="$1"
  echo "$RUN_DIR/${change_id}.status.json"
}

# aa status: 0 running/completed, 20 stopped, 30 needs_human_review,
# 40 failed (or command/data error). Prefer JSON body when present.
write_status_snapshot() {
  local change_id="$1"
  local rc=0
  local out
  out="$(status_json_path "$change_id")"
  "$AA_BIN" status --change "$change_id" --next --json >"$out" 2>>"$LOOP_LOG" || rc=$?
  case "$rc" in
    0|20|30|40)
      # Keep the snapshot when JSON parsed a known status (incl. failed→40).
      if python3 - "$out" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1])).get("status")
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if s in {"running", "completed", "stopped", "needs_human_review", "failed", "interrupted"} else 1)
PY
      then
        return 0
      fi
      rm -f "$out"
      return "$rc"
      ;;
    *)
      rm -f "$out"
      return "$rc"
      ;;
  esac
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
    # Flat GraphRuntime shape from `aa status --json` / `aa workflow status --json`.
    status = data.get("status")
    if status in ("completed", "stopped", "needs_human_review", "failed", "running"):
        print(status)
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
    print(data.get("terminal_reason") or "")
PY
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
2. You are in Cursor Agent. Do not call opencode.
3. Only archive if the change satisfies the archive contract. If not eligible,
   report the missing phases and do not fabricate archive artifacts.
4. Before ending, confirm whether qa/archive/${change_id}/ exists.
EOF
}

# Cross-change Retro closed loop through the canonical explicit Batch CLI.
run_retro_collect() {
  local collect_log="$RUN_DIR/retro-collect.log"
  local collect_exit=0
  log "stage 3/3 retro via explicit Batch manifest=$BATCH_MANIFEST ..."
  local agent_cmd
  agent_cmd="$(cursor_agent_cmd_prefix)"
  : >"$collect_log"
  run_batch_retro \
    "$AA_BIN" "$BATCH_MANIFEST" "$RETRO_ID" "$agent_cmd" "$RETRO_DRY_RUN" "$collect_log"
  collect_exit=$?
  cat "$collect_log" >>"$LOOP_LOG" || true
  return "$collect_exit"
}

record_item_result() {
  local change_id="$1" terminal="$2" detail="$3" archive_field="$4"
  local evidence_path="qa/changes/$change_id/events.jsonl"
  local batch_status availability outcome
  if [ -s "qa/archive/$change_id/events.jsonl" ]; then
    evidence_path="qa/archive/$change_id/events.jsonl"
  fi
  outcome="$(retro_batch_member_outcome "$terminal" "$evidence_path")"
  IFS='|' read -r batch_status availability <<<"$outcome"
  update_retro_batch_member "$BATCH_MANIFEST" "$change_id" "$batch_status" "$availability" || {
    log "ERROR: failed to update Batch member $change_id ($batch_status/$availability)"
    exit 1
  }
  ROW_RESULTS+=("$change_id|$terminal|$detail|$archive_field")
}

run_trace_verify_stage() {
  local change_id="$1" row
  local trace_file="$RUN_DIR/${change_id}.trace.json"
  local verify_file="$RUN_DIR/${change_id}.verify.json"
  local evidence_log="$RUN_DIR/${change_id}.trace-verify.log"
  local cid collection_status reason_code trace_exit integrity gap_count
  local verify_exit verdict blocking insufficient

  [ "$DO_TRACE_VERIFY" = "true" ] || return 0
  row="$(collect_trace_verify_evidence \
    "$AA_BIN" "$change_id" "$trace_file" "$verify_file" "$evidence_log" "$AA_PYTHON_BIN")"
  EVIDENCE_ROWS+=("$row")
  IFS='|' read -r \
    cid collection_status reason_code trace_exit integrity gap_count \
    verify_exit verdict blocking insufficient \
    <<<"$row"
  log "[$change_id] trace/verify: status=$collection_status trace_exit=$trace_exit integrity=$integrity gaps=$gap_count verify_exit=$verify_exit verdict=$verdict blocking=$blocking insufficient=$insufficient"
  case "$collection_status" in
    raw)
      if [ "$integrity" = "unknown" ] || [ "$gap_count" = "unknown" ] \
        || [ "$verdict" = "unknown" ] || [ "$blocking" = "unknown" ] \
        || [ "$insufficient" = "unknown" ]; then
        SPECIALTY_REPORT_FAILED="true"
      fi
      ;;
  esac
}

run_specialty_report_stage() {
  local change_id="$1" evidence_row cid collection_status reason_code
  local trace_exit integrity gap_count verify_exit verdict blocking insufficient
  local trace_file="$RUN_DIR/${change_id}.trace.json"
  local verify_file="$RUN_DIR/${change_id}.verify.json"
  local report_file="$RUN_DIR/${change_id}.specialty-report.json"
  local report_log="$RUN_DIR/${change_id}.specialty-report.log"
  local root_state root_invocation_id workflow_entrypoint collect_exit finalize_out registered
  local attempt_id report_path specialty_row replaced
  local -a next_rows=()

  [ "$DO_SPECIALTY_REPORT" = "true" ] || return 0
  trace_exit=""
  verify_exit=""
  for evidence_row in "${EVIDENCE_ROWS[@]}"; do
    IFS='|' read -r \
      cid collection_status reason_code trace_exit integrity gap_count \
      verify_exit verdict blocking insufficient \
      <<<"$evidence_row"
    if [ "$cid" = "$change_id" ]; then
      break
    fi
    trace_exit=""
    verify_exit=""
  done
  if [ -z "$trace_exit" ] || [ -z "$verify_exit" ]; then
    SPECIALTY_REPORT_FAILED="true"
    log "[$change_id] ERROR: trace/verify command status missing before specialty collection"
    return 1
  fi
  root_invocation_id=""
  workflow_entrypoint="$DRIVER_ENTRYPOINT"
  if root_state="$(read_workflow_root_state "$RUN_DIR" "$change_id" 2>/dev/null)"; then
    IFS='|' read -r root_invocation_id workflow_entrypoint <<<"$root_state"
  fi
  attempt_id="${change_id}-$(date +%s)-$$"
  collect_exit=0
  collect_benchmark_specialty_report \
    "$AA_PYTHON_BIN" "$SPECIALTY_REPORT_PY" "$PROJECT_ROOT" \
    "$change_id" "$trace_file" "$verify_file" "$report_file" "$report_log" \
    "$trace_exit" "$verify_exit" "$root_invocation_id" "$workflow_entrypoint" \
    "$attempt_id" \
    || collect_exit=$?
  finalize_out="$(finalize_benchmark_specialty_report \
    "$AA_PYTHON_BIN" "$SPECIALTY_REPORT_PY" "$change_id" "$report_file" "$collect_exit" "$attempt_id")"
  registered="${finalize_out##*$'\n'}"
  registered="${registered#registered=}"
  if [ "$registered" = "true" ]; then
    report_path="$(printf '%s\n' "$finalize_out" | sed -n '1p')"
    specialty_row="$(printf '%s\n' "$finalize_out" | sed -n '2p')"
    SPECIALTY_REPORT_FILES+=("$report_path")
    while IFS= read -r replaced; do
      [ -n "$replaced" ] || continue
      next_rows+=("$replaced")
    done < <(replace_evidence_row_for_change "$change_id" "$specialty_row" "${EVIDENCE_ROWS[@]+"${EVIDENCE_ROWS[@]}"}")
    EVIDENCE_ROWS=("${next_rows[@]}")
    IFS='|' read -r \
      cid collection_status reason_code trace_exit integrity gap_count \
      verify_exit verdict blocking insufficient \
      <<<"$specialty_row"
    case "$collection_status" in
      incomplete)
        SPECIALTY_REPORT_FAILED="true"
        ;;
    esac
    log "[$change_id] specialty report: $(basename "$report_path") status=$collection_status reason=$reason_code"
  fi
  if [ "$collect_exit" -ne 0 ]; then
    SPECIALTY_REPORT_FAILED="true"
    log "[$change_id] ERROR: specialty report/policy replay failed (see $(basename "$report_log"))"
    return 1
  fi
}

reuse_specialty_report_stage() {
  local change_id="$1" row
  local report_file="$RUN_DIR/${change_id}.specialty-report.json"
  local cid collection_status reason_code trace_exit integrity gap_count
  local verify_exit verdict blocking insufficient
  local -a next_rows=()
  local replaced

  if ! row="$(reuse_benchmark_specialty_report \
    "$AA_PYTHON_BIN" "$SPECIALTY_REPORT_PY" "$change_id" "$report_file")"; then
    SPECIALTY_REPORT_FAILED="true"
    log "[$change_id] ERROR: frozen specialty report is invalid: $(basename "$report_file")"
    return 1
  fi
  SPECIALTY_REPORT_FILES+=("$report_file")
  while IFS= read -r replaced; do
    [ -n "$replaced" ] || continue
    next_rows+=("$replaced")
  done < <(replace_evidence_row_for_change "$change_id" "$row" "${EVIDENCE_ROWS[@]+"${EVIDENCE_ROWS[@]}"}")
  EVIDENCE_ROWS=("${next_rows[@]}")
  IFS='|' read -r \
    cid collection_status reason_code trace_exit integrity gap_count \
    verify_exit verdict blocking insufficient \
    <<<"$row"
  case "$collection_status" in
    incomplete)
      SPECIALTY_REPORT_FAILED="true"
      ;;
    complete|legacy_unlayered)
      # Trace integrity incomplete or non-pass verify marks the specialty stage failed.
      if [ "$integrity" = "incomplete" ] || [ "$verdict" != "pass" ]; then
        SPECIALTY_REPORT_FAILED="true"
      fi
      ;;
  esac
  # Capability replay incomplete is encoded in report_collection_exit / evidence-row exit.
  if ! "$AA_PYTHON_BIN" "$SPECIALTY_REPORT_PY" evidence-row \
    --change-id "$change_id" "$report_file" >/dev/null 2>&1; then
    SPECIALTY_REPORT_FAILED="true"
  fi
  log "[$change_id] reused frozen specialty and trace/verify evidence status=$collection_status reason=$reason_code"
}

# Deterministic benchmark metrics over golden fixtures. This is observational:
# suite verdicts are reported but do not alter the workflow/archive gate.
declare -a BENCHMARK_EVAL_ROWS=()
run_benchmark_eval() {
  local eval_log="$RUN_DIR/benchmark-eval.log"
  local row suite verdict run_id
  : >"$eval_log"
  if [ ! -d "$EVAL_ENGINE_ROOT/eval/suites" ]; then
    log "benchmark eval: no eval/suites under $EVAL_ENGINE_ROOT — skipped"
    return 0
  fi
  log "stage: benchmark eval metrics suites=[$BENCHMARK_EVAL_SUITES] engine=$EVAL_ENGINE_ROOT sut=$PROJECT_ROOT"
  while IFS= read -r row; do
    [ -n "$row" ] || continue
    BENCHMARK_EVAL_ROWS+=("$row")
    IFS='|' read -r suite verdict run_id <<<"$row"
    log "benchmark-eval[$suite]: verdict=$verdict run_id=$run_id"
  done < <(collect_benchmark_eval_rows \
    "$AA_BIN" "$EVAL_ENGINE_ROOT" "$PROJECT_ROOT" "$BENCHMARK_EVAL_SUITES" "$eval_log")
  return 0
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
if [ "${DAEMON:-}" = "1" ]; then
  exec python3 "$SCRIPT_DIR/resume-logs/daemonize-loop.py" "$@"
fi

if ! command -v "$CURSOR_AGENT_BIN" >/dev/null 2>&1; then
  log "ERROR: cursor agent binary not found: $CURSOR_AGENT_BIN"
  exit 1
fi

# uv-based bootstrap (replaces the TS npm build/link path).
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
if [ "$DO_SPECIALTY_REPORT" = "true" ] && [ "$DO_TRACE_VERIFY" != "true" ]; then
  log "ERROR: DO_SPECIALTY_REPORT=true requires DO_TRACE_VERIFY=true"
  exit 1
fi
if [ "$DO_SPECIALTY_REPORT" = "true" ] && [ ! -f "$SPECIALTY_REPORT_PY" ]; then
  log "ERROR: missing specialty reporter: $SPECIALTY_REPORT_PY"
  exit 1
fi
if [ "$DO_TRACE_VERIFY" = "true" ] || [ "$DO_SPECIALTY_REPORT" = "true" ]; then
  if ! AA_PYTHON_BIN="$(resolve_aa_python_bin "$AA_BIN" "$AA_PYTHON_BIN")"; then
    log "ERROR: cannot resolve the Python interpreter backing $AA_BIN"
    exit 1
  fi
fi

if [ ! -d "$AA_SKILLS_ROOT" ]; then
  log "materializing aa skills into $AA_SKILLS_ROOT via aa skill refresh"
  ( cd "$PROJECT_ROOT" && "$AA_BIN" skill refresh >/dev/null 2>&1 ) || \
    log "WARN: aa skill refresh failed - archive/retro prompts may miss SKILL.md"
fi

log "cursor benchmark loop start - runstamp=$RUNSTAMP items=${#BENCHMARK_ITEMS[@]}${RESUME_RUNSTAMP:+ (resume)}"
log "project_root=$PROJECT_ROOT run_mode=$RUN_MODE run_tests=$RUN_TESTS test_types=$TEST_TYPES"
log "driver: adapter=headless entrypoint=$DRIVER_ENTRYPOINT max_healing=$MAX_HEALING_ATTEMPTS"
log "cursor_agent=$CURSOR_AGENT_BIN model=${CURSOR_MODEL:-default} max_attempts=$CURSOR_MAX_WORKFLOW_ATTEMPTS"
log "do_archive=$DO_ARCHIVE use_workflow_archive=$USE_WORKFLOW_ARCHIVE entrypoint=$ARCHIVE_ENTRYPOINT"
log "retro: canonical_batch_cli id=$RETRO_ID manifest=$BATCH_MANIFEST dry_run=$RETRO_DRY_RUN"
log "do_benchmark_eval=$DO_BENCHMARK_EVAL suites=[$BENCHMARK_EVAL_SUITES]"
log "do_trace_verify=$DO_TRACE_VERIFY"
log "do_specialty_report=$DO_SPECIALTY_REPORT"
log "auto_decide=$AUTO_DECIDE_BENCHMARK recover_healing=$RECOVER_HEALING_DEADLOCK"

setup_run_tracking

declare -a BATCH_CHANGE_IDS=()
for item in "${BENCHMARK_ITEMS[@]}"; do
  base_id="${item%%:*}"
  BATCH_CHANGE_IDS+=("${base_id}-${RUNSTAMP}-cursor")
done
if ! initialize_retro_batch_manifest "$BATCH_MANIFEST" "$RUNSTAMP" "${BATCH_CHANGE_IDS[@]}"; then
  log "ERROR: Batch manifest identity/membership mismatch: $BATCH_MANIFEST"
  exit 1
fi
log "retro_batch: id=$RUNSTAMP manifest=$BATCH_MANIFEST members=${#BATCH_CHANGE_IDS[@]}"

if [ -n "${RESUME_RUNSTAMP:-}" ] && [ "$CLEAN_ARTIFACTS" = "true" ]; then
  log "resume mode: skip clean (preserve in-flight changes)"
else
  clean_generated_artifacts
fi
ensure_test_infra

declare -a ROW_RESULTS=()
declare -a EVIDENCE_ROWS=()
declare -a SPECIALTY_REPORT_FILES=()
SPECIALTY_REPORT_FAILED="false"
item_idx=0
total_items=${#BENCHMARK_ITEMS[@]}

for item in "${BENCHMARK_ITEMS[@]}"; do
  item_idx=$((item_idx + 1))
  base_id="${item%%:*}"
  req_rel="${item#*:}"
  req_file="$req_rel"
  [ -f "$req_file" ] || req_file="$SCRIPT_DIR/$req_rel"

  change_id="${base_id}-${RUNSTAMP}-cursor"
  log "[$item_idx/$total_items] item=$base_id change_id=$change_id"
  if [ ! -f "$req_file" ]; then
    log "SKIP $change_id - requirement file not found: $req_rel"
    record_item_result "$change_id" "not_started" "requirement file missing" "archived=no"
    continue
  fi

  requirement="$(cat "$req_file")"
  workflow_kind="running"
  workflow_reason=""
  driver_exit=0
  attempt=1

  if [ -d "qa/changes/$change_id" ]; then
    workflow_kind="$(terminal_kind "$change_id")"
    log "[$change_id] resume existing change terminal=$workflow_kind"
  else
    seed_change "$change_id" "$base_id" "$requirement"
  fi

  if [ "$workflow_kind" = "completed" ]; then
    log "[$change_id] already completed — skip driver"
    if [ "$DO_SPECIALTY_REPORT" = "true" ]; then
      specialty_report="$RUN_DIR/${change_id}.specialty-report.json"
      if specialty_action="$(benchmark_specialty_resume_action \
        "$specialty_report" "qa/archive/$change_id")"; then
        if [ "$specialty_action" = "reuse" ]; then
          reuse_specialty_report_stage "$change_id" || true
        else
          run_trace_verify_stage "$change_id"
          run_specialty_report_stage "$change_id" || true
        fi
      else
        SPECIALTY_REPORT_FAILED="true"
        log "[$change_id] ERROR: specialty evidence missing after archive; refusing post-archive collection"
      fi
    else
      run_trace_verify_stage "$change_id"
    fi
    final_status="$(execution_final_status "$change_id")"
    archived="no"
    if [ -d "qa/archive/$change_id" ]; then
      archived="yes"
    elif benchmark_should_run_archive "$DO_ARCHIVE" "$workflow_kind" "$final_status"; then
      log "[$change_id] stage 2/2 archive (resume) ..."
      if run_archive_stage "$change_id"; then
        [ -d "qa/archive/${change_id}" ] && archived="yes"
      elif [ "${ARCHIVE_LAST_STATUS:-}" = "gate-stop" ]; then
        archived="no (archive-gate stop)"
      fi
      log "[$change_id] archive done (archived=$archived)"
    elif [ "$DO_ARCHIVE" = "true" ]; then
      archived="skipped (final_status=$final_status)"
      log "[$change_id] archive skipped because execution final_status=$final_status"
    fi
    record_item_result "$change_id" "completed" "final_status=$final_status" "archived=$archived"
    continue
  fi

  if [ "$workflow_kind" = "stopped" ]; then
    log "[$change_id] already stopped — skip driver"
    record_item_result "$change_id" "stopped" "$(terminal_reason "$change_id")" "archived=no"
    continue
  fi

  if [ "$workflow_kind" = "failed" ]; then
    log "[$change_id] already failed — persisted terminal cannot be restarted"
    record_item_result "$change_id" "failed" "$(terminal_reason "$change_id")" "archived=no"
    continue
  fi

  recover_dead_end "$change_id" || true
  workflow_kind="$(terminal_kind "$change_id")"

  while [ "$attempt" -le "$CURSOR_MAX_WORKFLOW_ATTEMPTS" ]; do
    wf_log="$RUN_DIR/${change_id}.workflow.attempt-${attempt}.cursor.log"
    log "[$change_id] stage 1/2 driver workflow attempt $attempt/$CURSOR_MAX_WORKFLOW_ATTEMPTS (adapter=headless/cursor-agent) ..."
    driver_exit=0
    if run_driver "$wf_log" "$change_id"; then
      log "[$change_id] driver attempt $attempt exited 0 (completed)"
    else
      driver_exit=$?
      log "[$change_id] driver attempt $attempt exited $driver_exit (see $(basename "$wf_log"))"
    fi

    workflow_kind="$(terminal_kind "$change_id")"
    workflow_reason="$(terminal_reason "$change_id")"
    if [ "$driver_exit" -eq 124 ] && ! workflow_attempts_should_stop "$workflow_kind"; then
      workflow_kind="hard_timeout"
      workflow_reason="STEP_TIMEOUT=${STEP_TIMEOUT}s"
    fi
    log "[$change_id] aa status terminal=$workflow_kind${workflow_reason:+ reason=$workflow_reason}"

    if workflow_attempts_should_stop "$workflow_kind" || [ "$workflow_kind" = "hard_timeout" ]; then
      python3 - "$change_id" <<'PYASSERT' || true
import json, sys
from pathlib import Path
cid = sys.argv[1]
events = Path(f"qa/changes/{cid}/events.jsonl")
if not events.is_file():
    raise SystemExit(0)
success = {}
for line in events.read_text().splitlines():
    if not line.strip():
        continue
    ev = json.loads(line)
    if ev.get("type") == "task_attempt_succeeded":
        tid = ev.get("task_id")
        if tid:
            success[tid] = success.get(tid, 0) + 1
dupes = [t for t, n in success.items() if n > 1]
if dupes:
    print(f"WARNING: duplicate successful task ids across restarts: {dupes[:5]}", file=sys.stderr)
    raise SystemExit(1)
PYASSERT
      break
    fi

    maybe_auto_decide "$change_id" || true
    recover_dead_end "$change_id" || true

    attempt=$((attempt + 1))
  done

  final_status="$(execution_final_status "$change_id")"
  archived="no"

  if [ "$workflow_kind" != "completed" ]; then
    log "[$change_id] workflow not complete - skip archive"
    record_item_result \
      "$change_id" "$workflow_kind" "${workflow_reason:-final_status=$final_status}" "archived=no"
    continue
  fi

  run_trace_verify_stage "$change_id"
  run_specialty_report_stage "$change_id" || true

  if benchmark_should_run_archive "$DO_ARCHIVE" "$workflow_kind" "$final_status"; then
    log "[$change_id] stage 2/2 archive ..."
    if run_archive_stage "$change_id"; then
      [ -d "qa/archive/${change_id}" ] && archived="yes"
      log "[$change_id] archive done (archived=$archived)"
    elif [ "${ARCHIVE_LAST_STATUS:-}" = "gate-stop" ]; then
      archived="no (archive-gate stop)"
    else
      log "[$change_id] archive exited non-zero"
    fi
  elif [ "$DO_ARCHIVE" = "true" ]; then
    archived="skipped (final_status=$final_status)"
    log "[$change_id] archive skipped because execution final_status=$final_status"
  fi

  record_item_result "$change_id" "completed" "final_status=$final_status" "archived=$archived"
done

retro_id="$RETRO_ID"
signal_count=""
change_count=""
improvement_count=""
retro_result="technical_failure"
retro_batch_id="$RUNSTAMP"
retro_improvement_ids=""
retro_outbox_id=""
retro_collect_exit=""
retro_review_queue=""
run_retro_collect
retro_collect_exit=$?
if capture_retro_artifacts "$RETRO_ID"; then
  retro_status_file="$RUN_DIR/retro-status.json"
  retro_result="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result","technical_failure"))' "$retro_status_file")"
  retro_batch_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("batch_id") or "")' "$retro_status_file")"
  retro_improvement_ids="$(python3 -c 'import json,sys; print(",".join(json.load(open(sys.argv[1])).get("improvement_ids") or []))' "$retro_status_file")"
  retro_outbox_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("outbox_id") or "")' "$retro_status_file")"
  log "retro complete: result=$retro_result retro_id=$retro_id batch_id=$retro_batch_id signal_count=${signal_count:-?} change_count=${change_count:-?}"
else
  log "retro technical failure: exit=$retro_collect_exit status artifact missing/invalid (see retro-collect.log)"
fi

if [ "$DO_BENCHMARK_EVAL" = "true" ]; then
  run_benchmark_eval
fi

{
  echo "# Cursor benchmark loop - $RUNSTAMP"
  echo
  echo "- project: \`$PROJECT_ROOT\`"
  echo "- engine: \`aa workflow run --adapter headless\` + \`cursor-agent\`"
  echo "- run_mode: \`$RUN_MODE\` run_tests: \`$RUN_TESTS\` test_types: \`$TEST_TYPES\` force_continue: \`$FORCE_CONTINUE\`"
  echo "- driver entrypoint: \`$DRIVER_ENTRYPOINT\` max healing attempts: \`$MAX_HEALING_ATTEMPTS\`"
  echo "- max workflow attempts: \`$CURSOR_MAX_WORKFLOW_ATTEMPTS\`"
  echo "- archive: \`DO_ARCHIVE=$DO_ARCHIVE\` via \`$([ "$USE_WORKFLOW_ARCHIVE" = "true" ] && echo "workflow:$ARCHIVE_ENTRYPOINT" || echo "legacy-cursor-prompt")\`"
  echo "- retro: \`aa retro --batch-manifest\` (exit: \`${retro_collect_exit:-n/a}\`)"
  echo "- batch manifest: \`benchmark/runs/$RUNSTAMP-cursor/batch-manifest.json\`"
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
  echo "## Retro Batch"
  echo
  echo "| change_id | execution_status | evidence_availability |"
  echo "|---|---|---|"
  python3 - "$BATCH_MANIFEST" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
for member in payload["members"]:
    print(
        f"| `{member['change_id']}` | {member['execution_status']} | "
        f"{member['evidence_availability']} |"
    )
PY
  if [ "$DO_TRACE_VERIFY" = "true" ]; then
    echo
    echo "## Trace / Verify Evidence"
    echo
    echo "| change_id | collection_status | reason_code | trace exit | integrity | gaps | verify exit | verdict | blocking gaps | insufficient |"
    echo "|---|---|---|---:|---|---:|---:|---|---:|---:|"
    for row in "${EVIDENCE_ROWS[@]}"; do
      IFS='|' read -r \
        cid collection_status reason_code trace_exit integrity gap_count \
        verify_exit verdict blocking insufficient <<<"$row"
      echo "| \`$cid\` | $collection_status | $reason_code | $trace_exit | $integrity | $gap_count | $verify_exit | $verdict | $blocking | $insufficient |"
    done
  fi
  if [ "$DO_SPECIALTY_REPORT" = "true" ]; then
    echo
    if [ "${#SPECIALTY_REPORT_FILES[@]}" -gt 0 ]; then
      if ! render_benchmark_specialty_sections \
        "$AA_PYTHON_BIN" "$SPECIALTY_REPORT_PY" "${SPECIALTY_REPORT_FILES[@]}"; then
        SPECIALTY_REPORT_FAILED="true"
        echo "## Capability + Contract + Policy"
        echo
        echo "Specialty report rendering failed; inspect per-item logs."
        echo
        echo "## Traceability / Evidence Projection"
        echo
        echo "Specialty report rendering failed; inspect per-item logs."
      fi
    else
      echo "## Capability + Contract + Policy"
      echo
      echo "No completed item produced specialty evidence."
      echo
      echo "## Traceability / Evidence Projection"
      echo
      echo "No completed item produced specialty evidence."
    fi
  fi
  echo
  echo "## Retro → Improvements"
  echo
  echo "- retro_id: \`$retro_id\`"
  echo "- batch_id: \`${retro_batch_id:-$RUNSTAMP}\`"
  echo "- result: \`$retro_result\`"
  echo "- change_count (window): \`${change_count:-0}\`"
  echo "- signal_count: \`${signal_count:-0}\`"
  echo "- improvement_ids: \`${retro_improvement_ids:-none}\`"
  echo "- outbox_id: \`${retro_outbox_id:-none}\`"
  echo "- improvements (ledger): \`${improvement_count:-0}\`"
  [ -f "$RUN_DIR/retro-status.json" ] && echo "- status: \`benchmark/runs/$RUNSTAMP-cursor/retro-status.json\`"
  [ -f "$RUN_DIR/proposal-candidates.json" ] && echo "- candidates: \`benchmark/runs/$RUNSTAMP-cursor/proposal-candidates.json\`"
  [ -f "$RUN_DIR/accept-status.json" ] && echo "- accept status: \`benchmark/runs/$RUNSTAMP-cursor/accept-status.json\`"
  [ -f "$RUN_DIR/auto-review-summary.json" ] && echo "- auto review: \`benchmark/runs/$RUNSTAMP-cursor/auto-review-summary.json\`"
  [ -f "$RUN_DIR/retro-summary.md" ] && echo "- summary: \`benchmark/runs/$RUNSTAMP-cursor/retro-summary.md\`"
  [ -n "${retro_review_queue:-}" ] && echo "- retro review queue: \`benchmark/runs/$RUNSTAMP-cursor/review-queue.md\`"
  [ -f "$RUN_DIR/improvements.json" ] && echo "- improvements projection: \`benchmark/runs/$RUNSTAMP-cursor/improvements.json\`"
  [ -f "$RUN_DIR/improvement-review-queue.json" ] && echo "- improvement review queue: \`benchmark/runs/$RUNSTAMP-cursor/improvement-review-queue.json\`"
  if [ "$retro_collect_exit" != "0" ]; then
    echo "- retro collect log: \`benchmark/runs/$RUNSTAMP-cursor/retro-collect.log\`"
  fi
  if [ "$DO_BENCHMARK_EVAL" = "true" ]; then
    echo
    echo "## Benchmark Eval Metrics (deterministic golden fixtures)"
    echo
    if [ "${#BENCHMARK_EVAL_ROWS[@]}" -gt 0 ]; then
      echo
      echo "| suite | verdict | run_id |"
      echo "|---|---|---|"
      for row in "${BENCHMARK_EVAL_ROWS[@]}"; do
        IFS='|' read -r es ev er <<<"$row"
        echo "| \`$es\` | $ev | \`$er\` |"
      done
    fi
    echo "- metrics: \`eval/out/runs/<run_id>/metrics.json\`"
    echo "- log: \`benchmark/runs/$RUNSTAMP-cursor/benchmark-eval.log\`"
  fi
  echo
  echo "## Artifacts"
  echo
  echo "- driver logs: \`benchmark/runs/$RUNSTAMP-cursor/*.workflow.attempt-*.cursor.log\`"
  echo "- archive logs: \`benchmark/runs/$RUNSTAMP-cursor/*.archive.workflow.log\` (or \`*.archive.cursor.jsonl\` if legacy)"
  echo "- retro collect log: \`benchmark/runs/$RUNSTAMP-cursor/retro-collect.log\`"
  echo "- status snapshots: \`benchmark/runs/$RUNSTAMP-cursor/*.status.json\`"
  if [ "$DO_TRACE_VERIFY" = "true" ]; then
    echo "- trace projections: \`benchmark/runs/$RUNSTAMP-cursor/*.trace.json\`"
    echo "- verify verdicts: \`benchmark/runs/$RUNSTAMP-cursor/*.verify.json\`"
    echo "- trace/verify logs: \`benchmark/runs/$RUNSTAMP-cursor/*.trace-verify.log\`"
  fi
  if [ "$DO_SPECIALTY_REPORT" = "true" ]; then
    echo "- specialty evidence + policy replay: \`benchmark/runs/$RUNSTAMP-cursor/*.specialty-report.json\`"
    echo "- specialty collection logs: \`benchmark/runs/$RUNSTAMP-cursor/*.specialty-report.log\`"
  fi
  echo "- loop log: \`benchmark/runs/$RUNSTAMP-cursor/loop.log\`"
} >"$SUMMARY"

log "cursor benchmark loop done - summary: $SUMMARY"
rm -f "$TRACK_PID_FILE"
echo
cat "$SUMMARY"

if ! benchmark_result_exit_code \
  "$DO_ARCHIVE" \
  "${ROW_RESULTS[@]+"${ROW_RESULTS[@]}"}"; then
  log "ERROR: benchmark result gate failed (workflow/archive result is not closed)"
  exit 1
fi
if ! benchmark_evidence_exit_code \
  "$DO_TRACE_VERIFY" \
  "${EVIDENCE_ROWS[@]+"${EVIDENCE_ROWS[@]}"}"; then
  log "ERROR: benchmark evidence gate failed (trace/verify result is not pass)"
  exit 1
fi
if [ "$DO_SPECIALTY_REPORT" = "true" ] && [ "$SPECIALTY_REPORT_FAILED" = "true" ]; then
  log "ERROR: benchmark specialty report/policy replay failed"
  exit 1
fi
