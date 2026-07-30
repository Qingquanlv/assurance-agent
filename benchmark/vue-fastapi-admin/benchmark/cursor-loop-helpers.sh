#!/usr/bin/env bash

# Helpers kept side-effect free until explicitly called so unit tests can source
# this file without launching the benchmark loop.

resolve_cursor_project_root() {
  local script_dir="$1" override="$2" candidate resolved
  candidate="${override:-$script_dir/..}"
  [ -d "$candidate" ] || return 1
  resolved="$(cd "$candidate" && pwd)" || return 1
  printf '%s' "$resolved"
}

benchmark_eval_setting() {
  local new_value="$1" legacy_value="$2" default_value="$3"
  if [ -n "$new_value" ]; then
    printf '%s' "$new_value"
  elif [ -n "$legacy_value" ]; then
    printf '%s' "$legacy_value"
  else
    printf '%s' "$default_value"
  fi
}

initialize_retro_batch_manifest() {
  local manifest="$1" batch_id="$2"
  shift 2
  python3 - "$manifest" "$batch_id" "$@" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
batch_id = sys.argv[2]
change_ids = sorted(sys.argv[3:])
if not batch_id or any(not change_id for change_id in change_ids):
    raise SystemExit("batch_contract_invalid")
if len(change_ids) != len(set(change_ids)):
    raise SystemExit("batch_members_duplicate")

if path.exists():
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        current_ids = [member["change_id"] for member in current["members"]]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"batch_manifest_invalid:{exc}") from exc
    if current.get("schema_version") != "1" or current.get("batch_id") != batch_id:
        raise SystemExit("batch_identity_mismatch")
    if current_ids != change_ids:
        raise SystemExit("batch_membership_mismatch")
    raise SystemExit(0)

payload = {
    "schema_version": "1",
    "batch_id": batch_id,
    "status": "incomplete",
    "members": [
        {
            "change_id": change_id,
            "execution_status": "not_started",
            "evidence_availability": "absent",
        }
        for change_id in change_ids
    ],
}
path.parent.mkdir(parents=True, exist_ok=True)
raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
try:
    temp.write_bytes(raw)
    os.replace(temp, path)
finally:
    temp.unlink(missing_ok=True)
PY
}

update_retro_batch_member() {
  local manifest="$1" change_id="$2" execution_status="$3" evidence_availability="$4"
  python3 - "$manifest" "$change_id" "$execution_status" "$evidence_availability" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
change_id, execution_status, availability = sys.argv[2:5]
allowed_statuses = {
    "completed", "failed", "stopped", "hard_timeout",
    "cancelled", "running", "not_started",
}
if execution_status not in allowed_statuses:
    raise SystemExit("batch_execution_status_invalid")
if availability not in {"complete", "partial", "absent"}:
    raise SystemExit("batch_evidence_availability_invalid")

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
    members = payload["members"]
except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"batch_manifest_invalid:{exc}") from exc

matched = False
for member in members:
    if member.get("change_id") == change_id:
        member["execution_status"] = execution_status
        member["evidence_availability"] = availability
        matched = True
        break
if not matched:
    raise SystemExit("batch_member_unknown")

settled = all(member["execution_status"] not in {"running", "not_started"} for member in members)
all_complete = all(member["evidence_availability"] == "complete" for member in members)
payload["status"] = "complete" if settled and all_complete else "incomplete"
payload["members"] = sorted(members, key=lambda member: member["change_id"])
raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
try:
    temp.write_bytes(raw)
    os.replace(temp, path)
finally:
    temp.unlink(missing_ok=True)
PY
}

retro_batch_member_outcome() {
  local status="$1" evidence_path="$2" availability="absent"
  if [ -s "$evidence_path" ]; then
    if [ "$status" = "completed" ]; then
      availability="complete"
    else
      availability="partial"
    fi
  fi
  case "$status" in
    completed|failed|stopped|hard_timeout|cancelled|running|not_started) ;;
    needs_human_review|interrupted) status="stopped" ;;
    SKIP) status="not_started" ;;
    *) status="failed" ;;
  esac
  [ "$status" = "not_started" ] && availability="absent"
  printf '%s|%s' "$status" "$availability"
}

run_batch_retro() {
  local aa_bin="$1" manifest="$2" retro_id="$3" agent_cmd="$4" dry_run="$5" log_file="$6"
  local -a command=(
    "$aa_bin" retro
    --batch-manifest "$manifest"
    --retro-id "$retro_id"
    --json
  )
  [ "$dry_run" = "true" ] && command+=(--dry-run)
  AA_RETRO_AGENT_CMD="$agent_cmd" "${command[@]}" >"$log_file" 2>&1
}

collect_benchmark_eval_rows() {
  local aa_bin="$1" engine_root="$2" sut_root="$3" suites_csv="$4" eval_log="$5"
  local suite out run_id verdict run_exit
  local -a suites=()
  IFS=',' read -ra suites <<<"$suites_csv"
  for suite in "${suites[@]}"; do
    suite="$(echo "$suite" | xargs)"
    [ -n "$suite" ] || continue
    run_exit=0
    out="$(cd "$engine_root" && AA_EVAL_FAKE_ADAPTER=1 "$aa_bin" eval run \
      --suite "$suite" --sut-dir "$sut_root" --json 2>>"$eval_log")" || run_exit=$?
    printf '%s\n' "$out" >>"$eval_log"
    run_id="$(printf '%s' "$out" | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("run_id", ""))' 2>/dev/null || true)"
    verdict="$(printf '%s' "$out" | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("verdict", ""))' 2>/dev/null || true)"
    if [ "$run_exit" -ne 0 ] || [ -z "$verdict" ]; then
      verdict="error"
    fi
    printf '%s|%s|%s\n' "$suite" "$verdict" "${run_id:-n/a}"
  done
  return 0
}

resolve_aa_python_bin() {
  local aa_bin="$1" override="$2" aa_path shebang interpreter interpreter_name
  if [ -n "$override" ]; then
    [ -x "$override" ] || return 1
    printf '%s' "$override"
    return 0
  fi
  aa_path="$(command -v "$aa_bin" 2>/dev/null)" || return 1
  shebang="$(sed -n '1p' "$aa_path" 2>/dev/null)"
  case "$shebang" in
    '#!'*) interpreter="${shebang#'#!'}" ;;
    *) return 1 ;;
  esac
  [ -x "$interpreter" ] || return 1
  interpreter_name="$(basename "$interpreter")"
  case "$interpreter_name" in
    python|python3|python3.*) ;;
    *) return 1 ;;
  esac
  printf '%s' "$interpreter"
}

collect_trace_verify_evidence() {
  local aa_bin="$1" change_id="$2" trace_file="$3" verify_file="$4" log_file="$5"
  local python_bin="$6"
  local trace_exit=0 verify_exit=0 summaries trace_summary verify_summary
  mkdir -p "$(dirname "$trace_file")" "$(dirname "$verify_file")" "$(dirname "$log_file")"
  : >"$log_file"

  "$aa_bin" trace --change "$change_id" --json >"$trace_file" 2>>"$log_file" || trace_exit=$?
  "$aa_bin" verify --change "$change_id" --json >"$verify_file" 2>>"$log_file" || verify_exit=$?

  summaries="$("$python_bin" - \
    "$trace_file" "$verify_file" "$change_id" "$trace_exit" "$verify_exit" <<'PY'
import json
import os
import sys
from pathlib import Path

trace_path = Path(sys.argv[1])
verify_path = Path(sys.argv[2])
change_id = sys.argv[3]
trace_exit = int(sys.argv[4])
verify_exit = int(sys.argv[5])


def load_json_or_write_error(path: Path, command: str, exit_code: int) -> tuple[dict, bool]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload, True
    except (OSError, TypeError, ValueError):
        raw = ""

    payload = {
        "change_id": change_id,
        "command": command,
        "error": "invalid_or_missing_json_output",
        "exit_code": exit_code,
        "schema_version": "1",
    }
    if raw:
        payload["raw_stdout"] = raw
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return payload, False


trace, trace_valid = load_json_or_write_error(trace_path, "trace", trace_exit)
verify, verify_valid = load_json_or_write_error(verify_path, "verify", verify_exit)

if trace_valid:
    integrity = trace.get("integrity") or "unknown"
    gaps = trace.get("gaps")
    print(f"{integrity}|{len(gaps) if isinstance(gaps, list) else -1}")
else:
    print("invalid|-1")

if verify_valid:
    verdict = verify.get("verdict") or "unknown"
    blocking = verify.get("blocking_gaps")
    insufficient = verify.get("insufficient")
    blocking_count = len(blocking) if isinstance(blocking, list) else -1
    insufficient_count = len(insufficient) if isinstance(insufficient, list) else -1
    print(f"{verdict}|{blocking_count}|{insufficient_count}")
else:
    print("invalid|-1|-1")
PY
)"
  trace_summary="${summaries%%$'\n'*}"
  verify_summary="${summaries#*$'\n'}"
  printf '%s|%s|%s|%s|%s' \
    "$change_id" "$trace_exit" "$trace_summary" "$verify_exit" "$verify_summary"
  return 0
}

benchmark_evidence_exit_code() {
  local enabled="$1"
  shift
  local row change_id trace_exit integrity gap_count verify_exit verdict blocking insufficient extra
  local failed=0

  [ "$enabled" = "true" ] || return 0
  [ "$#" -gt 0 ] || return 1
  for row in "$@"; do
    IFS='|' read -r \
      change_id trace_exit integrity gap_count verify_exit verdict blocking insufficient extra <<<"$row"
    if [ -z "$change_id" ] || [ -n "$extra" ] \
      || ! [[ "$trace_exit" =~ ^[0-9]+$ ]] \
      || ! [[ "$gap_count" =~ ^[0-9]+$ ]] \
      || ! [[ "$verify_exit" =~ ^[0-9]+$ ]] \
      || ! [[ "$blocking" =~ ^[0-9]+$ ]] \
      || ! [[ "$insufficient" =~ ^[0-9]+$ ]]; then
      failed=1
      continue
    fi
    case "$integrity" in
      complete|degraded|incomplete) ;;
      *) failed=1; continue ;;
    esac
    if [ "$trace_exit" != "0" ] || [ "$verify_exit" != "0" ] || [ "$verdict" != "pass" ]; then
      failed=1
    fi
  done
  return "$failed"
}

collect_benchmark_specialty_report() {
  local python_bin="$1" reporter="$2" project_root="$3" schema_root="$4"
  local change_id="$5" trace_file="$6" verify_file="$7" output_file="$8" log_file="$9"
  local trace_exit="${10}" verify_exit="${11}"
  local root_invocation_id="${12}" workflow_entrypoint="${13}"
  mkdir -p "$(dirname "$output_file")" "$(dirname "$log_file")"
  "$python_bin" "$reporter" collect \
    --project-root "$project_root" \
    --schema-root "$schema_root" \
    --change-id "$change_id" \
    --root-invocation-id "$root_invocation_id" \
    --workflow-entrypoint "$workflow_entrypoint" \
    --trace "$trace_file" \
    --verify "$verify_file" \
    --trace-exit "$trace_exit" \
    --verify-exit "$verify_exit" \
    --output "$output_file" \
    2>>"$log_file"
}

validate_workflow_command_result() {
  local result_file="$1" change_id="$2" entrypoint="$3"
  python3 - "$result_file" "$change_id" "$entrypoint" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_change = sys.argv[2]
expected_entrypoint = sys.argv[3]
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, TypeError, ValueError) as exc:
    raise SystemExit(f"workflow_result_invalid:{exc}") from exc
if payload.get("schema_version") != "1":
    raise SystemExit("workflow_result_schema_invalid")
if payload.get("change_id") != expected_change:
    raise SystemExit("workflow_result_change_mismatch")
if payload.get("entrypoint") != expected_entrypoint:
    raise SystemExit("workflow_result_entrypoint_mismatch")
root_id = payload.get("root_invocation_id")
if not isinstance(root_id, str) or not root_id.strip():
    raise SystemExit("workflow_result_invocation_missing")
started = payload.get("started_new_root")
if not isinstance(started, bool):
    raise SystemExit("workflow_result_started_new_root_invalid")
print(f"{root_id}|{str(started).lower()}")
PY
}

pin_workflow_root_from_result() {
  local run_dir="$1" change_id="$2" result_file="$3" entrypoint="$4"
  local state_file="$run_dir/${change_id}.workflow-root.json"
  local validated root_id started_new_root payload temp
  validated="$(validate_workflow_command_result "$result_file" "$change_id" "$entrypoint")" || return 1
  IFS='|' read -r root_id started_new_root <<<"$validated"
  [ "$started_new_root" = "true" ] || return 0
  if [ -f "$state_file" ]; then
    python3 - "$state_file" "$change_id" "$entrypoint" "$root_id" <<'PY' || return 1
import json
import sys
from pathlib import Path

current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
change_id, entrypoint, root_id = sys.argv[2:5]
if current.get("schema_version") != "1":
    raise SystemExit("workflow_root_schema_invalid")
if current.get("change_id") != change_id:
    raise SystemExit("workflow_root_change_mismatch")
if current.get("entrypoint") != entrypoint:
    raise SystemExit("workflow_root_entrypoint_mismatch")
if current.get("root_invocation_id") != root_id:
    raise SystemExit("workflow_root_invocation_mismatch")
PY
    return 0
  fi
  payload="$(python3 - "$change_id" "$entrypoint" "$root_id" <<'PY'
import json
import sys

print(
    json.dumps(
        {
            "schema_version": "1",
            "change_id": sys.argv[1],
            "entrypoint": sys.argv[2],
            "root_invocation_id": sys.argv[3],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
)
PY
)"
  mkdir -p "$run_dir"
  temp="$run_dir/.${change_id}.workflow-root.json.$$"
  printf '%s' "$payload" >"$temp"
  mv "$temp" "$state_file"
}

read_workflow_root_state() {
  local run_dir="$1" change_id="$2"
  local state_file="$run_dir/${change_id}.workflow-root.json"
  [ -f "$state_file" ] || return 1
  python3 - "$state_file" "$change_id" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_change = sys.argv[2]
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("schema_version") != "1":
    raise SystemExit("workflow_root_schema_invalid")
if payload.get("change_id") != expected_change:
    raise SystemExit("workflow_root_change_mismatch")
entrypoint = payload.get("entrypoint")
root_id = payload.get("root_invocation_id")
if not isinstance(entrypoint, str) or not entrypoint.strip():
    raise SystemExit("workflow_root_entrypoint_missing")
if not isinstance(root_id, str) or not root_id.strip():
    raise SystemExit("workflow_root_invocation_missing")
print(f"{root_id}|{entrypoint}")
PY
}

finalize_benchmark_specialty_report() {
  local python_bin="$1" reporter="$2" change_id="$3" report_file="$4" collect_exit="$5"
  local registered="false"
  if [ -s "$report_file" ]; then
    if "$python_bin" "$reporter" evidence-row --change-id "$change_id" "$report_file" >/dev/null 2>&1; then
      registered="true"
      printf '%s\n' "$report_file"
    fi
  fi
  if [ "$registered" = "true" ]; then
    printf 'registered=true\n'
  else
    printf 'registered=false\n'
  fi
  return "$collect_exit"
}

render_benchmark_specialty_sections() {
  local python_bin="$1" reporter="$2"
  shift 2
  [ "$#" -gt 0 ] || return 1
  "$python_bin" "$reporter" render "$@"
}

benchmark_specialty_resume_action() {
  local report_file="$1" archive_dir="$2"
  if [ -s "$report_file" ]; then
    printf 'reuse'
    return 0
  fi
  if [ -d "$archive_dir" ]; then
    printf 'missing_after_archive'
    return 1
  fi
  printf 'collect'
}

remove_generated_artifact_tree() {
  local target="$1"
  [ -e "$target" ] || return 0
  chmod -R u+w -- "$target" 2>/dev/null || true
  rm -rf -- "$target"
  [ ! -e "$target" ]
}

retro_artifacts_complete() {
  local retro_dir="$1"
  local status_file="$retro_dir/retro-status.json"
  [ -s "$status_file" ] || return 1
  python3 - "$status_file" <<'PY' >/dev/null 2>&1
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("schema_version") != "1":
    raise SystemExit(1)
if payload.get("result") not in {"completed", "completed_with_gaps", "pending_reconcile"}:
    raise SystemExit(1)
if not isinstance(payload.get("improvement_ids"), list):
    raise SystemExit(1)
PY
}

workflow_attempts_should_stop() {
  case "$1" in
    completed|stopped|failed) return 0 ;;
    *) return 1 ;;
  esac
}

benchmark_should_run_archive() {
  local do_archive="$1" terminal="$2" final_status="$3"
  [ "$do_archive" = "true" ] || return 1
  [ "$terminal" = "completed" ] || return 1
  case "$final_status" in
    PASS|PASS_WITH_WARNINGS) return 0 ;;
    *) return 1 ;;
  esac
}

benchmark_result_exit_code() {
  local do_archive="$1"
  shift
  local row cid term detail archive_field failed=0

  [ "$#" -gt 0 ] || return 1
  for row in "$@"; do
    IFS='|' read -r cid term detail archive_field <<<"$row"
    if [ "$term" != "completed" ]; then
      failed=1
    elif [ "$detail" != "final_status=PASS" ] && [ "$detail" != "final_status=PASS_WITH_WARNINGS" ]; then
      failed=1
    elif [ "$do_archive" = "true" ] && [ "$archive_field" != "archived=yes" ]; then
      failed=1
    fi
  done
  return "$failed"
}
