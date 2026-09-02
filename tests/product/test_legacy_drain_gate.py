from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_engine.boot.boot import BootValidationError
from graph_engine.canonical import canonical_digest

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS
from assurance_product.revision_registry import (
    CHECKPOINT_R_RELEASED_SHA,
    DrainAuthorizationError,
    DrainEvidence,
    OperatorTerminalRecord,
    RevisionRegistry,
    authorize_legacy_deletion,
    collect_drain_evidence,
)
from assurance_product.runtime_selection import (
    LangGraphRuntimeRecord,
    LegacyRuntimeRecord,
    complete_initialized,
    load_selection,
    select_runtime,
    validate_entrypoint_runtime_cutover,
    write_initializing,
)

_GOLDEN_LOCK = (
    Path(__file__).resolve().parents[2]
    / "packages/framework/graph-engine/tests/composition/invocation-lock-v2.golden.json"
)
_ACTIVE_STATES = (
    "running",
    "blocked",
    "interrupted",
    "stopped",
    "publication-indeterminate",
)
_EVIDENCE_GATES = (
    "checkpoint_r_sha",
    "product_lock",
    "graph_revision",
    "adapter_provider_model",
    "contract_inventory",
    "raw_binding_inventory",
    "agent_occurrence_inventory",
    "join_any_rows",
    "loop_scc_anchors",
    "min_matches_mapping",
    "validator_parity",
)


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("seed\n", encoding="utf-8")
    return ChangeWorkspace.prepare(project, "CH-DRAIN-001")


def _golden_lock_bytes() -> bytes:
    return _GOLDEN_LOCK.read_text(encoding="utf-8").strip().encode()


def _write_legacy(
    workspace: ChangeWorkspace,
    invocation_id: str,
    *,
    status: str,
    lock_bytes: bytes | None = None,
    unreadable: bool = False,
) -> None:
    digest = "a" * 64
    write_initializing(
        workspace,
        LegacyRuntimeRecord(
            phase="initializing",
            invocation_id=invocation_id,
            entrypoint="archive",
            root_input_digest="b" * 64,
            build_identity=digest,
        ),
    )
    complete_initialized(
        workspace,
        LegacyRuntimeRecord(
            phase="initialized",
            invocation_id=invocation_id,
            entrypoint="archive",
            root_input_digest="b" * 64,
            build_identity=digest,
            identity_digest=digest,
        ),
    )
    RevisionRegistry(workspace).bind(invocation_id, runtime="legacy-v2", revision_id=digest)
    invocation = workspace.paths.runtime_root / "invocations" / invocation_id
    invocation.mkdir(parents=True, exist_ok=True)
    lock_path = invocation / "invocation.lock.json"
    if unreadable:
        lock_path.write_bytes(b"{not-json")
    else:
        lock_path.write_bytes(lock_bytes if lock_bytes is not None else _golden_lock_bytes())
    (invocation / "legacy-drain.json").write_text(
        json.dumps({"status": status}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _operator_record(invocation_id: str, outcome: str = "terminated") -> OperatorTerminalRecord:
    payload = {
        "schema_version": "1",
        "invocation_id": invocation_id,
        "outcome": outcome,
        "operator_id": "operator.drain",
    }
    return OperatorTerminalRecord(
        invocation_id=invocation_id,
        outcome=outcome,  # type: ignore[arg-type]
        operator_id="operator.drain",
        record_digest=canonical_digest(payload),
    )


def _green_evidence(**overrides: object) -> DrainEvidence:
    evidence = collect_drain_evidence()
    data = evidence.model_dump(mode="json")
    data.update(overrides)
    return DrainEvidence.model_validate(data)


def test_production_cutover_forbids_legacy_values() -> None:
    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    assert set(ENTRYPOINT_RUNTIME_CUTOVER.values()) == {"langgraph-v1"}
    for name in PRODUCT_ENTRYPOINTS:
        assert select_runtime(name) == "langgraph-v1"
    with pytest.raises(BootValidationError, match="langgraph-v1"):
        validate_entrypoint_runtime_cutover({name: "legacy-v2" for name in PRODUCT_ENTRYPOINTS})


@pytest.mark.parametrize("status", _ACTIVE_STATES)
def test_deletion_authorization_fails_on_active_legacy(tmp_path: Path, status: str) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, f"inv-active-{status}", status=status)
    with pytest.raises(DrainAuthorizationError, match=status.replace("-", " ") if "-" in status else status):
        authorize_legacy_deletion(workspace, evidence=_green_evidence())


def test_deletion_authorization_fails_on_unreadable_identity(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-unreadable", status="completed", unreadable=True)
    with pytest.raises(DrainAuthorizationError, match="unreadable"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence())


def test_stopped_legacy_is_resumable_without_operator_record(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-stopped", status="stopped")
    with pytest.raises(DrainAuthorizationError, match="resumable"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence())


def test_operator_terminal_record_clears_stopped_legacy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-stopped-cleared", status="stopped")
    authorize_legacy_deletion(
        workspace,
        evidence=_green_evidence(),
        operator_records=(_operator_record("inv-stopped-cleared"),),
    )


@pytest.mark.parametrize("gate", _EVIDENCE_GATES)
def test_evidence_gate_failure_blocks_zero_legacy_authorization(tmp_path: Path, gate: str) -> None:
    workspace = _workspace(tmp_path)
    broken = {
        "checkpoint_r_sha": {"candidate_sha": "deadbeef"},
        "product_lock": {"product_lock_digest": ""},
        "graph_revision": {"graph_revision_id": ""},
        "adapter_provider_model": {"adapter": "cursor", "provider": "cursor", "model": "other"},
        "contract_inventory": {"contract_count": 32},
        "raw_binding_inventory": {"binding_count": 32},
        "agent_occurrence_inventory": {"agent_occurrence_count": 33},
        "join_any_rows": {"join_any_statuses": {"missing-row": "missing"}},
        "loop_scc_anchors": {"loop_scc_anchors": (("shifted", "anchor"),)},
        "min_matches_mapping": {
            "min_matches_mapping": {
                "assurance.generation.workflow.graph.generation/fanout": "send",
                "assurance.intake.workflow.graph.case-design/prepare": "send",
                "assurance.intake.workflow.graph.case-design/repair-prepare": "composite",
            }
        },
        "validator_parity": {"validator_parity": "missing"},
    }[gate]
    with pytest.raises(DrainAuthorizationError, match="evidence"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(**broken))


def test_join_any_xfail_or_waiver_blocks_authorization(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    live = collect_drain_evidence()
    statuses = dict(live.join_any_statuses)
    first = next(iter(statuses))
    statuses[first] = "xfailed"
    with pytest.raises(DrainAuthorizationError, match="join"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(join_any_statuses=statuses))
    statuses[first] = "waived"
    with pytest.raises(DrainAuthorizationError, match="join"):
        authorize_legacy_deletion(workspace, evidence=_green_evidence(join_any_statuses=statuses))


def test_zero_legacy_with_green_evidence_authorizes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence())
    assert result.authorized is True
    assert result.active_legacy == 0
    assert CHECKPOINT_R_RELEASED_SHA.startswith("bd41e0b9") or CHECKPOINT_R_RELEASED_SHA == "bd41e0b9"


def test_completed_legacy_does_not_block_authorization(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write_legacy(workspace, "inv-done", status="completed")
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence())
    assert result.authorized is True
    assert result.active_legacy == 0


def test_legacy_marked_reopen_keeps_old_artifact_while_new_start_is_langgraph(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    leftover = LegacyRuntimeRecord(
        phase="initialized",
        invocation_id="inv-leftover",
        entrypoint="archive",
        root_input_digest="c" * 64,
        build_identity="d" * 64,
        identity_digest="d" * 64,
    )
    write_initializing(
        workspace,
        LegacyRuntimeRecord(
            phase="initializing",
            invocation_id=leftover.invocation_id,
            entrypoint=leftover.entrypoint,
            root_input_digest=leftover.root_input_digest,
            build_identity=leftover.build_identity,
        ),
    )
    complete_initialized(workspace, leftover)
    existing = load_selection(workspace, leftover.invocation_id)
    assert existing is not None
    assert existing.runtime == "legacy-v2"
    assert select_runtime("archive") == "langgraph-v1"
    write_initializing(
        workspace,
        LangGraphRuntimeRecord(
            phase="initializing",
            invocation_id="inv-new-same-entrypoint",
            entrypoint="archive",
            root_input_digest="e" * 64,
            build_identity="f" * 64,
        ),
    )
    newest = load_selection(workspace, "inv-new-same-entrypoint")
    assert newest is not None
    assert newest.runtime == "langgraph-v1"
    assert load_selection(workspace, leftover.invocation_id) == existing


def test_legacy_drain_does_not_retire_pre_closure_langgraph_revision(tmp_path: Path) -> None:
    from graph_engine.boot.graph_revision import GraphRevision

    workspace = _workspace(tmp_path)
    revision = GraphRevision.build(
        product_lock_digest="a" * 64,
        wheel_source_digests={"assurance-product": "b" * 64},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={"product": "1"},
        langgraph_version="0.0.0",
        checkpoint_contract_version="1",
    )
    registry = RevisionRegistry(workspace)
    registry.remember(revision)
    registry.bind("inv-lg-live", runtime="langgraph-v1", revision_id=revision.revision_id)
    result = authorize_legacy_deletion(workspace, evidence=_green_evidence())
    assert result.authorized is True
    assert result.active_legacy == 0
    with pytest.raises(Exception, match="resumable Invocation"):
        registry.retire(revision.revision_id)
    assert registry.get(revision.revision_id).revision_id == revision.revision_id
