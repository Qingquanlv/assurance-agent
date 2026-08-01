"""Task 12: v4/v5 topology audit receipts and commit-safety resume barrier."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.core.graph_events import TopologySafetyCompatibilityRecordedEvent
from assurance_agent.workflow.driver.driver_state import (
    create_initial_driver_state,
    project_resume_compatibility,
    resume_compatibility_reason_from_error,
)
from assurance_agent.workflow.graph.compiler import compile_packaged_workflow
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog, load_execution_contracts
from assurance_agent.workflow.graph.historical_roles import discover_historical_assurance_roles
from assurance_agent.workflow.graph.models import CompiledWorkflow, GraphProjection, TaskProjection
from assurance_agent.workflow.graph.resume_compatibility import (
    LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
    TOPOLOGY_AUDIT_UNSAFE,
    TOPOLOGY_PROFILE_UNRECONSTRUCTABLE,
    TOPOLOGY_RECEIPT_CORRUPT,
    ResumeCompatibilityDecision,
    assess_remaining_work,
    audit_topology_for_resume,
    build_topology_compatibility_receipt,
    evaluate_resume_compatibility,
    event_to_receipt,
    receipt_to_event,
    verify_receipt_bindings,
)
from assurance_agent.workflow.graph.runtime import ResumeCompatibilityBarrier as RuntimeBarrier
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2, load_workflow_v2
from assurance_agent.workflow.graph.topology_semantics import (
    topology_safety_semantics_digest,
    topology_safety_semantics_object_digest,
)
from assurance_agent.workflow.graph.replay_schema import classify_pinned_layer_topology_v5
from tests.unit.workflow.graph.test_historical_roles import (
    _add_direct_codegen_bypass,
    _topology_spec,
)


def _packaged() -> tuple[WorkflowSchemaV2, ExecutionContractCatalog, CompiledWorkflow]:
    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    compiled = compile_packaged_workflow(schema, contracts)
    return schema, contracts, compiled


def _projection(
    *,
    event_schema_version: int = 5,
    params: dict[str, object] | None = None,
    tasks: dict[str, TaskProjection] | None = None,
    terminal: str | None = None,
    assurance_profile_digest: str = "profile-digest",
    graph_digest: str = "graph-digest",
) -> GraphProjection:
    return GraphProjection(
        invocation_id="root-1",
        entrypoint="full",
        checkpoint_ns="root-1",
        parent_invocation_id=None,
        parent_task_id=None,
        structural_path="main",
        graph_digest=graph_digest,
        event_schema_version=event_schema_version,
        ingest_catalog_digest="catalog-digest",
        contract_digests={"skill:aa-api-codegen": "c1"},
        assurance_profile_digest=assurance_profile_digest,
        params=params or {"test_types": ["api", "e2e"], "run_mode": "full"},
        root_tree_id="tree",
        current_tree_id="tree",
        event_seq=7,
        tasks=tasks or {},
        terminal=terminal,  # type: ignore[arg-type]
    )


def _pending_codegen_tasks(compiled) -> dict[str, TaskProjection]:
    roles, _ = discover_historical_assurance_roles(compiled.schema)
    assert roles is not None
    api = next(item for item in roles.layers if item.layer == "api")
    return {
        "t-codegen": TaskProjection(
            task_id="t-codegen",
            node_id=api.codegen_node_id,
            status="pending",
        )
    }


def _pending_report_tasks() -> dict[str, TaskProjection]:
    return {
        "t-report": TaskProjection(
            task_id="t-report",
            node_id="materialize-trace-projection",
            status="pending",
        )
    }


def test_audit_trigger_uses_discovered_roles_not_frozen_display_status() -> None:
    schema, contracts, compiled = _packaged()
    bypass = _add_direct_codegen_bypass(schema, "api")
    roles, _ = discover_historical_assurance_roles(bypass)
    assert roles is not None
    bypass_compiled = compiled.model_copy(update={"schema": bypass})

    # Frozen v4/v5 classifiers keep the known false-negative (may still say wired).
    v5 = classify_pinned_layer_topology_v5(bypass, _topology_spec("api"))
    assert (v5.semantics_id, v5.semantics_bound) == ("legacy_v5_unbound", False)
    assert v5.status in {"wired", "partial"}

    # v6 audit sees the bypass as partial.
    audit_result, per_layer, _ = audit_topology_for_resume(
        schema=bypass,
        historical_roles=roles,
        selected_layers=("api",),
        reachable_layers=("api",),
    )
    assert audit_result == "partial"
    assert per_layer["api"] == "partial"

    assessment = assess_remaining_work(
        schema=bypass,
        compiled=bypass_compiled,
        contracts=contracts,
        historical_roles=roles,
        projection=_projection(tasks=_pending_codegen_tasks(bypass_compiled)),
        selected_layers=("api",),
    )
    assert assessment.work_class == "commit_safety_bearing"
    assert any(item.startswith("reachable:codegen:api:") for item in assessment.audit_trigger_roles)


def test_safe_topology_appends_receipt_but_blocks_pending_codegen(tmp_path: Path) -> None:
    schema, contracts, compiled = _packaged()
    roles, issues = discover_historical_assurance_roles(schema)
    assert roles is not None
    assert not any(i.code == "missing_unique_role" for i in issues)

    audit_result, _, _ = audit_topology_for_resume(
        schema=schema,
        historical_roles=roles,
        selected_layers=("api", "e2e"),
        reachable_layers=("api",),
    )
    assert audit_result == "wired"

    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        event_schema_version=5,
        tasks=_pending_codegen_tasks(compiled),
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    projection = projection.model_copy(
        update={
            "contract_digests": dict(compiled.contract_digests),
            "ingest_catalog_digest": compiled.ingest_catalog_digest,
        }
    )
    change = tmp_path / "change"
    change.mkdir()
    decision, receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
        change_dir=change,
    )
    assert receipt is not None
    assert decision.allowed is False
    assert decision.reason == LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND
    assert decision.receipt_id == receipt.receipt_id
    assert decision.audit_triggered is True


def test_bypass_topology_cannot_get_bound_receipt() -> None:
    schema, contracts, compiled = _packaged()
    bypass = _add_direct_codegen_bypass(schema, "api")
    roles, _ = discover_historical_assurance_roles(bypass)
    assert roles is not None
    bypass_compiled = compiled.model_copy(update={"schema": bypass})
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        tasks=_pending_codegen_tasks(bypass_compiled),
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=bypass_compiled.digest,
    )
    decision, receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=bypass_compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
    )
    assert receipt is None
    assert decision.allowed is False
    assert decision.reason == TOPOLOGY_AUDIT_UNSAFE


def test_v4_unreconstructable_profile_blocks_before_receipt() -> None:
    schema, contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    projection = _projection(
        event_schema_version=4,
        tasks=_pending_codegen_tasks(compiled),
        assurance_profile_digest="not-the-current-profile",
        graph_digest=compiled.digest,
    )
    decision, receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=False,
    )
    assert receipt is None
    assert decision.allowed is False
    assert decision.reason == TOPOLOGY_PROFILE_UNRECONSTRUCTABLE


def test_report_terminal_only_continues_without_commit_safety_binding() -> None:
    schema, contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    # Mark all codegen nodes succeeded+committed so only report work remains.
    tasks = _pending_report_tasks()
    for layer_roles in roles.layers:
        if not layer_roles.codegen_node_id:
            continue
        tasks[f"done-{layer_roles.layer}"] = TaskProjection(
            task_id=f"done-{layer_roles.layer}",
            node_id=layer_roles.codegen_node_id,
            status="succeeded",
            outputs_committed=True,
        )
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        tasks=tasks,
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    decision, receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
    )
    assert decision.allowed is True
    assert decision.remaining_work_class == "report_terminal_only"
    assert decision.reason is None
    assert receipt is None


@pytest.mark.parametrize("event_schema_version", [4, 5])
def test_success_before_superstep_blocks_with_typed_reason(event_schema_version: int) -> None:
    schema, contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    api = next(item for item in roles.layers if item.layer == "api")
    tasks = {
        "t-success": TaskProjection(
            task_id="t-success",
            node_id=api.codegen_node_id,
            status="succeeded",
            outputs_committed=False,
            write_set_id="ws-1",
            target="skill:aa-api-codegen",
        )
    }
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        event_schema_version=event_schema_version,
        tasks=tasks,
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    decision, _receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
    )
    assert decision.allowed is False
    assert decision.reason == LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND


@pytest.mark.parametrize("event_schema_version", [4, 5])
def test_success_before_publication_effect_blocks_with_typed_reason(
    event_schema_version: int,
) -> None:
    schema, contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    api = next(item for item in roles.layers if item.layer == "api")
    tasks = {
        "t-effect": TaskProjection(
            task_id="t-effect",
            node_id=api.codegen_node_id,
            status="succeeded",
            outputs_committed=True,
            write_set_id="ws-1",
            target="skill:aa-api-codegen",
            durable_effects=({"effect_id": "eff-1", "kind": "healing_allocation/v2"},),
        )
    }
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        event_schema_version=event_schema_version,
        tasks=tasks,
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    decision, _receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
    )
    assert decision.allowed is False
    assert decision.reason == LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND


def test_receipt_exact_replay_is_idempotent(tmp_path: Path) -> None:
    schema, contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        tasks=_pending_codegen_tasks(compiled),
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    projection = projection.model_copy(
        update={
            "contract_digests": dict(compiled.contract_digests),
            "ingest_catalog_digest": compiled.ingest_catalog_digest,
        }
    )
    change = tmp_path / "change"
    change.mkdir()
    first, receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
        change_dir=change,
    )
    assert receipt is not None
    second, again = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=receipt,
        profile_reconstructable=True,
        change_dir=change,
    )
    assert again is None
    assert second.receipt_id == first.receipt_id == receipt.receipt_id
    event = receipt_to_event(receipt, checkpoint_ns=projection.checkpoint_ns)
    assert isinstance(event, TopologySafetyCompatibilityRecordedEvent)
    assert event_to_receipt(event).receipt_id == receipt.receipt_id


def test_receipt_same_identity_different_payload_is_corruption() -> None:
    schema, _contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    receipt = build_topology_compatibility_receipt(
        projection=projection,
        historical_roles=roles,
        topology_object_id=topology_safety_semantics_object_digest(),
        topology_digest=topology_safety_semantics_digest(),
        selected_layers=("api",),
        reachable_layers=("api",),
        per_layer_results={"api": "wired"},
        reachable_set_digest="reachable-1",
        source_sequence=3,
    )
    tampered = receipt.model_copy(update={"reachable_set_digest": "reachable-OTHER"})
    with pytest.raises(Exception) as excinfo:
        verify_receipt_bindings(
            tampered,
            projection=projection,
            historical_roles=roles,
            topology_object_id=topology_safety_semantics_object_digest(),
            topology_digest=topology_safety_semantics_digest(),
            reachable_set_digest="reachable-1",
        )
    assert getattr(excinfo.value, "reason_code", None) == TOPOLOGY_RECEIPT_CORRUPT


def test_receipt_from_another_root_is_corruption() -> None:
    schema, _contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    assert roles is not None
    from assurance_agent.verification.profile_manifest import assurance_profile_digest

    projection = _projection(
        assurance_profile_digest=assurance_profile_digest(),
        graph_digest=compiled.digest,
    )
    foreign = build_topology_compatibility_receipt(
        projection=projection.model_copy(update={"invocation_id": "other-root"}),
        historical_roles=roles,
        topology_object_id=topology_safety_semantics_object_digest(),
        topology_digest=topology_safety_semantics_digest(),
        selected_layers=("api",),
        reachable_layers=("api",),
        per_layer_results={"api": "wired"},
        reachable_set_digest="reachable-1",
        source_sequence=3,
    )
    with pytest.raises(Exception) as excinfo:
        verify_receipt_bindings(
            foreign,
            projection=projection,
            historical_roles=roles,
            topology_object_id=topology_safety_semantics_object_digest(),
            topology_digest=topology_safety_semantics_digest(),
            reachable_set_digest="reachable-1",
        )
    assert getattr(excinfo.value, "reason_code", None) == TOPOLOGY_RECEIPT_CORRUPT


def test_driver_state_surfaces_typed_decision_without_parsing_text() -> None:
    blocked = ResumeCompatibilityDecision(
        schema_version="1",
        allowed=False,
        reason=LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND,
        audit_triggered=True,
        requires_receipt=True,
        remaining_work_class="commit_safety_bearing",
        event_schema_version=5,
        root_invocation_id="root-1",
    )
    err = RuntimeBarrier(blocked)
    assert resume_compatibility_reason_from_error(err) == LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND
    # Must not depend on message text.
    assert resume_compatibility_reason_from_error(RuntimeError(str(err))) is None
    state = create_initial_driver_state(directory="/tmp/x")
    updated = project_resume_compatibility(state, blocked)
    assert updated.resume_compatibility_reason == LEGACY_COMMIT_SAFETY_SEMANTICS_UNBOUND
    assert updated.status == "failed"


def test_runtime_barrier_is_graph_runtime_error() -> None:
    from assurance_agent.workflow.graph.runtime import GraphRuntimeError

    assert issubclass(RuntimeBarrier, GraphRuntimeError)


def test_v6_roots_skip_legacy_audit() -> None:
    schema, contracts, compiled = _packaged()
    roles, _ = discover_historical_assurance_roles(schema)
    projection = _projection(event_schema_version=6, tasks=_pending_codegen_tasks(compiled))
    decision, receipt = evaluate_resume_compatibility(
        projection=projection,
        compiled=compiled,
        contracts=contracts,
        historical_roles=roles,
        existing_receipt=None,
        profile_reconstructable=True,
    )
    assert decision.allowed is True
    assert receipt is None
    assert decision.audit_triggered is False
