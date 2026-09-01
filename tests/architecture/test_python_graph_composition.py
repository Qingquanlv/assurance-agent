from __future__ import annotations

import ast
from configparser import ConfigParser
from pathlib import Path

FEATURE_PACKAGES = (
    "assurance_intake",
    "assurance_generation",
    "assurance_execution",
    "assurance_healing",
    "assurance_quality",
    "assurance_improvement",
)

FEATURE_SOURCE_TREES = (
    ("assurance_intake", "packages/capabilities/assurance-intake/assurance_intake"),
    ("assurance_generation", "packages/capabilities/assurance-generation/assurance_generation"),
    ("assurance_execution", "packages/capabilities/assurance-execution/assurance_execution"),
    ("assurance_healing", "packages/capabilities/assurance-healing/assurance_healing"),
    ("assurance_quality", "packages/capabilities/assurance-quality/assurance_quality"),
    ("assurance_improvement", "packages/capabilities/assurance-improvement/assurance_improvement"),
)

EXPECTED_FEATURE_FACTORY_MODULES = (
    "assurance_intake.graphs.factory",
    "assurance_generation.graphs.factory",
    "assurance_execution.graphs.factory",
    "assurance_healing.graphs.factory",
    "assurance_quality.graphs.factory",
    "assurance_improvement.graphs.factory",
)

FORBIDDEN_FEATURE_IMPLEMENTATION = (
    "operations",
    "validators",
    "effects",
    "resources",
    "resource_loader",
    "plugin",
)

FORBIDDEN_ADAPTERS = frozenset(
    {
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_product",
    }
)

COEXISTENCE_SUFFIXES = ("workflow", "graphs")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _imported_modules(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append((node.lineno, node.module))
    return tuple(found)


def _graph_python_files() -> tuple[Path, ...]:
    root = _repo_root()
    paths: list[Path] = []
    for package_name, relative in FEATURE_SOURCE_TREES:
        graphs = root / relative / "graphs"
        if not graphs.is_dir():
            continue
        del package_name
        for path in graphs.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            paths.append(path)
    return tuple(sorted(paths))


def test_fixed_factory_modules_are_the_only_capability_public_graph_surface() -> None:
    assert EXPECTED_FEATURE_FACTORY_MODULES == (
        "assurance_intake.graphs.factory",
        "assurance_generation.graphs.factory",
        "assurance_execution.graphs.factory",
        "assurance_healing.graphs.factory",
        "assurance_quality.graphs.factory",
        "assurance_improvement.graphs.factory",
    )
    root = _repo_root()
    public: list[str] = []
    for package_name, relative in FEATURE_SOURCE_TREES:
        package_root = root / relative
        for path in package_root.rglob("*.py"):
            if "__pycache__" in path.parts or "graphs" not in path.parts:
                continue
            for _lineno, imported in _imported_modules(path):
                if imported.endswith(".graphs") or imported.endswith(".graphs.factory"):
                    continue
                if any(
                    imported == f"{other}.graphs.{suffix}" or imported.startswith(f"{other}.graphs.{suffix}.")
                    for other, _rel in FEATURE_SOURCE_TREES
                    for suffix in ("state", "nodes", "routes", "prepare", "case")
                    if other != package_name
                ):
                    public.append(f"{path}:{imported}")
        init_path = package_root / "__init__.py"
        if init_path.is_file():
            for _lineno, imported in _imported_modules(init_path):
                if imported.startswith(f"{package_name}.graphs.") and not imported.startswith(
                    f"{package_name}.graphs.factory"
                ):
                    public.append(f"{init_path}:{imported}")
    assert public == []


def test_feature_graph_modules_reject_foreign_and_implementation_imports() -> None:
    violations: list[str] = []
    for path in _graph_python_files():
        owner = next(
            package_name
            for package_name, relative in FEATURE_SOURCE_TREES
            if Path(relative) in path.parents or str(path).find(f"/{package_name}/") != -1
        )
        for lineno, imported in _imported_modules(path):
            root_name = imported.split(".", 1)[0]
            if root_name in FORBIDDEN_ADAPTERS:
                violations.append(f"{path}:{lineno}:{imported}")
                continue
            if imported.startswith(f"{owner}."):
                suffix = imported[len(owner) + 1 :].split(".", 1)[0]
                if suffix in FORBIDDEN_FEATURE_IMPLEMENTATION:
                    violations.append(f"{path}:{lineno}:{imported}")
                continue
            if root_name in FEATURE_PACKAGES and root_name != owner:
                remainder = imported[len(root_name) + 1 :] if "." in imported else ""
                first = remainder.split(".", 1)[0]
                if first in {"graphs", *FORBIDDEN_FEATURE_IMPLEMENTATION}:
                    violations.append(f"{path}:{lineno}:{imported}")
    assert violations == []


def test_importlinter_keeps_workflow_and_graphs_during_coexistence() -> None:
    parser = ConfigParser()
    assert parser.read(_repo_root() / ".importlinter")
    peer_contracts = (
        "importlinter:contract:generation-intake-contracts-only",
        "importlinter:contract:execution-assurance-contracts-only",
        "importlinter:contract:healing-assurance-contracts-only",
        "importlinter:contract:quality-assurance-contracts-only",
        "importlinter:contract:improvement-assurance-contracts-only",
    )
    for section in peer_contracts:
        forbidden = {
            line.strip() for line in parser[section]["forbidden_modules"].splitlines() if line.strip()
        }
        suffixes_by_package: dict[str, set[str]] = {}
        for item in forbidden:
            if "." not in item:
                continue
            package_name, remainder = item.split(".", 1)
            if package_name in FEATURE_PACKAGES:
                suffixes_by_package.setdefault(package_name, set()).add(remainder.split(".", 1)[0])
        assert suffixes_by_package
        for package_name, suffixes in suffixes_by_package.items():
            for suffix in COEXISTENCE_SUFFIXES:
                assert suffix in suffixes, f"{section}:{package_name}.{suffix}"
