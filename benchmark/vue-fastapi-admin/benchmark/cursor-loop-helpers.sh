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

resume_benchmark_interrupt() {
  local aa_bin="$1" change_id="$2" interrupt_id="$3" action="$4"
  local reason="$5" adapter="$6" agent_cmd="$7"
  "$aa_bin" workflow resume \
    --change "$change_id" \
    --interrupt "$interrupt_id" \
    --action "$action" \
    --reason "$reason" \
    --adapter "$adapter" \
    --agent-cmd "$agent_cmd"
}

benchmark_http_ready() {
  local ready_url="$1"
  python3 - "$ready_url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(sys.argv[1], headers={"Accept": "*/*"})
    with opener.open(request, timeout=2.0) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

benchmark_process_identity() {
  local pid="$1"
  ps -p "$pid" -o lstart= -o command= 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

ensure_benchmark_sut() {
  local ready_url="$1" log_file="$2" pid_file="$3" max_attempts="$4" delay_s="$5"
  shift 5
  [ "${1:-}" = "--" ] && shift
  [ "$#" -gt 0 ] || return 2

  if benchmark_http_ready "$ready_url"; then
    return 0
  fi

  mkdir -p "$(dirname "$log_file")" "$(dirname "$pid_file")"
  : >"$log_file"
  "$@" >>"$log_file" 2>&1 &
  local pid=$! attempt=0 identity identity_file="${pid_file}.identity"
  printf '%s\n' "$pid" >"$pid_file"
  identity="$(benchmark_process_identity "$pid")"
  if [ -z "$identity" ]; then
    kill -TERM "$pid" 2>/dev/null || true
    rm -f "$pid_file" "$identity_file"
    return 1
  fi
  printf '%s\n' "$identity" >"$identity_file"

  while [ "$attempt" -lt "$max_attempts" ]; do
    if benchmark_http_ready "$ready_url"; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file" "$identity_file"
      return 1
    fi
    sleep "$delay_s"
    attempt=$((attempt + 1))
  done

  stop_benchmark_sut "$pid_file"
  return 1
}

stop_benchmark_sut() {
  local pid_file="$1"
  [ -f "$pid_file" ] || return 0
  local pid waited=0 identity_file="${pid_file}.identity" expected_identity current_identity
  pid="$(cat "$pid_file" 2>/dev/null)"
  case "$pid" in
    ''|*[!0-9]*) rm -f "$pid_file" "$identity_file"; return 1 ;;
  esac
  expected_identity="$(cat "$identity_file" 2>/dev/null)"
  current_identity="$(benchmark_process_identity "$pid")"
  if [ -z "$expected_identity" ] || [ "$current_identity" != "$expected_identity" ]; then
    rm -f "$pid_file" "$identity_file"
    return 1
  fi
  kill -TERM "$pid" 2>/dev/null || true
  while [ "$waited" -lt 50 ]; do
    current_identity="$(benchmark_process_identity "$pid")"
    [ "$current_identity" = "$expected_identity" ] || break
    sleep 0.1
    waited=$((waited + 1))
  done
  current_identity="$(benchmark_process_identity "$pid")"
  if [ "$current_identity" = "$expected_identity" ]; then
    kill -KILL "$pid" 2>/dev/null || return 1
    waited=0
    while [ "$waited" -lt 10 ]; do
      current_identity="$(benchmark_process_identity "$pid")"
      [ "$current_identity" = "$expected_identity" ] || break
      sleep 0.1
      waited=$((waited + 1))
    done
    current_identity="$(benchmark_process_identity "$pid")"
    [ "$current_identity" != "$expected_identity" ] || return 1
  fi
  rm -f "$pid_file" "$identity_file"
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

batch_members_settled() {
  local manifest="$1"
  python3 - "$manifest" <<'PY' >/dev/null 2>&1
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    members = payload["members"]
except (OSError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)

if not isinstance(members, list) or not members:
    raise SystemExit(1)
terminal = {"completed", "failed", "stopped", "cancelled"}
raise SystemExit(0 if all(member.get("execution_status") in terminal for member in members) else 1)
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
    needs_human_review|interrupted) status="running" ;;
    SKIP) status="not_started" ;;
    *) status="running" ;;
  esac
  [ "$status" = "not_started" ] && availability="absent"
  printf '%s|%s' "$status" "$availability"
}

run_batch_retro() {
  local aa_bin="$1" manifest="$2" retro_id="$3" agent_cmd="$4" dry_run="$5" log_file="$6"
  batch_members_settled "$manifest" || return 2
  local -a command=(
    "$aa_bin" retro
    --batch-manifest "$manifest"
    --retro-id "$retro_id"
    --json
  )
  [ "$dry_run" = "true" ] && command+=(--dry-run)
  AA_RETRO_AGENT_CMD="$agent_cmd" "${command[@]}" >"$log_file" 2>&1
}

promote_batch_knowledge_proposals() {
  local aa_bin="$1" manifest="$2"
  shift 2
  batch_members_settled "$manifest" || return 2
  local change_id proposal
  local -a proposals=()
  for change_id in "$@"; do
    proposals=("qa/changes/$change_id"/plans/data-knowledge.proposal.*.yaml)
    proposal="${proposals[0]}"
    [ -e "$proposal" ] || continue
    "$aa_bin" knowledge promote --change "$change_id" --yes || return $?
  done
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

dispatch_benchmark_workflow_entrypoint() {
  local timeout_runner="$1" log_file="$2" change_id="$3" aa_bin="$4"
  local entrypoint="$5" params="$6" adapter="$7" agent_cmd="$8"
  "$timeout_runner" "$log_file" "$change_id" \
    "$aa_bin" workflow run \
    --change "$change_id" \
    --entrypoint "$entrypoint" \
    --adapter "$adapter" \
    --params "$params" \
    --agent-cmd "$agent_cmd"
}

summarize_verification_metrics() {
  local change_dir="$1" expected_change_id="$2"
  python3 - "$change_dir" "$expected_change_id" <<'PY'
import json
import sys
from pathlib import Path

change_dir = Path(sys.argv[1])
expected_change_id = sys.argv[2]
inspect = change_dir / "inspect"
names = (
    "metrics-nightly.json",
    "metrics-nightly-shortboards.json",
    "metrics-c-layer.json",
    "quarantine-projection.json",
)
documents = {}
for name in names:
    path = inspect / name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"artifact_invalid:{name}:{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"artifact_invalid:{name}:expected_mapping")
    if payload.get("change_id") != expected_change_id:
        raise SystemExit(
            f"identity_mismatch:{name}:{payload.get('change_id')!r}!={expected_change_id!r}"
        )
    documents[name] = payload

nightly = documents["metrics-nightly.json"]
if nightly.get("schema_version") != "2" or nightly.get("cadence") != "nightly":
    raise SystemExit("artifact_invalid:metrics-nightly.json:contract")
floor_ratio = nightly.get("floor_ratio")
if floor_ratio is not None and (
    isinstance(floor_ratio, bool) or not isinstance(floor_ratio, (int, float))
):
    raise SystemExit("artifact_invalid:metrics-nightly.json:floor_ratio")

shortboards = documents["metrics-nightly-shortboards.json"]
if shortboards.get("source_rel") != "inspect/metrics-nightly.json":
    raise SystemExit("artifact_invalid:metrics-nightly-shortboards.json:source_rel")
verdict = shortboards.get("sufficiency_verdict")
# Mirror of MetricsSufficiencyVerdict; "reject" is the collection-gap /
# no-number-to-judge verdict and is a valid (honest) nightly outcome.
if verdict is not None and verdict not in {"pass", "needs_human", "reject", "stop", "skipped"}:
    raise SystemExit("artifact_invalid:metrics-nightly-shortboards.json:sufficiency_verdict")

c_layer = documents["metrics-c-layer.json"]
if c_layer.get("schema_version") != "1" or c_layer.get("cadence") != "report":
    raise SystemExit("artifact_invalid:metrics-c-layer.json:contract")
vector_names = (
    "escape_rate",
    "counterexample_promotion_rate",
    "coverage_gap_closure_rate",
    "seed_replay_stability",
)
evaluated = 0
for name in vector_names:
    vector = c_layer.get(name)
    if not isinstance(vector, dict) or vector.get("status") not in {
        "evaluated",
        "not_evaluated",
    }:
        raise SystemExit(f"artifact_invalid:metrics-c-layer.json:{name}")
    evaluated += vector["status"] == "evaluated"

quarantine = documents["quarantine-projection.json"]
if quarantine.get("schema_version") != "1" or not isinstance(quarantine.get("entries"), list):
    raise SystemExit("artifact_invalid:quarantine-projection.json:contract")
active = 0
for entry in quarantine["entries"]:
    if not isinstance(entry, dict) or entry.get("status") not in {"active", "released"}:
        raise SystemExit("artifact_invalid:quarantine-projection.json:entry")
    active += entry["status"] == "active"

floor_text = "n/a" if floor_ratio is None else str(floor_ratio)
verdict_text = "not_evaluated" if verdict is None else verdict
print(f"{floor_text}|{verdict_text}|{evaluated}/{len(vector_names)}|{active}", end="")
PY
}

execute_verification_metrics_stage() {
  local workflow_runner="$1" log_file="$2" change_id="$3" change_dir="$4"
  local entrypoint="$5" params="$6" summary
  if ! "$workflow_runner" "$log_file" "$change_id" "$entrypoint" "$params"; then
    printf '%s' "$change_id|failed|n/a|n/a|n/a|n/a"
    return 1
  fi
  if ! summary="$(summarize_verification_metrics "$change_dir" "$change_id")"; then
    printf '%s' "$change_id|invalid_artifacts|n/a|n/a|n/a|n/a"
    return 1
  fi
  printf '%s' "$change_id|completed|$summary"
}

snapshot_verification_metrics() {
  local change_dir="$1" run_dir="$2" change_id="$3"
  python3 - "$change_dir" "$run_dir" "$change_id" <<'PY'
import json
import os
import sys
from pathlib import Path

change_dir = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
change_id = sys.argv[3]
relative_paths = (
    "inspect/metrics-nightly.json",
    "inspect/metrics-nightly-shortboards.json",
    "inspect/metrics-c-layer.json",
    "inspect/quarantine-projection.json",
)
artifacts = {}
try:
    for rel in relative_paths:
        artifacts[rel] = json.loads((change_dir / rel).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"snapshot_source_invalid:{exc}") from exc

payload = {
    "schema_version": "1",
    "change_id": change_id,
    "artifacts": artifacts,
}
raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
run_dir.mkdir(parents=True, exist_ok=True)
target = run_dir / f"{change_id}.verification-metrics.json"
temp = run_dir / f".{change_id}.verification-metrics.{os.getpid()}.tmp"
try:
    temp.write_bytes(raw)
    os.replace(temp, target)
finally:
    temp.unlink(missing_ok=True)
PY
}

snapshot_coverage_repair() {
  local change_dir="$1" run_dir="$2" change_id="$3" python_bin="$4"
  "$python_bin" - "$change_dir" "$run_dir" "$change_id" <<'PY'
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.models.coverage_repair import (
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    CoverageRepairStatus,
)
from assurance_agent.artifacts.models.metrics import MetricsDocument

change_dir = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
change_id = sys.argv[3]


def load(rel, *, required=True):
    path = change_dir / rel
    if not path.is_file():
        if required:
            raise SystemExit(f"artifact_missing:{rel}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"artifact_invalid:{rel}:{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"artifact_invalid:{rel}:expected_mapping")
    if payload.get("change_id") != change_id:
        raise SystemExit(
            f"identity_mismatch:{rel}:{payload.get('change_id')!r}!={change_id!r}"
        )
    return payload


status = load("coverage-repair/status.json")
brief = load("coverage-repair/brief.json")
metrics = load("inspect/metrics.json")
metrics_source = load("inspect/metrics-source-batch.json")
try:
    CoverageRepairStatus.model_validate(status)
    CoverageRepairBrief.model_validate(brief)
    MetricsDocument.model_validate(metrics)
except ValidationError as exc:
    raise SystemExit(f"artifact_invalid:coverage-repair:contract:{exc}") from exc
repair_status = status.get("status")
if repair_status not in {"repaired", "exhausted", "not_eligible", "failed"}:
    raise SystemExit(f"artifact_invalid:coverage-repair/status.json:status={repair_status!r}")
attempts = status.get("attempts_used")
if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
    raise SystemExit("artifact_invalid:coverage-repair/status.json:attempts_used")
post_batch = metrics_source.get("batch_id")
if not isinstance(post_batch, str) or not post_batch:
    raise SystemExit("artifact_invalid:inspect/metrics-source-batch.json:batch_id")

artifacts = {
    "coverage-repair/status.json": status,
    "coverage-repair/brief.json": brief,
    "inspect/metrics.json": metrics,
    "inspect/metrics-source-batch.json": metrics_source,
}
safety_text = "not_run"
if attempts:
    safety = load("coverage-repair/safety-check.json")
    try:
        CoverageRepairSafetyCheck.model_validate(safety)
    except ValidationError as exc:
        raise SystemExit(f"artifact_invalid:coverage-repair/safety-check.json:{exc}") from exc
    if safety.get("attempt") != attempts:
        raise SystemExit("artifact_invalid:coverage-repair/safety-check.json:attempt")
    passed = safety.get("passed")
    needs_review = safety.get("needs_review")
    if not isinstance(passed, bool) or not isinstance(needs_review, bool):
        raise SystemExit("artifact_invalid:coverage-repair/safety-check.json:verdict")
    safety_text = "fail" if not passed else ("review" if needs_review else "pass")
    artifacts["coverage-repair/safety-check.json"] = safety

for rel in (
    "coverage-repair/apply-summary.json",
    "coverage-repair/entry-baseline.json",
):
    optional = load(rel, required=False)
    if optional is not None:
        artifacts[rel] = optional

source_batch = status.get("last_batch_id") or brief.get("batch_id") or "n/a"


def batch_key(value):
    match = re.fullmatch(r"([0-9]{8})-([0-9]{6})(?:-([0-9]{9}))?", value)
    if match is None:
        return None
    try:
        timestamp = datetime.strptime(f"{match.group(1)}-{match.group(2)}", "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return timestamp, int(match.group(3) or "0")


source_key = batch_key(source_batch)
post_key = batch_key(post_batch)
if source_key is None or post_key is None:
    raise SystemExit("artifact_invalid:coverage-repair:batch_id")
if attempts and post_key <= source_key:
    raise SystemExit("artifact_invalid:coverage-repair:post_batch_not_newer")
if not attempts and post_key != source_key:
    raise SystemExit("artifact_invalid:coverage-repair:unexpected_batch_change")
payload = {
    "schema_version": "1",
    "change_id": change_id,
    "artifacts": artifacts,
}
raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
run_dir.mkdir(parents=True, exist_ok=True)
target = run_dir / f"{change_id}.coverage-repair.json"
temp = run_dir / f".{change_id}.coverage-repair.{os.getpid()}.tmp"
try:
    temp.write_bytes(raw)
    os.replace(temp, target)
finally:
    temp.unlink(missing_ok=True)

print(
    f"{change_id}|{repair_status}|{attempts}|{source_batch}|{post_batch}|{safety_text}",
    end="",
)
PY
}

benchmark_verification_metrics_exit_code() {
  local enabled="$1"
  shift
  [ "$enabled" = "true" ] || return 0
  [ "$#" -gt 0 ] || return 1
  local row change_id status floor verdict c_layer quarantine_active
  for row in "$@"; do
    IFS='|' read -r change_id status floor verdict c_layer quarantine_active <<<"$row"
    if [ -z "$change_id" ] || [ "$status" != "completed" ] || \
      [ -z "$floor" ] || [ -z "$verdict" ] || [ -z "$c_layer" ] || \
      [ -z "$quarantine_active" ]; then
      return 1
    fi
    case "$verdict" in
      pass|needs_human|reject|stop|skipped|not_evaluated) ;;
      *) return 1 ;;
    esac
    case "$c_layer" in
      0/4|1/4|2/4|3/4|4/4) ;;
      *) return 1 ;;
    esac
    case "$quarantine_active" in
      ''|*[!0-9]*) return 1 ;;
    esac
  done
  return 0
}

benchmark_coverage_repair_exit_code() {
  [ "$#" -gt 0 ] || return 1
  local row change_id status attempts source_batch post_batch safety
  for row in "$@"; do
    IFS='|' read -r change_id status attempts source_batch post_batch safety <<<"$row"
    [ -n "$change_id" ] && [ -n "$post_batch" ] || return 1
    case "$status" in
      repaired|exhausted|not_eligible|failed) ;;
      *) return 1 ;;
    esac
    case "$attempts" in
      ''|*[!0-9]*) return 1 ;;
    esac
    case "$safety" in
      pass|review|fail|not_run) ;;
      *) return 1 ;;
    esac
    if [ "$status" = "repaired" ] && [ "$attempts" -eq 0 ]; then
      return 1
    fi
    if [ "$status" = "not_eligible" ] && [ "$attempts" -ne 0 ]; then
      return 1
    fi
    if [ "$attempts" -eq 0 ] && [ "$safety" != "not_run" ]; then
      return 1
    fi
    if [ "$attempts" -gt 0 ] && [ "$safety" = "not_run" ]; then
      return 1
    fi
    if [ "$attempts" -gt 0 ] && [ "$source_batch" = "n/a" ]; then
      return 1
    fi
  done
  return 0
}

render_coverage_repair_rows() {
  local row change_id status attempts source_batch post_batch safety
  for row in "$@"; do
    IFS='|' read -r change_id status attempts source_batch post_batch safety <<<"$row"
    printf '| `%s` | %s | %s | `%s` | `%s` | %s |\n' \
      "$change_id" "$status" "$attempts" "$source_batch" "$post_batch" "$safety"
  done
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

assert_no_duplicate_committed_task_ids() {
  local events_file="$1"
  [ -f "$events_file" ] || return 0
  python3 - "$events_file" <<'PY'
import json
import sys
from pathlib import Path

committed = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    event = json.loads(line)
    if event.get("type") != "superstep_committed":
        continue
    for task_id in event.get("committed_task_ids", []):
        committed[task_id] = committed.get(task_id, 0) + 1

duplicates = [task_id for task_id, count in committed.items() if count > 1]
if duplicates:
    print(
        f"WARNING: duplicate committed task ids across restarts: {duplicates[:5]}",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

workflow_attempts_should_stop() {
  case "$1" in
    completed|stopped|failed) return 0 ;;
    *) return 1 ;;
  esac
}

benchmark_interrupt_action() {
  # Proposal promotion is deferred until every Batch member settles. Continue
  # the current invocation without mutating synchronized L1 knowledge so the
  # Batch can actually reach that boundary.
  printf '%s' "accept_risk"
}

benchmark_knowledge_promotion_exit_code() {
  case "$1" in
    completed|not_run) return 0 ;;
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
