#!/usr/bin/env bash
# Build the committed Phase 1 graph-engine tree and prove wheel isolation.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/graph-engine-smoke.XXXXXX")"
smoke_root="$(cd "$smoke_root" && pwd -P)"

cleanup() {
  chmod -R u+w "$smoke_root" 2>/dev/null || true
  rm -rf "$smoke_root"
}
trap cleanup EXIT

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

uv run \
  --offline \
  --no-project \
  --python 3.11 \
  --managed-python \
  --no-python-downloads \
  python - "$engine_wheel" "$toy_a_wheel" "$toy_b_wheel" <<'PY'
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

# All installed-wheel checks run outside both the repository and archived
# source trees so imports cannot succeed through the current working directory.
cd "$smoke_root"

uv venv --offline --python 3.11 "$smoke_root/venv-a"
uv pip install \
  --offline \
  --python "$smoke_root/venv-a/bin/python" \
  --find-links "$dist_root" \
  "$engine_wheel"
"$smoke_root/venv-a/bin/python" - <<'PY'
import importlib.util

import graph_engine

assert graph_engine.ENGINE_API_VERSION == "2.0"
for package in ("assurance_agent", "assurance_kernel", "graph_engine_toy_a", "graph_engine_toy_b"):
    assert importlib.util.find_spec(package) is None, package
PY
set +e
"$smoke_root/venv-a/bin/python" -m graph_engine run >"$smoke_root/missing-product.out" 2>&1
missing_product_status="$?"
set -e
test "$missing_product_status" -eq 2
grep -q "product distribution is required" "$smoke_root/missing-product.out"

uv venv --offline --python 3.11 "$smoke_root/venv-b"
uv pip install \
  --offline \
  --python "$smoke_root/venv-b/bin/python" \
  --find-links "$dist_root" \
  "$engine_wheel" \
  "$toy_a_wheel"
"$smoke_root/venv-b/bin/python" -m graph_engine compile \
  --product-dist graph-engine-toy-a \
  --product-entrypoint toy-a \
  --plugin-dist graph-engine-toy-a \
  --plugin-entrypoint toy-a >"$smoke_root/toy-a-compile.json"
"$smoke_root/venv-b/bin/python" -m graph_engine run \
  --product-dist graph-engine-toy-a \
  --product-entrypoint toy-a \
  --plugin-dist graph-engine-toy-a \
  --plugin-entrypoint toy-a \
  --entrypoint hello \
  --invocation-id smoke-a \
  --root "$smoke_root/toy-a-root" >"$smoke_root/toy-a-run.json"
"$smoke_root/venv-b/bin/python" - "$smoke_root/toy-a-compile.json" "$smoke_root/toy-a-run.json" <<'PY'
import importlib.util
import json
import sys

from graph_engine.composition import (
    RegistryPlatform,
    ResolutionRequest,
    WheelPluginSource,
    WheelProductSource,
)

compiled = json.load(open(sys.argv[1], encoding="utf-8"))
completed = json.load(open(sys.argv[2], encoding="utf-8"))
assert compiled["compiled_digest"] == completed["compiled_digest"]
assert completed["status"] == "succeeded", completed
assert completed["output"] == {"message": "hello Ada"}, completed
for package in ("assurance_agent", "assurance_kernel", "graph_engine_toy_b"):
    assert importlib.util.find_spec(package) is None, package
composition = RegistryPlatform().resolve(
    ResolutionRequest(
        product=WheelProductSource(
            distribution="graph-engine-toy-a",
            entrypoint_name="toy-a",
            declaration_path="graph_engine_toy_a/product-declaration.json",
        ),
        plugins=(
            WheelPluginSource(
                distribution="graph-engine-toy-a",
                entrypoint_name="toy-a",
                declaration_path="graph_engine_toy_a/plugin-declaration.json",
            ),
        ),
    )
)
assert composition.manifest.product_id == "toy.a"
assert composition.lock.product.source.identity["declaration_path"] == (
    "graph_engine_toy_a/product-declaration.json"
)
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
from collections.abc import Mapping
from pathlib import Path

from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    RegistryPlatform,
    ResolutionRequest,
    WheelPluginSource,
    WheelProductSource,
)
from graph_engine.plugin_api import (
    InvocationWorkspaceBinding,
    TaskContext,
    TaskHandler,
)
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import TaskHostCallResult, TaskHostExecuteCall
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.task_workspace import TaskWorkspaceStore


class TrustedSmokeHost:
    """Explicit in-process host for reviewed toy-wheel smoke execution only."""

    def __init__(self) -> None:
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: TaskWorkspaceStore,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        identity = call.attempt_root.workspace_identity
        binding = self._store.begin(
            task_id=identity.task_id,
            attempt=identity.attempt,
            output_paths=identity.output_paths,
        )
        outcome = await handler.execute(
            call.request,
            TaskContext(
                project_root=binding.project_root,
                write_root=binding.write_root,
                workspace_identity=binding.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)


for package in ("assurance_agent", "assurance_kernel", "graph_engine_toy_a"):
    assert importlib.util.find_spec(package) is None, package

resolved = RegistryPlatform().resolve(
    ResolutionRequest(
        product=WheelProductSource(
            distribution="graph-engine-toy-b",
            entrypoint_name="toy-b",
            declaration_path="graph_engine_toy_b/product-declaration.json",
        ),
        plugins=(
            WheelPluginSource(
                distribution="graph-engine-toy-b",
                entrypoint_name="toy-b",
                declaration_path="graph_engine_toy_b/plugin-declaration.json",
            ),
        ),
    )
)
root = Path(sys.argv[1])
project_root = root.parent / f".{root.name}-project"
attempts_root = root.parent / f".{root.name}-attempts"
receipts_root = root.parent / f".{root.name}-receipts"
for path in (project_root, attempts_root, receipts_root):
    path.mkdir(exist_ok=True)
workspace_binding = InvocationWorkspaceBinding(
    project_root=project_root,
    attempts_root=attempts_root,
    receipts_root=receipts_root,
)
with Engine(root, host=TrustedSmokeHost()) as engine:
    with engine.start(
        resolved,
        entrypoint="review",
        invocation_id="smoke-b",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    ) as handle:
        blocked = engine.run_until_blocked(handle)
        assert blocked.status == "interrupted", blocked
        with engine.resume(handle, action="approve", payload={"reviewer": "smoke"}) as resumed:
            completed = engine.run_until_blocked(resumed)
            assert completed.status == "succeeded", completed
            envelopes = Ledger(resumed.invocation_root / "ledger").read_all()
            ledger_digest = canonical_digest(
                [envelope.model_dump(mode="json") for envelope in envelopes]
            )
evidence = {
    "blocked_status": blocked.status,
    "terminal_status": completed.status,
    "compiled_digest": resolved.workflow.digest,
    "lock_digest": resolved.lock_digest,
    "ledger_digest": ledger_digest,
}
print("TOY_B_EVIDENCE=" + json.dumps(evidence, sort_keys=True, separators=(",", ":")))
PY

echo "graph-engine smoke test: OK"
