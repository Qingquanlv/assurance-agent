"""Assemble the four-layer capability/policy replay matrix from frozen graph binding."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.models.assurance import CASE_TYPES, LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.eval.specialty_models import (
    CapabilitiesSummary,
    CapabilityPolicyReplayV2,
    CheckSummary,
    CompleteLayerRow,
    DefinitionBinding,
    EvidenceDigests,
    IncompleteLayerRow,
    LayerRow,
    MechanicalAggregate,
    NotSelectedLayerRow,
    NotWiredLayerRow,
    ReplayScenario,
    build_capability_replay_v2,
)
from assurance_agent.knowledge.capabilities import compute_missing_capabilities
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.replay_binding import (
    FrozenDefinitionBinding,
    ReplayBindingError,
    assert_layer_selection_evidence,
    bind_replay_definitions,
    evaluate_layer_selection,
    recover_layer_inputs,
)
from assurance_agent.workflow.graph.workspace import TreeStore
from assurance_agent.workflow.orchestration.plan_check_replay import replay_plan_check_policy


def collect_capability_policy_replay(
    *,
    change_dir: Path,
    change_id: str,
    root_invocation_id: str,
    expected_entrypoint: str,
    store: TreeStore | None = None,
) -> CapabilityPolicyReplayV2:
    """Combine graph binding, pinned topology classification, and counterfactual policy replay."""
    try:
        binding = bind_replay_definitions(
            change_dir=change_dir,
            change_id=change_id,
            root_invocation_id=root_invocation_id,
            expected_entrypoint=expected_entrypoint,
            store=store,
        )
    except ReplayBindingError as exc:
        return _incomplete_definition_failure(str(exc.reason_code))

    selections = evaluate_layer_selection(binding.compiled.schema, binding.assurance_params)
    assert_layer_selection_evidence(
        binding.sequenced_events,
        assurance_invocation_id=binding.assurance_invocation_id,
        selections=selections,
    )
    rows = [
        _row_for_layer(binding, layer=layer, case_type=case_type, change_dir=change_dir, store=store)
        for layer, case_type in zip(LAYER_NAMES, CASE_TYPES, strict=True)
    ]

    return build_capability_replay_v2(
        definition_binding=DefinitionBinding(
            root_invocation_id=binding.root_invocation_id,
            assurance_invocation_id=binding.assurance_invocation_id,
            graph_digest=binding.graph_digest,
            gate_definition_source=binding.gate_definition_source,
            baseline_policy_digest=binding.baseline_policy_digest,
            policy_source=binding.policy_source,
            policy_origin=binding.policy_origin,
            gate_semantics_digest=binding.gate_semantics_digest,
            assurance_profile_digest=binding.assurance_profile_digest,
        ),
        rows=rows,
    )


def _row_for_layer(
    binding: FrozenDefinitionBinding,
    *,
    layer: str,
    case_type: str,
    change_dir: Path,
    store: TreeStore | None,
) -> LayerRow:
    if layer not in binding.selected_layers:
        return NotSelectedLayerRow(layer=layer, case_type=case_type, status="not_selected")
    topology = binding.layer_topologies[layer]
    if topology.status == "legacy_unwired":
        return NotWiredLayerRow(layer=layer, case_type=case_type, status="not_wired")
    if topology.status == "partial":
        return IncompleteLayerRow(
            layer=layer,
            case_type=case_type,
            status="incomplete",
            reason_code="partial_assurance_wiring",
        )
    try:
        return replay_wired_layer(
            binding, layer=layer, case_type=case_type, change_dir=change_dir, store=store
        )
    except ReplayBindingError as exc:
        return IncompleteLayerRow(
            layer=layer,
            case_type=case_type,
            status="incomplete",
            reason_code=str(exc.reason_code),
        )


def _incomplete_definition_failure(reason_code: str) -> CapabilityPolicyReplayV2:
    rows = [
        IncompleteLayerRow(
            layer=layer,
            case_type=case_type,
            status="incomplete",
            reason_code=reason_code,
        )
        for layer, case_type in zip(LAYER_NAMES, CASE_TYPES, strict=True)
    ]
    return build_capability_replay_v2(
        definition_binding=None,
        definition_failure=reason_code,
        rows=rows,
    )


def replay_wired_layer(
    binding: FrozenDefinitionBinding,
    *,
    layer: str,
    case_type: str,
    change_dir: Path,
    store: TreeStore | None,
) -> CompleteLayerRow:
    """Recover wired-layer evidence and emit a complete counterfactual row."""
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    recovered = recover_layer_inputs(binding, layer=layer, change_dir=change_dir, store=store)
    checks_doc = recovered.checks.model
    applicable = checks_doc.applicability is not None and bool(checks_doc.applicability.applicable)
    replay = replay_plan_check_policy(
        gates=binding.compiled.schema.gates,
        profile=profile,
        review=recovered.review,
        checks=recovered.checks,
        data_knowledge=recovered.data_knowledge,
        base_policy=binding.policy,
        change_id=binding.change_id,
        params=recovered.params,
    )
    capabilities = None
    review_digest = None
    l1_digest = None
    if applicable:
        review_payload = (
            recovered.review.model.model_dump(mode="json") if recovered.review is not None else {}
        )
        l1_payload = (
            recovered.data_knowledge.model.model_dump(mode="json")
            if recovered.data_knowledge is not None
            else {}
        )
        capabilities = CapabilitiesSummary(
            required=sorted(str(item) for item in (review_payload.get("required_capabilities") or [])),
            missing=sorted(compute_missing_capabilities(review_payload, l1_payload)),
        )
        review_digest = recovered.review.sha256 if recovered.review is not None else None
        l1_digest = recovered.data_knowledge.sha256 if recovered.data_knowledge is not None else None

    check_summaries = _summarize_checks(checks_doc.checks)
    finding_count = sum(item.finding_count for item in check_summaries)
    return CompleteLayerRow(
        layer=layer,
        case_type=case_type,
        status="complete",
        applicability="applicable" if applicable else "not_applicable",
        gate_id=profile.gate_id,
        review_artifact=profile.review_artifact,
        checks_artifact=profile.checks_artifact,
        capabilities=capabilities,
        mechanical_checks=MechanicalAggregate(
            status=checks_doc.status,
            finding_count=finding_count,
            checks=tuple(check_summaries),
        ),
        mechanical_execution_contract_digest=recovered.mechanical_execution_contract_digest,
        evidence_digests=EvidenceDigests(
            review=review_digest,
            checks=recovered.checks.sha256,
            data_knowledge=l1_digest,
        ),
        scenarios=tuple(_scenario_row(item) for item in replay.scenarios),
    )


def _summarize_checks(checks) -> list[CheckSummary]:
    by_id = {item.check_id: item for item in checks}
    summaries: list[CheckSummary] = []
    for check_id in PLAN_CHECK_IDS:
        item = by_id.get(check_id)
        if item is None:
            summaries.append(CheckSummary(check_id=check_id, status="not_applicable", finding_count=0))
            continue
        summaries.append(
            CheckSummary(
                check_id=item.check_id,
                status=item.status,
                finding_count=len(item.findings),
            )
        )
    return summaries


def _scenario_row(scenario) -> ReplayScenario:
    return ReplayScenario(
        action=scenario.action,
        policy_digest=scenario.policy_digest,
        verdict=scenario.verdict,
        route=scenario.route,
        matched_rule=scenario.matched_rule,
        reason=scenario.reason,
        missing_capabilities=list(scenario.missing_capabilities),
        policy_effect=scenario.policy_effect,
    )


__all__ = ["collect_capability_policy_replay", "replay_wired_layer"]
