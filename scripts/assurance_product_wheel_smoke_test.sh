#!/usr/bin/env bash
# Build committed Phase 5 product wheels and prove isolation plus source authentication.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/assurance-product-smoke.XXXXXX")"
smoke_root="$(cd "$smoke_root" && pwd -P)"

cleanup() {
  local status=$?
  if [[ -n "${smoke_root:-}" && -d "$smoke_root" ]]; then
    chmod -R u+w "$smoke_root" 2>/dev/null || true
    rm -rf "$smoke_root" || true
  fi
  return "$status"
}
trap cleanup EXIT

unset PYTHONPATH
unset UV_PROJECT
export PYTHONNOUSERSITE=1

source_root="$smoke_root/source"
dist_root="$smoke_root/dist"
bindings_root="$smoke_root/bindings"
mkdir -p "$source_root" "$dist_root" "$bindings_root"
(
  cd "$repo_root"
  git archive HEAD
) | tar -x -C "$source_root"

cd "$source_root"
for package in \
  graph-engine \
  agent-runtime-contracts \
  assurance-intake \
  assurance-generation \
  assurance-execution \
  assurance-healing \
  assurance-quality \
  assurance-improvement \
  assurance-product \
  agent-runtime-opencode \
  agent-runtime-cursor
do
  uv build \
    --offline \
    --wheel \
    --no-sources \
    --python 3.11 \
    --package "$package" \
    --out-dir "$dist_root"
done

wheel_for() {
  local pattern="$1"
  local found
  found="$(find "$dist_root" -maxdepth 1 -type f -name "$pattern" -print)"
  if [[ -z "$found" ]]; then
    echo "missing wheel matching $pattern" >&2
    ls -la "$dist_root" >&2 || true
    exit 1
  fi
  local count
  count="$(printf '%s\n' "$found" | grep -c .)"
  if [[ "$count" -ne 1 ]]; then
    echo "expected exactly one wheel matching $pattern, found: $found" >&2
    exit 1
  fi
  printf '%s\n' "$found"
}

engine_wheel="$(wheel_for 'graph_engine-*.whl')"
contracts_wheel="$(wheel_for 'agent_runtime_contracts-*.whl')"
intake_wheel="$(wheel_for 'assurance_intake-*.whl')"
generation_wheel="$(wheel_for 'assurance_generation-*.whl')"
execution_wheel="$(wheel_for 'assurance_execution-*.whl')"
healing_wheel="$(wheel_for 'assurance_healing-*.whl')"
quality_wheel="$(wheel_for 'assurance_quality-*.whl')"
improvement_wheel="$(wheel_for 'assurance_improvement-*.whl')"
product_wheel="$(wheel_for 'assurance_product-*.whl')"
opencode_wheel="$(wheel_for 'agent_runtime_opencode-*.whl')"
cursor_wheel="$(wheel_for 'agent_runtime_cursor-*.whl')"

echo "PRODUCT_WHEEL_FILES=$(basename "$engine_wheel") $(basename "$contracts_wheel") $(basename "$intake_wheel") $(basename "$generation_wheel") $(basename "$execution_wheel") $(basename "$healing_wheel") $(basename "$quality_wheel") $(basename "$improvement_wheel") $(basename "$product_wheel") $(basename "$opencode_wheel") $(basename "$cursor_wheel")"

cat >"$smoke_root/check.py" <<'PY'
from __future__ import annotations

import argparse
import ast
import json
import sys
import zipfile
from email.parser import BytesParser
from importlib import metadata, util
from pathlib import Path

CLOSED_NAMES = (
    "graph-engine",
    "agent-runtime-contracts",
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-healing",
    "assurance-quality",
    "assurance-improvement",
    "assurance-product",
    "agent-runtime-opencode",
    "agent-runtime-cursor",
)
SECRET_CANARIES = (
    b"sk-secret-canary-value",
    b"runtime-only-token",
    b"OPENCODE_TOKEN=",
    b"CURSOR_API_KEY=",
)
DEPLOYMENT_SECRET_CANARIES = SECRET_CANARIES + (
    b"secret_value",
    b"Bearer ",
)
LEGACY_MODULES = ("assurance_agent", "assurance_kernel")
LEGACY_DISTS = ("assurance-agent", "assurance-kernel")
PRODUCT_ENTRY_POINTS = {
    "assurance-opencode": "assurance_product.product:AssuranceOpenCodeProductProvider",
    "assurance-cursor": "assurance_product.product:AssuranceCursorProductProvider",
}


def canonicalize_name(name: str) -> str:
    return name.lower().replace("_", "-")


def find_wheel(dist_root: Path, pattern: str) -> Path:
    matches = sorted(dist_root.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"expected one wheel matching {pattern}, found {matches}")
    return matches[0]


def scan_bytes(payload: bytes, canaries: tuple[bytes, ...], label: str) -> None:
    for canary in canaries:
        if canary in payload:
            raise SystemExit(f"secret fixture bytes {canary!r} found in {label}")


def scan_python_imports(source: str, label: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".", 1)[0]
            if root in LEGACY_MODULES:
                raise SystemExit(f"legacy import {name} in {label}")


def inspect_wheel_archive(wheel: Path, *, deployment: bool) -> None:
    canaries = DEPLOYMENT_SECRET_CANARIES if deployment else SECRET_CANARIES
    scan_bytes(wheel.read_bytes(), canaries, wheel.name)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for member in names:
            parts = Path(member).parts
            if "assurance_agent" in parts or "assurance_kernel" in parts:
                raise SystemExit(f"legacy path in {wheel.name}: {member}")
            payload = archive.read(member)
            scan_bytes(payload, canaries, f"{wheel.name}:{member}")
            if member.endswith(".py"):
                scan_python_imports(payload.decode("utf-8"), f"{wheel.name}:{member}")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        message = BytesParser().parsebytes(archive.read(metadata_name))
        dist_name = canonicalize_name(str(message["Name"]))
        if dist_name in LEGACY_DISTS:
            raise SystemExit(f"legacy distribution wheel: {wheel.name}")
        requirements = tuple(
            canonicalize_name(value.split(";", 1)[0].split(" ", 1)[0])
            for value in (message.get_all("Requires-Dist") or ())
        )
        if any(name in LEGACY_DISTS for name in requirements):
            raise SystemExit(f"legacy requirement in {wheel.name}: {requirements}")
        if dist_name == "assurance-product":
            scripts = {
                name.rsplit("/", 1)[-1]
                for name in names
                if "/scripts/" in name and not name.endswith("/")
            }
            if "aa" in scripts:
                raise SystemExit("product wheel must not ship the legacy aa script")
            if "aa-next" not in scripts:
                raise SystemExit("product wheel is missing the aa-next script")


def check_archives(dist_root: Path) -> None:
    expected = {
        "graph_engine-*.whl",
        "agent_runtime_contracts-*.whl",
        "assurance_intake-*.whl",
        "assurance_generation-*.whl",
        "assurance_execution-*.whl",
        "assurance_healing-*.whl",
        "assurance_quality-*.whl",
        "assurance_improvement-*.whl",
        "assurance_product-*.whl",
        "agent_runtime_opencode-*.whl",
        "agent_runtime_cursor-*.whl",
    }
    wheels = sorted(path for path in dist_root.glob("*.whl") if path.is_file())
    if len(wheels) != len(expected):
        raise SystemExit(f"expected {len(expected)} source wheels, found {[path.name for path in wheels]}")
    for pattern in expected:
        inspect_wheel_archive(find_wheel(dist_root, pattern), deployment=False)
    print("WHEEL_ARCHIVES_OK")


def check_deployment_archives(dist_root: Path) -> None:
    wheels = sorted(path for path in dist_root.glob("*.whl") if path.is_file())
    if not wheels:
        raise SystemExit(f"no deployment wheels in {dist_root}")
    for wheel in wheels:
        inspect_wheel_archive(wheel, deployment=True)
    print("DEPLOYMENT_WHEELS_OK")


def probe_prefix(prefix: str, expected_names: str, expected_entry_points: str, forbidden_names: str) -> None:
    expected = {canonicalize_name(name) for name in expected_names.split(",") if name}
    forbidden = {canonicalize_name(name) for name in forbidden_names.split(",") if name}
    expected_eps = sorted(name for name in expected_entry_points.split(",") if name)
    for module_name in LEGACY_MODULES:
        if util.find_spec(module_name) is not None:
            raise SystemExit(f"{prefix} legacy package present: {module_name}")
        if module_name in sys.modules:
            raise SystemExit(f"{prefix} legacy package imported: {module_name}")

    installed_local: set[str] = set()
    for dist in metadata.distributions():
        name = canonicalize_name(dist.metadata["Name"])
        if name in LEGACY_DISTS:
            raise SystemExit(f"{prefix} legacy distribution installed: {name}")
        for file in dist.files or ():
            parts = Path(str(file)).parts
            if "assurance_agent" in parts or "assurance_kernel" in parts:
                raise SystemExit(f"{prefix} legacy path installed: {file}")
        if name in forbidden:
            raise SystemExit(f"{prefix} forbidden distribution installed: {name}")
        if name not in CLOSED_NAMES:
            continue
        installed_local.add(name)
        direct_url = dist.read_text("direct_url.json")
        if direct_url is None:
            continue
        payload = json.loads(direct_url)
        if payload.get("dir_info") is not None:
            raise SystemExit(f"{prefix} {name} is an editable or direct-path install")
        url = str(payload.get("url") or "")
        if payload.get("archive_info") is None or (
            url.startswith("file:") and not url.rstrip("/").endswith(".whl")
        ):
            raise SystemExit(f"{prefix} {name} is not an archive wheel install")

    if installed_local != expected:
        raise SystemExit(f"{prefix} installed local {sorted(installed_local)} != {sorted(expected)}")

    from graph_engine import ENGINE_API_VERSION

    if ENGINE_API_VERSION != "2.0":
        raise SystemExit(f"{prefix} ENGINE_API_VERSION {ENGINE_API_VERSION!r} != '2.0'")

    plugin_eps = metadata.entry_points(group="graph_engine.plugins")
    plugin_names = sorted(ep.name for ep in plugin_eps)
    if plugin_names != expected_eps:
        raise SystemExit(f"{prefix} plugin entry points {plugin_names!r} != {expected_eps!r}")

    product_eps = {
        ep.name: f"{ep.module}:{ep.attr}"
        for ep in metadata.entry_points(group="graph_engine.products")
    }
    if product_eps != PRODUCT_ENTRY_POINTS:
        raise SystemExit(f"{prefix} product entry points {product_eps!r} != {PRODUCT_ENTRY_POINTS!r}")

    scripts = {ep.name for ep in metadata.entry_points(group="console_scripts")}
    if "aa" in scripts:
        raise SystemExit(f"{prefix} legacy aa console script is installed")
    if "aa-next" not in scripts:
        raise SystemExit(f"{prefix} aa-next console script is missing")

    for dist in metadata.distributions():
        name = canonicalize_name(dist.metadata["Name"])
        if name not in expected and not name.startswith("assurance-product-bindings-"):
            continue
        root = Path(str(dist.locate_file("")))
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json", ".yaml", ".yml", ".txt"}:
                continue
            scan_bytes(path.read_bytes(), SECRET_CANARIES, f"{prefix}:{path}")
    print(f"PREFIX={prefix} ENTRY_POINTS={','.join(expected_eps)}")


def check_compile_ok(output: str, product: str) -> None:
    document = json.loads(output)
    if document.get("engine_api") != "2.0":
        raise SystemExit(f"compile engine_api {document.get('engine_api')!r} != '2.0'")
    if document.get("product") != product:
        raise SystemExit(f"compile product {document.get('product')!r} != {product!r}")
    if not document.get("lock_digest"):
        raise SystemExit("compile output is missing lock_digest")
    audit = document.get("audit") or {}
    for key in (
        "unreachable_nodes",
        "dead_ends",
        "forbidden_direct_targets",
        "missing_bindings",
        "uninventoried_nodes",
    ):
        if audit.get(key):
            raise SystemExit(f"compile audit {key} is not empty: {audit.get(key)}")
    print(f"COMPILE_OK product={product} lock_digest={document['lock_digest']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    archives = sub.add_parser("archives")
    archives.add_argument("--dist", required=True)
    deployment = sub.add_parser("deployment")
    deployment.add_argument("--dist", required=True)
    prefix = sub.add_parser("prefix")
    prefix.add_argument("--prefix", required=True)
    prefix.add_argument("--expected-names", required=True)
    prefix.add_argument("--expected-entry-points", default="")
    prefix.add_argument("--forbidden-names", default="")
    compile_ok = sub.add_parser("compile-ok")
    compile_ok.add_argument("--output", required=True)
    compile_ok.add_argument("--product", required=True)
    args = parser.parse_args(argv)
    if args.command == "archives":
        check_archives(Path(args.dist))
        return 0
    if args.command == "deployment":
        check_deployment_archives(Path(args.dist))
        return 0
    if args.command == "compile-ok":
        check_compile_ok(Path(args.output).read_text(encoding="utf-8"), args.product)
        return 0
    probe_prefix(args.prefix, args.expected_names, args.expected_entry_points, args.forbidden_names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY

cd "$smoke_root"
uv run \
  --offline \
  --no-project \
  --python 3.11 \
  --managed-python \
  --no-python-downloads \
  python "$smoke_root/check.py" archives --dist "$dist_root"

config_tree="$source_root/tests/phase5/fixtures/project-config"
opencode_manifest="$source_root/tests/phase5/fixtures/deployment/opencode.yaml"
cursor_manifest="$source_root/tests/phase5/fixtures/deployment/cursor.yaml"
test -d "$config_tree"
test -f "$opencode_manifest"
test -f "$cursor_manifest"

install_env() {
  local name="$1"
  shift
  local venv="$smoke_root/venv-$name"
  uv venv --offline --python 3.11 "$venv"
  uv pip install \
    --offline \
    --python "$venv/bin/python" \
    --find-links "$dist_root" \
    "$@"
  if [[ -x "$venv/bin/aa" ]]; then
    echo "legacy aa must be absent in $name" >&2
    exit 1
  fi
  if [[ ! -x "$venv/bin/aa-next" ]]; then
    echo "aa-next must be installed in $name" >&2
    exit 1
  fi
}

inspect_prefix() {
  local name="$1"
  local expected_names="$2"
  local expected_eps="$3"
  local forbidden_names="$4"
  local venv="$smoke_root/venv-$name"
  (
    unset PYTHONPATH
    unset UV_PROJECT
    export PYTHONNOUSERSITE=1
    cd "$smoke_root"
    "$venv/bin/python" "$smoke_root/check.py" prefix \
      --prefix "$name" \
      --expected-names "$expected_names" \
      --expected-entry-points "$expected_eps" \
      --forbidden-names "$forbidden_names"
  )
}

build_deployment() {
  local adapter="$1"
  local manifest="$2"
  local output="$bindings_root/$adapter"
  mkdir -p "$output"
  local built_json="$smoke_root/bindings-$adapter.json"
  (
    unset PYTHONPATH
    unset UV_PROJECT
    export PYTHONNOUSERSITE=1
    PATH="$smoke_root/venv-base-no-adapter/bin:$PATH"
    aa-next bindings build \
      --json \
      --manifest "$manifest" \
      --output-dir "$output" \
      >"$built_json"
  )
  "$smoke_root/venv-base-no-adapter/bin/python" - "$built_json" "$smoke_root/bindings-$adapter.env" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
required = ("wheel", "distribution", "declaration_path", "entry_point_value")
missing = [key for key in required if not document.get(key)]
if missing:
    raise SystemExit(f"bindings build missing {missing}: {document}")
Path(sys.argv[2]).write_text(
    "\n".join(
        [
            f"wheel={document['wheel']}",
            f"distribution={document['distribution']}",
            f"declaration_path={document['declaration_path']}",
            f"entry_point_value={document['entry_point_value']}",
        ]
    )
    + "\n",
    encoding="utf-8",
)
PY
}

load_binding_env() {
  local adapter="$1"
  # shellcheck disable=SC1090
  source "$smoke_root/bindings-$adapter.env"
  binding_wheel="$wheel"
  binding_distribution="$distribution"
  binding_declaration="$declaration_path"
  test -f "$binding_wheel"
  test -n "$binding_distribution"
  test -n "$binding_declaration"
}

install_binding() {
  local name="$1"
  local wheel="$2"
  uv pip install \
    --offline \
    --python "$smoke_root/venv-$name/bin/python" \
    --find-links "$dist_root" \
    "$wheel"
}

compile_product() {
  local venv="$1"
  local product="$2"
  local binding_dist="$3"
  local declaration_path="$4"
  (
    unset PYTHONPATH
    unset UV_PROJECT
    export PYTHONNOUSERSITE=1
    cd "$source_root"
    PATH="${venv}/bin:${PATH}"
    aa-next compile \
      --product "${product}" \
      --binding-dist "${binding_dist}" \
      --binding-entrypoint deployment \
      --binding-declaration "${declaration_path}" \
      --config-tree "${config_tree}"
  )
}

expect_compile_ok() {
  local name="$1"
  local product="$2"
  local binding_dist="$3"
  local declaration_path="$4"
  local out="$smoke_root/${name}-compile.json"
  compile_product "$smoke_root/venv-$name" "$product" "$binding_dist" "$declaration_path" >"$out"
  "$smoke_root/venv-$name/bin/python" "$smoke_root/check.py" compile-ok \
    --output "$out" \
    --product "$product"
}

expect_compile_fail() {
  local name="$1"
  local product="$2"
  local binding_dist="$3"
  local declaration_path="$4"
  local out="$smoke_root/${name}-compile.out"
  set +e
  compile_product "$smoke_root/venv-$name" "$product" "$binding_dist" "$declaration_path" >"$out" 2>&1
  local status="$?"
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "$name compile unexpectedly succeeded" >&2
    cat "$out" >&2
    exit 1
  fi
  echo "ENV=$name COMPILE_FAIL=$status"
}

BASE_NAMES="graph-engine,agent-runtime-contracts,assurance-intake,assurance-generation,assurance-execution,assurance-healing,assurance-quality,assurance-improvement,assurance-product"
BASE_EPS="execution,generation,healing,improvement,intake,quality"
FORBIDDEN_BASE="agent-runtime-opencode,agent-runtime-cursor,assurance-agent,assurance-kernel"
FORBIDDEN_OPENCODE="agent-runtime-cursor,assurance-agent,assurance-kernel"
FORBIDDEN_CURSOR="agent-runtime-opencode,assurance-agent,assurance-kernel"

install_env base-no-adapter "$product_wheel"
inspect_prefix base-no-adapter "$BASE_NAMES" "$BASE_EPS" "$FORBIDDEN_BASE"

build_deployment opencode "$opencode_manifest"
build_deployment cursor "$cursor_manifest"
uv run \
  --offline \
  --no-project \
  --python 3.11 \
  --managed-python \
  --no-python-downloads \
  python "$smoke_root/check.py" deployment --dist "$bindings_root/opencode"
uv run \
  --offline \
  --no-project \
  --python 3.11 \
  --managed-python \
  --no-python-downloads \
  python "$smoke_root/check.py" deployment --dist "$bindings_root/cursor"

load_binding_env opencode
opencode_binding_wheel="$binding_wheel"
opencode_binding_distribution="$binding_distribution"
opencode_binding_declaration="$binding_declaration"
load_binding_env cursor
cursor_binding_wheel="$binding_wheel"
cursor_binding_distribution="$binding_distribution"
cursor_binding_declaration="$binding_declaration"

install_binding base-no-adapter "$opencode_binding_wheel"
expect_compile_fail base-no-adapter assurance-opencode \
  "$opencode_binding_distribution" "$opencode_binding_declaration"

install_env opencode-product "${product_wheel}[opencode]"
inspect_prefix opencode-product \
  "$BASE_NAMES,agent-runtime-opencode" \
  "$BASE_EPS,opencode" \
  "$FORBIDDEN_OPENCODE"
install_binding opencode-product "$opencode_binding_wheel"
expect_compile_ok opencode-product assurance-opencode \
  "$opencode_binding_distribution" "$opencode_binding_declaration"

install_env cursor-product "${product_wheel}[cursor]"
inspect_prefix cursor-product \
  "$BASE_NAMES,agent-runtime-cursor" \
  "$BASE_EPS,cursor" \
  "$FORBIDDEN_CURSOR"
install_binding cursor-product "$cursor_binding_wheel"
expect_compile_ok cursor-product assurance-cursor \
  "$cursor_binding_distribution" "$cursor_binding_declaration"

install_env source-drift "${product_wheel}[opencode]"
inspect_prefix source-drift \
  "$BASE_NAMES,agent-runtime-opencode" \
  "$BASE_EPS,opencode" \
  "$FORBIDDEN_OPENCODE"
install_binding source-drift "$opencode_binding_wheel"
tamper_path="$(
  "$smoke_root/venv-source-drift/bin/python" - <<'PY'
from importlib import metadata
from pathlib import Path

print(Path(metadata.distribution("assurance-product").locate_file(
    "assurance_product/product-declaration-opencode.json"
)))
PY
)"
test -f "$tamper_path"
printf '\n' >>"$tamper_path"
expect_compile_fail source-drift assurance-opencode \
  "$opencode_binding_distribution" "$opencode_binding_declaration"

echo "assurance product wheel smoke test: OK"
