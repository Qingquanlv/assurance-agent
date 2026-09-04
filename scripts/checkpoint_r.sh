#!/usr/bin/env bash
# Protected exact-candidate Checkpoint R. Fail closed; no skip, xfail, or waiver path.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

if [[ "${AA_CHECKPOINT_R_LIVE:-}" != "1" ]]; then
  echo "missing protected input: AA_CHECKPOINT_R_LIVE=1" >&2
  exit 1
fi
if [[ -n "${OPENCODE_MODEL:-}" ]]; then
  echo "OPENCODE_MODEL override is not accepted" >&2
  exit 1
fi
for name in \
  CHECKPOINT_R_OPENCODE_AUTH_JSON \
  CHECKPOINT_R_OPENCODE_PROVIDER_JSON \
  CHECKPOINT_R_OPENCODE_BINARY_SHA256 \
  CHECKPOINT_R_SERVER_SECRET \
  RUNNER_TEMP \
  GITHUB_SHA
do
  if [[ -z "${!name:-}" ]]; then
    echo "missing protected input: ${name}" >&2
    exit 1
  fi
done

runner_temp="$(cd "$RUNNER_TEMP" && pwd -P)"
case "$runner_temp" in
  "$repo_root"|"$repo_root"/*)
    echo "RUNNER_TEMP must not be inside the repository" >&2
    exit 1
    ;;
esac

head_sha="$(git rev-parse HEAD)"
if [[ "$head_sha" != "$GITHUB_SHA" ]]; then
  echo "checked-out HEAD ${head_sha} is not GITHUB_SHA ${GITHUB_SHA}" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "checkout is not clean" >&2
  git status --porcelain >&2
  exit 1
fi

export PYTHONNOUSERSITE=1
uv sync --dev

work="$runner_temp/checkpoint-r-$head_sha"
mkdir -p "$work"
auth_path="$work/opencode-auth.json"
provider_path="$work/opencode-provider.json"
printf '%s' "$CHECKPOINT_R_OPENCODE_AUTH_JSON" >"$auth_path"
printf '%s' "$CHECKPOINT_R_OPENCODE_PROVIDER_JSON" >"$provider_path"
chmod 600 "$auth_path" "$provider_path"

binary="$(command -v opencode || true)"
if [[ -z "$binary" ]]; then
  echo "missing official OpenCode binary" >&2
  exit 1
fi
actual_digest="$(uv run python - "$binary" <<'PY'
import hashlib, sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
expected_digest="$(printf '%s' "$CHECKPOINT_R_OPENCODE_BINARY_SHA256" | tr '[:upper:]' '[:lower:]')"
if [[ "$actual_digest" != "$expected_digest" ]]; then
  echo "OpenCode binary digest ${actual_digest} does not match the protected digest" >&2
  exit 1
fi

state_root="$work/opencode-state"
project_root="$work/live-project"
mkdir -p "$state_root/home" "$state_root/xdg-config/opencode" "$state_root/xdg-data/opencode" "$project_root"
uv run python - "$project_root" <<'PY'
from pathlib import Path
import sys
from tests.product.checkpoint_r_support import seed_change_workspace
from tests.product.cli_support import write_project_dir
root = write_project_dir(Path(sys.argv[1]))
seed_change_workspace(root)
print(root)
PY
uv run python - "$provider_path" "$state_root/xdg-config/opencode/opencode.json" "$project_root/opencode.json" <<'PY'
import json, sys
from pathlib import Path
provider = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(provider, dict) or not isinstance(provider.get("provider"), dict):
    raise SystemExit("CHECKPOINT_R_OPENCODE_PROVIDER_JSON must carry a provider object")
if not isinstance(provider.get("model"), str) or not provider["model"].strip():
    raise SystemExit("protected provider configuration must lock an exact model")
agents = {
    "agent": {
        "assurance-v1-doc-author": {
            "description": "Checkpoint R Product Agent",
            "mode": "all",
            "prompt": "Execute the supplied instructions and return exactly one JSON object.",
        }
    }
}
Path(sys.argv[2]).write_text(json.dumps({**agents, "provider": provider["provider"]}), encoding="utf-8")
Path(sys.argv[3]).write_text(json.dumps(agents), encoding="utf-8")
print(provider["model"].strip())
PY
locked_model="$(uv run python - "$provider_path" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["model"].strip())
PY
)"
cp "$auth_path" "$state_root/xdg-data/opencode/auth.json"
chmod 600 "$state_root/xdg-data/opencode/auth.json"
git -C "$project_root" init -q

sock_port="$(uv run python - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"
endpoint="http://127.0.0.1:${sock_port}"
serve_log="$state_root/serve.log"
(
  cd "$project_root"
  HOME="$state_root/home" \
  XDG_CONFIG_HOME="$state_root/xdg-config" \
  XDG_DATA_HOME="$state_root/xdg-data" \
    "$binary" serve --port "$sock_port" --hostname 127.0.0.1 \
    >"$serve_log" 2>&1
) &
serve_pid=$!
cleanup() {
  if [[ -n "${serve_pid:-}" ]] && kill -0 "$serve_pid" 2>/dev/null; then
    kill "$serve_pid" 2>/dev/null || true
    wait "$serve_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

uv run python - "$endpoint" "$serve_log" "$serve_pid" <<'PY'
import sys, time, urllib.request
endpoint, log_path, pid = sys.argv[1], sys.argv[2], int(sys.argv[3])
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"{endpoint}/global/health", timeout=1) as response:
            if response.status == 200:
                raise SystemExit(0)
    except OSError:
        time.sleep(0.1)
raise SystemExit(f"official OpenCode serve did not become healthy; see {log_path}")
PY

deployment_yaml="$work/deployment.yaml"
uv run python - "$repo_root/tests/product/fixtures/deployment/checkpoint-r-opencode.yaml" "$deployment_yaml" "$endpoint" "$locked_model" "$project_root" <<'PY'
import sys
from pathlib import Path
import yaml
from tests.product.checkpoint_r_support import apply_live_deployment_overrides
source, destination, endpoint, model, project_scope = sys.argv[1:6]
document = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
apply_live_deployment_overrides(
    document,
    endpoint=endpoint,
    model=model,
    project_scope=project_scope,
)
if float(document["adapter_binding"]["observation_horizon_seconds"]) <= 30:
    raise SystemExit("live package must not lock the 30s fixture horizon")
Path(destination).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
PY

wheel_root="$work/deployment-wheel"
mkdir -p "$wheel_root"
binding_meta="$work/binding-meta.json"
uv run python - "$deployment_yaml" "$wheel_root" "$binding_meta" <<'PY'
import json, sys
from pathlib import Path
from assurance_product.binding_builder import build_deployment_wheel
built = build_deployment_wheel(Path(sys.argv[1]), Path(sys.argv[2]))
Path(sys.argv[3]).write_text(
    json.dumps(
        {
            "distribution": built.distribution,
            "declaration_path": built.declaration_path,
            "wheel": str(built.wheel),
        }
    ),
    encoding="utf-8",
)
print(built.distribution)
print(built.declaration_path)
PY
binding_dist="$(uv run python - "$binding_meta" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["distribution"])
PY
)"
binding_declaration="$(uv run python - "$binding_meta" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["declaration_path"])
PY
)"

export AA_CHECKPOINT_R_BINDING_DIST="$binding_dist"
export AA_CHECKPOINT_R_BINDING_DECLARATION="$binding_declaration"
export AA_CHECKPOINT_R_CONFIG_TREE="$repo_root/tests/product/fixtures/project-config"
export AA_CHECKPOINT_R_SECRET_ENV="CHECKPOINT_R_MATERIALIZED_TOKEN"
export CHECKPOINT_R_MATERIALIZED_TOKEN="$CHECKPOINT_R_SERVER_SECRET"
export AA_CHECKPOINT_R_EVIDENCE="$work/agent-rows.jsonl"
export AA_CHECKPOINT_R_OPENCODE_BINARY="$binary"
export AA_CHECKPOINT_R_PROJECT_ROOT="$project_root"

live_junit="$work/live-junit.xml"
det_junit="$work/deterministic-junit.xml"
uv run pytest -q \
  tests/product/test_checkpoint_r_live.py \
  -m checkpoint_r_live \
  --tb=short \
  --junitxml="$live_junit"

uv run pytest -q \
  tests/product/test_raw_agent_checkpoint.py \
  tests/architecture/test_attempt_runtime_production_closure.py \
  tests/product/test_python_native_cutover.py \
  tests/product/test_cli_langgraph_lifecycle.py \
  tests/product/test_application_export_archive.py \
  tests/product/test_archive_after_publish.py \
  tests/product/test_langgraph_sqlite_restart.py \
  tests/product/test_replay_properties.py \
  tests/product/test_cli_sqlite_system_interrupt.py \
  tests/product/test_binding_builder_security.py \
  tests/product/test_export_security.py \
  tests/product/test_project_configuration_security.py \
  packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py \
  packages/framework/graph-engine/tests/attempts/test_fault_matrix.py \
  packages/capabilities/assurance-improvement/tests/test_effects.py \
  packages/capabilities/assurance-healing/tests/test_effects.py \
  packages/capabilities/assurance-improvement/tests/test_evaluate_graph_contract.py \
  -m "not checkpoint_r_live" \
  --tb=short \
  --junitxml="$det_junit"

bash scripts/graph_engine_smoke_test.sh
bash scripts/assurance_capability_wheel_smoke_test.sh
bash scripts/assurance_product_wheel_smoke_test.sh

manifest="$runner_temp/checkpoint-r-${head_sha}.json"
uv run python - "$repo_root" "$head_sha" "$actual_digest" "$locked_model" "$live_junit" "$det_junit" "$AA_CHECKPOINT_R_EVIDENCE" "$manifest" "$binding_meta" <<'PY'
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

repo, sha, binary_digest, model, live_junit, det_junit, evidence_path, manifest_path, binding_meta = sys.argv[1:]

def load_cases(path: str) -> list[dict[str, object]]:
    root = ET.parse(path).getroot()
    rows: list[dict[str, object]] = []
    for case in root.iter("testcase"):
        status = "passed"
        if case.find("skipped") is not None:
            status = "skipped"
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("error") is not None:
            status = "failed"
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        if "xfail" in name or case.find("skipped") is not None and "xfail" in (case.find("skipped").attrib.get("message") or ""):
            status = "xfailed" if status == "skipped" else status
        rows.append(
            {
                "id": f"{classname}::{name}",
                "status": status,
                "kind": "pytest",
            }
        )
    return rows

live_rows = load_cases(live_junit)
det_rows = load_cases(det_junit)
agent_rows = []
evidence = Path(evidence_path)
if evidence.is_file():
    for line in evidence.read_text(encoding="utf-8").splitlines():
        if line.strip():
            agent_rows.append(json.loads(line))

forbidden = {"skipped", "xfailed", "cancelled", "waived", "timed_out", "failed"}
for row in (*live_rows, *det_rows, *agent_rows):
    if row.get("status") in forbidden:
        raise SystemExit(f"Checkpoint R row is not passing: {row}")

ids = [str(row.get("contract_id") or row.get("id")) for row in agent_rows]
if len(ids) != 33 or len(set(ids)) != 33:
    raise SystemExit(f"expected 33 unique Agent evidence rows, found {ids}")
live_ids = [row["id"] for row in live_rows]
if len(live_ids) != 33 or len(set(live_ids)) != 33:
    raise SystemExit(f"expected 33 unique live pytest rows, found {live_ids}")

binding = json.loads(Path(binding_meta).read_text(encoding="utf-8"))
document = {
    "schema_version": "checkpoint-r-evidence-v1",
    "candidate_sha": sha,
    "github_sha": sha,
    "opencode_binary_digest": binary_digest,
    "provider": "opencode",
    "model": model,
    "deployment": binding,
    "agent_rows": agent_rows,
    "live_pytest": live_rows,
    "deterministic_pytest": det_rows,
    "wheel_smokes": (
        "scripts/graph_engine_smoke_test.sh",
        "scripts/assurance_capability_wheel_smoke_test.sh",
        "scripts/assurance_product_wheel_smoke_test.sh",
    ),
    "status": "passed",
}
Path(manifest_path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
embedded = json.loads(Path(manifest_path).read_text(encoding="utf-8"))["candidate_sha"]
if embedded != sha:
    raise SystemExit("evidence manifest SHA does not match the candidate")
print(f"CHECKPOINT_R_OK candidate_sha={sha} manifest={manifest_path}")
PY

test "$(uv run python - "$manifest" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["candidate_sha"])
PY
)" = "$head_sha"
test "$(uv run python - "$manifest" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["github_sha"])
PY
)" = "$GITHUB_SHA"
echo "CHECKPOINT_R_OK candidate_sha=${head_sha}"
