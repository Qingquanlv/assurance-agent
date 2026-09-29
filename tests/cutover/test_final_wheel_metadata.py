from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from tests.product.test_product_packaging import read_wheel_metadata


CLOSED_WHEEL_PACKAGES = (
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
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def smoke_script(repo_root: Path) -> str:
    return (repo_root / "scripts/assurance_product_wheel_smoke_test.sh").read_text(encoding="utf-8")


@pytest.fixture
def built_product_wheel(tmp_path: Path) -> Path:
    import subprocess

    subprocess.run(
        ["uv", "build", "--package", "assurance-product", "--out-dir", str(tmp_path)],
        check=True,
    )
    wheels = tuple(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_legacy_packaging_smoke_is_removed(repo_root: Path) -> None:
    assert not (repo_root / "scripts/packaging_smoke_test.sh").exists()


def test_ci_uses_final_product_smoke(repo_root: Path) -> None:
    ci = (repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "packaging_smoke_test.sh" not in ci
    assert "scripts/check_no_legacy.py" not in ci
    assert "scripts/assurance_product_wheel_smoke_test.sh" in ci


def test_product_wheel_owns_aa_and_rejects_legacy_names(built_product_wheel: Path) -> None:
    metadata = read_wheel_metadata(built_product_wheel)
    assert metadata.name == "assurance-product"
    assert metadata.entry_points["console_scripts"] == {"aa": "assurance_product.cli:main"}
    assert "aa-next" not in metadata.entry_points["console_scripts"]
    assert all("assurance-agent" not in item for item in metadata.requires_dist)
    assert all("assurance-kernel" not in item for item in metadata.requires_dist)


def test_no_workspace_wheel_ships_obsolete_orchestration_contracts(
    repo_root: Path, built_product_wheel: Path
) -> None:
    forbidden = (
        "workflow-schema.yaml",
        "execution-contracts.yaml",
        "resources/workflow/module.yaml",
        "resources/workflow/main.yaml",
        "graph-inventory.yaml",
        "runtime_selection.py",
    )
    with zipfile.ZipFile(built_product_wheel) as archive:
        names = archive.namelist()
    assert all(not any(item in name for item in forbidden) for name in names)
    sample = repo_root / "examples" / "minimal-product"
    assert not (sample / "aa_sample" / "_resources" / "schemas" / "workflow-schema.yaml").exists()
    assert not (sample / "aa_sample" / "_resources" / "schemas" / "execution-contracts.yaml").exists()
    pyproject = (sample / "pyproject.toml").read_text(encoding="utf-8")
    assert "workflow-schema" not in pyproject
    assert "execution-contracts" not in pyproject


def test_final_smoke_builds_eleven_wheels_and_installs_aa(smoke_script: str) -> None:
    for package in CLOSED_WHEEL_PACKAGES:
        assert f"--package {package}" in smoke_script
    assert "aa-next compile" not in smoke_script
    assert "aa-next bindings" not in smoke_script
    assert 'if [[ ! -x "$venv/bin/aa" ]]; then' in smoke_script
    assert "aa compile \\" in smoke_script
    assert "aa bindings build \\" in smoke_script
    assert "check_no_legacy.py" not in smoke_script
