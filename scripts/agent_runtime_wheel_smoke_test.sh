#!/usr/bin/env bash
# Build committed agent-runtime wheels and prove explicit rebinding isolation.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/agent-runtime-smoke.XXXXXX")"
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
uv build --offline --wheel --package agent-runtime-contracts --out-dir "$dist_root"
uv build --offline --wheel --package agent-runtime-opencode --out-dir "$dist_root"
uv build --offline --wheel --package agent-runtime-cursor --out-dir "$dist_root"
uv build --offline --wheel --package agent-runtime-fixture --out-dir "$dist_root"

engine_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'graph_engine-*.whl' -print -quit)"
contracts_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'agent_runtime_contracts-*.whl' -print -quit)"
opencode_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'agent_runtime_opencode-*.whl' -print -quit)"
cursor_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'agent_runtime_cursor-*.whl' -print -quit)"
fixture_wheel="$(find "$dist_root" -maxdepth 1 -type f -name 'agent_runtime_fixture-*.whl' -print -quit)"
test -f "$engine_wheel"
test -f "$contracts_wheel"
test -f "$opencode_wheel"
test -f "$cursor_wheel"
test -f "$fixture_wheel"

uv run \
  --offline \
  --no-project \
  --python 3.11 \
  --managed-python \
  --no-python-downloads \
  python - "$engine_wheel" "$contracts_wheel" "$opencode_wheel" "$cursor_wheel" "$fixture_wheel" <<'PY'
from __future__ import annotations

import sys
import zipfile
from email.parser import BytesParser

paths = sys.argv[1:]


def wheel_metadata(path: str):
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        return BytesParser().parsebytes(archive.read(metadata_name))


def requirements(path: str) -> list[str]:
    return [
        value.lower().replace("_", "-")
        for value in wheel_metadata(path).get_all("Requires-Dist", [])
    ]


forbidden = ("assurance-agent", "assurance-kernel")
for path in paths:
    found = requirements(path)
    assert not any(item.startswith(forbidden) for item in found), (path, found)

assert not any(item.startswith(("agent-runtime-opencode", "agent-runtime-cursor")) for item in requirements(paths[4]))
assert not any(item.startswith(("agent-runtime-cursor", "agent-runtime-fixture")) for item in requirements(paths[2]))
assert not any(item.startswith(("agent-runtime-opencode", "agent-runtime-fixture")) for item in requirements(paths[3]))
print("WHEEL_METADATA_OK")
PY

# Installed-wheel checks run outside the repository and archived source trees.
cd "$smoke_root"
manifest_root="$source_root/examples/agent-runtime-fixture/manifests"

install_env() {
  local name="$1"
  shift
  uv venv --offline --python 3.11 "$smoke_root/$name"
  # Local wheels stay on --find-links. Do not pass --offline: yanked
  # pydantic==2.12.1 poisons uv's offline resolver on CI caches.
  uv pip install \
    --python "$smoke_root/$name/bin/python" \
    --find-links "$dist_root" \
    "$@"
}

assert_no_default_product() {
  local python_bin="$1"
  local out="$2"
  set +e
  "$python_bin" -m graph_engine run >"$out" 2>&1
  local status="$?"
  set -e
  test "$status" -eq 2
  grep -q "product distribution is required" "$out"
}

# 1. Graph Engine + contracts only.
install_env venv-contracts "$engine_wheel" "$contracts_wheel"
"$smoke_root/venv-contracts/bin/python" - <<'PY'
import importlib.util
import sys

import agent_runtime_contracts
import graph_engine

assert graph_engine.ENGINE_API_VERSION == "2.0"
for package in (
    "agent_runtime_opencode",
    "agent_runtime_cursor",
    "agent_runtime_fixture",
    "assurance_agent",
    "assurance_kernel",
):
    assert importlib.util.find_spec(package) is None, package
    assert package not in sys.modules, package
PY
assert_no_default_product "$smoke_root/venv-contracts/bin/python" "$smoke_root/missing-product-contracts.out"

# 2. Graph Engine + contracts + OpenCode + fixture.
install_env venv-opencode "$engine_wheel" "$contracts_wheel" "$opencode_wheel" "$fixture_wheel"
"$smoke_root/venv-opencode/bin/python" - <<'PY'
import importlib.util
import sys

assert importlib.util.find_spec("agent_runtime_cursor") is None
assert importlib.util.find_spec("assurance_agent") is None
assert importlib.util.find_spec("assurance_kernel") is None
import agent_runtime_fixture

assert "agent_runtime_opencode" not in sys.modules
assert "agent_runtime_cursor" not in sys.modules
assert "assurance_agent" not in sys.modules
import agent_runtime_opencode

assert "agent_runtime_cursor" not in sys.modules
assert "assurance_agent" not in sys.modules
PY
"$smoke_root/venv-opencode/bin/python" - "$manifest_root/opencode.json" <<'PY'
import json
import sys

from agent_runtime_contracts.schema import canonical_json_bytes, thaw_json
from graph_engine.composition import RegistryPlatform, ResolutionRequest

composition = RegistryPlatform().resolve(
    ResolutionRequest.model_validate(json.loads(open(sys.argv[1], encoding="utf-8").read()))
)
from agent_runtime_fixture import (
    EXPECTED_AGENT_RUN_REQUEST_BYTES,
    assemble_request,
    fixture_config,
    fixture_resources,
)

binding = composition.registries.capabilities.bindings["fixture.binding.run"]
assert binding.target_capability_id == "runtime.opencode.execute"
graph = composition.workflow.graphs[composition.manifest.entrypoints["run"]]
assert canonical_json_bytes(thaw_json(graph.nodes[graph.start].definition.input)) == (
    assemble_request(fixture_resources(), fixture_config()).canonical_bytes()
)
assert assemble_request(fixture_resources(), fixture_config()).canonical_bytes() == (
    EXPECTED_AGENT_RUN_REQUEST_BYTES
)
print("OPENCODE_BINDING_OK")
PY
assert_no_default_product "$smoke_root/venv-opencode/bin/python" "$smoke_root/missing-product-opencode.out"

# 3. Graph Engine + contracts + Cursor + fixture.
install_env venv-cursor "$engine_wheel" "$contracts_wheel" "$cursor_wheel" "$fixture_wheel"
"$smoke_root/venv-cursor/bin/python" - <<'PY'
import importlib.util
import sys

assert importlib.util.find_spec("agent_runtime_opencode") is None
assert importlib.util.find_spec("assurance_agent") is None
assert importlib.util.find_spec("assurance_kernel") is None
import agent_runtime_fixture

assert "agent_runtime_opencode" not in sys.modules
assert "agent_runtime_cursor" not in sys.modules
import agent_runtime_cursor

assert "agent_runtime_opencode" not in sys.modules
assert "assurance_agent" not in sys.modules
PY
"$smoke_root/venv-cursor/bin/python" - "$manifest_root/cursor.json" <<'PY'
import json
import sys

from agent_runtime_contracts.schema import canonical_json_bytes, thaw_json
from graph_engine.composition import RegistryPlatform, ResolutionRequest

composition = RegistryPlatform().resolve(
    ResolutionRequest.model_validate(json.loads(open(sys.argv[1], encoding="utf-8").read()))
)
from agent_runtime_fixture import EXPECTED_AGENT_RUN_REQUEST_BYTES

binding = composition.registries.capabilities.bindings["fixture.binding.run"]
assert binding.target_capability_id == "runtime.cursor.execute"
graph = composition.workflow.graphs[composition.manifest.entrypoints["run"]]
assert canonical_json_bytes(thaw_json(graph.nodes[graph.start].definition.input)) == (
    EXPECTED_AGENT_RUN_REQUEST_BYTES
)
print("CURSOR_BINDING_OK")
PY
assert_no_default_product "$smoke_root/venv-cursor/bin/python" "$smoke_root/missing-product-cursor.out"

# 4. Both adapters + fixture, one explicit selected binding.
install_env venv-both "$engine_wheel" "$contracts_wheel" "$opencode_wheel" "$cursor_wheel" "$fixture_wheel"
"$smoke_root/venv-both/bin/python" - <<'PY'
import sys

import agent_runtime_fixture

assert "agent_runtime_opencode" not in sys.modules
assert "agent_runtime_cursor" not in sys.modules
assert "assurance_agent" not in sys.modules
import agent_runtime_opencode

assert "agent_runtime_cursor" not in sys.modules
assert "assurance_agent" not in sys.modules
import agent_runtime_cursor

assert "assurance_agent" not in sys.modules
PY
"$smoke_root/venv-both/bin/python" - "$manifest_root/opencode.json" "$smoke_root/opencode.lock" <<'PY'
import json
import sys

from agent_runtime_contracts.schema import canonical_json_bytes, thaw_json
from graph_engine.composition import RegistryPlatform, ResolutionRequest

composition = RegistryPlatform().resolve(
    ResolutionRequest.model_validate(json.loads(open(sys.argv[1], encoding="utf-8").read()))
)
from agent_runtime_fixture import EXPECTED_AGENT_RUN_REQUEST_BYTES

assert composition.registries.capabilities.bindings["fixture.binding.run"].target_capability_id == (
    "runtime.opencode.execute"
)
graph = composition.workflow.graphs[composition.manifest.entrypoints["run"]]
assert canonical_json_bytes(thaw_json(graph.nodes[graph.start].definition.input)) == (
    EXPECTED_AGENT_RUN_REQUEST_BYTES
)
open(sys.argv[2], "w", encoding="utf-8").write(composition.lock_digest)
print("SELECTED_OPENCODE_OK")
PY
"$smoke_root/venv-both/bin/python" - "$manifest_root/cursor.json" "$smoke_root/cursor.lock" <<'PY'
import json
import sys

from agent_runtime_contracts.schema import canonical_json_bytes, thaw_json
from graph_engine.composition import RegistryPlatform, ResolutionRequest

composition = RegistryPlatform().resolve(
    ResolutionRequest.model_validate(json.loads(open(sys.argv[1], encoding="utf-8").read()))
)
from agent_runtime_fixture import EXPECTED_AGENT_RUN_REQUEST_BYTES

assert composition.registries.capabilities.bindings["fixture.binding.run"].target_capability_id == (
    "runtime.cursor.execute"
)
graph = composition.workflow.graphs[composition.manifest.entrypoints["run"]]
assert canonical_json_bytes(thaw_json(graph.nodes[graph.start].definition.input)) == (
    EXPECTED_AGENT_RUN_REQUEST_BYTES
)
open(sys.argv[2], "w", encoding="utf-8").write(composition.lock_digest)
print("SELECTED_CURSOR_OK")
PY
test "$(cat "$smoke_root/opencode.lock")" != "$(cat "$smoke_root/cursor.lock")"
echo "SELECTED_BINDING_OK"
assert_no_default_product "$smoke_root/venv-both/bin/python" "$smoke_root/missing-product-both.out"

echo "agent-runtime wheel smoke test: OK"
