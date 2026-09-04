from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

CURRENT_DOC_RELATIVES = (
    "README.md",
    "AGENTS.md",
    "packages/framework/graph-engine/README.md",
    "packages/products/assurance-product/README.md",
)

PRODUCT_DOC_RELATIVES = (
    "README.md",
    "AGENTS.md",
    "packages/products/assurance-product/README.md",
)

FORBIDDEN_FRAGMENTS = (
    "assurance_agent",
    "assurance_kernel",
    "aa-next",
    "workspace/trees",
    "HEAD.json",
    "result-tree",
    "result registry",
    "result-registry",
    "whole-tree export",
    "whole tree export",
    "aa workflow",
    "aa apply",
)

DELIVERY_FLOW = "`aa run` to achieved, then `aa export`, then optional `aa archive`"
OWNERSHIP = "`aa` is owned by `assurance-product`"
YAML_RULES = (
    "YAML replaces graph",
    "YAML replaces the graph",
    "Python wheels own `StateGraph` topology",
    "Python wheels own topology",
)
WHEELS_RULES = (
    "Python wheels add installed capability",
    "Python wheels add capability",
    "Python wheels own `StateGraph` topology",
    "Python wheels own the graph",
)
ORG_CONFIGS = (
    "`.aa/` holds organization configuration only",
    "Organization configuration stays in the project's `.aa/`",
    "`.aa/` contains organization configuration",
    "`.aa/` contains closed organization data only",
)
NO_SUT_PLUGINS = (
    "does not load executable plugins from the system under test",
    "does not scan the SUT",
    "does not scan a SUT",
    "does not load executable plugins, graphs, handlers, schemas, validators, or runtime bindings from the system under test",
)


def current_document_paths(repo_root: Path) -> tuple[Path, ...]:
    return tuple(repo_root / relative for relative in CURRENT_DOC_RELATIVES)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _folded(path: Path) -> str:
    return " ".join(_read(path).split())


def _has_any(text: str, options: tuple[str, ...]) -> bool:
    return any(option in text for option in options)


@pytest.fixture
def repo_root() -> Path:
    return REPO


def test_current_docs_exist(repo_root: Path) -> None:
    for path in current_document_paths(repo_root):
        assert path.is_file(), path


def test_current_docs_omit_legacy_package_command_and_result_language(repo_root: Path) -> None:
    for path in current_document_paths(repo_root):
        text = _read(path)
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in text, f"{path.relative_to(repo_root)} still names {fragment}"


def test_current_docs_state_installed_product_plugin_rules(repo_root: Path) -> None:
    for path in current_document_paths(repo_root):
        text = _folded(path)
        relative = path.relative_to(repo_root).as_posix()
        assert _has_any(text, YAML_RULES), relative
        assert _has_any(text, WHEELS_RULES), relative
        assert _has_any(text, ORG_CONFIGS), relative
        assert _has_any(text, NO_SUT_PLUGINS), relative


def test_product_docs_state_final_delivery_workflow_and_ownership(repo_root: Path) -> None:
    for relative in PRODUCT_DOC_RELATIVES:
        text = _folded(repo_root / relative)
        assert DELIVERY_FLOW in text, relative
        assert OWNERSHIP in text, relative
        assert "aa compile" in text, relative
        assert "aa start" in text, relative
        assert "aa resume" in text, relative
        assert "aa bindings build" in text, relative
        assert "aa lock show" in text, relative


def test_workspace_readme_names_final_product_and_benchmark_paths(repo_root: Path) -> None:
    text = _folded(repo_root / "README.md")
    assert "tests/product/" in text
    assert "benchmark/assurance-product/" in text
    assert "Cursor live" not in text


def test_graph_engine_readme_states_neutral_installed_product_ownership(repo_root: Path) -> None:
    text = _folded(repo_root / "packages/framework/graph-engine/README.md")
    assert "business-neutral" in text
    assert "no default product" in text
    assert OWNERSHIP not in text
    assert DELIVERY_FLOW not in text
    assert "Cursor live" not in text
