#!/usr/bin/env bash
#
# run-workflow-loop.sh - scheduled benchmark loop using OpenCode.
#
# GraphRuntime lifecycle, while phases are dispatched to bounded aa-* agents
# through a running OpenCode server.
#
# One tick:
#   1. Seed intake inputs for each item (.qa.yaml + proposal.md).
#   2. `aa workflow run --entrypoint full|… --adapter opencode --server …`
#      drives the change to a terminal state through bounded OpenCode agents.
#   3. Verify completion with deterministic `aa status`.
#   4. Archive via GraphRuntime: `aa workflow run --entrypoint archive`
#      (skill:aa-archive + archive-gate; not a free-form agent prompt).
#   5. After every invocation reaches a persisted terminal, run one explicit
#      Batch Retro through `aa retro --batch-manifest ...`. Failed/stopped items
#      remain members and become typed evidence gaps. A driver hard timeout is
#      not a persisted workflow terminal, so Retro waits for a later resume.
#      Artifacts: context.json, proposal-candidates.json, accept-status.json,
#      retro-summary.md, review-queue.md, plus qa/improvements/*.
#      Legacy nightly collection and free-form proposal paths are gone.
#
# Process-group hard timeout (run_with_hard_timeout.py) wraps driver / archive /
# retro runs so leftover local driver processes do not strand
# the loop. Status-poll early kill is retained as a safety net if the driver
# process lingers after a terminal state is already recorded.
#
# Usage:
#   ./benchmark/run-workflow-loop.sh
#   OPENCODE_MODEL=openai/gpt-5.1-codex ./benchmark/run-workflow-loop.sh
#   OPENCODE_SERVER=http://127.0.0.1:4096 ./benchmark/run-workflow-loop.sh
#   OPENCODE_MAX_WORKFLOW_ATTEMPTS=4 ./benchmark/run-workflow-loop.sh
#   USE_WORKFLOW_ARCHIVE=false              # legacy free-form archive prompt
#   RESUME_RUNSTAMP=20260713-113457 ./benchmark/run-workflow-loop.sh
#   DAEMON=1 is intentionally rejected; this script has no OpenCode daemon helper.
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOOP_HELPERS="$SCRIPT_DIR/loop-helpers.sh"
if [ ! -f "$LOOP_HELPERS" ]; then
  printf 'ERROR: missing %s\n' "$LOOP_HELPERS" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$LOOP_HELPERS"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Skills are mirrored into both project discovery roots and the OMO user
# runtime. The user mirror is defense-in-depth only: benchmark preflight still
# requires the server itself to have been started from this SUT so OMO's actual
# project catalog cannot diverge from OpenCode's directory-scoped catalog.
AA_SKILLS_ROOT="${AA_SKILLS_ROOT:-$PROJECT_ROOT/skills}"
# SCRIPT_DIR = <aa-repo>/benchmark/vue-fastapi-admin/benchmark → repo root is ../../..
AA_REPO_ROOT="${AA_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
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
EVAL_ENGINE_ROOT="${EVAL_ENGINE_ROOT:-$AA_REPO_ROOT}"   # holds eval/suites + eval/baselines
DO_VERIFICATION_METRICS="${DO_VERIFICATION_METRICS:-true}"
VERIFICATION_METRICS_ENTRYPOINT="${VERIFICATION_METRICS_ENTRYPOINT:-metrics-nightly}"
STEP_TIMEOUT="${STEP_TIMEOUT:-5400}"
STATUS_POLL_INTERVAL="${STATUS_POLL_INTERVAL:-120}"

CLEAN_ARTIFACTS="${CLEAN_ARTIFACTS:-true}"
CLEAN_TARGETS="${CLEAN_TARGETS:-qa/cases qa/changes}"

# Python workflow driver -----------------------------------------------------
AA_BIN="${AA_BIN:-aa}"
AA_PYTHON="${AA_PYTHON:-$AA_REPO_ROOT/.venv/bin/python}"
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
MAX_COVERAGE_REPAIR_ATTEMPTS="${MAX_COVERAGE_REPAIR_ATTEMPTS:-1}"
DRIVER_ADAPTER="${DRIVER_ADAPTER:-opencode}"
OPENCODE_SERVER="${OPENCODE_SERVER:-http://127.0.0.1:4096}"

OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
OPENCODE_MODEL="${OPENCODE_MODEL:-}"
OPENCODE_MAX_WORKFLOW_ATTEMPTS="${OPENCODE_MAX_WORKFLOW_ATTEMPTS:-3}"
OPENCODE_OUTPUT_FORMAT="${OPENCODE_OUTPUT_FORMAT:-json}"

# QA test-runtime endpoints (inherited by the driver → operation:run-tests → pytest).
# QA_SQLITE_FILE is pinned later to the run-scoped SUT copy so isolated workers
# share the migrated DB without mutating the benchmark checkout.
export BASE_URL="${BASE_URL:-http://127.0.0.1:9999}"
export API_BASE_URL="${API_BASE_URL:-$BASE_URL}"
export E2E_BACKEND_URL="${E2E_BACKEND_URL:-$BASE_URL}"
export E2E_FRONTEND_URL="${E2E_FRONTEND_URL:-http://127.0.0.1:3100}"
export QA_ADMIN_USERNAME="${QA_ADMIN_USERNAME:-admin}"
export QA_ADMIN_PASSWORD="${QA_ADMIN_PASSWORD:-123456}"
export AA_ADMIN_USERNAME="${AA_ADMIN_USERNAME:-$QA_ADMIN_USERNAME}"
export AA_ADMIN_PASSWORD="${AA_ADMIN_PASSWORD:-$QA_ADMIN_PASSWORD}"
# Fuzz schema acquisition: hit the LIVE SUT (from_url) instead of importing the
# app in-process (from_asgi). from_asgi boots the app lifespan → aerich migrate →
# writes migrations/** inside the task sandbox (forbidden_write) AND fuzzes an
# in-process app bound to the sandbox DB, inconsistent with the real-DB seeds.
# Both env names are set because generated fuzz files vary in which they read.
export QA_FUZZ_SCHEMA_MODE="${QA_FUZZ_SCHEMA_MODE:-uri}"
export FUZZ_SCHEMA_MODE="${FUZZ_SCHEMA_MODE:-uri}"

# Default to the canonical five-item benchmark batch.
if [ -z "${BENCHMARK_ITEMS+x}" ] || [ "${#BENCHMARK_ITEMS[@]}" -eq 0 ]; then
  BENCHMARK_ITEMS=(
    "RET-dept-management:requirements/dept-management.md"
    "RET-user-management:requirements/user-management.md"
    "RET-api-management:requirements/api-management.md"
    "RET-role-management:requirements/role-management.md"
    "RET-menu-management:requirements/menu-management.md"
  )
fi

SESSION_STAMP="$(date +%Y%m%d-%H%M%S)"
RUNSTAMP="${RESUME_RUNSTAMP:-$SESSION_STAMP}"
RUN_DIR="$SCRIPT_DIR/runs/$RUNSTAMP-opencode"
RETRO_ID="${RETRO_ID:-retro-${RUNSTAMP}-opencode}"
BATCH_MANIFEST="$RUN_DIR/batch-manifest.json"
LOOP_LOG="$RUN_DIR/loop.log"
SUMMARY="$RUN_DIR/loop-summary.md"
TRACK_LOG="$RESUME_LOG_DIR/opencode-loop-${SESSION_STAMP}.log"
TRACK_PID_FILE="$RESUME_LOG_DIR/opencode-loop-latest.pid"
TRACK_LATEST_LOG="$RESUME_LOG_DIR/opencode-loop-latest.log"
MANAGE_BENCHMARK_SUT="${MANAGE_BENCHMARK_SUT:-true}"
SUT_HOST="${SUT_HOST:-127.0.0.1}"
SUT_PORT="${SUT_PORT:-9999}"
SUT_READY_URL="${SUT_READY_URL:-${BASE_URL%/}/openapi.json}"
SUT_START_MAX_ATTEMPTS="${SUT_START_MAX_ATTEMPTS:-60}"
SUT_START_DELAY_S="${SUT_START_DELAY_S:-0.5}"
SUT_PID_FILE="$RUN_DIR/sut.pid"
SUT_LOG="$RUN_DIR/sut.log"
SUT_RUNTIME_ROOT="$RUN_DIR/sut-runtime"
# The backend is launched from a run-scoped copy of app/migrations, so its
# settings-derived SQLite path is isolated without modifying the benchmark SUT.
export QA_SQLITE_FILE="${QA_SQLITE_FILE:-$SUT_RUNTIME_ROOT/db.sqlite3}"
MANAGE_BENCHMARK_FRONTEND="${MANAGE_BENCHMARK_FRONTEND:-true}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-3100}"
FRONTEND_READY_URL="${FRONTEND_READY_URL:-${E2E_FRONTEND_URL%/}/}"
FRONTEND_START_MAX_ATTEMPTS="${FRONTEND_START_MAX_ATTEMPTS:-120}"
FRONTEND_START_DELAY_S="${FRONTEND_START_DELAY_S:-0.5}"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
FRONTEND_LOG="$RUN_DIR/frontend.log"
PNPM_BIN="${PNPM_BIN:-pnpm}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  local line
  line="$(printf '[%s] %s' "$(date +%H:%M:%S)" "$*")"
  printf '%s\n' "$line" | tee -a "$LOOP_LOG" >>"$TRACK_LOG"
}

preflight_log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

setup_run_tracking() {
  : >"$TRACK_LOG"
  ln -sfn "$(basename "$TRACK_LOG")" "$TRACK_LATEST_LOG"
  echo "$$" >"$TRACK_PID_FILE"
  log "tracking: $TRACK_LOG (latest → opencode-loop-latest.log)"
}

cleanup_loop_resources() {
  stop_benchmark_sut "$FRONTEND_PID_FILE"
  stop_benchmark_sut "$SUT_PID_FILE"
  rm -f "$TRACK_PID_FILE"
}

ensure_loop_sut() {
  if benchmark_http_ready "$SUT_READY_URL"; then
    log "sut: reuse ready service at $SUT_READY_URL"
    return 0
  fi
  if [ "$MANAGE_BENCHMARK_SUT" != "true" ]; then
    log "ERROR: SUT not ready at $SUT_READY_URL and MANAGE_BENCHMARK_SUT=$MANAGE_BENCHMARK_SUT"
    return 1
  fi
  local python_bin="$PROJECT_ROOT/.venv/bin/python"
  if [ ! -x "$python_bin" ]; then
    log "ERROR: SUT Python missing: $python_bin"
    return 1
  fi
  mkdir -p "$SUT_RUNTIME_ROOT/app" "$SUT_RUNTIME_ROOT/migrations"
  if ! cp -R "$PROJECT_ROOT/app/." "$SUT_RUNTIME_ROOT/app"; then
    log "ERROR: failed to prepare run-scoped SUT app at $SUT_RUNTIME_ROOT"
    return 1
  fi
  if ! cp -R "$PROJECT_ROOT/migrations/." "$SUT_RUNTIME_ROOT/migrations"; then
    log "ERROR: failed to prepare run-scoped SUT migrations at $SUT_RUNTIME_ROOT"
    return 1
  fi
  log "sut: starting managed backend at $SUT_HOST:$SUT_PORT (log=$(basename "$SUT_LOG"))"
  if ! ensure_benchmark_sut \
    "$SUT_READY_URL" "$SUT_LOG" "$SUT_PID_FILE" \
    "$SUT_START_MAX_ATTEMPTS" "$SUT_START_DELAY_S" -- \
    /bin/sh -c 'cd "$1" && exec "$2" -m uvicorn app:app --host "$3" --port "$4"' \
    benchmark-sut "$SUT_RUNTIME_ROOT" "$python_bin" "$SUT_HOST" "$SUT_PORT"; then
    log "ERROR: managed SUT failed readiness at $SUT_READY_URL (see $SUT_LOG)"
    return 1
  fi
  log "sut: ready at $SUT_READY_URL pid=$(cat "$SUT_PID_FILE")"
}

prepare_execution_credentials() {
  local token
  if [ -n "${E2E_API_TOKEN:-}" ]; then
    export E2E_API_TOKEN
    export API_ADMIN_TOKEN="${API_ADMIN_TOKEN:-$E2E_API_TOKEN}"
    log "test credentials: reuse configured API token"
    return 0
  fi
  if ! token="$("$AA_PYTHON" - <<'PY'
import json
import os
import urllib.request

request = urllib.request.Request(
    os.environ["BASE_URL"].rstrip("/") + "/api/v1/base/access_token",
    data=json.dumps(
        {
            "username": os.environ["QA_ADMIN_USERNAME"],
            "password": os.environ["QA_ADMIN_PASSWORD"],
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)
token = payload.get("data", {}).get("access_token")
if not isinstance(token, str) or not token:
    raise SystemExit("login response did not contain data.access_token")
print(token)
PY
)"; then
    log "ERROR: failed to acquire benchmark administrator token"
    return 1
  fi
  if [ -z "$token" ]; then
    log "ERROR: benchmark administrator token is empty"
    return 1
  fi
  export E2E_API_TOKEN="${E2E_API_TOKEN:-$token}"
  export API_ADMIN_TOKEN="${API_ADMIN_TOKEN:-$E2E_API_TOKEN}"
  log "test credentials: administrator token acquired"
}

ensure_loop_frontend() {
  if benchmark_http_ready "$FRONTEND_READY_URL"; then
    log "frontend: reuse ready service at $FRONTEND_READY_URL"
    return 0
  fi
  if [ "$MANAGE_BENCHMARK_FRONTEND" != "true" ]; then
    log "ERROR: frontend not ready at $FRONTEND_READY_URL and MANAGE_BENCHMARK_FRONTEND=$MANAGE_BENCHMARK_FRONTEND"
    return 1
  fi
  if ! command -v "$PNPM_BIN" >/dev/null 2>&1; then
    log "ERROR: frontend package manager missing: $PNPM_BIN"
    return 1
  fi
  if [ ! -d "$PROJECT_ROOT/web/node_modules" ]; then
    log "ERROR: frontend dependencies missing: run pnpm --dir $PROJECT_ROOT/web install --frozen-lockfile"
    return 1
  fi
  log "frontend: starting managed Vite server at $FRONTEND_HOST:$FRONTEND_PORT (log=$(basename "$FRONTEND_LOG"))"
  if ! ensure_benchmark_sut \
    "$FRONTEND_READY_URL" "$FRONTEND_LOG" "$FRONTEND_PID_FILE" \
    "$FRONTEND_START_MAX_ATTEMPTS" "$FRONTEND_START_DELAY_S" -- \
    env BROWSER=none "$PNPM_BIN" --dir "$PROJECT_ROOT/web" run dev \
    --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"; then
    log "ERROR: managed frontend failed readiness at $FRONTEND_READY_URL (see $FRONTEND_LOG)"
    return 1
  fi
  log "frontend: ready at $FRONTEND_READY_URL pid=$(cat "$FRONTEND_PID_FILE")"
}

# Auto-resume the first pending GraphRuntime interrupt (autonomous benchmark path).
# Knowledge proposals are deliberately left pending until the whole Batch settles;
# promoting them here would mutate an input frozen by other active invocations.
maybe_auto_decide() {
  local change_id="$1"
  local proposal_state="unchanged"
  [ "$AUTO_DECIDE_BENCHMARK" = "true" ] || return 1
  if compgen -G "qa/changes/$change_id/plans/data-knowledge.proposal.*.yaml" >/dev/null; then
    proposal_state="proposal_pending"
    log "[$change_id] knowledge proposal deferred until Batch boundary; stop current invocation"
  fi
  local status_json interrupt_id action reason
  status_json="$("$AA_BIN" status --change "$change_id" --json 2>/dev/null || true)"
  [ -n "$status_json" ] || return 1
  interrupt_id="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); ints=d.get("pending_interrupts") or []; print((ints[0].get("interrupt_id") or ints[0].get("id") or "") if ints else "")' "$status_json")"
  [ -n "$interrupt_id" ] || return 1
  action="$(benchmark_interrupt_action "$proposal_state")"
  reason="benchmark auto resume interrupt $interrupt_id; defer synchronized knowledge changes to Batch boundary"
  log "[$change_id] auto resume interrupt=$interrupt_id action=$action"
  # Interrupt resume continues the driver with --adapter; default headless would
  # silently switch an OpenCode loop onto cursor-agent mid-run.
  local -a adapter_args=(
    --adapter "$DRIVER_ADAPTER"
    --server "$OPENCODE_SERVER"
    --directory "$PROJECT_ROOT"
  )
  [ -n "$OPENCODE_MODEL" ] && adapter_args+=(--model "$OPENCODE_MODEL")
  "$AA_BIN" workflow resume \
    --change "$change_id" \
    --interrupt "$interrupt_id" \
    --action "$action" \
    --reason "$reason" \
    "${adapter_args[@]}"
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
  preflight_log "ERROR: missing $HARD_TIMEOUT_PY"
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
# the SUT repo (tests/__init__.py, config.py, conftest.py, schema_validation.py).
# Verify they are present up front so codegen does not STOP and tests can run.
ensure_test_infra() {
  local missing=()
  local f
  for f in tests/__init__.py tests/config.py tests/conftest.py tests/schema_validation.py; do
    [ -f "$PROJECT_ROOT/$f" ] || missing+=("$f")
  done
  if [ ${#missing[@]} -ne 0 ]; then
    log "ERROR: missing test infra: ${missing[*]}"
    log "       restore tests/__init__.py, tests/config.py, tests/conftest.py, tests/schema_validation.py"
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
  MAX_COVERAGE_REPAIR="$MAX_COVERAGE_REPAIR_ATTEMPTS" \
  python3 -c '
import os, json
print(json.dumps({
    "run_mode": os.environ["RUN_MODE"],
    "test_types": [t for t in os.environ["TEST_TYPES"].split(",") if t],
    "run_tests": os.environ["RUN_TESTS"] == "true",
    "force_continue": os.environ["FORCE_CONTINUE"] == "true",
    "max_healing_attempts": int(os.environ["MAX_HEALING"]),
    "max_coverage_repair_attempts": int(os.environ["MAX_COVERAGE_REPAIR"]),
    "auto_archive": False,
}))'
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
  local params
  local -a adapter_args=(
    --adapter "$DRIVER_ADAPTER"
    --server "$OPENCODE_SERVER"
    --directory "$PROJECT_ROOT"
  )
  [ -n "$OPENCODE_MODEL" ] && adapter_args+=(--model "$OPENCODE_MODEL")
  params="$(driver_params_json)"
  local has_invocation="false"
  if "$AA_BIN" status --change "$change_id" --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("status") else 1)'; then
    has_invocation="true"
  fi
  if [ "$has_invocation" = "true" ]; then
    # resume has no --params because invocation parameters are already pinned.
    run_hard_timeout "$logf" "$change_id" \
      "$AA_BIN" workflow resume \
      --change "$change_id" \
      "${adapter_args[@]}"
  else
    run_hard_timeout "$logf" "$change_id" \
      "$AA_BIN" workflow run \
      --change "$change_id" \
      --entrypoint "$DRIVER_ENTRYPOINT" \
      --params "$params" \
      "${adapter_args[@]}"
  fi
}

# One-shot opencode prompt (legacy archive path only).
# $1=logfile $2=prompt
run_opencode_agent() {
  local logf="$1" prompt="$2"
  local -a cmd=(
    "$OPENCODE_BIN"
    run
    --format "$OPENCODE_OUTPUT_FORMAT"
    --agent aa-archiver
    --auto
    --attach "$OPENCODE_SERVER"
    --dir "$PROJECT_ROOT"
  )
  [ -n "$OPENCODE_MODEL" ] && cmd+=(--model "$OPENCODE_MODEL")
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
  local -a adapter_args=(
    --adapter "$DRIVER_ADAPTER"
    --server "$OPENCODE_SERVER"
    --directory "$PROJECT_ROOT"
  )
  [ -n "$OPENCODE_MODEL" ] && adapter_args+=(--model "$OPENCODE_MODEL")
  run_hard_timeout "$logf" "$change_id" \
    "$AA_BIN" workflow run \
    --change "$change_id" \
    --entrypoint "$entrypoint" \
    --params "$params" \
    "${adapter_args[@]}"
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
  ar_log="$RUN_DIR/${change_id}.archive.opencode.jsonl"
  log "[$change_id] archive via legacy OpenCode prompt ..."
  run_opencode_agent "$ar_log" "$(archive_prompt "$change_id")"
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
    # Flat GraphRuntime shape from `aa status --json`.
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
2. You are the bounded aa-archiver OpenCode agent. Do not invoke another agent.
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
  : >"$collect_log"
  batch_members_settled "$BATCH_MANIFEST" || return 2
  local -a retro_command=(
    "$AA_BIN" retro
    --batch-manifest "$BATCH_MANIFEST"
    --retro-id "$RETRO_ID"
    --json
  )
  [ "$RETRO_DRY_RUN" = "true" ] && retro_command+=(--dry-run)
  retro_command+=(--adapter "$DRIVER_ADAPTER")
  retro_command+=(--server "$OPENCODE_SERVER")
  retro_command+=(--directory "$PROJECT_ROOT")
  [ -n "$OPENCODE_MODEL" ] && retro_command+=(--model "$OPENCODE_MODEL")
  "${retro_command[@]}" >"$collect_log" 2>&1
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
  local metrics_row metrics_recorded="false"
  for metrics_row in "${VERIFICATION_METRICS_ROWS[@]-}"; do
    if [ "${metrics_row%%|*}" = "$change_id" ]; then
      metrics_recorded="true"
      break
    fi
  done
  if [ "$metrics_recorded" != "true" ]; then
    VERIFICATION_METRICS_ROWS+=("$change_id|not_run|n/a|n/a|n/a|n/a")
  fi
  ROW_RESULTS+=("$change_id|$terminal|$detail|$archive_field")
}

run_verification_metrics_stage() {
  local change_id="$1"
  if [ "$DO_VERIFICATION_METRICS" != "true" ]; then
    VERIFICATION_METRICS_ROWS+=("$change_id|disabled|n/a|n/a|n/a|n/a")
    return 0
  fi
  local metrics_log="$RUN_DIR/${change_id}.metrics-nightly.workflow.log"
  local change_dir="$PROJECT_ROOT/qa/changes/$change_id"
  local row rc=0 status floor_ratio verdict c_layer quarantine_active
  row="$(execute_verification_metrics_stage \
    run_workflow_entrypoint "$metrics_log" "$change_id" "$change_dir" \
    "$VERIFICATION_METRICS_ENTRYPOINT" '{}')" || rc=$?
  IFS='|' read -r _ status floor_ratio verdict c_layer quarantine_active <<<"$row"
  if [ "$status" = "completed" ]; then
    if ! snapshot_verification_metrics "$change_dir" "$RUN_DIR" "$change_id"; then
      row="$change_id|snapshot_failed|n/a|n/a|n/a|n/a"
      rc=1
    fi
  fi
  VERIFICATION_METRICS_ROWS+=("$row")
  return "$rc"
}

record_coverage_repair_result() {
  local change_id="$1"
  local change_dir="$PROJECT_ROOT/qa/changes/$change_id"
  local row
  if row="$(snapshot_coverage_repair "$change_dir" "$RUN_DIR" "$change_id" "$AA_PYTHON")"; then
    COVERAGE_REPAIR_ROWS+=("$row")
    return 0
  fi
  COVERAGE_REPAIR_ROWS+=("$change_id|invalid_artifacts|n/a|n/a|n/a|n/a")
  return 1
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
if ! command -v "$OPENCODE_BIN" >/dev/null 2>&1; then
  preflight_log "ERROR: OpenCode binary not found: $OPENCODE_BIN"
  exit 1
fi

if [ "$DRIVER_ADAPTER" != "opencode" ]; then
  preflight_log "ERROR: run-workflow-loop.sh requires DRIVER_ADAPTER=opencode so bounded aa-* permissions apply"
  exit 1
fi

# T3: install kernel+assurance wheels into one env, then pin product resources.
if ! bootstrap_assurance_runtime "$AA_REPO_ROOT"; then
  preflight_log "ERROR: aa CLI not found: $AA_BIN (install with 'uv sync --project $AA_REPO_ROOT' or 'uv tool install --from $AA_REPO_ROOT assurance-agent')"
  exit 1
fi
if [ ! -x "$AA_PYTHON" ]; then
  preflight_log "ERROR: pinned assurance-agent Python missing: $AA_PYTHON"
  exit 1
fi
if ! preflight_assurance_wheels; then
  preflight_log "ERROR: T3 runtime must import assurance_kernel and assurance_agent from the same env as $AA_BIN"
  exit 1
fi

preflight_log "syncing and verifying current aa runtime skills and bounded OpenCode agents"
( cd "$PROJECT_ROOT" && "$AA_BIN" skill refresh --sync-agents --sync-opencode-user-skills --sync-opencode-user-agents >/dev/null 2>&1 ) || {
  preflight_log "ERROR: aa skill refresh --sync-agents --sync-opencode-user-skills --sync-opencode-user-agents failed"
  exit 1
}
if ! "$AA_PYTHON" -c "${ASSURANCE_PYTHON_PREAMBLE}"'import sys; from pathlib import Path; from assurance_agent.workflow.core.assets import opencode_user_agents_root, opencode_user_skills_root, verify_packaged_agents, verify_packaged_skills; root = Path(sys.argv[1]); verify_packaged_skills(root / "skills"); verify_packaged_skills(root / ".opencode" / "skills"); verify_packaged_skills(opencode_user_skills_root(), namespaced_only=True); verify_packaged_agents(opencode_user_agents_root())' "$PROJECT_ROOT"; then
  preflight_log "ERROR: synced OpenCode/OMO skill or agent hashes/runtime namespace are invalid"
  exit 1
fi

if ! curl -sf -o /dev/null "$OPENCODE_SERVER" 2>/dev/null; then
  preflight_log "ERROR: no OpenCode server reachable at $OPENCODE_SERVER"
  preflight_log "       start it after agent sync (for example: opencode serve --port 4096)"
  exit 1
fi

preflight_log "validating live bounded OpenCode agent policies"
if ! "$AA_PYTHON" -c "${ASSURANCE_PYTHON_PREAMBLE}"'import sys; from assurance_agent.workflow.driver.opencode_adapter import validate_bounded_agent_server; validate_bounded_agent_server(sys.argv[1], sys.argv[2])' "$OPENCODE_SERVER" "$PROJECT_ROOT"; then
  preflight_log "ERROR: live OpenCode agents are stale or unsafe after sync"
  preflight_log "       restart OpenCode so it reloads the project .opencode/agents policies"
  exit 1
fi

preflight_log "validating live bounded OpenCode agents from an isolated Graph task project"
if ! "$AA_PYTHON" -c "${ASSURANCE_PYTHON_PREAMBLE}"'
import subprocess
import sys
import tempfile
from assurance_agent.workflow.driver.opencode_adapter import validate_bounded_agent_server
with tempfile.TemporaryDirectory(prefix="aa-opencode-agent-preflight-") as directory:
    subprocess.run(["git", "init", "-q"], cwd=directory, check=True, capture_output=True)
    validate_bounded_agent_server(sys.argv[1], directory)
' "$OPENCODE_SERVER"; then
  preflight_log "ERROR: live OpenCode user-level AA agents are stale or unsafe for isolated task projects"
  preflight_log "       restart OpenCode so it reloads the synchronized user agents"
  exit 1
fi

preflight_log "validating OpenCode server working directory, live boundary plugin, and exact skill catalog"
if ! "$AA_PYTHON" -c "${ASSURANCE_PYTHON_PREAMBLE}"'import sys; from assurance_agent.workflow.driver.opencode_adapter import validate_packaged_skill_server; validate_packaged_skill_server(sys.argv[1], sys.argv[2])' "$OPENCODE_SERVER" "$PROJECT_ROOT"; then
  preflight_log "ERROR: OpenCode server working directory, live boundary plugin, or skill catalog failed validation"
  preflight_log "       restart OpenCode from $PROJECT_ROOT so it loads the synchronized plugin and skill catalog"
  exit 1
fi

if [ "${DAEMON:-}" = "1" ]; then
  preflight_log "ERROR: DAEMON=1 is not supported by the OpenCode benchmark; run it in the foreground"
  exit 1
fi

mkdir -p "$RUN_DIR" "$RESUME_LOG_DIR"
setup_run_tracking
trap cleanup_loop_resources EXIT

log "opencode benchmark loop start - runstamp=$RUNSTAMP items=${#BENCHMARK_ITEMS[@]}${RESUME_RUNSTAMP:+ (resume)}"
log "project_root=$PROJECT_ROOT run_mode=$RUN_MODE run_tests=$RUN_TESTS test_types=$TEST_TYPES"
log "driver: adapter=$DRIVER_ADAPTER server=$OPENCODE_SERVER entrypoint=$DRIVER_ENTRYPOINT max_healing=$MAX_HEALING_ATTEMPTS"
log "opencode=$OPENCODE_BIN model=${OPENCODE_MODEL:-default} max_attempts=$OPENCODE_MAX_WORKFLOW_ATTEMPTS"
log "do_archive=$DO_ARCHIVE use_workflow_archive=$USE_WORKFLOW_ARCHIVE entrypoint=$ARCHIVE_ENTRYPOINT"
log "retro: canonical_batch_cli id=$RETRO_ID manifest=$BATCH_MANIFEST dry_run=$RETRO_DRY_RUN"
log "do_benchmark_eval=$DO_BENCHMARK_EVAL suites=[$BENCHMARK_EVAL_SUITES]"
log "auto_decide=$AUTO_DECIDE_BENCHMARK recover_healing=$RECOVER_HEALING_DEADLOCK"

declare -a BATCH_CHANGE_IDS=()
for item in "${BENCHMARK_ITEMS[@]}"; do
  base_id="${item%%:*}"
  BATCH_CHANGE_IDS+=("${base_id}-${RUNSTAMP}-opencode")
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
ensure_loop_sut || exit 1
prepare_execution_credentials || exit 1
ensure_loop_frontend || exit 1

declare -a ROW_RESULTS=()
declare -a VERIFICATION_METRICS_ROWS=()
declare -a COVERAGE_REPAIR_ROWS=()
item_idx=0
total_items=${#BENCHMARK_ITEMS[@]}

for item in "${BENCHMARK_ITEMS[@]}"; do
  item_idx=$((item_idx + 1))
  base_id="${item%%:*}"
  req_rel="${item#*:}"
  req_file="$req_rel"
  [ -f "$req_file" ] || req_file="$SCRIPT_DIR/$req_rel"

  change_id="${base_id}-${RUNSTAMP}-opencode"
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
    final_status="$(execution_final_status "$change_id")"
    archived="no"
    record_coverage_repair_result "$change_id" || true
    run_verification_metrics_stage "$change_id" || true
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

  while [ "$attempt" -le "$OPENCODE_MAX_WORKFLOW_ATTEMPTS" ]; do
    wf_log="$RUN_DIR/${change_id}.workflow.attempt-${attempt}.opencode.log"
    log "[$change_id] stage 1/2 driver workflow attempt $attempt/$OPENCODE_MAX_WORKFLOW_ATTEMPTS (adapter=$DRIVER_ADAPTER) ..."
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
      assert_no_duplicate_committed_task_ids "qa/changes/$change_id/events.jsonl" || true
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

  record_coverage_repair_result "$change_id" || true
  run_verification_metrics_stage "$change_id" || true

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

# BEGIN batch knowledge promotion boundary
run_batch_knowledge_promotion_boundary() {
  if promote_batch_knowledge_proposals "$AA_BIN" "$BATCH_MANIFEST" "${BATCH_CHANGE_IDS[@]}"; then
    knowledge_promotion_status="completed"
    log "knowledge proposal promotion boundary check complete"
  else
    knowledge_promotion_status="failed"
    if [ "$DO_BENCHMARK_EVAL" = "true" ]; then
      benchmark_eval_status="skipped_knowledge_promotion_failed"
    fi
    log "knowledge proposal promotion failed at Batch boundary; preserving Retro evidence"
  fi

  # Promotion controls L1 mutation, not failure analysis.  A conflict is
  # itself evidence and must not erase the Batch Retro/Improvement path.
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

  if [ "$knowledge_promotion_status" = "completed" ] && [ "$DO_BENCHMARK_EVAL" = "true" ]; then
    run_benchmark_eval
    benchmark_eval_status="completed"
  fi
}
# END batch knowledge promotion boundary

# BEGIN benchmark eval summary
render_benchmark_eval_summary() {
  [ "$DO_BENCHMARK_EVAL" = "true" ] || return 0
  echo
  echo "## Benchmark Eval Metrics (deterministic golden fixtures)"
  echo
  echo "- status: \`$benchmark_eval_status\`"
  [ "$benchmark_eval_status" = "completed" ] || return 0
  if [ "${#BENCHMARK_EVAL_ROWS[@]}" -gt 0 ]; then
    echo
    echo "| suite | verdict | run_id |"
    echo "|---|---|---|"
    local row es ev er
    for row in "${BENCHMARK_EVAL_ROWS[@]}"; do
      IFS='|' read -r es ev er <<<"$row"
      echo "| \`$es\` | $ev | \`$er\` |"
    done
  fi
  echo "- metrics: \`eval/out/runs/<run_id>/metrics.json\`"
  echo "- log: \`benchmark/runs/$RUNSTAMP-opencode/benchmark-eval.log\`"
}
# END benchmark eval summary

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
knowledge_promotion_status="not_run"
if [ "$DO_BENCHMARK_EVAL" = "true" ]; then
  benchmark_eval_status="not_run"
else
  benchmark_eval_status="disabled"
fi
if batch_members_settled "$BATCH_MANIFEST"; then
  run_batch_knowledge_promotion_boundary
else
  retro_result="skipped_nonterminal_batch"
  retro_collect_exit="skipped"
  if [ "$DO_BENCHMARK_EVAL" = "true" ]; then
    benchmark_eval_status="skipped_nonterminal_batch"
  fi
  log "retro/eval skipped: Batch contains running, not_started, or hard_timeout members"
fi

{
  echo "# OpenCode benchmark loop - $RUNSTAMP"
  echo
  echo "- project: \`$PROJECT_ROOT\`"
  echo "- engine: \`aa workflow run --adapter $DRIVER_ADAPTER --server $OPENCODE_SERVER\`"
  echo "- run_mode: \`$RUN_MODE\` run_tests: \`$RUN_TESTS\` test_types: \`$TEST_TYPES\` force_continue: \`$FORCE_CONTINUE\`"
  echo "- driver entrypoint: \`$DRIVER_ENTRYPOINT\` max healing attempts: \`$MAX_HEALING_ATTEMPTS\`"
  echo "- max workflow attempts: \`$OPENCODE_MAX_WORKFLOW_ATTEMPTS\`"
  echo "- archive: \`DO_ARCHIVE=$DO_ARCHIVE\` via \`$([ "$USE_WORKFLOW_ARCHIVE" = "true" ] && echo "workflow:$ARCHIVE_ENTRYPOINT" || echo "legacy-opencode-prompt")\`"
  echo "- retro: \`aa retro --batch-manifest\` (exit: \`${retro_collect_exit:-n/a}\`)"
  echo "- knowledge promotion boundary: \`$knowledge_promotion_status\`"
  echo "- verification metrics: \`DO_VERIFICATION_METRICS=$DO_VERIFICATION_METRICS\` via \`$VERIFICATION_METRICS_ENTRYPOINT\`"
  echo "- batch manifest: \`benchmark/runs/$RUNSTAMP-opencode/batch-manifest.json\`"
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
  echo "## Verification Metrics (M2–M4)"
  echo
  echo "| change_id | status | floor_ratio | sufficiency | C-layer evaluated | quarantine active |"
  echo "|---|---|---:|---|---:|---:|"
  for row in "${VERIFICATION_METRICS_ROWS[@]}"; do
    IFS='|' read -r cid status floor_ratio verdict c_layer quarantine_active <<<"$row"
    echo "| \`$cid\` | $status | $floor_ratio | $verdict | $c_layer | $quarantine_active |"
  done
  echo
  echo "Atomic snapshots: \`benchmark/runs/$RUNSTAMP-opencode/<change_id>.verification-metrics.json\`."
  echo
  echo "## Coverage Repair Fast Loop"
  echo
  echo "| change_id | status | attempts | source batch | post-repair batch | safety |"
  echo "|---|---|---:|---|---|---|"
  render_coverage_repair_rows "${COVERAGE_REPAIR_ROWS[@]+"${COVERAGE_REPAIR_ROWS[@]}"}"
  echo
  echo "Atomic snapshots: \`benchmark/runs/$RUNSTAMP-opencode/<change_id>.coverage-repair.json\`."
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
  [ -f "$RUN_DIR/retro-status.json" ] && echo "- status: \`benchmark/runs/$RUNSTAMP-opencode/retro-status.json\`"
  [ -f "$RUN_DIR/proposal-candidates.json" ] && echo "- candidates: \`benchmark/runs/$RUNSTAMP-opencode/proposal-candidates.json\`"
  [ -f "$RUN_DIR/accept-status.json" ] && echo "- accept status: \`benchmark/runs/$RUNSTAMP-opencode/accept-status.json\`"
  [ -f "$RUN_DIR/auto-review-summary.json" ] && echo "- auto review: \`benchmark/runs/$RUNSTAMP-opencode/auto-review-summary.json\`"
  [ -f "$RUN_DIR/retro-summary.md" ] && echo "- summary: \`benchmark/runs/$RUNSTAMP-opencode/retro-summary.md\`"
  [ -n "${retro_review_queue:-}" ] && echo "- retro review queue: \`benchmark/runs/$RUNSTAMP-opencode/review-queue.md\`"
  [ -f "$RUN_DIR/improvements.json" ] && echo "- improvements projection: \`benchmark/runs/$RUNSTAMP-opencode/improvements.json\`"
  [ -f "$RUN_DIR/improvement-review-queue.json" ] && echo "- improvement review queue: \`benchmark/runs/$RUNSTAMP-opencode/improvement-review-queue.json\`"
  if [ "$retro_collect_exit" != "0" ]; then
    echo "- retro collect log: \`benchmark/runs/$RUNSTAMP-opencode/retro-collect.log\`"
  fi
  render_benchmark_eval_summary
  echo
  echo "## Artifacts"
  echo
  echo "- driver logs: \`benchmark/runs/$RUNSTAMP-opencode/*.workflow.attempt-*.opencode.log\`"
  echo "- archive logs: \`benchmark/runs/$RUNSTAMP-opencode/*.archive.workflow.log\` (or \`*.archive.opencode.jsonl\` if legacy)"
  echo "- retro collect log: \`benchmark/runs/$RUNSTAMP-opencode/retro-collect.log\`"
  echo "- status snapshots: \`benchmark/runs/$RUNSTAMP-opencode/*.status.json\`"
  echo "- verification metrics logs: \`benchmark/runs/$RUNSTAMP-opencode/*.metrics-nightly.workflow.log\`"
  echo "- loop log: \`benchmark/runs/$RUNSTAMP-opencode/loop.log\`"
} >"$SUMMARY"

log "opencode benchmark loop done - summary: $SUMMARY"
rm -f "$TRACK_PID_FILE"
echo
cat "$SUMMARY"

benchmark_failed=0
if ! benchmark_result_exit_code \
  "$DO_ARCHIVE" \
  "${ROW_RESULTS[@]+"${ROW_RESULTS[@]}"}"; then
  log "ERROR: benchmark result gate failed (workflow/archive result is not closed)"
  benchmark_failed=1
fi
if ! benchmark_verification_metrics_exit_code \
  "$DO_VERIFICATION_METRICS" \
  "${VERIFICATION_METRICS_ROWS[@]+"${VERIFICATION_METRICS_ROWS[@]}"}"; then
  log "ERROR: verification metrics gate failed (entrypoint or artifact validation incomplete)"
  benchmark_failed=1
fi
if ! benchmark_coverage_repair_exit_code \
  "${COVERAGE_REPAIR_ROWS[@]+"${COVERAGE_REPAIR_ROWS[@]}"}"; then
  log "ERROR: coverage repair evidence gate failed (status/snapshot incomplete)"
  benchmark_failed=1
fi
if ! benchmark_knowledge_promotion_exit_code "$knowledge_promotion_status"; then
  log "ERROR: knowledge proposal promotion failed at Batch boundary"
  benchmark_failed=1
fi
[ "$benchmark_failed" -eq 0 ] || exit 1
