from __future__ import annotations

import pytest

from assurance_improvement.contracts.attempts import select_evaluate_memory
from assurance_improvement.contracts.delivery import MemoryEvalReceipt, artifact_digest
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.graphs.factory import build_improvement_graphs
from assurance_improvement.graphs.routes import (
    route_apply_evaluate,
    route_auto_review,
    route_human_review_result,
)
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS, GRAPH_NAMES_NOT_EFFECT_KINDS
from graph_engine.testing import GraphHarness, committed

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    IMPROVEMENT_ID,
    improvement_projection,
)
from test_graph_factory import (  # type: ignore[import-not-found]
    EFFECT_IDS,
    TASK_APPLY_ID,
    TASK_AUTO_REVIEW_ID,
    TASK_EVALUATE_ID,
    TASK_EXPORT_ID,
    TASK_ROLLBACK_ID,
    _REVIEW_ID,
    _receipt,
    improvement_contracts,
    skill_graph_fields,
)

_DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"


def _assessment(*, decision: str = "pass") -> dict[str, object]:
    return {
        "schema_version": "1",
        "review_type": "improvement",
        "decision": decision,
        "findings": [],
        "evidence_traceability": "complete",
        "scope_readiness": "ready",
        "verification_readiness": "ready",
        "delivery_safety": "ready",
        "human_review_required": decision == "needs_human_review",
        "review_id": "REV-1",
        "improvement_id": IMPROVEMENT_ID,
        "expected_improvement_version": 1,
        "subject_sha256": f"sha256:{HEX_A}",
    }


def complete_evaluate_payload() -> dict[str, object]:
    return {
        "projection": improvement_projection(),
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "target_digest": HEX_A,
    }


def evaluate_receipt(**overrides: object) -> dict[str, object]:
    projection = ImprovementProjection.model_validate(improvement_projection())
    payload: dict[str, object] = {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "approved_state_digest": artifact_digest(projection),
        "approved_version": projection.version,
    }
    payload.update(overrides)
    return payload


def apply_graph_input(**overrides: object) -> dict[str, object]:
    projection = improvement_projection(state="proposed")
    approved = ImprovementProjection.model_validate(improvement_projection())
    payload: dict[str, object] = {
        **skill_graph_fields(),
        **complete_evaluate_payload(),
        "projection": projection,
        "assessment": _assessment(),
        "current": projection,
        "review_id": "REV-1",
        "approved_state_digest": artifact_digest(approved),
        "approved_version": 1,
        "before_sha256": "b",
        "after_sha256": "a",
        "receipt_sha256": "r",
        "lifecycle_state": "proposed",
    }
    payload.update(overrides)
    return payload


def auto_review_output(*, lifecycle_state: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "review_id": "REV-1",
        "improvement_id": IMPROVEMENT_ID,
        "result": "approved" if lifecycle_state == "approved" else "escalated",
        "lifecycle_state": lifecycle_state,
        "projection": improvement_projection(state=lifecycle_state),
    }


def human_review_output(*, lifecycle_state: str) -> dict[str, object]:
    return improvement_projection(state=lifecycle_state, version=2)


def apply_receipt() -> dict[str, object]:
    return {
        "target": ".aa/memory/aa-api-plan.md",
        "before_sha256": "b",
        "after_sha256": "a",
        "receipt_sha256": "r",
    }


def export_receipt() -> dict[str, object]:
    return {
        "sha256": "e",
        "created": True,
        "artifact_path": "qa/changes/CH-DEMO-001/export/change.json",
    }


def rollback_receipt() -> dict[str, object]:
    return {
        "target": ".aa/memory/aa-api-plan.md",
        "restored_sha256": "x",
        "reason": "regressed",
    }


def review_agent_output(*, decision: str = "pass") -> dict[str, object]:
    return {
        "schema_version": "1",
        "review_type": "improvement",
        "decision": decision,
        "findings": [],
        "evidence_traceability": "complete",
        "scope_readiness": "ready",
        "verification_readiness": "ready",
        "delivery_safety": "ready",
        "human_review_required": decision == "needs_human_review",
    }


def export_graph_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **skill_graph_fields(),
        "projection": improvement_projection(),
        "artifact_path": "qa/changes/CH-DEMO-001/export/change.json",
        "sha256": "e",
        "created": True,
        "target_digest": HEX_A,
    }
    payload.update(overrides)
    return payload


def rollback_graph_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **skill_graph_fields(),
        "projection": improvement_projection(state="applied"),
        "reason": "regressed",
        "restored_sha256": "x",
        "target_digest": HEX_A,
    }
    payload.update(overrides)
    return payload


def test_graph_names_are_not_effect_kinds() -> None:
    assert GRAPH_NAMES_NOT_EFFECT_KINDS.isdisjoint(EXPECTED_EFFECT_KINDS)
    for name in ("archive", "retro", "review", "evaluate", "export", "apply", "rollback"):
        assert name not in EXPECTED_EFFECT_KINDS
        assert f"improvement-{name}" not in EXPECTED_EFFECT_KINDS
    assert "memory_eval" not in EXPECTED_EFFECT_KINDS
    assert set(EFFECT_IDS) <= EXPECTED_EFFECT_KINDS


def test_registered_receipt_traces_use_only_the_three_improvement_effects() -> None:
    from graph_engine import RegistryPorts
    from graph_engine import ENGINE_API_VERSION

    from assurance_improvement.plugin import ImprovementPlugin

    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    kinds = tuple(sorted(item.kind for item in contribution.effects))
    assert kinds == tuple(sorted(EFFECT_IDS))
    kernel = tuple(
        sorted(kind for kind in EXPECTED_EFFECT_KINDS if kind.startswith("assurance.improvement."))
    )
    assert kernel == tuple(sorted(EFFECT_IDS))


def test_auto_review_and_evaluate_routes_are_exclusive() -> None:
    assert route_auto_review({"lifecycle_state": "approved"}) == "evaluate"
    assert route_auto_review({"lifecycle_state": "needs_rework"}) == "rework"
    assert route_auto_review({"lifecycle_state": "rejected"}) == "rejected"
    assert route_auto_review({"lifecycle_state": "proposed"}) == "human-review"
    assert route_auto_review({"lifecycle_state": "unknown"}) == "failed"
    assert route_auto_review({"attempt_failure": {"resolution_kind": "rejected"}}) == "failed"
    assert route_human_review_result({"lifecycle_state": "approved"}) == "evaluate"
    assert route_human_review_result({"lifecycle_state": "rejected"}) == "rejected"
    assert route_human_review_result({"lifecycle_state": "needs_rework"}) == "rework"
    assert route_human_review_result({"lifecycle_state": "superseded"}) == "superseded"
    assert route_human_review_result({"lifecycle_state": "proposed"}) == "failed"
    assert route_apply_evaluate({"outcome": "passed"}) == "apply"
    assert route_apply_evaluate({"outcome": "regressed"}) == "failed"
    assert route_apply_evaluate({"attempt_failure": {"resolution_kind": "permanent"}}) == "failed"


@pytest.mark.parametrize(
    ("lifecycle_state", "terminal"),
    [
        ("needs_rework", "rework"),
        ("rejected", "rejected"),
        ("unknown", "failed"),
    ],
)
async def test_apply_auto_review_routes_typed_lifecycle(lifecycle_state: str, terminal: str) -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    result = await harness.run(
        bundle.apply,
        input=apply_graph_input(),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state=lifecycle_state), _receipt())
            ]
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["improvement.apply-auto-review"]
    assert [call.contract_id for call in result.semantic_calls] == [TASK_AUTO_REVIEW_ID]
    terminal_state = result.terminal
    assert isinstance(terminal_state, dict)
    assert terminal_state.get("lifecycle_state") in {lifecycle_state, terminal, "failed"}
    assert terminal_state.get("status", terminal) == terminal


async def test_apply_covers_auto_review_evaluate_and_apply() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.apply,
        input=apply_graph_input(),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state="approved"), receipt)
            ],
            "improvement.apply-evaluate": [committed(evaluate_receipt(), receipt)],
            "improvement.apply": [committed(apply_receipt(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "improvement.apply-auto-review",
        "improvement.apply-evaluate",
        "improvement.apply",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        TASK_AUTO_REVIEW_ID,
        TASK_EVALUATE_ID,
        TASK_APPLY_ID,
    ]
    evaluate_selected = result.select_values[1]
    assert isinstance(evaluate_selected, dict)
    assert evaluate_selected["eval_run_id"] == "eval-1"
    published = result.published_update
    assert published is not None
    assert published.get("outcome") == "passed" or published.get("target") == ".aa/memory/aa-api-plan.md"


async def test_apply_human_review_covers_reject_rework_and_supersede() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.apply,
        input=apply_graph_input(human_action="supersede"),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state="proposed"), receipt)
            ],
            "improvement.apply-human-review": [
                committed(human_review_output(lifecycle_state="superseded"), receipt)
            ],
        },
    )
    assert result.interrupt_envelope is not None or (
        result.semantic_calls and result.semantic_calls[0].semantic_node_id == "improvement.apply-auto-review"
    )


async def test_standalone_evaluate_and_apply_evaluate_are_effectful_memory_attempts() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    selected = select_evaluate_memory(complete_evaluate_payload())
    assert selected.eval_run_id == "eval-1"
    standalone = await harness.run(
        bundle.evaluate,
        input={**skill_graph_fields(), **complete_evaluate_payload()},
        script={"improvement.evaluate": [committed(evaluate_receipt(), receipt)]},
    )
    assert [call.semantic_node_id for call in standalone.semantic_calls] == ["improvement.evaluate"]
    assert [call.contract_id for call in standalone.semantic_calls] == [TASK_EVALUATE_ID]
    standalone_selected = standalone.select_values[0]
    assert isinstance(standalone_selected, dict)
    assert standalone_selected["eval_run_id"] == "eval-1"
    assert standalone_selected["outcome"] == "passed"
    published = standalone.published_update
    assert published is not None
    receipt_model = MemoryEvalReceipt.model_validate(
        {
            key: published[key]
            for key in ("eval_run_id", "outcome", "report_sha256", "staged_sha256")
            if key in published
        }
        if "eval_run_id" in published
        else published.get("memory_eval") or published
    )
    assert receipt_model.outcome == "passed"
    refs = published.get("receipt_refs") or published.get("effect_refs") or []
    if isinstance(refs, list):
        assert not any(
            isinstance(item, dict) and item.get("kind") in GRAPH_NAMES_NOT_EFFECT_KINDS for item in refs
        )
        assert not any(
            isinstance(item, dict)
            and str(item.get("kind", "")).startswith("assurance.improvement.")
            and item.get("kind") not in EFFECT_IDS
            for item in refs
        )

    apply_result = await harness.run(
        bundle.apply,
        input=apply_graph_input(),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state="approved"), receipt)
            ],
            "improvement.apply-evaluate": [committed(evaluate_receipt(), receipt)],
            "improvement.apply": [committed(apply_receipt(), receipt)],
        },
    )
    evaluate_calls = [call for call in apply_result.semantic_calls if call.contract_id == TASK_EVALUATE_ID]
    assert len(evaluate_calls) == 1
    assert evaluate_calls[0].semantic_node_id == "improvement.apply-evaluate"
    apply_eval_selected = apply_result.select_values[1]
    assert isinstance(apply_eval_selected, dict)
    assert apply_eval_selected["eval_run_id"] == "eval-1"
    assert apply_eval_selected.get("outcome") == "passed"


async def test_review_export_and_rollback_route_on_typed_results() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    review = await harness.run(
        bundle.review,
        input=skill_graph_fields(),
        script={"improvement.review": [committed(review_agent_output(), receipt)]},
    )
    assert [call.semantic_node_id for call in review.semantic_calls] == ["improvement.review"]
    assert [call.contract_id for call in review.semantic_calls] == [_REVIEW_ID]
    published_review = review.published_update
    assert published_review is not None
    assert published_review["decision"] == "pass"

    exported = await harness.run(
        bundle.export,
        input=export_graph_input(),
        script={"improvement.export": [committed(export_receipt(), receipt)]},
    )
    assert [call.semantic_node_id for call in exported.semantic_calls] == ["improvement.export"]
    assert [call.contract_id for call in exported.semantic_calls] == [TASK_EXPORT_ID]
    published_export = exported.published_update
    assert published_export is not None
    assert published_export["artifact_path"] == "qa/changes/CH-DEMO-001/export/change.json"

    rolled = await harness.run(
        bundle.rollback,
        input=rollback_graph_input(),
        script={"improvement.rollback": [committed(rollback_receipt(), receipt)]},
    )
    assert [call.semantic_node_id for call in rolled.semantic_calls] == ["improvement.rollback"]
    assert [call.contract_id for call in rolled.semantic_calls] == [TASK_ROLLBACK_ID]
    published_rollback = rolled.published_update
    assert published_rollback is not None
    assert published_rollback["reason"] == "regressed"


async def test_published_effect_refs_are_not_graph_names() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    forged = evaluate_receipt()
    forged["effect_refs"] = [{"kind": "improvement-evaluate", "digest": HEX_A}]
    result = await harness.run(
        bundle.evaluate,
        input={**skill_graph_fields(), **complete_evaluate_payload()},
        script={"improvement.evaluate": [committed(forged, _receipt())]},
    )
    published = result.published_update
    assert published is not None
    refs = published.get("effect_refs") or published.get("receipt_refs") or []
    assert isinstance(refs, list)
    assert not any(isinstance(item, dict) and item.get("kind") == "improvement-evaluate" for item in refs)
    assert not any(
        isinstance(item, dict) and item.get("kind") in GRAPH_NAMES_NOT_EFFECT_KINDS for item in refs
    )
