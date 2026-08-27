from __future__ import annotations

from pathlib import Path

import pytest


def write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def minimal_final_tree(tmp_path: Path) -> Path:
    write(tmp_path / "pyproject.toml", "[project]\nname = 'assurance-workspace'\n")
    write(tmp_path / "packages/assurance-product/pyproject.toml", "[project]\nname = 'assurance-product'\n")
    write(tmp_path / "packages/assurance-product/assurance_product/__init__.py", "")
    write(tmp_path / "packages/graph-engine/pyproject.toml", "[project]\nname = 'graph-engine'\n")
    write(tmp_path / "packages/graph-engine/graph_engine/__init__.py", "")
    write(tmp_path / "README.md", "# workspace\n")
    write(tmp_path / "AGENTS.md", "# agents\n")
    write(tmp_path / ".github/workflows/ci.yml", "name: CI\n")
    write(
        tmp_path / "examples/minimal-product/aa_sample/product.py",
        "class SampleProduct:\n    id = 'sample'\n",
    )
    write(tmp_path / "scripts/no_legacy_allowlist.txt", "")
    return tmp_path


def test_no_legacy_scanner_module_is_importable() -> None:
    from scripts.check_no_legacy import scan_repository

    assert callable(scan_repository)


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("packages/assurance-product/assurance_product/bad.py", "import assurance_agent\n"),
        ("packages/graph-engine/graph_engine/bad.py", "import assurance_kernel\n"),
        ("packages/assurance-product/pyproject.toml", "aa-next = 'x:y'\n"),
        ("packages/graph-engine/graph_engine/default.yaml", "graph: assurance-full\n"),
        ("scripts/bad.sh", "AA_RUNTIME=legacy\n"),
    ],
)
def test_no_legacy_gate_rejects_forbidden_seams(tmp_path: Path, relative: str, source: str) -> None:
    from scripts.check_no_legacy import scan_repository

    root = minimal_final_tree(tmp_path)
    write(root / relative, source)
    assert scan_repository(root, (), scope="repository")


def test_runtime_scope_ignores_current_docs_but_repository_scope_does_not(tmp_path: Path) -> None:
    from scripts.check_no_legacy import scan_repository

    root = minimal_final_tree(tmp_path)
    write(root / "README.md", "install aa-next from the deleted product\n")
    assert scan_repository(root, (), scope="runtime") == ()
    assert scan_repository(root, (), scope="repository")


def test_repository_scope_rejects_leftover_unit_imports(tmp_path: Path) -> None:
    from scripts.check_no_legacy import scan_repository

    root = minimal_final_tree(tmp_path)
    write(root / "tests/unit/leftover.py", "from assurance_agent.product import select_product\n")
    assert scan_repository(root, (), scope="repository")


def test_allowlist_rejects_wildcards_and_directories(tmp_path: Path) -> None:
    from scripts.check_no_legacy import load_allowlist

    wildcard = tmp_path / "wild.txt"
    wildcard.write_text("docs/superpowers/plans/*.md\n", encoding="utf-8")
    with pytest.raises(ValueError, match="wildcard"):
        load_allowlist(wildcard)

    directory = tmp_path / "dir.txt"
    directory.write_text("docs/superpowers/plans\n", encoding="utf-8")
    write(tmp_path / "docs/superpowers/plans/example.md", "historical\n")
    with pytest.raises(ValueError, match="directory"):
        load_allowlist(directory, root=tmp_path)


def test_exact_allowlist_exempts_only_that_historical_file(tmp_path: Path) -> None:
    from scripts.check_no_legacy import load_allowlist, scan_repository

    root = minimal_final_tree(tmp_path)
    historical = Path("docs/superpowers/specs/frozen.md")
    live = Path("docs/superpowers/specs/other.md")
    write(root / historical, "import assurance_agent\n")
    write(root / live, "import assurance_kernel\n")
    write(root / "scripts/no_legacy_allowlist.txt", f"{historical.as_posix()}\n")
    allowlist = load_allowlist(root / "scripts/no_legacy_allowlist.txt", root=root)
    violations = scan_repository(root, allowlist, scope="repository")
    assert violations
    assert all(historical.as_posix() not in str(item.path) for item in violations)
    assert any(live.as_posix() in Path(item.path).as_posix() for item in violations)


def test_real_repository_has_no_legacy_violations(repo_root: Path) -> None:
    from scripts.check_no_legacy import load_allowlist, scan_repository

    allowlist = load_allowlist(repo_root / "scripts/no_legacy_allowlist.txt", root=repo_root)
    assert scan_repository(repo_root, allowlist, scope="repository") == ()


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
