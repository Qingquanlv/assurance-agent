from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.invocation_identity import (
    InvocationIdentityRecord,
    complete_initialized,
    identity_path,
    load_identity,
    write_initializing,
)
from graph_engine.canonical import canonical_json_bytes


def valid_identity_document() -> dict[str, str]:
    return {
        "schema_version": "1",
        "phase": "initialized",
        "invocation_id": "inv-1",
        "entrypoint": "intake",
        "root_input_digest": "a" * 64,
        "product_lock_digest": "b" * 64,
        "revision_id": "c" * 64,
    }


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("seed\n", encoding="utf-8")
    return ChangeWorkspace.prepare(project, "CH-ID-001")


def _record(**overrides: str) -> InvocationIdentityRecord:
    payload = valid_identity_document()
    payload.update(overrides)
    return InvocationIdentityRecord.model_validate(payload)


def test_identity_has_no_runtime_discriminator() -> None:
    record = InvocationIdentityRecord(
        schema_version="1",
        phase="initialized",
        invocation_id="inv-1",
        entrypoint="intake",
        root_input_digest="a" * 64,
        product_lock_digest="b" * 64,
        revision_id="c" * 64,
    )
    assert "runtime" not in record.model_dump(mode="json")


@pytest.mark.parametrize("runtime", ["legacy-v2", "langgraph-v1"])
def test_identity_rejects_runtime_field(runtime: str) -> None:
    with pytest.raises(ValidationError):
        InvocationIdentityRecord.model_validate({**valid_identity_document(), "runtime": runtime})


def test_identity_serializes_exactly_seven_fields() -> None:
    record = _record()
    dumped = record.model_dump(mode="json")
    assert dumped == valid_identity_document()
    assert set(dumped) == {
        "schema_version",
        "phase",
        "invocation_id",
        "entrypoint",
        "root_input_digest",
        "product_lock_digest",
        "revision_id",
    }


def test_identity_rejects_unknown_fields_and_missing_version() -> None:
    with pytest.raises(ValidationError):
        InvocationIdentityRecord.model_validate({**valid_identity_document(), "identity_digest": "d" * 64})
    missing = dict(valid_identity_document())
    missing.pop("schema_version")
    with pytest.raises(ValidationError):
        InvocationIdentityRecord.model_validate(missing)


def test_identity_path_uses_identities_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = identity_path(workspace, "inv-1")
    assert path == workspace.paths.langgraph_identities / "inv-1.json"
    assert path.parent.name == "identities"
    assert not hasattr(workspace.paths, "langgraph_selections")


def test_write_and_load_round_trip_uses_canonical_bytes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    initializing = _record(phase="initializing")
    written = write_initializing(workspace, initializing)
    assert written.phase == "initializing"
    loaded = load_identity(workspace, "inv-1")
    assert loaded == initializing
    encoded = workspace.paths.langgraph_identities.joinpath("inv-1.json").read_bytes()
    assert encoded == canonical_json_bytes(initializing.model_dump(mode="json")) + b"\n"


def test_load_identity_rejects_symlink_and_non_canonical_bytes(tmp_path: Path) -> None:
    from assurance_product.invocation_identity import RuntimeSelectionError

    workspace = _workspace(tmp_path)
    write_initializing(workspace, _record(phase="initializing"))
    path = identity_path(workspace, "inv-1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeSelectionError):
        load_identity(workspace, "inv-1")

    backup = canonical_json_bytes(_record(phase="initializing").model_dump(mode="json")) + b"\n"
    path.unlink()
    target = path.with_name("inv-1.real.json")
    target.write_bytes(backup)
    path.symlink_to(target)
    with pytest.raises(RuntimeSelectionError):
        load_identity(workspace, "inv-1")


def test_identity_drift_and_phase_regression_fail_closed(tmp_path: Path) -> None:
    from assurance_product.invocation_identity import RuntimeSelectionError

    workspace = _workspace(tmp_path)
    write_initializing(workspace, _record(phase="initializing"))
    with pytest.raises(RuntimeSelectionError):
        write_initializing(workspace, _record(phase="initializing", entrypoint="archive"))
    with pytest.raises(RuntimeSelectionError):
        complete_initialized(workspace, _record(phase="initialized", revision_id="d" * 64))
    completed = complete_initialized(workspace, _record(phase="initialized"))
    assert completed.phase == "initialized"
    with pytest.raises(RuntimeSelectionError):
        write_initializing(workspace, _record(phase="initializing"))
    assert load_identity(workspace, "inv-1") == completed


def test_complete_initialized_is_idempotent_for_matching_record(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    write_initializing(workspace, _record(phase="initializing"))
    first = complete_initialized(workspace, _record(phase="initialized"))
    second = complete_initialized(workspace, _record(phase="initialized"))
    assert first == second
    assert load_identity(workspace, "inv-1") == first


def test_pending_file_is_replaced_atomically(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    write_initializing(workspace, _record(phase="initializing"))
    path = identity_path(workspace, "inv-1")
    assert not path.with_name(f".{path.name}.pending").exists()
    complete_initialized(workspace, _record(phase="initialized"))
    assert not path.with_name(f".{path.name}.pending").exists()
    assert load_identity(workspace, "inv-1") == _record(phase="initialized")
