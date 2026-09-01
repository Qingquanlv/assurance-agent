from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.boot.graph_revision import GraphRevision

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.revision_registry import RevisionRegistry, RevisionRegistryError
from assurance_product.runtime_selection import (
    LangGraphRuntimeRecord,
    complete_initialized,
    write_initializing,
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


def test_registry_counts_active_invocations_by_runtime_and_revision(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    first = _revision(lock="a" * 64)
    second = _revision(lock="c" * 64)
    registry.remember(first)
    registry.remember(second)
    registry.bind("inv-lg-1", runtime="langgraph-v1", revision_id=first.revision_id)
    registry.bind("inv-lg-2", runtime="langgraph-v1", revision_id=first.revision_id)
    registry.bind("inv-legacy", runtime="legacy-v2", revision_id="d" * 64)

    counts = registry.active_counts()
    assert counts[("langgraph-v1", first.revision_id)] == 2
    assert ("langgraph-v1", second.revision_id) not in counts
    assert counts[("legacy-v2", "d" * 64)] == 1


def test_retire_refuses_while_a_resumable_invocation_exists(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    registry = RevisionRegistry(workspace)
    revision = _revision(lock="a" * 64)
    registry.remember(revision)
    registry.bind("inv-live", runtime="langgraph-v1", revision_id=revision.revision_id)

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
    registry.bind("inv-pin", runtime="langgraph-v1", revision_id=recorded.revision_id)
    write_initializing(
        workspace,
        LangGraphRuntimeRecord(
            phase="initializing",
            invocation_id="inv-pin",
            entrypoint="improvement-evaluate",
            root_input_digest="f" * 64,
            build_identity=recorded.product_lock_digest,
        ),
    )
    complete_initialized(
        workspace,
        LangGraphRuntimeRecord(
            phase="initialized",
            invocation_id="inv-pin",
            entrypoint="improvement-evaluate",
            root_input_digest="f" * 64,
            build_identity=recorded.product_lock_digest,
            identity_digest="1" * 64,
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
    registry.bind("inv-pin", runtime="langgraph-v1", revision_id=recorded.revision_id)

    imports: list[str] = []
    original_import = __import__

    def _watch(name: str, *args: object, **kwargs: object):
        imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _watch)

    import assurance_product.revision_registry as live_registry

    with pytest.raises(ValueError, match="required artifact"):
        live_registry.assert_recorded_revision(workspace, "inv-pin", current)
    assert not any("assurance_product" in name and name.endswith("whl") for name in imports)
    assert recorded.revision_id != current.revision_id
