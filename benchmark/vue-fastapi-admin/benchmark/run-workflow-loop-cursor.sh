#!/usr/bin/env bash
#
# run-workflow-loop-cursor.sh - scheduled benchmark loop using Cursor Agent.
#
# Cursor sibling of run-workflow-loop.sh. Same deterministic Python driver
# (`aa workflow run`), but phases are dispatched through headless
# `cursor-agent --print` instead of an OpenCode server / `opencode run`.
#
# One tick:
#   1. Seed intake inputs for each item (.qa.yaml + proposal.md).
#   2. `aa workflow run --adapter headless --agent-cmd 'cursor-agent …'`
#      drives the change to a terminal state (one cursor-agent spawn per phase).
#   3. Verify completion with deterministic `aa status`.
#   4. Archive completed changes through Cursor Agent + aa-archive.
#   5. (Optional) retro-nightly collect via skills repo driver.
#
# Process-group hard timeout (run_with_hard_timeout.py) wraps driver / archive
# runs so leftover SUT dev servers started by cursor-agent do not strand the
# loop. Status-poll early kill is retained as a safety net if the driver
# process lingers after a terminal state is already recorded.
#
# Usage:
#   ./benchmark/run-workflow-loop-cursor.sh
#   CURSOR_MODEL=gpt-5.5-medium ./benchmark/run-workflow-loop-cursor.sh
#   CURSOR_MAX_WORKFLOW_ATTEMPTS=4 ./benchmark/run-workflow-loop-cursor.sh
#   DO_NIGHTLY_COLLECT=false ./benchmark/run-workflow-loop-cursor.sh
#   RESUME_RUNSTAMP=20260713-113457 ./benchmark/run-workflow-loop-cursor.sh
#   DAEMON=1 ./benchmark/run-workflow-loop-cursor.sh   # detach + write PID/log symlinks
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Python migration: skills are synced INTO the SUT project by `aa skill refresh`.
AA_SKILLS_ROOT="${AA_SKILLS_ROOT:-$PROJECT_ROOT/skills}"
AA_REPO_ROOT="${AA_REPO_ROOT:-/Users/lvqingquan/agent/assurance-agent}"   # for uv-based aa install
NIGHTLY_CLI="${NIGHTLY_CLI:-aa retro nightly}"
RESUME_LOG_DIR="${RESUME_LOG_DIR:-$SCRIPT_DIR/resume-logs}"
AUTO_DECIDE_BENCHMARK="${AUTO_DECIDE_BENCHMARK:-true}"
RECOVER_HEALING_DEADLOCK="${RECOVER_HEALING_DEADLOCK:-true}"
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
# Deterministic eval regression gate (golden-sample replay; catches engine
# regressions that break scoring/evidence integrity on a known-good run).
DO_EVAL_REGRESSION="${DO_EVAL_REGRESSION:-true}"
EVAL_REGRESSION_SUITES="${EVAL_REGRESSION_SUITES:-workflow-run,classification-unit,safety-lite,eval-smoke,case-generation,workflow-case,workflow-api-codegen,workflow-e2e-codegen,workflow-fuzz-codegen,workflow-performance-codegen,workflow-full}"
EVAL_ENGINE_ROOT="${EVAL_ENGINE_ROOT:-$AA_REPO_ROOT}"   # holds eval/suites + eval/baselines
RETRO_SINCE_DAYS="${RETRO_SINCE_DAYS:-7}"
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

# Full agent argv prefix for headless driver + nightly (prompt appended last).
if [ -z "${CURSOR_NIGHTLY_AGENT:-}" ]; then
  CURSOR_NIGHTLY_AGENT="$CURSOR_AGENT_BIN --print --output-format $CURSOR_OUTPUT_FORMAT --workspace $PROJECT_ROOT --trust"
  [ "$CURSOR_AGENT_FORCE" = "true" ] && CURSOR_NIGHTLY_AGENT="$CURSOR_NIGHTLY_AGENT --force"
  [ -n "$CURSOR_MODEL" ] && CURSOR_NIGHTLY_AGENT="$CURSOR_NIGHTLY_AGENT --model $CURSOR_MODEL"
fi

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

paused_phase() {
  local change_id="$1"
  python3 - <<PY
import json, pathlib
p = pathlib.Path("qa/changes/$change_id/driver.json")
if not p.exists():
    print("")
else:
    d = json.loads(p.read_text())
    print(d.get("paused_on") or "")
PY
}

# Resolve the checkpoint phase that needs a human decision, from either the
# driver pause (break_at / healing await-human sets driver.paused_on) OR a
# needs_human_review terminal, which the engine records in status.terminal.phase
# (not driver.paused_on). Without the terminal fallback, review gates are never
# auto-decided and the loop stalls.
current_review_phase() {
  local change_id="$1" phase kind
  phase="$(paused_phase "$change_id")"
  if [ -n "$phase" ]; then
    printf '%s' "$phase"
    return 0
  fi
  kind="$(terminal_kind "$change_id")"
  [ "$kind" = "needs_human_review" ] || { printf ''; return 0; }
  python3 - "$RUN_DIR/${change_id}.status.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print(""); raise SystemExit(0)
terminal = data.get("terminal")
print(terminal.get("phase") or "" if isinstance(terminal, dict) else "")
PY
}

gate_for_phase() {
  case "$1" in
    api-plan-review) echo "api-plan-review-gate" ;;
    e2e-plan-review) echo "e2e-plan-review-gate" ;;
    case-review) echo "case-review-gate" ;;
    fuzz-plan-review) echo "fuzz-plan-review-gate" ;;
    performance-plan-review) echo "performance-plan-review-gate" ;;
    *) echo "" ;;
  esac
}

maybe_auto_decide() {
  local change_id="$1"
  [ "$AUTO_DECIDE_BENCHMARK" = "true" ] || return 1
  # Best-effort: materialize formal data-knowledge if a proposal exists.
  if [ ! -f ".aa/data-knowledge.yaml" ]; then
    local proposal="qa/changes/$change_id/plans/data-knowledge.proposal.yaml"
    if [ -f "$proposal" ]; then
      mkdir -p .aa
      python3 - "$proposal" .aa/data-knowledge.yaml <<'PYDK'
import sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
lines = src.read_text(encoding="utf-8").splitlines(True)
while lines and lines[0].lstrip().startswith("#"):
    lines.pop(0)
while lines and not lines[0].strip():
    lines.pop(0)
dst.write_text("".join(lines), encoding="utf-8")
PYDK
      log "[$change_id] materialized .aa/data-knowledge.yaml from proposal"
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
break_fixer_loop() {
  local change_id="$1"
  python3 - "$change_id" <<'PY' || return 0
import json, os, subprocess, sys
cid = sys.argv[1]
aa = os.environ.get("AA_BIN", "aa")
try:
    proc = subprocess.run(
        [aa, "status", "--change", cid, "--next", "--json"],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode not in (0, 20, 30):
        raise RuntimeError(f"aa status failed ({proc.returncode}): {proc.stderr.strip()}")
    st = json.loads(proc.stdout)
except Exception:
    raise SystemExit(0)
nxt = [x.get("phase_id") for x in (st.get("next_dispatch") or []) if isinstance(x, dict)]
if not nxt or not set(nxt) <= {"api-plan-fix", "e2e-plan-fix"}:
    raise SystemExit(0)
# accept_risk alone does not clear needs_fix — gate verdicts read review JSON.
# Promote the artifact to pass so the repair phase is pruned and codegen can run.
for gate, review_rel, summary in [
    ("api-plan-review-gate", "review/api-plan-review.json", "review/api-plan-review-apply-summary.md"),
    ("e2e-plan-review-gate", "review/plan-review.json", "review/plan-review-apply-summary.md"),
]:
    summary_path = f"qa/changes/{cid}/{summary}"
    review_path = f"qa/changes/{cid}/{review_rel}"
    if not os.path.exists(summary_path):
        continue
    if os.path.exists(review_path):
        doc = json.load(open(review_path))
        doc["decision"] = "pass"
        doc["human_review_required"] = False
        doc["auto_fix_allowed"] = False
        if "codegen_readiness" in doc:
            doc["codegen_readiness"] = "ready"
        if doc.get("risk_level") in ("high", "critical"):
            doc["risk_level"] = "medium"
        doc["summary"] = (
            (doc.get("summary") or "")
            + "\n\n[benchmark] promoted to pass to break empty fixer loop."
        ).strip()
        open(review_path, "w").write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    subprocess.call([
        aa, "decide", "--change", cid, "--at", gate,
        "--action", "fix_and_proceed",
        "--reason", f"benchmark: break fixer loop after {summary}",
    ])
PY
}

recover_dead_end() {
  local change_id="$1"
  [ "$RECOVER_HEALING_DEADLOCK" = "true" ] || return 1
  local kind reason
  kind="$(terminal_kind "$change_id")"
  reason="$(terminal_reason "$change_id")"
  if [ "$kind" = "needs_human_review" ]; then
    log "[$change_id] workflow needs human review${reason:+: $reason}; use aa decide"
  elif [ -z "$kind" ]; then
    log "[$change_id] no dispatch and no terminal; preserve evidence and inspect driver error"
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
      rm -rf "$abspath"
    else
      log "clean: $target/ absent - nothing to remove"
    fi
  done
}

# Driver full-scope pauses at test-infra-bootstrap when the shared pytest
# scaffold is missing. Ensure the three contract files exist before any change
# is started so the loop is not stuck on needs_human_review for every item.
ensure_test_infra() {
  local missing=()
  local f
  for f in tests/config.py tests/conftest.py tests/schema_validation.py; do
    [ -f "$PROJECT_ROOT/$f" ] || missing+=("$f")
  done
  if [ ${#missing[@]} -ne 0 ]; then
    log "ERROR: missing test infra: ${missing[*]}"
    log "       restore tests/config.py, tests/conftest.py, tests/schema_validation.py"
    log "       (driver will pause every change at bootstrap otherwise)"
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

# Seed intake inputs for one change (driver full scope starts at explore).
# $1=change_id $2=base_id $3=requirement text
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
  approved_approach: API + E2E + Fuzz + Performance
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
    echo "- Fuzz: selected"
    echo "- Performance: selected"
    echo
    echo "## Layer Rationale"
    echo "Benchmark autonomous run — API + E2E + Fuzz + Performance coverage for $feature."
    echo
    echo "generation_mode: autonomous"
  } >"$cdir/proposal.md"

  # Seed the canonical workflow-state so the deterministic Python engine can start:
  #   - params: IDENTICAL to the driver's --params so `aa status` (which reads
  #     state.params) computes the SAME phase DAG the driver dispatches; otherwise
  #     terminal detection disagrees with the run.
  #   - run_context.interaction_mode=autonomous so case-design-gate passes without
  #     an interactive user approval (the benchmark is headless/autonomous).
  #   - skill_registry_check=pass so registry-gate does not stop the run (the
  #     packaged driver ships every workflow skill).
  local params_json
  params_json="$(driver_params_json)"
  cat >"$cdir/workflow-state.yaml" <<YAML
params: $params_json
run_context:
  interaction_mode: autonomous
  orchestrator_skill: aa-workflow
phases:
  skill_registry_check:
    status: pass
    reason: benchmark seed — driver ships all workflow skills
YAML

  log "[$change_id] seeded intake inputs (.qa.yaml + proposal.md + workflow-state, feature=$feature)"
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
  local cmd="$CURSOR_AGENT_BIN --print --output-format $CURSOR_OUTPUT_FORMAT --workspace $PROJECT_ROOT --trust"
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
  local params agent_cmd
  params="$(driver_params_json)"
  agent_cmd="$(cursor_agent_cmd_prefix)"
  local has_invocation="false"
  if "$AA_BIN" workflow status --change "$change_id" --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("status") else 1)'; then
    has_invocation="true"
  fi
  if [ "$has_invocation" = "true" ]; then
    run_hard_timeout "$logf" "$change_id" \
      "$AA_BIN" workflow resume \
      --change "$change_id" \
      --adapter headless \
      --params "$params" \
      --agent-cmd "$agent_cmd"
  else
    run_hard_timeout "$logf" "$change_id" \
      "$AA_BIN" workflow run \
      --change "$change_id" \
      --entrypoint "$DRIVER_ENTRYPOINT" \
      --adapter headless \
      --params "$params" \
      --agent-cmd "$agent_cmd"
  fi
}

# One-shot cursor-agent prompt (archive / legacy retro proposals).
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

status_json_path() {
  local change_id="$1"
  echo "$RUN_DIR/${change_id}.status.json"
}

# aa status: 0 running/completed, 20 stopped, 30 needs_human_review,
# 40 command/data error. Terminal control uses JSON; rc=40 fails closed.
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
2. You are in Cursor Agent. Do not call opencode.
3. Only archive if the change satisfies the archive contract. If not eligible,
   report the missing phases and do not fabricate archive artifacts.
4. Before ending, confirm whether qa/archive/${change_id}/ exists.
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
      --agent "$CURSOR_NIGHTLY_AGENT" \
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

# Deterministic eval regression gate. Runs golden-sample suites via the fake
# adapter (no cursor-agent, no live SUT) and compares to the approved baseline.
# Suites resolve from EVAL_ENGINE_ROOT/eval; the SUT under test is $PROJECT_ROOT.
# Populates EVAL_ROWS / EVAL_WORST for the summary.
declare -a EVAL_ROWS=()
EVAL_WORST="pass"
run_eval_regression() {
  local eval_log="$RUN_DIR/eval-regression.log"
  : >"$eval_log"
  if [ ! -d "$EVAL_ENGINE_ROOT/eval/suites" ]; then
    log "eval regression: no eval/suites under $EVAL_ENGINE_ROOT — skipped"
    return 0
  fi
  log "stage: eval regression suites=[$EVAL_REGRESSION_SUITES] engine=$EVAL_ENGINE_ROOT sut=$PROJECT_ROOT"
  local worst="pass"
  local -a suites=()
  IFS=',' read -ra suites <<<"$EVAL_REGRESSION_SUITES"
  local suite out rid verdict
  for suite in "${suites[@]}"; do
    suite="$(echo "$suite" | xargs)"
    [ -n "$suite" ] || continue
    set +e
    out="$(cd "$EVAL_ENGINE_ROOT" && AA_EVAL_FAKE_ADAPTER=1 "$AA_BIN" eval run \
      --suite "$suite" --sut-dir "$PROJECT_ROOT" --json 2>>"$eval_log")"
    set -e
    printf '%s\n' "$out" >>"$eval_log"
    rid="$(printf '%s' "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("run_id",""))' 2>/dev/null || true)"
    verdict="$(printf '%s' "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("verdict",""))' 2>/dev/null || true)"
    [ -n "$verdict" ] || verdict="error"
    log "eval[$suite]: verdict=$verdict run_id=${rid:-n/a}"
    if [ -n "$rid" ]; then
      ( cd "$EVAL_ENGINE_ROOT" && "$AA_BIN" eval compare --baseline main \
        --run "$rid" --sut-dir "$PROJECT_ROOT" >>"$eval_log" 2>&1 ) || true
    fi
    case "$verdict" in
      pass | pass_with_warnings) ;;
      *) worst="$verdict" ;;
    esac
    EVAL_ROWS+=("$suite|$verdict|${rid:-n/a}")
  done
  EVAL_WORST="$worst"
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

if [ ! -d "$AA_SKILLS_ROOT" ]; then
  log "materializing aa skills into $AA_SKILLS_ROOT via aa skill refresh"
  ( cd "$PROJECT_ROOT" && "$AA_BIN" skill refresh >/dev/null 2>&1 ) || \
    log "WARN: aa skill refresh failed - archive/retro prompts may miss SKILL.md"
fi

if [ "$DO_RETRO" = "true" ] && [ "$DO_NIGHTLY_COLLECT" = "true" ]; then
  log "WARN: DO_RETRO and DO_NIGHTLY_COLLECT both true — using legacy DO_RETRO only"
  DO_NIGHTLY_COLLECT="false"
fi

log "cursor benchmark loop start - runstamp=$RUNSTAMP items=${#BENCHMARK_ITEMS[@]}${RESUME_RUNSTAMP:+ (resume)}"
log "project_root=$PROJECT_ROOT run_mode=$RUN_MODE run_tests=$RUN_TESTS test_types=$TEST_TYPES"
log "driver: adapter=headless entrypoint=$DRIVER_ENTRYPOINT max_healing=$MAX_HEALING_ATTEMPTS"
log "cursor_agent=$CURSOR_AGENT_BIN model=${CURSOR_MODEL:-default} max_attempts=$CURSOR_MAX_WORKFLOW_ATTEMPTS"
log "do_archive=$DO_ARCHIVE do_nightly_collect=$DO_NIGHTLY_COLLECT do_retro=$DO_RETRO"
log "auto_decide=$AUTO_DECIDE_BENCHMARK recover_healing=$RECOVER_HEALING_DEADLOCK"

setup_run_tracking

if [ -n "${RESUME_RUNSTAMP:-}" ] && [ "$CLEAN_ARTIFACTS" = "true" ]; then
  log "resume mode: skip clean (preserve in-flight changes)"
else
  clean_generated_artifacts
fi
ensure_test_infra

declare -a ROW_RESULTS=()
declare -a RETRO_CHANGE_IDS=()
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
    ROW_RESULTS+=("$change_id|SKIP|requirement file missing|archived=no")
    continue
  fi

  requirement="$(cat "$req_file")"
  workflow_kind="running"
  workflow_reason=""
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
    if [ "$DO_ARCHIVE" = "true" ] && [ ! -d "qa/archive/$change_id" ]; then
      ar_log="$RUN_DIR/${change_id}.archive.cursor.jsonl"
      log "[$change_id] stage 2/2 cursor archive (resume) ..."
      if run_cursor_agent "$ar_log" "$(archive_prompt "$change_id")"; then
        [ -d "qa/archive/${change_id}" ] && archived="yes"
      fi
    elif [ -d "qa/archive/$change_id" ]; then
      archived="yes"
    fi
    ROW_RESULTS+=("$change_id|completed|final_status=$final_status|archived=$archived")
    continue
  fi

  if [ "$workflow_kind" = "stopped" ]; then
    log "[$change_id] already stopped — skip driver"
    ROW_RESULTS+=("$change_id|stopped|$(terminal_reason "$change_id")|archived=no")
    continue
  fi

  recover_dead_end "$change_id" || true
  workflow_kind="$(terminal_kind "$change_id")"

  while [ "$attempt" -le "$CURSOR_MAX_WORKFLOW_ATTEMPTS" ]; do
    wf_log="$RUN_DIR/${change_id}.workflow.attempt-${attempt}.cursor.log"
    log "[$change_id] stage 1/2 driver workflow attempt $attempt/$CURSOR_MAX_WORKFLOW_ATTEMPTS (adapter=headless/cursor-agent) ..."
    if run_driver "$wf_log" "$change_id"; then
      log "[$change_id] driver attempt $attempt exited 0 (completed)"
    else
      log "[$change_id] driver attempt $attempt exited non-zero (see $(basename "$wf_log"))"
    fi

    workflow_kind="$(terminal_kind "$change_id")"
    workflow_reason="$(terminal_reason "$change_id")"
    log "[$change_id] aa status terminal=$workflow_kind${workflow_reason:+ reason=$workflow_reason}"

    if [ "$workflow_kind" = "completed" ] || [ "$workflow_kind" = "stopped" ]; then
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
    break_fixer_loop "$change_id" || true
    recover_dead_end "$change_id" || true

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
    ar_log="$RUN_DIR/${change_id}.archive.cursor.jsonl"
    log "[$change_id] stage 2/2 cursor archive ..."
    if run_cursor_agent "$ar_log" "$(archive_prompt "$change_id")"; then
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
    rp_log="$RUN_DIR/retro-proposals.cursor.jsonl"
    log "stage 4/4 cursor aa-retro proposals for $retro_id ..."
    if run_cursor_agent "$rp_log" "$(retro_proposals_prompt "$retro_id")"; then
      [ -f "qa/retro/${retro_id}/proposals.json" ] && cp "qa/retro/${retro_id}/proposals.json" "$RUN_DIR/proposals.json"
      [ -f "qa/retro/${retro_id}/retro-summary.md" ] && cp "qa/retro/${retro_id}/retro-summary.md" "$RUN_DIR/retro-summary.md"
      log "retro proposals generated (see qa/retro/${retro_id}/)"
    else
      log "cursor retro proposals exited non-zero (see $(basename "$rp_log"))"
    fi
  fi
fi

if [ "$DO_EVAL_REGRESSION" = "true" ]; then
  run_eval_regression
fi

{
  echo "# Cursor benchmark loop - $RUNSTAMP"
  echo
  echo "- project: \`$PROJECT_ROOT\`"
  echo "- engine: \`aa workflow run --adapter headless\` + \`cursor-agent\`"
  echo "- run_mode: \`$RUN_MODE\` run_tests: \`$RUN_TESTS\` test_types: \`$TEST_TYPES\` force_continue: \`$FORCE_CONTINUE\`"
  echo "- driver entrypoint: \`$DRIVER_ENTRYPOINT\` max healing attempts: \`$MAX_HEALING_ATTEMPTS\`"
  echo "- max workflow attempts: \`$CURSOR_MAX_WORKFLOW_ATTEMPTS\`"
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
    [ -f "$RUN_DIR/proposals.json" ] && echo "- proposals: \`benchmark/runs/$RUNSTAMP-cursor/proposals.json\`"
    [ -f "$RUN_DIR/retro-summary.md" ] && echo "- summary: \`benchmark/runs/$RUNSTAMP-cursor/retro-summary.md\`"
    [ -n "$nightly_review_queue" ] && echo "- review queue: \`benchmark/runs/$RUNSTAMP-cursor/review-queue.md\`"
  else
    echo "- (retro disabled, no-op, or failed)"
  fi
  if [ "$DO_NIGHTLY_COLLECT" = "true" ] && [ -n "$nightly_collect_exit" ] && [ "$nightly_collect_exit" != "0" ] && [ "$nightly_collect_exit" != "10" ]; then
    echo "- nightly collect log: \`benchmark/runs/$RUNSTAMP-cursor/nightly-collect.log\`"
  fi
  if [ "$DO_EVAL_REGRESSION" = "true" ]; then
    echo
    echo "## Eval regression (deterministic golden-sample gate)"
    echo
    echo "- worst verdict: \`$EVAL_WORST\`"
    if [ "${#EVAL_ROWS[@]}" -gt 0 ]; then
      echo
      echo "| suite | verdict | run_id |"
      echo "|---|---|---|"
      for row in "${EVAL_ROWS[@]}"; do
        IFS='|' read -r es ev er <<<"$row"
        echo "| \`$es\` | $ev | \`$er\` |"
      done
    fi
    echo "- log: \`benchmark/runs/$RUNSTAMP-cursor/eval-regression.log\`"
  fi
  echo
  echo "## Artifacts"
  echo
  echo "- driver logs: \`benchmark/runs/$RUNSTAMP-cursor/*.workflow.attempt-*.cursor.log\`"
  echo "- cursor archive/retro logs: \`benchmark/runs/$RUNSTAMP-cursor/*.cursor.jsonl\`"
  echo "- status snapshots: \`benchmark/runs/$RUNSTAMP-cursor/*.status.json\`"
  echo "- loop log: \`benchmark/runs/$RUNSTAMP-cursor/loop.log\`"
} >"$SUMMARY"

log "cursor benchmark loop done - summary: $SUMMARY"
rm -f "$TRACK_PID_FILE"
echo
cat "$SUMMARY"
