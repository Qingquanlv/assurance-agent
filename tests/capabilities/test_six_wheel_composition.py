from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import sys
import tomllib
from typing import Any, cast
import zipfile

import yaml

from tests.capabilities.six_wheel_harness import (
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
    composition = resolve_fixture("six-wheel-opencode")
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


def test_opencode_product_uses_graph_factory_without_workflow() -> None:
    opencode = resolve_fixture("six-wheel-opencode")
    assert not hasattr(opencode.composition.manifest, "workflow")
    assert (
        opencode.composition.manifest.graph_factory_symbol
        == "test_six_wheel_product.product:build_six_wheel_graphs"
    )
    assert opencode.composition.manifest.entrypoints == {"fixture": "root"}


def test_binding_digests_recompute_from_checked_in_bytes() -> None:
    expected = binding_data_template()
    execution = cast(dict[str, Any], expected["execution"])
    assert execution["permission_profile_digest"] == hashlib_sha256(FIXTURE_PERMISSION_BYTES)
    assert expected["request_policy_digest"] == hashlib_sha256(
        (BINDINGS_ROOTS["six-wheel-opencode"] / "model-policy.json").read_bytes()
    )
    for root in BINDINGS_ROOTS.values():
        policy_bytes = (root / "model-policy.json").read_bytes()
        assert hashlib_sha256(policy_bytes) == expected["request_policy_digest"]
        document = _load_yaml(root / "plugin.yaml")
        for binding in cast(list[dict[str, Any]], document["bindings"]):
            assert binding["data"] == expected


def test_checked_in_declaration_binds_opencode_entrypoint() -> None:
    opencode = _load_json(PRODUCT_ROOT / "test_six_wheel_product" / "product-opencode-declaration.json")
    opencode_source = cast(dict[str, Any], opencode["source"])
    opencode_manifest = cast(dict[str, Any], opencode["manifest"])
    assert opencode_source["entrypoint_name"] == "six-wheel-opencode"
    assert str(opencode_source["entrypoint_value"]).endswith("SixWheelOpenCodeProduct")
    assert opencode_source["distribution"] == PRODUCT_DISTRIBUTION
    assert opencode_manifest["entrypoints"] == {"fixture": "root"}
    assert not (PRODUCT_ROOT / "test_six_wheel_product" / "product-cursor-declaration.json").exists()


def test_fixture_product_is_absent_from_workspace_dependencies_archives_and_entrypoints() -> None:
    root_project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = tuple(root_project["tool"]["uv"]["workspace"]["members"])
    dependencies = tuple(root_project.get("project", {}).get("dependencies", ()))
    dev = tuple(root_project["dependency-groups"]["dev"])
    sources = root_project["tool"]["uv"]["sources"]
    fixture_rel = "tests/capabilities/fixtures/six-wheel-product"
    assert fixture_rel not in members
    assert PRODUCT_DISTRIBUTION not in dependencies
    assert PRODUCT_DISTRIBUTION not in dev
    assert PRODUCT_DISTRIBUTION not in sources
    assert "six-wheel-opencode" not in production_product_entrypoints()
    assert "six-wheel-cursor" not in production_product_entrypoints()
    assert "six-wheel-opencode" not in production_aa_products()
    assert "six-wheel-cursor" not in production_aa_products()
    lock_text = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    hatch = root_project.get("tool", {}).get("hatch", {})
    hatch_packages = tuple(hatch.get("build", {}).get("targets", {}).get("wheel", {}).get("packages", ()))
    assert PRODUCT_DISTRIBUTION not in lock_text
    assert "test_six_wheel_product" not in lock_text
    assert "test_six_wheel_product" not in hatch_packages
    smoke_scripts = (
        REPO_ROOT / "scripts" / "graph_engine_smoke_test.sh",
        REPO_ROOT / "scripts" / "assurance_capability_wheel_smoke_test.sh",
        REPO_ROOT / "scripts" / "assurance_product_wheel_smoke_test.sh",
    )
    for script in smoke_scripts:
        assert fixture_rel not in script.read_text(encoding="utf-8")
    installed_names = {
        str(dist.metadata["Name"])
        for dist in metadata.distributions()
        if "Name" in dist.metadata and dist.metadata["Name"]
    }
    assert PRODUCT_DISTRIBUTION not in installed_names
    built = resolve_fixture("six-wheel-opencode")
    assert built.fixture_wheel.is_file()
    assert not built.fixture_wheel.is_relative_to(REPO_ROOT)
    with zipfile.ZipFile(built.fixture_wheel) as archive:
        names = archive.namelist()
    assert any(name.startswith("test_six_wheel_product/") for name in names)


def test_six_wheel_resolve_authenticates_committed_quality_and_restores_imports() -> None:
    committed = REPO_ROOT / "packages" / "capabilities" / "assurance-quality" / "assurance_quality"
    resolved = resolve_fixture("six-wheel-opencode")
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
