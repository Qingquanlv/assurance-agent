from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.plugin_api import FrozenModel

from assurance_healing.contracts.agent import CoverageRepairInputV1, FixProposalInputV1
from assurance_healing.contracts.coverage_repair import HealingRepairOutcome
from assurance_healing.contracts.decisions import advance_repair_round
from assurance_healing.contracts.workflow import RepairRoundKind
from assurance_healing.graphs.state import HealingRepairPublicV1, HealingState

COVERAGE_REVIEW_ACTIONS = ("approve", "reject")
_REPAIR_KINDS = frozenset({"failure", "coverage"})


class CoverageReviewDecision(FrozenModel):
    action: Literal["approve", "reject"]


def _output_payload(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def _published_int(payload: Mapping[str, object], key: str, fallback: object) -> int:
    value = payload.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an int")
    return value


def _as_effect_refs(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("effect refs must be a list")
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("effect ref must be a mapping")
        kind = item.get("kind")
        digest = item.get("digest")
        if not isinstance(kind, str) or not isinstance(digest, str):
            raise TypeError("effect ref requires kind and digest")
        refs.append({"kind": kind, "digest": digest})
    return refs


def _require_kind(state: Mapping[str, object]) -> RepairRoundKind:
    kind = state.get("kind")
    if kind not in _REPAIR_KINDS:
        raise ValueError("repair kind must be parent-supplied")
    return kind  # type: ignore[return-value]


def select_failure(state: Mapping[str, object]) -> FixProposalInputV1:
    return FixProposalInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "owner_id": state["owner_id"],
            "capability_leafs": state["capability_leafs"],
            "allowed_paths": state["allowed_paths"],
            "allowed_roots": state["allowed_roots"],
            "baseline_digest": state["baseline_digest"],
            "candidate_digest": state["candidate_digest"],
            "policy_digest": state["policy_digest"],
            "mapping_paths": state["mapping_paths"],
            "execution_evidence_digest": state["execution_evidence_digest"],
        }
    )


def select_coverage(state: Mapping[str, object]) -> CoverageRepairInputV1:
    return CoverageRepairInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "brief": state["brief"],
            "baseline_digest": state["baseline_digest"],
            "allowed_roots": state["allowed_roots"],
        }
    )


def activation_repair(state: Mapping[str, object]) -> BusinessActivation:
    _require_kind(state)
    raw = state.get("activation")
    if isinstance(raw, Mapping):
        kind = raw.get("kind")
        value = raw.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("repair business activation value is missing")
        if kind == "round":
            return BusinessActivation.for_round(int(value))
        if kind == "trigger":
            return BusinessActivation.for_trigger(value)
        raise ValueError("repair business activation is not canonical")
    trigger = state.get("current_trigger")
    if isinstance(trigger, Mapping):
        arrival_id = trigger.get("arrival_id")
        if isinstance(arrival_id, str) and arrival_id:
            return BusinessActivation.for_trigger(arrival_id)
    raise ValueError("repair business activation must be parent-supplied")


def publish_repair(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    change_id = state["change_id"]
    kind = _require_kind(state)
    status = payload.get("status", "repaired")
    if status not in {"exhausted", "failed", "needs_review", "not_eligible", "repaired"}:
        raise ValueError("repair status is not canonical")
    typed_status: HealingRepairOutcome = status  # type: ignore[assignment]
    if not isinstance(change_id, str):
        raise TypeError("change_id must be a string")
    return HealingRepairPublicV1(
        change_id=change_id,
        effect_refs=_as_effect_refs(payload.get("effect_refs")),
        kind=kind,
        rounds_budget=_published_int(payload, "rounds_budget", state.get("rounds_budget")),
        rounds_used=_published_int(payload, "rounds_used", state.get("rounds_used")),
        status=typed_status,
    ).model_dump(mode="json")


def advance_repair_round_node(state: Mapping[str, object]) -> dict[str, object]:
    return advance_repair_round(
        {
            "kind": _require_kind(state),
            "rounds_used": state["rounds_used"],
            "rounds_budget": state["rounds_budget"],
        }
    ).model_dump(mode="json")


def admit_passthrough(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {}


def _coerce_review_decision(raw: object) -> CoverageReviewDecision:
    if isinstance(raw, str):
        return CoverageReviewDecision(action=raw)  # type: ignore[arg-type]
    if isinstance(raw, Mapping):
        action = raw.get("action", raw.get("decision"))
        return CoverageReviewDecision.model_validate({"action": action})
    return CoverageReviewDecision.model_validate(raw)


def coverage_review(state: Mapping[str, object]) -> dict[str, object]:
    raw = interrupt(
        {
            "reason": "coverage_repair_needs_review",
            "actions": list(COVERAGE_REVIEW_ACTIONS),
            "interrupt_id": "coverage-repair-needs-review",
            "ordinal": 0,
            "rounds_used": state.get("rounds_used", 0),
            "rounds_budget": state.get("rounds_budget", 1),
        }
    )
    decision = _coerce_review_decision(raw)
    return {"human_action": decision.action}


def _public_terminal(state: Mapping[str, object], status: HealingRepairOutcome) -> dict[str, object]:
    return {
        "status": status,
        "kind": state.get("kind"),
        "change_id": state.get("change_id"),
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 0),
        "effect_refs": state.get("effect_refs") or [],
    }


def terminal_done(state: HealingState) -> dict[str, object]:
    status = state.get("status", "repaired")
    if status not in {"exhausted", "failed", "needs_review", "not_eligible", "repaired"}:
        status = "repaired"
    return _public_terminal(state, status)  # type: ignore[arg-type]


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "exhausted")


def terminal_not_eligible(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "not_eligible")


def terminal_failed(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "failed")


__all__ = [
    "COVERAGE_REVIEW_ACTIONS",
    "CoverageReviewDecision",
    "activation_repair",
    "admit_passthrough",
    "advance_repair_round_node",
    "coverage_review",
    "publish_repair",
    "select_coverage",
    "select_failure",
    "terminal_done",
    "terminal_exhausted",
    "terminal_failed",
    "terminal_not_eligible",
]
