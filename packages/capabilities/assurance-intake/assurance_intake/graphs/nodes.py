from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from langgraph.types import interrupt
from pydantic import BaseModel

from assurance_intake.contracts.agent import (
    CaseDesignInputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    IntakeInputV1,
)
from assurance_intake.contracts.decisions import advance_review_round
from assurance_intake.contracts.workflow import (
    CaseFlowResultV1,
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
)
from assurance_intake.contracts.plan import (
    LoadPlanInputV1,
    ResolvePlanInputV1,
    ResolvePlanOutputV1,
)
from assurance_intake.graphs.state import (
    CaseReviewArrival,
    consume_case_review_trigger,
    empty_case_review_inbox,
    make_case_review_arrival,
    offer_case_review_arrival,
)
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")
activation_one_shot = BusinessActivation.one_shot()


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def _skill_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "change_id": state["change_id"],
        "capability_leafs": state["capability_leafs"],
        "artifact_paths": state["allowed_artifact_paths"],
    }


def select_intake(state: Mapping[str, object]) -> IntakeInputV1:
    return IntakeInputV1.model_validate(
        {
            **_skill_payload(state),
            "requirement": state["requirement"],
            "candidate_test_families": state.get("candidate_test_families", ()),
        }
    )


def select_explore(state: Mapping[str, object]) -> ExploreInputV1:
    return ExploreInputV1.model_validate(
        {
            **_skill_payload(state),
            "candidate_test_families": state["candidate_test_families"],
        }
    )


def _requirement_digest(state: Mapping[str, object]) -> str:
    requirement = state.get("requirement")
    if not isinstance(requirement, str):
        raise ValueError("plan resolution requires the normalized requirement")
    return canonical_digest(cast(JSONValue, {"requirement": requirement}))


def _resource(state: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = state.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"plan resolution requires {name}")
    return value


def _preparation_refs(state: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    value = state.get("preparation_refs")
    if not isinstance(value, (list, tuple)):
        raise ValueError("plan resolution requires preparation_refs")
    return tuple(item for item in value if isinstance(item, Mapping))


def select_resolve_plan(state: Mapping[str, object]) -> ResolvePlanInputV1:
    policy = _resource(state, "product_policy")
    catalog = _resource(state, "capability_catalog")
    knowledge = _resource(state, "data_knowledge")
    return ResolvePlanInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "requirement_digest": _requirement_digest(state),
            "candidate_test_families": state["candidate_test_families"],
            "budgets": state["budgets"],
            "policy_resource_id": policy["resource_id"],
            "policy_digest": policy["sha256"],
            "family_policy": state["family_policy"],
            "exploration_ref": next(
                item
                for item in _preparation_refs(state)
                if item["path"] == "qa/results/explore/exploration.json"
            ),
            "impact_inventory_ref": next(
                item
                for item in _preparation_refs(state)
                if item["path"] == "qa/results/explore/impact-inventory.json"
            ),
            "source_resource_digests": (
                (catalog["resource_id"], catalog["sha256"]),
                (knowledge["resource_id"], knowledge["sha256"]),
            ),
            "capability_leafs": state["capability_leafs"],
        }
    )


def select_load_plan(state: Mapping[str, object]) -> LoadPlanInputV1:
    policy = _resource(state, "product_policy")
    catalog = _resource(state, "capability_catalog")
    knowledge = _resource(state, "data_knowledge")
    return LoadPlanInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "requirement_digest": _requirement_digest(state),
            "resolved_plan_ref": state["resolved_plan_ref"],
            "budgets": state["budgets"],
            "policy_resource_id": policy["resource_id"],
            "policy_digest": policy["sha256"],
            "source_resource_digests": (
                (catalog["resource_id"], catalog["sha256"]),
                (knowledge["resource_id"], knowledge["sha256"]),
            ),
            "capability_leafs": state["capability_leafs"],
        }
    )


def publish_plan(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    resolved = ResolvePlanOutputV1.model_validate(output)
    current = state.get("plan_digest")
    if current is not None and current != resolved.plan.plan_digest:
        raise ValueError("frozen assurance plan cannot be replaced")
    refs = _mapping_items(state.get("preparation_refs"))
    refs.append(resolved.plan_ref.model_dump(mode="json"))
    by_path = {str(item["path"]): item for item in refs}
    return {
        "selected_test_families": list(resolved.plan.selected_test_families),
        "plan_digest": resolved.plan.plan_digest,
        "plan_ref": resolved.plan_ref.model_dump(mode="json"),
        "policy_digest": resolved.plan.policy_digest,
        "preparation_refs": [by_path[path] for path in sorted(by_path)],
    }


def select_case_design(state: Mapping[str, object]) -> CaseDesignInputV1:
    return CaseDesignInputV1.model_validate(
        {
            **_skill_payload(state),
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "selected_test_families": state["selected_test_families"],
            "case_delta_paths": state["case_delta_paths"],
            "coverage_epoch": state.get("coverage_epoch", 0),
            "preparation_refs": state.get("preparation_refs", ()),
            "case_rework_context": state.get("case_rework_context"),
            "validation_attempt": 0,
        }
    )


def select_case_design_retry(state: Mapping[str, object]) -> CaseDesignInputV1:
    return select_case_design(state)


def select_case_design_repair(state: Mapping[str, object]) -> CaseDesignInputV1:
    return CaseDesignInputV1.model_validate(
        {
            **_skill_payload(state),
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "selected_test_families": state["selected_test_families"],
            "case_delta_paths": state["case_delta_paths"],
            "validation_attempt": 1,
            "validation_error": state["validation_error"],
        }
    )


def select_case_review(state: Mapping[str, object]) -> CaseReviewInputV1:
    return CaseReviewInputV1.model_validate(
        {
            **_skill_payload(state),
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "case_delta_paths": state["case_delta_paths"],
            "coverage_epoch": state.get("coverage_epoch", 0),
            "review_round": state.get("rounds_used", 0),
            "preparation_refs": state.get("preparation_refs", ()),
            "case_refs": state.get("case_refs", ()),
        }
    )


def _trigger(state: Mapping[str, object]) -> Mapping[str, object] | None:
    inbox = state.get("case_review_inbox")
    if isinstance(inbox, Mapping):
        nested = inbox.get("current_trigger")
        if isinstance(nested, Mapping):
            return nested
    return None


def activation_case_design(state: Mapping[str, object]) -> BusinessActivation:
    trigger = _trigger(state)
    arrival_id = trigger.get("arrival_id") if trigger is not None else None
    if isinstance(arrival_id, str) and arrival_id:
        return BusinessActivation.for_trigger(arrival_id)
    return BusinessActivation.one_shot()


def activation_case_design_repair(state: Mapping[str, object]) -> BusinessActivation:
    trigger = _trigger(state)
    arrival_id = trigger.get("arrival_id") if trigger is not None else None
    if isinstance(arrival_id, str) and arrival_id:
        return BusinessActivation.for_trigger(f"{arrival_id}.repair")
    return BusinessActivation.for_round(1)


def activation_case_review(state: Mapping[str, object]) -> BusinessActivation:
    trigger = _trigger(state)
    arrival_id = trigger.get("arrival_id") if trigger is not None else None
    if isinstance(arrival_id, str) and arrival_id:
        return BusinessActivation.for_trigger(f"{arrival_id}.review")
    used = state.get("rounds_used", 0)
    return BusinessActivation.for_round(int(used) if isinstance(used, int) else 0)


def _output_payload(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _selection_ref(
    state: Mapping[str, object], payload: Mapping[str, object]
) -> EvidenceArtifactRefV1 | None:
    reviewed = payload.get("reviewed_case")
    if isinstance(reviewed, Mapping) and reviewed.get("selection_ref") is not None:
        return EvidenceArtifactRefV1.model_validate(reviewed["selection_ref"])
    epoch = state.get("coverage_epoch", 0)
    expected = f"qa/results/cases/epochs/{epoch}/selection.json"
    for item in _mapping_items(payload.get("artifacts")):
        if item.get("path") == expected:
            return EvidenceArtifactRefV1.model_validate(item)
    raw = state.get("selection_ref")
    if raw is not None:
        return EvidenceArtifactRefV1.model_validate(raw)
    return None


def publish_artifacts(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    artifacts = payload.get("artifacts")
    files = payload.get("output_files")
    if artifacts is None and isinstance(files, list):
        artifacts = [{"path": path} for path in files]
    current_refs = _mapping_items(state.get("preparation_refs"))
    artifact_items = _mapping_items(artifacts)
    for item in artifact_items:
        if isinstance(item, Mapping) and isinstance(item.get("digest"), str):
            current_refs.append(EvidenceArtifactRefV1.model_validate(item).model_dump(mode="json"))
    unique_refs = {
        (str(item["path"]), str(item["digest"])): item
        for item in current_refs
        if isinstance(item, Mapping) and "path" in item and "digest" in item
    }
    return {
        "artifacts": artifact_items,
        "preparation_refs": [unique_refs[key] for key in sorted(unique_refs)],
        "decision": payload.get("decision", "pass"),
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 2),
    }


def publish_case_design(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    artifacts = _mapping_items(payload.get("artifacts"))
    case_refs = [
        EvidenceArtifactRefV1.model_validate(item).model_dump(mode="json")
        for item in artifacts
        if isinstance(item, Mapping) and str(item.get("path", "")).endswith("/case.yaml")
    ]
    # Carry committed preparation replacements forward, including the frozen
    # plan; never re-hash mutable project files when publishing Case Design.
    preparation_refs = [
        EvidenceArtifactRefV1.model_validate(item).model_dump(mode="json")
        for item in (*_mapping_items(state.get("preparation_refs")), *artifacts)
        if isinstance(item, Mapping)
        and isinstance(item.get("digest"), str)
        and not str(item.get("path", "")).endswith("/case.yaml")
    ]
    preparation_by_path = {str(item["path"]): item for item in preparation_refs}
    published_case_refs = sorted(case_refs, key=lambda item: (item["path"], item["digest"]))
    update = {
        "validation_status": payload.get("validation_status", "pass"),
        "validation_attempt": payload.get("validation_attempt", 0),
        "validation_error": payload.get("validation_error"),
        "artifacts": artifacts,
        "case_refs": published_case_refs,
        "preparation_refs": [preparation_by_path[path] for path in sorted(preparation_by_path)],
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 2),
    }
    if published_case_refs:
        update["case_delta_paths"] = [item["path"] for item in published_case_refs]
    return update


def publish_case_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    payload = _output_payload(output)
    update = {
        "decision": payload["decision"],
        "auto_fix_allowed": bool(payload.get("auto_fix_allowed", False)),
        "human_review_required": bool(payload.get("human_review_required", False)),
        "artifacts": payload.get("artifacts") or [],
        "rounds_used": _as_int(state["rounds_used"], name="rounds_used"),
        "rounds_budget": _as_int(state["rounds_budget"], name="rounds_budget"),
    }
    if payload.get("history_ref") is not None:
        update["history_refs"] = [
            EvidenceArtifactRefV1.model_validate(payload["history_ref"]).model_dump(mode="json")
        ]
    if payload.get("decision") != "pass":
        return update
    review_path = "qa/results/review/case-review.json"
    review_ref = next(
        (
            EvidenceArtifactRefV1.model_validate(item)
            for item in _mapping_items(payload.get("artifacts"))
            if item.get("path") == review_path
        ),
        None,
    )
    if review_ref is None:
        return update
    selection_ref = _selection_ref(state, payload)
    if selection_ref is None:
        return update
    reviewed = ReviewedCaseV1.model_validate(
        {
            "change_id": state["change_id"],
            "coverage_epoch": state.get("coverage_epoch", 0),
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "preparation_refs": state.get("preparation_refs", ()),
            "case_refs": state.get("case_refs", ()),
            "review_ref": review_ref,
            "selection_ref": selection_ref,
        }
    )
    receipt_ref = receipt if isinstance(receipt, ReceiptRef) else None
    if receipt_ref is None and isinstance(receipt, Mapping):
        receipt_ref = ReceiptRef.model_validate(receipt)
    if receipt_ref is not None:
        update["reviewed_case"] = reviewed.model_dump(mode="json")
        update["case_receipt"] = receipt_ref.model_dump(mode="json")
    return update


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def apply_current_trigger(state: Mapping[str, object]) -> dict[str, object]:
    trigger = _trigger(state)
    if trigger is None:
        raise ValueError("case-design-retry reads current_trigger.value only")
    value = trigger.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("current trigger value is missing")
    arrival: CaseReviewArrival = {
        "business_epoch": _as_int(trigger["business_epoch"], name="business_epoch"),
        "predecessor": trigger["predecessor"],  # type: ignore[typeddict-item]
        "source_activation": str(trigger["source_activation"]),
        "sequence": _as_int(trigger["sequence"], name="sequence"),
        "value": {
            "rounds_used": _as_int(value["rounds_used"], name="rounds_used"),
            "rounds_budget": _as_int(value["rounds_budget"], name="rounds_budget"),
        },
        "arrival_id": str(trigger["arrival_id"]),
    }
    return {
        "current_trigger": arrival,
        "rounds_used": arrival["value"]["rounds_used"],
        "rounds_budget": arrival["value"]["rounds_budget"],
    }


def _next_sequence(inbox: Mapping[str, object]) -> int:
    arrivals = inbox.get("arrivals") or []
    if not isinstance(arrivals, list) or not arrivals:
        return 1
    return (
        max(_as_int(item["sequence"], name="sequence") for item in arrivals if isinstance(item, Mapping)) + 1
    )


def offer_advance(state: Mapping[str, object], predecessor: str) -> dict[str, object]:
    inbox = state.get("case_review_inbox") or empty_case_review_inbox()
    if not isinstance(inbox, Mapping):
        inbox = empty_case_review_inbox()
    if inbox.get("current_trigger"):
        inbox = consume_case_review_trigger(inbox)
    used = _as_int(state["rounds_used"], name="rounds_used")
    arrival = make_case_review_arrival(
        predecessor=predecessor,
        business_epoch=max(0, used - 1),
        sequence=_next_sequence(inbox),
        value={"rounds_used": used, "rounds_budget": _as_int(state["rounds_budget"], name="rounds_budget")},
    )
    merged = offer_case_review_arrival(inbox, arrival)
    return {"case_review_inbox": merged}


def advance_review_round_node(state: Mapping[str, object]) -> dict[str, object]:
    return advance_review_round(
        {"rounds_used": state["rounds_used"], "rounds_budget": state["rounds_budget"]}
    ).model_dump(mode="json")


def review_round_advance(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node(state)
    return {**advanced, **offer_advance({**dict(state), **advanced}, "review-round-advance")}


def review_round_advance_retry(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node(state)
    return {**advanced, **offer_advance({**dict(state), **advanced}, "review-round-advance-retry")}


def review_round_advance_rework_retry(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node(state)
    return {**advanced, **offer_advance({**dict(state), **advanced}, "review-round-advance-rework-retry")}


def advance_join(state: Mapping[str, object]) -> dict[str, object]:
    return apply_current_trigger(state)


def _coerce_human_decision(raw: object) -> HumanReviewDecision:
    if isinstance(raw, str):
        return HumanReviewDecision(action=raw)  # type: ignore[arg-type]
    if isinstance(raw, Mapping):
        action = raw.get("action", raw.get("decision"))
        return HumanReviewDecision.model_validate({"action": action})
    return HumanReviewDecision.model_validate(raw)


def human_review(state: Mapping[str, object]) -> dict[str, object]:
    raw = interrupt(
        {
            "reason": "needs_human_review",
            "actions": list(HUMAN_REVIEW_ACTIONS),
            "rounds_used": state.get("rounds_used", 0),
            "rounds_budget": state.get("rounds_budget", 2),
        }
    )
    decision = _coerce_human_decision(raw)
    return {"human_action": decision.action}


def human_review_retry(state: Mapping[str, object]) -> dict[str, object]:
    return human_review(state)


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    return {"status": "passed", "decision": state.get("decision", "pass")}


def terminal_reviewed(state: Mapping[str, object]) -> dict[str, object]:
    result = CaseFlowResultV1.model_validate(
        {
            "status": "reviewed",
            "reviewed_case": state.get("reviewed_case"),
            "receipt": state.get("case_receipt"),
        }
    )
    return {**result.model_dump(mode="json"), "decision": "pass"}


def terminal_prepared(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "prepared"}


def terminal_failed(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "failed"}


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    return {"status": "rejected", "decision": "reject"}


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "exhausted",
        "decision": "exhausted",
        "rounds_used": state.get("rounds_used", 0),
    }


__all__ = [
    "HUMAN_REVIEW_ACTIONS",
    "HumanReviewDecision",
    "activation_case_design",
    "activation_case_design_repair",
    "activation_case_review",
    "activation_one_shot",
    "advance_join",
    "advance_review_round_node",
    "apply_current_trigger",
    "human_review",
    "human_review_retry",
    "publish_artifacts",
    "publish_case_design",
    "publish_case_review",
    "review_round_advance",
    "review_round_advance_retry",
    "review_round_advance_rework_retry",
    "select_case_design",
    "select_case_design_repair",
    "select_case_design_retry",
    "select_case_review",
    "select_explore",
    "select_load_plan",
    "select_resolve_plan",
    "publish_plan",
    "select_intake",
    "terminal_failed",
    "terminal_done",
    "terminal_exhausted",
    "terminal_prepared",
    "terminal_rejected",
    "terminal_reviewed",
]
