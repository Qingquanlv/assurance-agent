#!/usr/bin/env bash

# Helpers kept side-effect free until explicitly called so unit tests can source
# this file without launching the benchmark loop.

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
