"""Change-local API materialization into temporary workspace (Phase 1, Task C)."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.discovery import GeneratedFileEntry, GeneratedManifest
from assurance_agent.verification.manifest import ManifestValidationError, digest_file
from assurance_agent.workflow.discovery.materialize import (
    MaterializationError,
    destroy_workspace,
    materialize_round,
    materialize_test_overlay,
    validate_generated_manifest_op,
)


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    path.write_bytes(data)
    return sha256_bytes(data)


def _product_root(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    _write(root / "app" / "main.py", "print('sut')\n")
    _write(root / "tests" / "api" / "test_smoke.py", "def test_smoke():\n    assert True\n")
    return root


def _round_dir(
    tmp_path: Path,
    *,
    bodies: dict[str, str] | None = None,
) -> tuple[Path, GeneratedManifest]:
    """Build round tree + manifest.

    ``bodies`` maps target basename -> source body. Default one selected file.
    """
    if bodies is None:
        bodies = {"test_tenant.py": "def test_tenant():\n    assert True\n"}

    round_dir = tmp_path / "change" / "discovery" / "rounds" / "R0001"
    files: list[GeneratedFileEntry] = []
    selection: list[str] = []
    for name, body in bodies.items():
        source = f"generated/tests/api/{name}"
        target = f"tests/api/{name}"
        digest = _write(round_dir / Path(source), body)
        files.append(
            GeneratedFileEntry(
                source=source,
                target=target,
                sha256=digest,
                role="search_test",
            )
        )
        selection.append(target)

    # Default: only first file in execution_selection (subset test support).
    manifest = GeneratedManifest(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        parent_round_ids=(),
        base_revision="deadbeef",
        strategy_ids=("api.auth.tenant-boundary",),
        files=tuple(files),
        execution_selection=(selection[0],),
        oracle_refs=("ORACLE-auth-tenant-isolation",),
        seed=12345,
    )
    return round_dir, manifest


def test_happy_path_materialize_selected_api_test(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(tmp_path)
    result = materialize_round(
        project_root=product,
        round_dir=round_dir,
        manifest=manifest,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-1",
    )
    try:
        materialized = result.workspace / "tests" / "api" / "test_tenant.py"
        assert materialized.is_file()
        assert digest_file(materialized) == manifest.files[0].sha256
        assert any(m.target == "tests/api/test_tenant.py" for m in result.receipt.materialized)
        # Product smoke test was copied into workspace
        assert (result.workspace / "tests" / "api" / "test_smoke.py").is_file()
        assert (result.workspace / "app" / "main.py").is_file()
    finally:
        destroy_workspace(result.workspace)


def test_materialize_selection_only_skips_unselected(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(
        tmp_path,
        bodies={
            "test_selected.py": "def test_selected():\n    assert True\n",
            "test_other.py": "def test_other():\n    assert True\n",
        },
    )
    # execution_selection is only the first target
    assert manifest.execution_selection == ("tests/api/test_selected.py",)
    result = materialize_round(
        project_root=product,
        round_dir=round_dir,
        manifest=manifest,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-sel",
        selection_only=True,
    )
    try:
        assert (result.workspace / "tests" / "api" / "test_selected.py").is_file()
        assert not (result.workspace / "tests" / "api" / "test_other.py").exists()
        assert [m.target for m in result.receipt.materialized] == ["tests/api/test_selected.py"]
    finally:
        destroy_workspace(result.workspace)


def test_reject_path_traversal_via_validation(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, _ = _round_dir(tmp_path)
    # Symlink escape under generated/
    outside = tmp_path / "escape.py"
    outside.write_text("bad\n")
    link = round_dir / "generated" / "tests" / "api" / "test_tenant.py"
    link.unlink()
    link.symlink_to(outside)
    digest = digest_file(outside)
    manifest = GeneratedManifest(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        base_revision="deadbeef",
        strategy_ids=("api.auth.tenant-boundary",),
        files=(
            GeneratedFileEntry(
                source="generated/tests/api/test_tenant.py",
                target="tests/api/test_tenant.py",
                sha256=digest,
                role="search_test",
            ),
        ),
        execution_selection=("tests/api/test_tenant.py",),
        seed=1,
    )
    with pytest.raises(ManifestValidationError) as exc:
        materialize_round(
            project_root=product,
            round_dir=round_dir,
            manifest=manifest,
            temp_factory=lambda: tmp_path / "workspaces" / "ws-bad",
        )
    assert exc.value.code == "symlink_escape"


def test_reject_extra_unmanifested_file(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(tmp_path)
    _write(round_dir / "generated" / "tests" / "api" / "orphan.py", "orphan\n")
    with pytest.raises(ManifestValidationError) as exc:
        materialize_round(
            project_root=product,
            round_dir=round_dir,
            manifest=manifest,
            temp_factory=lambda: tmp_path / "workspaces" / "ws-extra",
        )
    assert exc.value.code == "extra_unmanifested_file"


def test_reject_digest_mismatch_after_copy(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(tmp_path)
    # Tamper source after building a matching manifest — validate fails before copy.
    source = round_dir / "generated" / "tests" / "api" / "test_tenant.py"
    source.write_text("tampered\n")
    with pytest.raises(ManifestValidationError) as exc:
        materialize_round(
            project_root=product,
            round_dir=round_dir,
            manifest=manifest,
            temp_factory=lambda: tmp_path / "workspaces" / "ws-digest",
        )
    assert exc.value.code == "digest_mismatch"


def test_refuse_writing_to_project_root_tests(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(tmp_path)
    product_tenant = product / "tests" / "api" / "test_tenant.py"
    assert not product_tenant.exists()
    before = (product / "tests" / "api" / "test_smoke.py").read_bytes()

    result = materialize_round(
        project_root=product,
        round_dir=round_dir,
        manifest=manifest,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-iso",
    )
    try:
        assert not product_tenant.exists()
        assert (product / "tests" / "api" / "test_smoke.py").read_bytes() == before
        assert result.workspace.resolve() != product.resolve()
        assert product.resolve() not in result.workspace.resolve().parents
        # Workspace is not inside product either when temp_factory is sibling
        assert not str(result.workspace.resolve()).startswith(str(product.resolve()) + "/")
    finally:
        destroy_workspace(result.workspace)


def test_temp_destroyed_change_discovery_persists(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(tmp_path)
    source = round_dir / "generated" / "tests" / "api" / "test_tenant.py"
    assert source.is_file()
    source_bytes = source.read_bytes()

    result = materialize_round(
        project_root=product,
        round_dir=round_dir,
        manifest=manifest,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-tmp",
    )
    workspace = result.workspace
    assert workspace.is_dir()
    destroy_workspace(workspace)
    assert not workspace.exists()
    assert source.is_file()
    assert source.read_bytes() == source_bytes


def test_reject_canonical_target_collision(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    # Materializing onto an existing product test path is forbidden for search_test.
    round_dir = tmp_path / "change" / "discovery" / "rounds" / "R0001"
    digest = _write(
        round_dir / "generated" / "tests" / "api" / "test_smoke.py",
        "def test_smoke():\n    assert False\n",
    )
    manifest = GeneratedManifest(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        base_revision="deadbeef",
        strategy_ids=("api.auth.tenant-boundary",),
        files=(
            GeneratedFileEntry(
                source="generated/tests/api/test_smoke.py",
                target="tests/api/test_smoke.py",
                sha256=digest,
                role="search_test",
            ),
        ),
        execution_selection=("tests/api/test_smoke.py",),
        seed=1,
    )
    with pytest.raises(MaterializationError) as exc:
        materialize_round(
            project_root=product,
            round_dir=round_dir,
            manifest=manifest,
            temp_factory=lambda: tmp_path / "workspaces" / "ws-collide",
        )
    assert exc.value.code == "canonical_target_exists"


def test_thin_op_wrappers(tmp_path: Path) -> None:
    product = _product_root(tmp_path)
    round_dir, manifest = _round_dir(tmp_path)
    ok = validate_generated_manifest_op(round_dir, manifest)
    assert ok.file_digests
    result = materialize_test_overlay(
        project_root=product,
        round_dir=round_dir,
        manifest=manifest,
        temp_factory=lambda: tmp_path / "workspaces" / "ws-op",
    )
    try:
        assert result.receipt.seed == 12345
        assert result.receipt.environment_digest.startswith("sha256:")
    finally:
        destroy_workspace(result.workspace)
