"""Current-tree tracer gate for isolated plugin wheel loads."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class PackageSpec:
    distribution: str
    entry_point: str
    source_root: Path
    declaration_path: str


ALLOWED_PACKAGES: Mapping[str, PackageSpec] = {
    "graph-engine-toy-a": PackageSpec(
        distribution="graph-engine-toy-a",
        entry_point="toy-a",
        source_root=REPO_ROOT / "examples" / "graph-engine-toy-a",
        declaration_path="graph_engine_toy_a/plugin-declaration.json",
    ),
    "assurance-intake": PackageSpec(
        distribution="assurance-intake",
        entry_point="intake",
        source_root=REPO_ROOT / "packages" / "features" / "assurance-intake",
        declaration_path="assurance_intake/plugin-declaration.json",
    ),
    "assurance-generation": PackageSpec(
        distribution="assurance-generation",
        entry_point="generation",
        source_root=REPO_ROOT / "packages" / "features" / "assurance-generation",
        declaration_path="assurance_generation/plugin-declaration.json",
    ),
    "assurance-execution": PackageSpec(
        distribution="assurance-execution",
        entry_point="execution",
        source_root=REPO_ROOT / "packages" / "features" / "assurance-execution",
        declaration_path="assurance_execution/plugin-declaration.json",
    ),
    "assurance-healing": PackageSpec(
        distribution="assurance-healing",
        entry_point="healing",
        source_root=REPO_ROOT / "packages" / "features" / "assurance-healing",
        declaration_path="assurance_healing/plugin-declaration.json",
    ),
    "assurance-quality": PackageSpec(
        distribution="assurance-quality",
        entry_point="quality",
        source_root=REPO_ROOT / "packages" / "features" / "assurance-quality",
        declaration_path="assurance_quality/plugin-declaration.json",
    ),
    "assurance-improvement": PackageSpec(
        distribution="assurance-improvement",
        entry_point="improvement",
        source_root=REPO_ROOT / "packages" / "features" / "assurance-improvement",
        declaration_path="assurance_improvement/plugin-declaration.json",
    ),
}

_ALWAYS_BUILD: tuple[str, ...] = ("graph-engine", "agent-runtime-contracts")
_LOCAL_SOURCE_ROOTS: Mapping[str, Path] = {
    "graph-engine": REPO_ROOT / "packages" / "framework" / "graph-engine",
    "agent-runtime-contracts": REPO_ROOT / "packages" / "clients" / "agent-runtime-contracts",
    **{name: spec.source_root for name, spec in ALLOWED_PACKAGES.items()},
}
_BUILD_ORDER: tuple[str, ...] = (
    "graph-engine",
    "agent-runtime-contracts",
    "graph-engine-toy-a",
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-healing",
    "assurance-quality",
    "assurance-improvement",
)
_PROBE_SOURCE = r"""
from __future__ import annotations

import json
import sys
from importlib import metadata, util
from pathlib import Path

from packaging.utils import canonicalize_name

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDescriptor, validate_contribution

package, entry_point, declaration_path = sys.argv[1], sys.argv[2], sys.argv[3]
local_names = {canonicalize_name(name) for name in sys.argv[4].split(",") if name}
allowed_entry_points = [name for name in sys.argv[5].split(",") if name]

for module_name in ("assurance_agent", "assurance_kernel"):
    if util.find_spec(module_name) is not None:
        raise SystemExit(f"legacy package present: {module_name}")

for dist in metadata.distributions():
    name = canonicalize_name(dist.metadata["Name"])
    if name in {"assurance-agent", "assurance-kernel"}:
        raise SystemExit(f"legacy distribution installed: {name}")
    for file in dist.files or ():
        parts = Path(str(file)).parts
        if "assurance_agent" in parts or "assurance_kernel" in parts:
            raise SystemExit(f"legacy path installed: {file}")
    if name in local_names:
        direct_url = dist.read_text("direct_url.json")
        if direct_url is not None:
            payload = json.loads(direct_url)
            if payload.get("dir_info") is not None:
                raise SystemExit(f"{name} is an editable or direct-path install")
            url = str(payload.get("url") or "")
            if payload.get("archive_info") is None or (
                url.startswith("file:") and not url.rstrip("/").endswith(".whl")
            ):
                raise SystemExit(f"{name} is not an archive wheel install")

eps = metadata.entry_points(group="graph_engine.plugins")
names = sorted(ep.name for ep in eps)
if names != allowed_entry_points:
    raise SystemExit(f"entry points {names!r} != {allowed_entry_points!r}")
if entry_point not in names:
    raise SystemExit(f"missing expected entry point: {entry_point!r}")
provider_cls = next(ep for ep in eps if ep.name == entry_point).load()
provider = provider_cls() if isinstance(provider_cls, type) else provider_cls
descriptor = provider.descriptor()
contribution = provider.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
validate_contribution(descriptor, contribution)

dist = metadata.distribution(package)
located = Path(str(dist.locate_file(declaration_path)))
static = json.loads(located.read_text(encoding="utf-8"))
payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
expected = PluginDescriptor.model_validate(payload)
if expected != descriptor:
    raise SystemExit("static/live declaration mismatch")
print(f"isolated {package} entry-point={entry_point}")
"""


def isolate_package(package: str, expect_entry_point: str) -> None:
    spec = ALLOWED_PACKAGES.get(package)
    if spec is None:
        raise ValueError(
            "package is not in the closed isolation set: "
            f"{package!r}; allowed: {', '.join(sorted(ALLOWED_PACKAGES))}"
        )
    if expect_entry_point != spec.entry_point:
        raise ValueError(f"expect-entry-point {expect_entry_point!r} does not match {spec.entry_point!r}")
    if not spec.source_root.is_dir():
        raise FileNotFoundError(f"requested wheel source is missing: {spec.source_root}")

    with tempfile.TemporaryDirectory(prefix="phase4-wheel-isolation-") as temporary:
        root = Path(temporary)
        wheelhouse = root / "wheelhouse"
        venv = root / "venv"
        wheelhouse.mkdir()
        distributions = _distributions_to_build(package)
        for name in distributions:
            _build_wheel(_source_root(name), wheelhouse)
        _create_venv(venv)
        python = venv / "bin" / "python"
        _install_wheels(wheelhouse, python, distributions)
        _run_probe(python, spec, distributions)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.phase4.wheel_isolation")
    parser.add_argument("--package", required=True)
    parser.add_argument("--expect-entry-point", required=True)
    args = parser.parse_args(argv)
    isolate_package(args.package, args.expect_entry_point)
    return 0


def _distributions_to_build(package: str) -> tuple[str, ...]:
    needed: set[str] = {str(canonicalize_name(package)), *_ALWAYS_BUILD}
    stack = [str(canonicalize_name(package))]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        needed.add(current)
        source = _LOCAL_SOURCE_ROOTS.get(current)
        if source is None:
            continue
        if (source / "pyproject.toml").is_file():
            stack.extend(_local_dependencies(source))
    return tuple(name for name in _BUILD_ORDER if name in needed)


def _local_dependencies(source_root: Path) -> tuple[str, ...]:
    data = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    raw = data.get("project", {}).get("dependencies", [])
    found: list[str] = []
    for item in raw:
        name = str(canonicalize_name(Requirement(str(item)).name))
        if name in _LOCAL_SOURCE_ROOTS:
            found.append(name)
    return tuple(found)


def _source_root(distribution: str) -> Path:
    root = _LOCAL_SOURCE_ROOTS[distribution]
    if not root.is_dir():
        raise FileNotFoundError(f"local wheel source is missing: {root}")
    return root


def _build_wheel(source_root: Path, wheelhouse: Path) -> None:
    _run(
        [
            _uv(),
            "build",
            "--offline",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(wheelhouse),
            str(source_root),
        ]
    )


def _create_venv(venv: Path) -> None:
    _run([_uv(), "venv", "--python", "3.11", str(venv)])


def _install_wheels(wheelhouse: Path, python: Path, distributions: Sequence[str]) -> None:
    wheels = tuple(sorted(wheelhouse.glob("*.whl")))
    built = {_wheel_distribution(path) for path in wheels}
    missing = [name for name in distributions if name not in built]
    if missing:
        raise RuntimeError(f"missing built wheels: {', '.join(missing)}")
    selected = tuple(path for path in wheels if _wheel_distribution(path) in set(distributions))
    _run(
        [
            _uv(),
            "pip",
            "install",
            "--python",
            str(python),
            "--find-links",
            str(wheelhouse),
            *[str(path) for path in selected],
        ]
    )


def _run_probe(python: Path, spec: PackageSpec, distributions: Sequence[str]) -> None:
    probe = python.parent.parent / "probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")
    allowed_entry_points = tuple(
        sorted(ALLOWED_PACKAGES[name].entry_point for name in distributions if name in ALLOWED_PACKAGES)
    )
    _run(
        [
            str(python),
            str(probe),
            spec.distribution,
            spec.entry_point,
            spec.declaration_path,
            ",".join(distributions),
            ",".join(allowed_entry_points),
        ]
    )


def _wheel_distribution(path: Path) -> str:
    return canonicalize_name(path.name.split("-", 1)[0].replace("_", "-"))


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        raise RuntimeError("uv is required for wheel isolation")
    return found


def _run(command: Sequence[str]) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    subprocess.run(command, check=True, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
