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
        "assurance_product",
    }
)

CROSS_FEATURE_FORBIDDEN_SUFFIXES = ("graphs",)
OP_IMPLEMENTATION_MODULES = frozenset({"hooks", "models"})


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


def _is_op_package(owner: str, name: str) -> bool:
    trees = dict(FEATURE_SOURCE_TREES)
    return (_repo_root() / trees[owner] / "ops" / name / "__init__.py").is_file()


def _is_forbidden_graph_import(imported: str, owner: str, *, graph_module: str | None = None) -> bool:
    del graph_module
    parts = imported.split(".")
    root_name = parts[0]
    if root_name in FORBIDDEN_ADAPTERS:
        return True
    if root_name not in FEATURE_PACKAGES:
        return False
    rest = parts[1:]
    if rest and rest[0] == "ops":
        if root_name != owner or len(rest) == 1 or not _is_op_package(owner, rest[1]):
            return True
        return len(rest) > 3 or (len(rest) == 3 and rest[2] in OP_IMPLEMENTATION_MODULES)
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
    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES

    root = _repo_root()
    packages = dict(FEATURE_SOURCE_TREES)
    for ref in FEATURE_GRAPH_FACTORIES:
        module_name, attribute = ref.symbol.split(":", 1)
        package_name, _, remainder = module_name.partition(".")
        assert remainder == "graphs.factory"
        assert not attribute.startswith("_")
        assert (root / packages[package_name] / "graphs" / "factory.py").is_file()
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
                    for suffix in (
                        "state",
                        "nodes",
                        "calls",
                        "steps",
                        "routes",
                        "prepare",
                        "preparation",
                        "case",
                    )
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
    module_name = "assurance_intake.graphs.state"
    cases = (
        ("from ..operations import prepare", "assurance_intake.operations"),
        ("from ..ops import router", "assurance_intake.ops.router"),
        ("from ..ops.case_design import hooks", "assurance_intake.ops.case_design.hooks"),
        ("from ..ops.case_design.models import CaseDesignInputV1", "assurance_intake.ops.case_design.models"),
        ("from assurance_generation.ops.plan import op", "assurance_generation.ops.plan.op"),
        ("from ..validators import check", "assurance_intake.validators"),
        ("from ..effects import apply", "assurance_intake.effects"),
        ("from . import operations", "assurance_intake.graphs.operations"),
        ("from assurance_generation import graphs", "assurance_generation.graphs"),
        ("from assurance_generation.graphs import factory", "assurance_generation.graphs"),
        ("from assurance_product import cli", "assurance_product"),
        ("from agent_runtime_opencode import client", "agent_runtime_opencode"),
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
            if _is_forbidden_graph_import(imported, owner, graph_module=module_name):
                violations.append(f"{path}:{lineno}:{imported}")
    assert violations == []


def test_graphs_read_only_their_own_op_declarations() -> None:
    assert not _is_forbidden_graph_import("assurance_intake.ops.case_design.op", "assurance_intake")
    assert not _is_forbidden_graph_import(
        "assurance_intake.ops.case_design.CaseDesignInputV1", "assurance_intake"
    )
    assert _is_forbidden_graph_import("assurance_intake.ops.case_design.hooks", "assurance_intake")
    assert _is_forbidden_graph_import("assurance_intake.ops.case_design.models.X", "assurance_intake")
    assert _is_forbidden_graph_import("assurance_intake.ops.router", "assurance_intake")
    assert _is_forbidden_graph_import("assurance_intake.ops.case_design.op", "assurance_generation")
    assert not _is_forbidden_graph_import(
        "assurance_intake.domain.history_refs.merge_history_refs",
        "assurance_generation",
    )


def test_graph_attempts_use_registration_helper() -> None:
    root = _repo_root()
    examples = (
        "examples/graph-engine-toy-a/graph_engine_toy_a/product.py",
        "examples/graph-engine-toy-b/graph_engine_toy_b/product.py",
        "examples/agent-runtime-fixture/agent_runtime_fixture/product.py",
    )
    paths = (*_graph_python_files(), *(root / relative for relative in examples))
    violations = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{path}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "attempt"
        )
    assert violations == []


def test_importlinter_keeps_graphs_forbidden_across_features() -> None:
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
            for suffix in CROSS_FEATURE_FORBIDDEN_SUFFIXES:
                assert suffix in suffixes, f"{section}:{package_name}.{suffix}"


def test_authenticated_factory_symbols_exist_as_public_callables() -> None:
    import importlib

    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES

    for ref in FEATURE_GRAPH_FACTORIES:
        module_name, attribute = ref.symbol.split(":", 1)
        assert module_name.endswith(".graphs.factory")
        assert not attribute.startswith("_")
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
        assert callable(factory)
        assert factory.__name__ == attribute


def test_yaml_workflow_modules_are_gone_after_v3_cutover() -> None:
    root = _repo_root()
    for _package_name, relative in FEATURE_SOURCE_TREES:
        module_yaml = root / relative / "resources" / "workflow" / "module.yaml"
        factory = root / relative / "graphs" / "factory.py"
        assert not module_yaml.exists(), module_yaml
        assert factory.is_file(), factory
