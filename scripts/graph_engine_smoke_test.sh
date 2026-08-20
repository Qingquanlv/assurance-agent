#!/usr/bin/env bash
# Build the committed Phase 1 graph-engine tree and prove wheel isolation.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/graph-engine-smoke.XXXXXX")"
trap 'rm -rf "$smoke_root"' EXIT

source_root="$smoke_root/source"
dist_root="$smoke_root/dist"
mkdir -p "$source_root" "$dist_root"
git -C "$repo_root" archive HEAD | tar -x -C "$source_root"

cd "$source_root"
uv build --offline --wheel --package graph-engine --out-dir "$dist_root"
uv build --offline --wheel --package graph-engine-toy-a --out-dir "$dist_root"
uv build --offline --wheel --package graph-engine-toy-b --out-dir "$dist_root"

engine_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'graph_engine-*.whl' -print -quit)"
toy_a_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'graph_engine_toy_a-*.whl' -print -quit)"
toy_b_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'graph_engine_toy_b-*.whl' -print -quit)"
test -f "$engine_wheel"
test -f "$toy_a_wheel"
test -f "$toy_b_wheel"

python3 - "$engine_wheel" "$toy_a_wheel" "$toy_b_wheel" <<'PY'
from __future__ import annotations

import json
import sys
import zipfile
from email.parser import BytesParser


engine_path, toy_a_path, toy_b_path = sys.argv[1:]


def wheel_metadata(path: str):
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        return BytesParser().parsebytes(archive.read(metadata_name))


with zipfile.ZipFile(engine_path) as archive:
    names = sorted(archive.namelist())
    dist_info_roots = {
        name.split("/", 1)[0]
        for name in names
        if name.split("/", 1)[0].endswith(".dist-info")
    }
    assert len(dist_info_roots) == 1, dist_info_roots
    dist_info_root = next(iter(dist_info_roots))
    assert all(
        name.startswith("graph_engine/") or name.startswith(f"{dist_info_root}/")
        for name in names
    ), names
    import_roots = {
        name.split("/", 1)[0]
        for name in names
        if "/" in name and not name.split("/", 1)[0].endswith((".dist-info", ".data"))
    }
    assert import_roots == {"graph_engine"}, import_roots
    forbidden_suffixes = (".yaml", ".yml", ".json", ".md", ".markdown")
    assert not any(name.lower().endswith(forbidden_suffixes) for name in names), names
    assert not any(
        root.startswith(("assurance", "graph_engine_toy")) for root in import_roots
    ), import_roots

for wheel_path in (engine_path, toy_a_path, toy_b_path):
    requirements = [
        value.lower().replace("_", "-")
        for value in wheel_metadata(wheel_path).get_all("Requires-Dist", [])
    ]
    assert not any(
        requirement.startswith(("assurance-agent", "assurance-kernel"))
        for requirement in requirements
    ), (wheel_path, requirements)

print("ENGINE_WHEEL_FILES=" + json.dumps(names, separators=(",", ":")))
PY

uv venv --offline --python 3.11 "$smoke_root/venv-a"
uv pip install \
  --offline \
  --python "$smoke_root/venv-a/bin/python" \
  --find-links "$dist_root" \
  "$engine_wheel"
"$smoke_root/venv-a/bin/python" - <<'PY'
import importlib.util

import graph_engine

assert graph_engine.ENGINE_API_VERSION == "1.0"
for package in ("assurance_agent", "assurance_kernel", "graph_engine_toy_a", "graph_engine_toy_b"):
    assert importlib.util.find_spec(package) is None, package
PY
set +e
"$smoke_root/venv-a/bin/python" -m graph_engine run >"$smoke_root/missing-product.out" 2>&1
missing_product_status="$?"
set -e
test "$missing_product_status" -eq 2
grep -q "product is required" "$smoke_root/missing-product.out"

uv venv --offline --python 3.11 "$smoke_root/venv-b"
uv pip install \
  --offline \
  --python "$smoke_root/venv-b/bin/python" \
  --find-links "$dist_root" \
  "$engine_wheel" \
  "$toy_a_wheel"
"$smoke_root/venv-b/bin/python" -m graph_engine compile \
  --product toy-a >"$smoke_root/toy-a-compile.json"
"$smoke_root/venv-b/bin/python" -m graph_engine run \
  --product toy-a \
  --plugin toy-a \
  --entrypoint hello \
  --invocation-id smoke-a \
  --root "$smoke_root/toy-a-root" >"$smoke_root/toy-a-run.json"
"$smoke_root/venv-b/bin/python" - "$smoke_root/toy-a-compile.json" "$smoke_root/toy-a-run.json" <<'PY'
import importlib.util
import json
import sys

compiled = json.load(open(sys.argv[1], encoding="utf-8"))
completed = json.load(open(sys.argv[2], encoding="utf-8"))
assert compiled["compiled_digest"] == completed["compiled_digest"]
assert completed["status"] == "succeeded", completed
assert completed["output"] == {"message": "hello Ada"}, completed
for package in ("assurance_agent", "assurance_kernel", "graph_engine_toy_b"):
    assert importlib.util.find_spec(package) is None, package
print("TOY_A_EVIDENCE=" + json.dumps(completed, sort_keys=True, separators=(",", ":")))
PY

uv venv --offline --python 3.11 "$smoke_root/venv-c"
uv pip install \
  --offline \
  --python "$smoke_root/venv-c/bin/python" \
  --find-links "$dist_root" \
  "$engine_wheel" \
  "$toy_b_wheel"
"$smoke_root/venv-c/bin/python" - "$smoke_root/toy-b-root" <<'PY'
from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest
from graph_engine.product import load_plugin_entrypoint, load_product_entrypoint, resolve_product
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.ledger import Ledger


class TrustedSmokeHost:
    """Explicit in-process host for reviewed toy-wheel smoke execution only."""

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        return await handler(
            request,
            TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
        )


for package in ("assurance_agent", "assurance_kernel", "graph_engine_toy_a"):
    assert importlib.util.find_spec(package) is None, package

product = load_product_entrypoint("toy-b")
plugin = load_plugin_entrypoint("toy-b")
resolved = resolve_product(product, {"toy.b": plugin})
with Engine(Path(sys.argv[1]), host=TrustedSmokeHost()) as engine:
    with engine.start(resolved, entrypoint="review", invocation_id="smoke-b") as handle:
        blocked = engine.run_until_blocked(handle)
        assert blocked.status == "interrupted", blocked
        with engine.resume(handle, action="approve", payload={"reviewer": "smoke"}) as resumed:
            completed = engine.run_until_blocked(resumed)
            assert completed.status == "succeeded", completed
            envelopes = Ledger(resumed.invocation_root / "ledger").read_all()
            ledger_digest = canonical_digest(
                [envelope.model_dump(mode="json") for envelope in envelopes]
            )
            with resumed.workspace as workspace:
                final_tree_id = workspace.head_tree_id()

evidence = {
    "blocked_status": blocked.status,
    "terminal_status": completed.status,
    "compiled_digest": resolved.workflow.digest,
    "product_digest": resolved.digest,
    "ledger_digest": ledger_digest,
    "final_tree_id": final_tree_id,
}
print("TOY_B_EVIDENCE=" + json.dumps(evidence, sort_keys=True, separators=(",", ":")))
PY

echo "graph-engine smoke test: OK"
