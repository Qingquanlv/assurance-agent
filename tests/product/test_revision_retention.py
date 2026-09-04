from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from graph_engine.boot.graph_revision import GraphRevision

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.invocation_identity import (
    InvocationIdentityRecord,
    complete_initialized,
    write_initializing,
)
from assurance_product.revision_registry import (
    RevisionRegistry,
    RevisionRegistryError,
)


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("seed\n", encoding="utf-8")
    return ChangeWorkspace.prepare(project, "CH-RET-001")


def _revision(*, lock: str, wheel: str = "b" * 64) -> GraphRevision:
    return GraphRevision.build(
        product_lock_digest=lock,
        wheel_source_digests={"assurance-product": wheel},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={"product": "1"},
        langgraph_version="0.0.0",
        checkpoint_contract_version="1",
    )


def _identity(invocation_id: str, *, phase: str, lock: str, revision_id: str) -> InvocationIdentityRecord:
    return InvocationIdentityRecord(
        schema_version="1",
        phase=phase,  # type: ignore[arg-type]
        invocation_id=invocation_id,
        entrypoint="improvement-evaluate",
        root_input_digest="f" * 64,
        product_lock_digest=lock,
        revision_id=revision_id,
    )


def test_registry_counts_active_invocations_by_revision(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    first = _revision(lock="a" * 64)
    second = _revision(lock="c" * 64)
    registry.remember(first)
    registry.remember(second)
    registry.bind("inv-lg-1", first.revision_id)
    registry.bind("inv-lg-2", first.revision_id)

    counts = registry.active_counts()
    assert counts[first.revision_id] == 2
    assert second.revision_id not in counts
    assert registry.revision_for("inv-lg-1") == first.revision_id


def _binding_path(workspace: ChangeWorkspace, invocation_id: str) -> Path:
    return workspace.paths.langgraph_leases / "revisions" / "bindings" / f"{invocation_id}.json"


def test_revision_binding_payload_has_no_runtime_discriminator(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    revision = _revision(lock="a" * 64)
    registry.remember(revision)
    registry.bind("inv-bind", revision.revision_id)
    payload = _binding_path(workspace, "inv-bind").read_text()
    assert "runtime" not in payload
    assert '"invocation_id"' in payload
    assert '"revision_id"' in payload


def test_revision_for_and_active_counts_reject_extra_fields(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    revision = _revision(lock="a" * 64)
    registry.remember(revision)
    registry.bind("inv-extra", revision.revision_id)
    path = _binding_path(workspace, "inv-extra")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runtime"] = "legacy-v2"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(RevisionRegistryError):
        registry.revision_for("inv-extra")
    with pytest.raises(RevisionRegistryError):
        registry.active_counts()


def test_revision_for_rejects_non_canonical_bytes_and_invocation_id_mismatch(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    revision = _revision(lock="a" * 64)
    registry.remember(revision)
    registry.bind("inv-canon", revision.revision_id)
    path = _binding_path(workspace, "inv-canon")
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RevisionRegistryError):
        registry.revision_for("inv-canon")

    registry.bind("inv-mismatch", revision.revision_id)
    mismatch = _binding_path(workspace, "inv-mismatch")
    mismatch.write_text(
        json.dumps({"invocation_id": "inv-other", "revision_id": revision.revision_id}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RevisionRegistryError):
        registry.revision_for("inv-mismatch")
    with pytest.raises(RevisionRegistryError):
        registry.active_counts()


def test_retire_refuses_while_a_resumable_invocation_exists(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    revision = _revision(lock="a" * 64)
    registry.remember(revision)
    registry.bind("inv-live", revision.revision_id)

    with pytest.raises(RevisionRegistryError, match="resumable Invocation"):
        registry.retire(revision.revision_id)
    assert registry.get(revision.revision_id).revision_id == revision.revision_id


def test_retire_succeeds_only_when_no_resumable_invocation_holds_revision(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    revision = _revision(lock="a" * 64)
    registry.remember(revision)
    registry.retire(revision.revision_id)
    with pytest.raises(RevisionRegistryError, match="missing"):
        registry.get(revision.revision_id)


def test_revision_mismatch_reports_required_artifact_before_checkpoint_or_kernel(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _workspace(tmp_path)
    recorded = _revision(lock="a" * 64, wheel="b" * 64)
    current = _revision(lock="c" * 64, wheel="e" * 64)
    registry = RevisionRegistry(workspace)
    registry.remember(recorded)
    registry.bind("inv-pin", recorded.revision_id)
    write_initializing(
        workspace,
        _identity(
            "inv-pin",
            phase="initializing",
            lock=recorded.product_lock_digest,
            revision_id=recorded.revision_id,
        ),
    )
    complete_initialized(
        workspace,
        _identity(
            "inv-pin",
            phase="initialized",
            lock=recorded.product_lock_digest,
            revision_id=recorded.revision_id,
        ),
    )

    opened = {"checkpoint": False, "kernel": False}

    def _forbid_open(*_args: object, **_kwargs: object) -> None:
        opened["checkpoint"] = True
        opened["kernel"] = True
        raise AssertionError("checkpoint or Kernel opened before revision check")

    monkeypatch.setattr("assurance_product.runtime_ports.ProductRuntimePorts.open", _forbid_open)

    import assurance_product.revision_registry as live_registry

    with pytest.raises(ValueError, match="required artifact") as exc_info:
        live_registry.assert_recorded_revision(workspace, "inv-pin", current)
    message = str(exc_info.value)
    assert recorded.revision_id in message
    assert recorded.product_lock_digest in message
    assert opened == {"checkpoint": False, "kernel": False}


def test_same_process_does_not_import_a_second_wheel_version_on_mismatch(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    recorded = _revision(lock="a" * 64)
    current = _revision(lock="c" * 64)
    registry = RevisionRegistry(workspace)
    registry.remember(recorded)
    registry.bind("inv-pin", recorded.revision_id)

    imports: list[str] = []
    original_import = __import__

    def _watch(name: str, *args: Any, **kwargs: Any):
        imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _watch)

    import assurance_product.revision_registry as live_registry

    with pytest.raises(ValueError, match="required artifact"):
        live_registry.assert_recorded_revision(workspace, "inv-pin", current)
    assert not any("assurance_product" in name and name.endswith("whl") for name in imports)
    assert recorded.revision_id != current.revision_id


def test_resume_file_asserts_revision_before_opening_ports(tmp_path: Path, monkeypatch) -> None:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.revision_registry import assert_recorded_revision as live_assert

    workspace = _workspace(tmp_path)
    recorded = _revision(lock="a" * 64)
    record = _identity(
        "inv-resume-pin",
        phase="initialized",
        lock=recorded.product_lock_digest,
        revision_id=recorded.revision_id,
    )
    write_initializing(
        workspace,
        _identity(
            record.invocation_id,
            phase="initializing",
            lock=record.product_lock_digest,
            revision_id=record.revision_id,
        ),
    )
    complete_initialized(workspace, record)
    RevisionRegistry(workspace).remember(recorded)
    RevisionRegistry(workspace).bind(record.invocation_id, recorded.revision_id)

    current = _revision(lock="c" * 64)
    order: list[str] = []

    def _forbid_open(*_args: object, **_kwargs: object):
        order.append("open")
        raise AssertionError("checkpoint or Kernel opened before revision check")

    def _assert(*_args: Any, **_kwargs: Any):
        order.append("assert")
        return live_assert(*_args, **_kwargs)

    monkeypatch.setattr("assurance_product.application.assert_recorded_revision", _assert)
    monkeypatch.setattr("assurance_product.application.ProductRuntimePorts.open", _forbid_open)
    monkeypatch.setattr(
        "assurance_product.application.product_lock_from_composition",
        lambda _composition: type("Lock", (), {"digest": current.product_lock_digest})(),
    )
    monkeypatch.setattr(
        "assurance_product.application.product_graph_manifest",
        lambda *_args, **_kwargs: type("Manifest", (), {"revision": current})(),
    )
    monkeypatch.setattr(
        AssuranceProductApplication,
        "_resolve_existing",
        lambda *_args, **_kwargs: record,
    )
    resume_file = tmp_path / "resume.json"
    resume_file.write_text('{"action": "approve", "reason": "accepted"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="required artifact") as exc_info:
        AssuranceProductApplication().resume(
            workspace=workspace,
            composition=object(),
            authorization=object(),  # type: ignore[arg-type]
            invocation_id=record.invocation_id,
            action=None,
            reason=None,
            resume_file=resume_file,
        )
    assert recorded.revision_id in str(exc_info.value)
    assert order == ["assert"]


def test_reopen_bind_keeps_recorded_lock_instead_of_current_composition(tmp_path: Path, monkeypatch) -> None:
    from assurance_product.application import _bind_revision

    workspace = _workspace(tmp_path)
    recorded = _revision(lock="a" * 64)
    current = _revision(lock="c" * 64)
    write_initializing(
        workspace,
        _identity(
            "inv-reopen-pin",
            phase="initializing",
            lock=recorded.product_lock_digest,
            revision_id=recorded.revision_id,
        ),
    )
    opened = {"checkpoint": False}

    def _forbid_open(*_args: object, **_kwargs: object):
        opened["checkpoint"] = True
        raise AssertionError("checkpoint or Kernel opened before revision check")

    monkeypatch.setattr("assurance_product.application.ProductRuntimePorts.open", _forbid_open)
    monkeypatch.setattr(
        "assurance_product.application.product_graph_manifest",
        lambda *_args, **_kwargs: type("Manifest", (), {"revision": current})(),
    )

    with pytest.raises(ValueError, match="required artifact") as exc_info:
        _bind_revision(
            workspace,
            invocation_id="inv-reopen-pin",
            composition=object(),
            product_lock=object(),  # type: ignore[arg-type]
            product_lock_digest=recorded.product_lock_digest,
        )
    assert recorded.product_lock_digest in str(exc_info.value)
    assert opened == {"checkpoint": False}
    with pytest.raises(ValueError, match="missing"):
        RevisionRegistry(workspace).revision_for("inv-reopen-pin")
