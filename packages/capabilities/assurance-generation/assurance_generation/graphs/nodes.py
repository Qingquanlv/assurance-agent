from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from langgraph.types import interrupt
from pydantic import BaseModel

from assurance_generation.contracts.agent import CodegenInputV1, PlanInputV1
from assurance_generation.contracts.init_runtime import InitTestRuntimeInputV1, InitTestRuntimeResultV1
from assurance_generation.contracts.decisions import advance_review_round, complete_generation
from assurance_generation.contracts.families import GENERATION_FAMILIES
from assurance_generation.contracts.workflow import (
    CompleteGenerationInputV1,
    GenerationCycleResultV1,
    ResolveGenerationInputV1,
)
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_generation.graphs.state import (
    PlanRoundArrival,
    consume_plan_round_trigger,
    empty_plan_round_inbox,
    make_family_lane_result,
    make_plan_round_arrival,
    offer_plan_round_arrival,
)
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")
activation_one_shot = BusinessActivation.one_shot()


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _published_int(payload: Mapping[str, object], key: str, fallback: object) -> object:
    value = payload.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _output_payload(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def _trigger(state: Mapping[str, object]) -> Mapping[str, object] | None:
    inbox = state.get("plan_round_inbox")
    if isinstance(inbox, Mapping):
        nested = inbox.get("current_trigger")
        if isinstance(nested, Mapping):
            return nested
    return None


def select_plan(state: Mapping[str, object]) -> PlanInputV1:
    local_round = _as_int(state.get("rounds_used", 0), name="rounds_used")
    return PlanInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "capability_leafs": state["capability_leafs"],
            "artifact_paths": state["allowed_artifact_paths"],
            "coverage_epoch": state.get("coverage_epoch", 0),
            "local_round": local_round,
            "reviewed_case": state.get("reviewed_case"),
            "reviewed_plan": state["reviewed_plan"] if local_round > 0 else None,
        }
    )


def select_init_runtime(state: Mapping[str, object]) -> InitTestRuntimeInputV1:
    return InitTestRuntimeInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "data_knowledge": state["data_knowledge"],
            "capability_leafs": state.get("capability_leafs") or (),
            "allowed_artifact_paths": state.get("allowed_artifact_paths") or (),
        }
    )


def activation_init_runtime(state: Mapping[str, object]) -> BusinessActivation:
    del state
    return BusinessActivation.one_shot()


def publish_init_runtime(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state
    result = InitTestRuntimeResultV1.model_validate(output)
    published = {
        "status": "completed",
        "init_result": result.model_dump(mode="json"),
    }
    if receipt is not None:
        published["receipts"] = [ReceiptRef.model_validate(receipt).model_dump(mode="json")]
    return published


def select_generation_inputs(state: Mapping[str, object]) -> ResolveGenerationInputV1:
    return ResolveGenerationInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "coverage_epoch": state.get("coverage_epoch", 0),
            "reviewed_case": state.get("reviewed_case"),
            "source_artifacts": state.get("source_artifacts") or state.get("artifacts") or (),
        }
    )


def activation_generation_inputs(state: Mapping[str, object]) -> BusinessActivation:
    epoch = _as_int(state.get("coverage_epoch", 0), name="coverage_epoch")
    return BusinessActivation.for_trigger(f"coverage.{epoch}.resolve-inputs")


def activation_generation_cycle(state: Mapping[str, object]) -> BusinessActivation:
    epoch = _as_int(state["coverage_epoch"], name="coverage_epoch")
    return BusinessActivation.for_trigger(f"coverage.{epoch}.publish-cycle")


def publish_generation_inputs(
    state: Mapping[str, object], output: object, receipt: object
) -> dict[str, object]:
    del state, receipt
    reviewed = ReviewedCaseV1.model_validate(output)
    return {"reviewed_case": reviewed.model_dump(mode="json"), "generation_result": {}}


def select_generation_cycle(state: Mapping[str, object]) -> CompleteGenerationInputV1:
    epoch = _as_int(state["coverage_epoch"], name="coverage_epoch")
    results = [
        item
        for item in _as_items(state.get("family_results"))
        if isinstance(item, Mapping) and item.get("coverage_epoch") == epoch and item.get("selected")
    ]
    if any(item.get("status") != "passed" for item in results):
        raise ValueError("generation cycle requires every selected family to pass")
    return CompleteGenerationInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "coverage_epoch": epoch,
            "reviewed_case": state["reviewed_case"],
            "selected_test_families": state["selected_test_families"],
            "capability_leafs": state["capability_leafs"],
            "families": [item.get("generated") for item in results],
        }
    )


def publish_generation_cycle(
    state: Mapping[str, object], output: object, receipt: object
) -> dict[str, object]:
    result = GenerationCycleResultV1.model_validate(output)
    if result.change_id != state["change_id"] or result.coverage_epoch != state["coverage_epoch"]:
        raise ValueError("generation cycle does not match the current epoch")
    return {
        "generation_result": result.model_dump(mode="json"),
        "generation_receipt": ReceiptRef.model_validate(receipt).model_dump(mode="json"),
    }


def select_plan_review(state: Mapping[str, object]) -> PlanInputV1:
    return select_plan(state).model_copy(update={"reviewed_plan": state.get("reviewed_plan")})


def select_codegen(state: Mapping[str, object]) -> CodegenInputV1:
    return CodegenInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "capability_leafs": state["capability_leafs"],
            "codegen_output": state.get("codegen_output"),
            "artifact_paths": state.get("allowed_artifact_paths") or (),
            "coverage_epoch": state.get("coverage_epoch", 0),
            "local_round": state.get("rounds_used", 0),
            "reviewed_case": state.get("reviewed_case"),
        }
    )


def select_codegen_review(state: Mapping[str, object]) -> CodegenInputV1:
    return select_codegen(state)


def activation_plan(state: Mapping[str, object]) -> BusinessActivation:
    return _epoch_activation(state, "plan")


def activation_plan_review(state: Mapping[str, object]) -> BusinessActivation:
    return _epoch_activation(state, "plan-review")


def activation_codegen(state: Mapping[str, object]) -> BusinessActivation:
    return _epoch_activation(state, "codegen")


def activation_codegen_review(state: Mapping[str, object]) -> BusinessActivation:
    return _epoch_activation(state, "codegen-review")


def _epoch_activation(state: Mapping[str, object], stage: str) -> BusinessActivation:
    epoch = _as_int(state.get("coverage_epoch", 0), name="coverage_epoch")
    family = state.get("family")
    if not isinstance(family, str) or not family:
        raise ValueError("generation activation requires family")
    local_round = _as_int(state.get("rounds_used", 0), name="rounds_used")
    raw_reviewed = state.get("reviewed_case")
    if raw_reviewed is None:
        case_version = "unbound"
    else:
        reviewed = ReviewedCaseV1.model_validate(raw_reviewed)
        case_version = canonical_digest(cast(JSONValue, reviewed.model_dump(mode="json")))[:12]
    return BusinessActivation.for_trigger(f"coverage.{epoch}.{family}.{stage}.{local_round}.{case_version}")


def publish_plan(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    return {
        "reviewed_plan": payload,
        "plan_files": payload.get("output_files", []),
        "artifacts": payload.get("artifacts") or [],
        "rounds_used": _published_int(payload, "rounds_used", state.get("rounds_used", 0)),
        "rounds_budget": _published_int(payload, "rounds_budget", state.get("rounds_budget", 3)),
    }


def publish_codegen_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    return publish_plan_review(state, output, receipt)


def publish_plan_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    raw_ids = payload.get("finding_ids")
    finding_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list | tuple) else []
    update = {
        "route": payload.get("route", "codegen"),
        "finding_ids": finding_ids,
        "artifacts": payload.get("artifacts") or [],
        "rounds_used": _published_int(payload, "rounds_used", state.get("rounds_used", 0)),
        "rounds_budget": _published_int(payload, "rounds_budget", state.get("rounds_budget", 3)),
        "semantic_reviews": payload.get("semantic_reviews") or [],
    }
    if payload.get("history_ref") is not None:
        update["history_refs"] = [
            EvidenceArtifactRefV1.model_validate(payload["history_ref"]).model_dump(mode="json")
        ]
    return update


def publish_codegen(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del state
    payload = _output_payload(output)
    family = payload.get("layer")
    plan_files = (
        [
            f"qa/results/codegen/{family}-codegen-summary.md",
            f"qa/results/codegen/{family}-generated-files.json",
        ]
        if isinstance(family, str) and family
        else []
    )
    return {
        "codegen_output": payload,
        "codegen_receipt": ReceiptRef.model_validate(receipt).model_dump(mode="json"),
        "plan_files": plan_files,
    }


def apply_current_trigger(state: Mapping[str, object]) -> dict[str, object]:
    trigger = _trigger(state)
    if trigger is None:
        raise ValueError("plan-retry reads current_trigger.value only")
    value = trigger.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("current trigger value is missing")
    arrival: PlanRoundArrival = {
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
    inbox = state.get("plan_round_inbox") or empty_plan_round_inbox()
    if not isinstance(inbox, Mapping):
        inbox = empty_plan_round_inbox()
    if inbox.get("current_trigger"):
        inbox = consume_plan_round_trigger(inbox)
    used = _as_int(state["rounds_used"], name="rounds_used")
    arrival = make_plan_round_arrival(
        predecessor=predecessor,
        business_epoch=max(0, used - 1),
        sequence=_next_sequence(inbox),
        value={"rounds_used": used, "rounds_budget": _as_int(state["rounds_budget"], name="rounds_budget")},
    )
    return {"plan_round_inbox": offer_plan_round_arrival(inbox, arrival)}


def advance_review_round_node(state: Mapping[str, object]) -> dict[str, object]:
    stage = state.get("review_stage") or "plan"
    return advance_review_round(
        {
            "family": state["family"],
            "stage": stage,
            "rounds_used": state["rounds_used"],
            "rounds_budget": state["rounds_budget"],
        }
    ).model_dump(mode="json")


def review_round_advance(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node({**dict(state), "review_stage": "codegen"})
    return {**advanced, **offer_advance({**dict(state), **advanced}, "codegen-review-round-advance")}


def review_round_advance_retry(state: Mapping[str, object]) -> dict[str, object]:
    advanced = advance_review_round_node({**dict(state), "review_stage": "codegen"})
    return {**advanced, **offer_advance({**dict(state), **advanced}, "codegen-review-round-advance-retry")}


def plan_round_join(state: Mapping[str, object]) -> dict[str, object]:
    return apply_current_trigger(state)


def _as_items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def complete_generation_node(state: Mapping[str, object]) -> dict[str, object]:
    epoch = _as_int(state.get("coverage_epoch", 0), name="coverage_epoch")
    results = [
        item
        for item in _as_items(state.get("family_results"))
        if isinstance(item, Mapping) and item.get("coverage_epoch", 0) == epoch
    ]
    families = [str(item.get("family")) for item in results]
    if len(results) != 4 or set(families) != set(GENERATION_FAMILIES):
        raise ValueError("generation completion requires one result for each family")
    if any(item.get("selected") and item.get("status") != "passed" for item in results):
        return {"status": "failed", "generation_result": {}}
    selected = state.get("selected_test_families")
    return complete_generation(
        {
            "completed": [{"value": True}] * 4,
            "selected_families": selected if isinstance(selected, list) else [],
        }
    ).model_dump(mode="json")


def route_generation_completion(state: Mapping[str, object]) -> Literal["publish", "failed"]:
    return "failed" if state.get("status") == "failed" else "publish"


def join_selected(state: Mapping[str, object]) -> dict[str, object]:
    epoch = _as_int(state.get("coverage_epoch", 0), name="coverage_epoch")
    results = [
        item
        for item in _as_items(state.get("family_results"))
        if isinstance(item, Mapping) and item.get("coverage_epoch", 0) == epoch
    ]
    if len(results) != 4:
        raise ValueError("join-selected requires four family results")
    return {}


def _family_result(state: Mapping[str, object], *, status: str, selected: bool) -> dict[str, object]:
    family = state.get("family")
    if not isinstance(family, str) or not family:
        raise ValueError("family lane result requires family")
    result = {
        "family_results": [
            make_family_lane_result(
                coverage_epoch=_as_int(state.get("coverage_epoch", 0), name="coverage_epoch"),
                family=family,
                receipt_id=f"receipt-{family}",
                selected=selected,
                status=status,
            )
        ]
    }
    if selected and status == "passed":
        codegen = state.get("codegen_output")
        receipt = state.get("codegen_receipt")
        if isinstance(codegen, Mapping) and isinstance(receipt, Mapping):
            lane = result["family_results"][0]
            lane["receipt_id"] = str(receipt["receipt_id"])
            lane["generated"] = {
                "family": family,
                "coverage_epoch": state.get("coverage_epoch", 0),
                "plan_files": state.get("plan_files", []),
                "files": codegen.get("files", []),
                "mapping": codegen.get("mapping"),
                "receipt": dict(receipt),
                "method_plans": codegen.get("method_plans") or [],
                "semantic_reviews": state.get("semantic_reviews") or [],
            }
    return {"family_results": result["family_results"]}


def generation_done(state: Mapping[str, object]) -> dict[str, object]:
    if state.get("attempt_failure") or state.get("status") == "failed":
        return {"status": "failed", "generation_result": {}}
    GenerationCycleResultV1.model_validate(state.get("generation_result"))
    return {"status": "passed"}


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    status = "failed" if state.get("attempt_failure") else "passed"
    update: dict[str, object] = {
        "status": status,
        "decision": state.get("decision", "pass") if status == "passed" else "failed",
    }
    if isinstance(state.get("family"), str) and state.get("family"):
        update.update(_family_result(state, status=status, selected=True))
    return update


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "rejected",
        "decision": "reject",
        **_family_result(state, status="rejected", selected=True),
    }


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "exhausted",
        "decision": "exhausted",
        "rounds_used": state.get("rounds_used", 0),
        **_family_result(state, status="exhausted", selected=True),
    }


def terminal_skipped(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "skipped",
        **_family_result(state, status="skipped", selected=False),
    }


def _coerce_human_decision(raw: object) -> HumanReviewDecision:
    if isinstance(raw, str):
        return HumanReviewDecision(action=raw)  # type: ignore[arg-type]
    if isinstance(raw, Mapping):
        action = raw.get("action", raw.get("decision"))
        return HumanReviewDecision.model_validate({"action": action})
    return HumanReviewDecision.model_validate(raw)


def _interrupt_payload(state: Mapping[str, object], *, retry: bool) -> dict[str, object]:
    family = str(state.get("family") or "api")
    suffix = "-retry" if retry else ""
    return {
        "reason": f"{family}_codegen_needs_human_review",
        "actions": list(HUMAN_REVIEW_ACTIONS),
        "interrupt_id": f"{family}-codegen-human-review{suffix}",
        "ordinal": 1 if retry else 0,
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 3),
    }


def human_review(state: Mapping[str, object]) -> dict[str, object]:
    raw = interrupt(_interrupt_payload(state, retry=False))
    decision = _coerce_human_decision(raw)
    return {"human_action": decision.action}


def human_review_retry(state: Mapping[str, object]) -> dict[str, object]:
    raw = interrupt(_interrupt_payload(state, retry=True))
    decision = _coerce_human_decision(raw)
    return {"human_action": decision.action}


__all__ = [
    "HUMAN_REVIEW_ACTIONS",
    "HumanReviewDecision",
    "activation_codegen",
    "activation_codegen_review",
    "activation_generation_inputs",
    "activation_generation_cycle",
    "activation_init_runtime",
    "activation_one_shot",
    "activation_plan",
    "activation_plan_review",
    "advance_review_round_node",
    "apply_current_trigger",
    "complete_generation_node",
    "generation_done",
    "human_review",
    "human_review_retry",
    "join_selected",
    "plan_round_join",
    "publish_codegen",
    "publish_codegen_review",
    "publish_generation_inputs",
    "publish_generation_cycle",
    "publish_init_runtime",
    "publish_plan",
    "publish_plan_review",
    "review_round_advance",
    "review_round_advance_retry",
    "route_generation_completion",
    "select_codegen",
    "select_codegen_review",
    "select_generation_inputs",
    "select_generation_cycle",
    "select_init_runtime",
    "select_plan",
    "select_plan_review",
    "terminal_done",
    "terminal_exhausted",
    "terminal_rejected",
    "terminal_skipped",
]
