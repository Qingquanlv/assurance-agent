from __future__ import annotations

import ast
from configparser import ConfigParser
import sys
import tomllib
from pathlib import Path

import pytest

CLIENT_DISTRIBUTIONS = (
    ("agent-runtime-contracts", "agent-runtime-contracts"),
    ("agent-runtime-opencode", "agent-runtime-opencode"),
    ("agent-runtime-cursor", "agent-runtime-cursor"),
)

CLIENT_DOWNWARD_FORBIDDEN = frozenset(
    {
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "assurance_product",
    }
)

CLIENT_SOURCE_TREES = (
    ("agent_runtime_contracts", "packages/adapters/agent-runtime-contracts/agent_runtime_contracts"),
    ("agent_runtime_opencode", "packages/adapters/agent-runtime-opencode/agent_runtime_opencode"),
    ("agent_runtime_cursor", "packages/adapters/agent-runtime-cursor/agent_runtime_cursor"),
)

ADAPTER_PEERS = {
    "agent_runtime_opencode": "agent_runtime_cursor",
    "agent_runtime_cursor": "agent_runtime_opencode",
}

FEATURE_DISTRIBUTIONS = (
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-healing",
    "assurance-quality",
    "assurance-improvement",
)

FEATURE_SOURCE_TREES = (
    ("assurance_intake", "packages/capabilities/assurance-intake/assurance_intake"),
    ("assurance_generation", "packages/capabilities/assurance-generation/assurance_generation"),
    ("assurance_execution", "packages/capabilities/assurance-execution/assurance_execution"),
    ("assurance_healing", "packages/capabilities/assurance-healing/assurance_healing"),
    ("assurance_quality", "packages/capabilities/assurance-quality/assurance_quality"),
    ("assurance_improvement", "packages/capabilities/assurance-improvement/assurance_improvement"),
)

FEATURE_DOWNWARD_FORBIDDEN = frozenset(
    {
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_product",
    }
)

FEATURE_IMPLEMENTATION_SUFFIXES = (
    "plugin",
    "operations",
    "validators",
    "effects",
    "resource_loader",
    "resources",
    "workflow",
)

FOUR_ROLE_ROOTS = frozenset(
    {
        "graph_engine",
        "agent_runtime_contracts",
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "assurance_product",
    }
)

PRODUCT_SOURCE_TREE = "packages/products/assurance-product/assurance_product"

PRODUCT_LOWER_PUBLIC_SURFACES = frozenset(
    {
        "graph_engine",
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "agent_runtime_contracts",
        "agent_runtime_opencode",
        "agent_runtime_cursor",
    }
)


ALLOWED_ROOTS = set(sys.stdlib_module_names) | {
    "graph_engine",
    "packaging",
    "pydantic",
    "pydantic_core",
    "yaml",
}

FRAMEWORK_FORBIDDEN_ROOTS = frozenset(
    {
        "agent_runtime_contracts",
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "assurance_product",
        "graph_engine_toy_a",
        "graph_engine_toy_b",
        "agent_runtime_fixture",
    }
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_framework_has_the_only_graph_engine_source_tree(repo_root: Path) -> None:
    assert (repo_root / "packages/framework/graph-engine/graph_engine").is_dir()
    assert not (repo_root / "packages" / "graph-engine").exists()


def test_clients_have_the_only_agent_runtime_source_trees(repo_root: Path) -> None:
    for directory_name, project_name in CLIENT_DISTRIBUTIONS:
        target = repo_root / "packages" / "adapters" / directory_name
        assert target.is_dir()
        assert not (repo_root / "packages" / directory_name).exists()
        pyproject = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["name"] == project_name


def test_clients_import_no_features_or_product(repo_root: Path) -> None:
    violations: list[str] = []
    for package_name, relative in CLIENT_SOURCE_TREES:
        forbidden = set(CLIENT_DOWNWARD_FORBIDDEN)
        if package_name == "agent_runtime_contracts":
            forbidden.update({"agent_runtime_opencode", "agent_runtime_cursor"})
        else:
            forbidden.add(ADAPTER_PEERS[package_name])
        root = repo_root / relative
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".", 1)[0]]
                else:
                    continue
                for name in names:
                    if name in forbidden:
                        violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []


def test_all_capability_wheels_are_feature_subprojects(repo_root: Path) -> None:
    expected = {
        "assurance-intake",
        "assurance-generation",
        "assurance-execution",
        "assurance-healing",
        "assurance-quality",
        "assurance-improvement",
    }
    assert {path.name for path in (repo_root / "packages/capabilities").iterdir()} == expected
    for directory_name in FEATURE_DISTRIBUTIONS:
        target = repo_root / "packages" / "capabilities" / directory_name
        assert target.is_dir()
        assert not (repo_root / "packages" / directory_name).exists()
        pyproject = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["name"] == directory_name


def test_features_import_no_concrete_clients_or_product(repo_root: Path) -> None:
    violations: list[str] = []
    for _package_name, relative in FEATURE_SOURCE_TREES:
        root = repo_root / relative
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".", 1)[0]]
                else:
                    continue
                for name in names:
                    if name in FEATURE_DOWNWARD_FORBIDDEN:
                        violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []


def test_product_is_the_only_assurance_product_source_tree(repo_root: Path) -> None:
    target = repo_root / "packages" / "products" / "assurance-product"
    assert (target / "assurance_product").is_dir()
    assert not (repo_root / "packages" / "assurance-product").exists()
    pyproject = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["name"] == "assurance-product"
    assert pyproject["project"]["scripts"] == {"aa": "assurance_product.cli:main"}
    owners: list[str] = []
    for path in (repo_root / "packages").rglob("pyproject.toml"):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "aa" in (data.get("project") or {}).get("scripts", {}):
            owners.append(data["project"]["name"])
    assert owners == ["assurance-product"]
    assert {path.name for path in (repo_root / "packages/products").iterdir() if path.is_dir()} == {
        "assurance-product"
    }


def _importlinter_names(parser: ConfigParser, section: str, option: str) -> set[str]:
    return {line.strip() for line in parser[section][option].splitlines() if line.strip()}


def test_import_linter_encodes_four_role_matrix(repo_root: Path) -> None:
    parser = ConfigParser()
    assert parser.read(repo_root / ".importlinter")
    roots = _importlinter_names(parser, "importlinter", "root_packages")
    assert FOUR_ROLE_ROOTS <= roots
    assert "assurance_product" in roots

    framework_forbidden = _importlinter_names(
        parser, "importlinter:contract:graph-engine-independent", "forbidden_modules"
    )
    assert {
        "agent_runtime_contracts",
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
        "assurance_product",
    } <= framework_forbidden

    clients_forbidden = _importlinter_names(
        parser, "importlinter:contract:clients-downward", "forbidden_modules"
    )
    assert CLIENT_DOWNWARD_FORBIDDEN <= clients_forbidden

    features_forbidden = _importlinter_names(
        parser, "importlinter:contract:features-downward", "forbidden_modules"
    )
    assert FEATURE_DOWNWARD_FORBIDDEN <= features_forbidden

    generation_forbidden = _importlinter_names(
        parser, "importlinter:contract:generation-intake-contracts-only", "forbidden_modules"
    )
    for suffix in FEATURE_IMPLEMENTATION_SUFFIXES:
        assert f"assurance_intake.{suffix}" in generation_forbidden
    assert "assurance_intake.contracts" not in generation_forbidden

    composition_sources = _importlinter_names(
        parser, "importlinter:contract:product-composition-layer", "source_modules"
    )
    composition_forbidden = _importlinter_names(
        parser, "importlinter:contract:product-composition-layer", "forbidden_modules"
    )
    assert composition_sources == FOUR_ROLE_ROOTS - {"assurance_product"}
    assert composition_forbidden == {"assurance_product"}

    for section in parser.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        if parser[section].get("type") != "forbidden":
            continue
        sources = _importlinter_names(parser, section, "source_modules")
        assert "assurance_product" not in sources, section


def test_product_may_import_lower_public_surfaces(repo_root: Path) -> None:
    root = repo_root / PRODUCT_SOURCE_TREE
    imported: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", 1)[0]]
            else:
                continue
            imported.update(names)
    assert "graph_engine" in imported
    assert imported & {
        "assurance_intake",
        "assurance_generation",
        "assurance_execution",
        "assurance_healing",
        "assurance_quality",
        "assurance_improvement",
    }
    assert imported <= (
        PRODUCT_LOWER_PUBLIC_SURFACES
        | {"assurance_product"}
        | set(sys.stdlib_module_names)
        | {"click", "packaging", "pydantic", "pydantic_core", "yaml"}
    )


def test_graph_engine_imports_no_product_packages(repo_root: Path) -> None:
    root = repo_root / "packages/framework/graph-engine/graph_engine"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", 1)[0]]
            else:
                continue
            for name in names:
                if name in FRAMEWORK_FORBIDDEN_ROOTS or name not in ALLOWED_ROOTS:
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []
