from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import sys
import tomllib
import zipfile

import yaml

from tests.phase4.six_wheel_harness import (
    BINDINGS_ROOTS,
    FIXTURE_PERMISSION_BYTES,
    PRODUCT_DISTRIBUTION,
    PRODUCT_ROOT,
    REPO_ROOT,
    binding_data_template,
    hashlib_sha256,
    production_aa_products,
    production_product_entrypoints,
    resolve_fixture,
)


def test_six_wheel_product_resolves_exact_dependency_order() -> None:
    composition = resolve_fixture("phase4-opencode")
    assert composition.dependency_order == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
        "runtime.opencode",
        "test.assurance.bindings",
    )


def test_cursor_product_resolves_runtime_cursor_last_before_bindings() -> None:
    composition = resolve_fixture("phase4-cursor")
    assert composition.dependency_order == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
        "runtime.cursor",
        "test.assurance.bindings",
    )


def test_adapter_selections_have_different_composition_and_lock_digests() -> None:
    opencode = resolve_fixture("phase4-opencode")
    cursor = resolve_fixture("phase4-cursor")
    assert opencode.digest != cursor.digest
    assert opencode.lock_digest != cursor.lock_digest
    assert opencode.composition.manifest.workflow == cursor.composition.manifest.workflow
    assert opencode.composition.manifest.entrypoints == {"fixture": "root"}
    assert cursor.composition.manifest.entrypoints == {"fixture": "root"}


def test_binding_digests_recompute_from_checked_in_bytes() -> None:
    expected = binding_data_template()
    assert expected["execution"]["permission_profile_digest"] == hashlib_sha256(FIXTURE_PERMISSION_BYTES)
    assert expected["request_policy_digest"] == hashlib_sha256(
        (BINDINGS_ROOTS["phase4-opencode"] / "model-policy.json").read_bytes()
    )
    for root in BINDINGS_ROOTS.values():
        policy_bytes = (root / "model-policy.json").read_bytes()
        assert hashlib_sha256(policy_bytes) == expected["request_policy_digest"]
        document = _load_yaml(root / "plugin.yaml")
        for binding in document["bindings"]:
            assert binding["data"] == expected


def test_checked_in_declarations_bind_distinct_entrypoints() -> None:
    opencode = _load_json(
        PRODUCT_ROOT / "test_assurance_phase4_product" / "product-opencode-declaration.json"
    )
    cursor = _load_json(PRODUCT_ROOT / "test_assurance_phase4_product" / "product-cursor-declaration.json")
    assert opencode["source"]["entrypoint_name"] == "phase4-opencode"
    assert cursor["source"]["entrypoint_name"] == "phase4-cursor"
    assert opencode["source"]["entrypoint_value"].endswith("Phase4OpenCodeProduct")
    assert cursor["source"]["entrypoint_value"].endswith("Phase4CursorProduct")
    assert opencode["source"]["distribution"] == cursor["source"]["distribution"] == PRODUCT_DISTRIBUTION
    assert opencode["manifest"]["entrypoints"] == cursor["manifest"]["entrypoints"] == {"fixture": "root"}


def test_fixture_product_is_absent_from_workspace_dependencies_archives_and_entrypoints() -> None:
    root_project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = tuple(root_project["tool"]["uv"]["workspace"]["members"])
    dependencies = tuple(root_project["project"]["dependencies"])
    dev = tuple(root_project["dependency-groups"]["dev"])
    sources = root_project["tool"]["uv"]["sources"]
    fixture_rel = "tests/phase4/fixtures/six-wheel-product"
    assert fixture_rel not in members
    assert PRODUCT_DISTRIBUTION not in dependencies
    assert PRODUCT_DISTRIBUTION not in dev
    assert PRODUCT_DISTRIBUTION not in sources
    assert "phase4-opencode" not in production_product_entrypoints()
    assert "phase4-cursor" not in production_product_entrypoints()
    assert "phase4-opencode" not in production_aa_products()
    assert "phase4-cursor" not in production_aa_products()
    lock_text = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    hatch_packages = tuple(root_project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
    assert PRODUCT_DISTRIBUTION not in lock_text
    assert "test_assurance_phase4_product" not in lock_text
    assert "test_assurance_phase4_product" not in hatch_packages
    assert fixture_rel not in (REPO_ROOT / "scripts" / "packaging_smoke_test.sh").read_text(encoding="utf-8")
    installed_names = {
        dist.metadata["Name"] for dist in metadata.distributions() if dist.metadata.get("Name")
    }
    assert PRODUCT_DISTRIBUTION not in installed_names
    built = resolve_fixture("phase4-opencode")
    assert built.fixture_wheel.is_file()
    assert not built.fixture_wheel.is_relative_to(REPO_ROOT)
    with zipfile.ZipFile(built.fixture_wheel) as archive:
        names = archive.namelist()
    assert any(name.startswith("test_assurance_phase4_product/") for name in names)


def test_six_wheel_resolve_authenticates_committed_quality_and_restores_imports() -> None:
    committed = REPO_ROOT / "packages" / "assurance-quality" / "assurance_quality"
    resolved = resolve_fixture("phase4-opencode")
    copied = resolved.workspace / "wheels" / "assurance-quality" / "assurance_quality"
    for path in committed.rglob("*.py"):
        rel = path.relative_to(committed)
        assert (copied / rel).read_bytes() == path.read_bytes()
    wheel_root = resolved.workspace / "wheels"
    leftover = [entry for entry in sys.path if entry.startswith(str(resolved.workspace))]
    assert leftover == []
    assert str(resolved.product_root) not in sys.path
    assert str(wheel_root / "assurance-quality") not in sys.path
    import assurance_quality

    quality_file = Path(assurance_quality.__file__).resolve()
    assert not quality_file.is_relative_to(resolved.workspace)


def _load_json(path: Path) -> dict[str, object]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _load_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded
