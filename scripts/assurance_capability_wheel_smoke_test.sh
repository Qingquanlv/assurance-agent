#!/usr/bin/env bash
# Build committed Phase 4 capability wheels and prove prefix isolation.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

if ! git -C "$repo_root" diff --quiet -- || ! git -C "$repo_root" diff --cached --quiet --; then
  echo "dirty tracked worktree; commit before running committed-HEAD isolation" >&2
  git -C "$repo_root" diff --stat -- >&2 || true
  git -C "$repo_root" diff --cached --stat -- >&2 || true
  exit 1
fi

smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/assurance-capability-smoke.XXXXXX")"
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
mkdir -p "$source_root" "$dist_root"
git -C "$repo_root" archive HEAD | tar -x -C "$source_root"

cd "$source_root"
for package in \
  graph-engine \
  agent-runtime-contracts \
  assurance-intake \
  assurance-generation \
  assurance-execution \
  assurance-healing \
  assurance-quality \
  assurance-improvement
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

echo "CAPABILITY_WHEEL_FILES=$(basename "$engine_wheel") $(basename "$contracts_wheel") $(basename "$intake_wheel") $(basename "$generation_wheel") $(basename "$execution_wheel") $(basename "$healing_wheel") $(basename "$quality_wheel") $(basename "$improvement_wheel")"

cat >"$smoke_root/check.py" <<'PY'
from __future__ import annotations

import argparse
import ast
import json
import sys
import zipfile
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
)
WHEEL_GLOBS = {
    "graph-engine": "graph_engine-*.whl",
    "agent-runtime-contracts": "agent_runtime_contracts-*.whl",
    "assurance-intake": "assurance_intake-*.whl",
    "assurance-generation": "assurance_generation-*.whl",
    "assurance-execution": "assurance_execution-*.whl",
    "assurance-healing": "assurance_healing-*.whl",
    "assurance-quality": "assurance_quality-*.whl",
    "assurance-improvement": "assurance_improvement-*.whl",
}
ASSURANCE_SPECS = {
    "assurance-intake": {
        "import_root": "assurance_intake",
        "entry_point": "intake",
        "contract": ("assurance_intake.contracts", "CaseYaml"),
        "package_root": "packages/features/assurance-intake/assurance_intake",
    },
    "assurance-generation": {
        "import_root": "assurance_generation",
        "entry_point": "generation",
        "contract": ("assurance_generation.contracts", "PlanResultV1"),
        "package_root": "packages/features/assurance-generation/assurance_generation",
    },
    "assurance-execution": {
        "import_root": "assurance_execution",
        "entry_point": "execution",
        "contract": ("assurance_execution.contracts", "ExecutionEvidenceV1"),
        "package_root": "packages/features/assurance-execution/assurance_execution",
    },
    "assurance-healing": {
        "import_root": "assurance_healing",
        "entry_point": "healing",
        "contract": ("assurance_healing.contracts", "FixProposal"),
        "package_root": "packages/features/assurance-healing/assurance_healing",
    },
    "assurance-quality": {
        "import_root": "assurance_quality",
        "entry_point": "quality",
        "contract": ("assurance_quality.contracts", "QualityReport"),
        "package_root": "packages/features/assurance-quality/assurance_quality",
    },
    "assurance-improvement": {
        "import_root": "assurance_improvement",
        "entry_point": "improvement",
        "contract": ("assurance_improvement.contracts", "ImprovementCandidate"),
        "package_root": "packages/features/assurance-improvement/assurance_improvement",
    },
}


def canonicalize_name(name: str) -> str:
    return name.lower().replace("_", "-")


def parse_file_maps(plugin_py: Path) -> dict[str, str]:
    tree = ast.parse(plugin_py.read_text(encoding="utf-8"))
    maps: dict[str, str] = {}
    for node in tree.body:
        names: list[str] = []
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        if value is None:
            continue
        if any(name.endswith("RESOURCE_FILES") or name.endswith("SCHEMA_FILES") for name in names):
            maps.update(ast.literal_eval(value))
    if not maps:
        raise SystemExit(f"no resource/schema maps in {plugin_py}")
    return maps


def find_wheel(dist_root: Path, pattern: str) -> Path:
    matches = sorted(dist_root.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"expected one wheel matching {pattern}, found {matches}")
    return matches[0]


def contributed_paths(spec: dict[str, object], maps: dict[str, str]) -> tuple[str, tuple[str, ...]]:
    import_root = str(spec["import_root"])
    declaration = f"{import_root}/plugin-declaration.json"
    resources = tuple(f"{import_root}/resources/{relative}" for relative in maps.values())
    return declaration, resources


def check_archives(source_root: Path, dist_root: Path) -> None:
    wheels = sorted(path for path in dist_root.glob("*.whl") if path.is_file())
    if len(wheels) != len(CLOSED_NAMES):
        raise SystemExit(f"expected {len(CLOSED_NAMES)} wheels, found {[path.name for path in wheels]}")
    for distribution, pattern in WHEEL_GLOBS.items():
        wheel = find_wheel(dist_root, pattern)
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            for member in names:
                parts = Path(member).parts
                if "assurance_agent" in parts or "assurance_kernel" in parts:
                    raise SystemExit(f"legacy path in {wheel.name}: {member}")
            if any(".data/scripts/" in member for member in names):
                raise SystemExit(f"undeclared executable scripts in {wheel.name}")
        if distribution not in ASSURANCE_SPECS:
            continue
        spec = ASSURANCE_SPECS[distribution]
        package_root = source_root / str(spec["package_root"])
        maps = parse_file_maps(package_root / "plugin.py")
        declaration, resources = contributed_paths(spec, maps)
        allowed = {declaration, *resources}
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            if declaration not in names:
                raise SystemExit(f"missing declaration in {wheel.name}: {declaration}")
            declaration_source = package_root / "plugin-declaration.json"
            if archive.read(declaration) != declaration_source.read_bytes():
                raise SystemExit(f"declaration bytes differ from source in {wheel.name}")
            for resource in resources:
                if resource not in names:
                    raise SystemExit(f"missing contributed path in {wheel.name}: {resource}")
                relative = resource.split("/resources/", 1)[1]
                source = package_root / "resources" / relative
                if archive.read(resource) != source.read_bytes():
                    raise SystemExit(f"wheel/source byte mismatch in {wheel.name}: {resource}")
            for member in names:
                if member.endswith("/"):
                    continue
                top = member.split("/", 1)[0]
                if top.endswith(".dist-info"):
                    continue
                info = archive.getinfo(member)
                mode = (info.external_attr >> 16) & 0o777
                if member.endswith(".py"):
                    continue
                if member in allowed:
                    if mode & 0o111:
                        raise SystemExit(f"undeclared executable mode in {wheel.name}: {member}")
                    continue
                raise SystemExit(f"undeclared executable/config file in {wheel.name}: {member}")
    print("WHEEL_ARCHIVES_OK")


def probe_prefix(source_root: Path, prefix: str, expected_names: str, expected_entry_points: str) -> None:
    expected = [canonicalize_name(name) for name in expected_names.split(",") if name]
    expected_eps = sorted(name for name in expected_entry_points.split(",") if name)
    for module_name in ("assurance_agent", "assurance_kernel"):
        if util.find_spec(module_name) is not None:
            raise SystemExit(f"legacy package present: {module_name}")
        if module_name in sys.modules:
            raise SystemExit(f"legacy package imported: {module_name}")

    installed_local: set[str] = set()
    for dist in metadata.distributions():
        name = canonicalize_name(dist.metadata["Name"])
        if name in {"assurance-agent", "assurance-kernel"}:
            raise SystemExit(f"legacy distribution installed: {name}")
        for file in dist.files or ():
            parts = Path(str(file)).parts
            if "assurance_agent" in parts or "assurance_kernel" in parts:
                raise SystemExit(f"legacy path installed: {file}")
        if name not in CLOSED_NAMES:
            continue
        installed_local.add(name)
        direct_url = dist.read_text("direct_url.json")
        if direct_url is None:
            continue
        payload = json.loads(direct_url)
        if payload.get("dir_info") is not None:
            raise SystemExit(f"{name} is an editable or direct-path install")
        url = str(payload.get("url") or "")
        if payload.get("archive_info") is None or (
            url.startswith("file:") and not url.rstrip("/").endswith(".whl")
        ):
            raise SystemExit(f"{name} is not an archive wheel install")

    if installed_local != set(expected):
        raise SystemExit(f"{prefix} installed local {sorted(installed_local)} != {expected}")

    from graph_engine import ENGINE_API_VERSION, RegistryPorts
    from graph_engine.plugin_api import PluginDescriptor, validate_contribution

    if ENGINE_API_VERSION != "2.0":
        raise SystemExit(f"ENGINE_API_VERSION {ENGINE_API_VERSION!r} != '2.0'")

    eps = metadata.entry_points(group="graph_engine.plugins")
    names = sorted(ep.name for ep in eps)
    if names != expected_eps:
        raise SystemExit(f"{prefix} entry points {names!r} != {expected_eps!r}")

    ports = RegistryPorts(engine_api=ENGINE_API_VERSION)
    for distribution in expected:
        spec = ASSURANCE_SPECS.get(distribution)
        if spec is None:
            continue
        entry = next(ep for ep in eps if ep.name == spec["entry_point"])
        loaded = entry.load()
        provider = loaded() if isinstance(loaded, type) else loaded
        descriptor = provider.descriptor()
        contribution = provider.contribute(ports)
        validate_contribution(descriptor, contribution)
        dist = metadata.distribution(distribution)
        declaration = Path(str(dist.locate_file(f"{spec['import_root']}/plugin-declaration.json")))
        static = json.loads(declaration.read_text(encoding="utf-8"))
        payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
        if PluginDescriptor.model_validate(payload) != descriptor:
            raise SystemExit(f"{distribution} static/live declaration mismatch")
        module_name, attr = spec["contract"]
        imported = getattr(__import__(module_name, fromlist=[attr]), attr)
        if imported is None:
            raise SystemExit(f"{distribution} public contract missing: {module_name}.{attr}")
        maps = parse_file_maps(source_root / str(spec["package_root"]) / "plugin.py")
        for relative in maps.values():
            installed = Path(str(dist.locate_file(f"{spec['import_root']}/resources/{relative}")))
            source = source_root / str(spec["package_root"]) / "resources" / relative
            if installed.read_bytes() != source.read_bytes():
                raise SystemExit(f"installed/source byte mismatch {distribution} {relative}")
    print(f"PREFIX={prefix} ENTRY_POINTS={','.join(expected_eps)}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    archives = sub.add_parser("archives")
    archives.add_argument("--source", required=True)
    archives.add_argument("--dist", required=True)
    prefix = sub.add_parser("prefix")
    prefix.add_argument("--source", required=True)
    prefix.add_argument("--prefix", required=True)
    prefix.add_argument("--expected-names", required=True)
    prefix.add_argument("--expected-entry-points", default="")
    args = parser.parse_args(argv)
    if args.command == "archives":
        check_archives(Path(args.source), Path(args.dist))
        return 0
    probe_prefix(Path(args.source), args.prefix, args.expected_names, args.expected_entry_points)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY

# Archive checks run outside the repository and archived source trees.
cd "$smoke_root"
uv run \
  --offline \
  --no-project \
  --python 3.11 \
  --managed-python \
  --no-python-downloads \
  python "$smoke_root/check.py" archives --source "$source_root" --dist "$dist_root"

run_prefix() {
  local name="$1"
  local expected_names="$2"
  local expected_eps="$3"
  shift 3
  local venv="$smoke_root/venv-$name"
  uv venv --offline --python 3.11 "$venv"
  # Local wheels stay on --find-links. Do not pass --offline: yanked
  # pydantic==2.12.1 poisons uv's offline resolver on CI caches.
  uv pip install \
    --python "$venv/bin/python" \
    --find-links "$dist_root" \
    "$@"
  (
    unset PYTHONPATH
    unset UV_PROJECT
    export PYTHONNOUSERSITE=1
    cd "$smoke_root"
    "$venv/bin/python" "$smoke_root/check.py" prefix \
      --source "$source_root" \
      --prefix "$name" \
      --expected-names "$expected_names" \
      --expected-entry-points "$expected_eps"
  )
}

run_prefix engine \
  "graph-engine,agent-runtime-contracts" \
  "" \
  "$engine_wheel" \
  "$contracts_wheel"

run_prefix intake \
  "graph-engine,agent-runtime-contracts,assurance-intake" \
  "intake" \
  "$engine_wheel" \
  "$contracts_wheel" \
  "$intake_wheel"

run_prefix generation \
  "graph-engine,agent-runtime-contracts,assurance-intake,assurance-generation" \
  "generation,intake" \
  "$engine_wheel" \
  "$contracts_wheel" \
  "$intake_wheel" \
  "$generation_wheel"

run_prefix execution \
  "graph-engine,agent-runtime-contracts,assurance-intake,assurance-generation,assurance-execution" \
  "execution,generation,intake" \
  "$engine_wheel" \
  "$contracts_wheel" \
  "$intake_wheel" \
  "$generation_wheel" \
  "$execution_wheel"

run_prefix healing \
  "graph-engine,agent-runtime-contracts,assurance-intake,assurance-generation,assurance-execution,assurance-healing" \
  "execution,generation,healing,intake" \
  "$engine_wheel" \
  "$contracts_wheel" \
  "$intake_wheel" \
  "$generation_wheel" \
  "$execution_wheel" \
  "$healing_wheel"

run_prefix quality \
  "graph-engine,agent-runtime-contracts,assurance-intake,assurance-generation,assurance-execution,assurance-healing,assurance-quality" \
  "execution,generation,healing,intake,quality" \
  "$engine_wheel" \
  "$contracts_wheel" \
  "$intake_wheel" \
  "$generation_wheel" \
  "$execution_wheel" \
  "$healing_wheel" \
  "$quality_wheel"

run_prefix improvement \
  "graph-engine,agent-runtime-contracts,assurance-intake,assurance-generation,assurance-execution,assurance-healing,assurance-quality,assurance-improvement" \
  "execution,generation,healing,improvement,intake,quality" \
  "$engine_wheel" \
  "$contracts_wheel" \
  "$intake_wheel" \
  "$generation_wheel" \
  "$execution_wheel" \
  "$healing_wheel" \
  "$quality_wheel" \
  "$improvement_wheel"

echo "assurance capability wheel smoke test: OK"
