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
    "assurance_quality.graphs.factory",
    "assurance_healing.graphs.factory",
    "assurance_improvement.graphs.factory",
)

EXPECTED_FEATURE_FACTORY_SYMBOLS = (
    "assurance_intake.graphs.factory:build_intake_graphs",
    "assurance_generation.graphs.factory:build_generation_graphs",
    "assurance_execution.graphs.factory:build_execution_graphs",
    "assurance_quality.graphs.factory:build_quality_graphs",
    "assurance_healing.graphs.factory:build_healing_graphs",
    "assurance_improvement.graphs.factory:build_improvement_graphs",
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


def _graph_module_identity(path: Path) -> tuple[str, str]:
    resolved = path.resolve()
    root = _repo_root()
    for package_name, relative in FEATURE_SOURCE_TREES:
        package_root = (root / relative).resolve()
        try:
            rel = resolved.relative_to(package_root)
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return package_name, ".".join((package_name, *parts))
    raise ValueError(f"not a feature package path: {path}")


def _resolve_import_from(node: ast.ImportFrom, module_name: str, *, is_package: bool) -> tuple[str, ...]:
    parts = module_name.split(".")
    package_parts = parts if is_package else parts[:-1]
    if node.level:
        drop = node.level - 1
        base_parts = package_parts[: max(0, len(package_parts) - drop)]
        parent_parts = [*base_parts, *([node.module] if node.module else [])]
        parent = ".".join(part for part in parent_parts if part)
    else:
        parent = node.module or ""
    names: list[str] = []
    if parent:
        names.append(parent)
    for alias in node.names:
        if alias.name == "*":
            continue
        names.append(f"{parent}.{alias.name}" if parent else alias.name)
    return tuple(names)


def _imported_names(source: str, module_name: str, *, is_package: bool = False) -> tuple[str, ...]:
    return tuple(name for _lineno, name in _walk_imported_names(source, module_name, is_package=is_package))


def _walk_imported_names(
    source: str,
    module_name: str,
    *,
    is_package: bool,
    filename: str = "<graph>",
) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(source, filename=filename)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                (node.lineno, imported)
                for imported in _resolve_import_from(node, module_name, is_package=is_package)
            )
    return tuple(found)


def _imported_modules(path: Path, *, module_name: str | None = None) -> tuple[tuple[int, str], ...]:
    is_package = path.name == "__init__.py"
    if module_name is None:
        try:
            _owner, module_name = _graph_module_identity(path)
        except ValueError:
            module_name = path.stem
            is_package = False
    return _walk_imported_names(
        path.read_text(encoding="utf-8"),
        module_name,
        is_package=is_package,
        filename=str(path),
    )


def _is_forbidden_graph_import(imported: str, owner: str) -> bool:
    parts = imported.split(".")
    root_name = parts[0]
    if root_name in FORBIDDEN_ADAPTERS:
        return True
    if root_name not in FEATURE_PACKAGES:
        return False
    rest = parts[1:]
    if any(part in FORBIDDEN_FEATURE_IMPLEMENTATION for part in rest):
        return True
    return root_name != owner and "graphs" in rest


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
        "assurance_quality.graphs.factory",
        "assurance_healing.graphs.factory",
        "assurance_improvement.graphs.factory",
    )
    assert EXPECTED_FEATURE_FACTORY_SYMBOLS == (
        "assurance_intake.graphs.factory:build_intake_graphs",
        "assurance_generation.graphs.factory:build_generation_graphs",
        "assurance_execution.graphs.factory:build_execution_graphs",
        "assurance_quality.graphs.factory:build_quality_graphs",
        "assurance_healing.graphs.factory:build_healing_graphs",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    )
    root = _repo_root()
    public: list[str] = []
    for package_name, relative in FEATURE_SOURCE_TREES:
        package_root = root / relative
        for path in package_root.rglob("*.py"):
            if "__pycache__" in path.parts or "graphs" not in path.parts:
                continue
            for _lineno, imported in _imported_modules(path, module_name=_graph_module_identity(path)[1]):
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


def test_architecture_scan_rejects_relative_and_from_import_forms() -> None:
    owner = "assurance_intake"
    module_name = "assurance_intake.graphs.nodes"
    cases = (
        ("from ..operations import prepare", "assurance_intake.operations"),
        ("from ..validators import check", "assurance_intake.validators"),
        ("from ..effects import apply", "assurance_intake.effects"),
        ("from . import operations", "assurance_intake.graphs.operations"),
        ("from assurance_generation import graphs", "assurance_generation.graphs"),
        ("from assurance_generation.graphs import factory", "assurance_generation.graphs"),
        ("from assurance_product import cli", "assurance_product"),
        ("from agent_runtime_opencode import client", "agent_runtime_opencode"),
        ("from agent_runtime_cursor import client", "agent_runtime_cursor"),
    )
    for source, expected in cases:
        imported = _imported_names(source, module_name)
        violations = [name for name in imported if _is_forbidden_graph_import(name, owner)]
        assert any(name == expected or name.startswith(f"{expected}.") for name in violations), (
            f"{source!r} resolved {imported!r}, expected a violation of {expected!r}"
        )


def test_feature_graph_modules_reject_foreign_and_implementation_imports() -> None:
    violations: list[str] = []
    for path in _graph_python_files():
        owner, module_name = _graph_module_identity(path)
        for lineno, imported in _imported_modules(path, module_name=module_name):
            if _is_forbidden_graph_import(imported, owner):
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


def test_authenticated_factory_symbols_exist_as_public_callables() -> None:
    import importlib

    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES

    assert tuple(item.symbol for item in FEATURE_GRAPH_FACTORIES) == EXPECTED_FEATURE_FACTORY_SYMBOLS
    for symbol, module_name in zip(
        EXPECTED_FEATURE_FACTORY_SYMBOLS, EXPECTED_FEATURE_FACTORY_MODULES, strict=True
    ):
        module_from_symbol, attribute = symbol.split(":")
        assert module_from_symbol == module_name
        assert not attribute.startswith("_")
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
        assert callable(factory)
        assert factory.__name__ == attribute


def test_yaml_workflow_modules_remain_for_legacy_coexistence() -> None:
    root = _repo_root()
    for _package_name, relative in FEATURE_SOURCE_TREES:
        module_yaml = root / relative / "resources" / "workflow" / "module.yaml"
        factory = root / relative / "graphs" / "factory.py"
        assert module_yaml.is_file(), module_yaml
        assert factory.is_file(), factory
        assert module_yaml.stat().st_size > 0
