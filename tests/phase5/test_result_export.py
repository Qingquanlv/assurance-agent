from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_export_contains_authenticated_projection_and_result_tree(completed_invocation, tmp_path):
    from assurance_product.export import digest_directory, export_invocation

    exported = export_invocation(
        completed_invocation.engine,
        completed_invocation.id,
        tmp_path / "export",
        authorization=completed_invocation.authorization,
    )
    assert exported.lock_digest == completed_invocation.lock_digest
    assert exported.status.status == "completed"
    assert exported.result_tree_digest == digest_directory(tmp_path / "export/result-tree")
    assert (tmp_path / "export/manifest.json").exists()


def test_repeated_export_is_byte_identical(completed_invocation, tmp_path):
    from assurance_product.export import digest_directory, export_invocation

    export_invocation(
        completed_invocation.engine,
        completed_invocation.id,
        tmp_path / "a",
        authorization=completed_invocation.authorization,
    )
    export_invocation(
        completed_invocation.engine,
        completed_invocation.id,
        tmp_path / "b",
        authorization=completed_invocation.authorization,
    )
    assert digest_directory(tmp_path / "a") == digest_directory(tmp_path / "b")


def test_result_export_schema_version_is_string_one(completed_invocation, tmp_path: Path):
    from assurance_product.export import export_invocation
    from assurance_product.models import ResultExportV1

    exported = export_invocation(
        completed_invocation.engine,
        completed_invocation.id,
        tmp_path / "export-schema",
        authorization=completed_invocation.authorization,
    )
    assert exported.schema_version == "1"
    assert ResultExportV1.model_validate(exported.model_dump(mode="json")).schema_version == "1"
    assert (tmp_path / "export-schema" / "status.json").is_file()
    assert (tmp_path / "export-schema" / "artifact-index.json").is_file()


def test_running_invocation_refuses_result_tree_export(
    tmp_path: Path, installed_sources, monkeypatch: pytest.MonkeyPatch
):
    from assurance_product.export import ResultExportError, export_invocation
    from tests.phase5.cli_support import SECRET_ENV, SECRET_VALUE, start_lifecycle_invocation

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    started = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-export-running",
        drive=False,
    )
    try:
        with pytest.raises(ResultExportError, match="running"):
            export_invocation(
                started.engine,
                started.id,
                tmp_path / "running-export",
                authorization=started.authorization,
            )
        assert not (tmp_path / "running-export").exists()
    finally:
        started.engine.close()
