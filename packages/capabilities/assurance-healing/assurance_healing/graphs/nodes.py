from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Literal, Self, cast

from langgraph.types import interrupt
from pydantic import BaseModel, model_validator

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel

from assurance_healing.contracts.agent import (
    CoverageRepairInputV1,
    FixProposalInputV1,
    FixProposalResultV1,
)
from assurance_healing.contracts.application import (
    AppliedTestRepairV1,
    ApplyTestRepairInputV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.coverage_repair import HEALING_REPAIR_OUTCOMES, HealingRepairOutcome
from assurance_healing.contracts.decisions import advance_repair_round
from assurance_healing.contracts.status import RepairRoundKind
from assurance_healing.graphs.state import HealingRepairPublicV1, HealingState
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

COVERAGE_REVIEW_ACTIONS = ("approve", "reject")
PROPOSAL_APPROVAL_ACTIONS = ("approve", "reject")
_REPAIR_KINDS = frozenset({"failure", "coverage"})


class CoverageReviewDecision(FrozenModel):
    action: Literal["approve", "reject"]


class ProposalApprovalDecision(FrozenModel):
    action: Literal["approve", "reject"]
    approval_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _approved_requires_reference(self) -> Self:
        if self.action == "approve" and self.approval_ref is None:
            raise ValueError("approval requires an authenticated approval reference")
        if self.action == "reject" and self.approval_ref is not None:
            raise ValueError("rejection cannot carry an approval reference")
        return self


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


def _receipt_payload(receipt: object) -> Mapping[str, object]:
    if isinstance(receipt, BaseModel):
        return receipt.model_dump(mode="json")
    if isinstance(receipt, Mapping):
        return receipt
    return {}


def _closed_repair_status(value: object, *, kind: object) -> HealingRepairOutcome:
    del kind
    if value in HEALING_REPAIR_OUTCOMES:
        return value  # type: ignore[return-value]
    return "failed"


def _require_kind(state: Mapping[str, object]) -> RepairRoundKind:
    kind = state.get("kind")
    if kind not in _REPAIR_KINDS:
        raise ValueError("repair kind must be parent-supplied")
    return kind  # type: ignore[return-value]


def select_failure(state: Mapping[str, object]) -> FixProposalInputV1:
    return FixProposalInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "owner_id": state["owner_id"],
            "capability_leafs": state["capability_leafs"],
            "allowed_paths": state["allowed_paths"],
            "allowed_roots": state["allowed_roots"],
            "baseline_digest": state["baseline_digest"],
            "candidate_digest": state["candidate_digest"],
            "policy_digest": state["policy_digest"],
            "mapping_paths": state["mapping_paths"],
            "execution_evidence_digest": state["execution_evidence_digest"],
            "issue_analysis_ref": state.get("issue_analysis_ref"),
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


def select_application(state: Mapping[str, object]) -> ApplyTestRepairInputV1:
    repair_round = state.get("repair_round", state.get("rounds_used"))
    repair_authorization = state.get("repair_authorization")
    verified_repair = repair_authorization is not None
    return ApplyTestRepairInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "coverage_epoch": state["coverage_epoch"],
            "repair_round": repair_round,
            "reviewed_case": state["reviewed_case"],
            "proposal_ref": state["proposal_ref"],
            "approval_ref": state.get("approval_ref"),
            "execution_ref": state["execution_ref"],
            "mapping_ref": state["mapping_ref"],
            "source_refs": state["source_refs"],
            "allowed_test_paths": state["allowed_test_paths"],
            "generation": state.get("generation_result") if verified_repair else None,
            "validation_profile": state.get("validation_profile") if verified_repair else None,
            "selected_test_families": (state.get("selected_test_families", ()) if verified_repair else ()),
            "capability_leafs": state.get("capability_leafs", ()) if verified_repair else (),
            "repair_authorization": repair_authorization,
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
    payload = _output_payload(output)
    change_id = state["change_id"]
    kind = _require_kind(state)
    typed_status = _closed_repair_status(payload.get("status"), kind=kind)
    if not isinstance(change_id, str):
        raise TypeError("change_id must be a string")
    return HealingRepairPublicV1(
        change_id=change_id,
        effect_refs=_as_effect_refs(_receipt_payload(receipt).get("effect_refs")),
        kind=kind,
        rounds_budget=_published_int(payload, "rounds_budget", state.get("rounds_budget")),
        rounds_used=_published_int(payload, "rounds_used", state.get("rounds_used")),
        status=typed_status,
    ).model_dump(mode="json")


def publish_proposal(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    payload = _output_payload(output)
    proposal = FixProposalResultV1.model_validate(
        {name: payload[name] for name in FixProposalResultV1.model_fields if name in payload}
    )
    relative = f"qa/changes/{proposal.change_id}/healing/fix-proposal.json"
    data = canonical_json_bytes(cast(JSONValue, proposal.model_dump(mode="json"))) + b"\n"
    return {
        "proposal_result": proposal.model_dump(mode="json"),
        "proposal_ref": {"path": relative, "digest": hashlib.sha256(data).hexdigest()},
        "proposal_receipt": dict(_receipt_payload(receipt)),
    }


def publish_applied_repair(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    verified = VerifiedTestRepairV1.model_validate(_output_payload(output))
    applied = AppliedTestRepairV1(
        change_id=verified.change_id,
        plan_digest=verified.plan_digest,
        plan_ref=verified.plan_ref,
        coverage_epoch=verified.coverage_epoch,
        repair_round=verified.repair_round,
        status="applied",
        changed_test_refs=verified.changed_test_refs,
        mapping_ref=verified.mapping_ref,
        receipt=ReceiptRef.model_validate(_receipt_payload(receipt)),
    )
    return HealingRepairPublicV1(
        change_id=verified.change_id,
        effect_refs=_as_effect_refs(_receipt_payload(receipt).get("effect_refs")),
        kind=_require_kind(state),
        rounds_budget=_published_int({}, "rounds_budget", state.get("rounds_budget")),
        rounds_used=_published_int({}, "rounds_used", state.get("rounds_used")),
        status="applied",
        repair_result=applied.model_dump(mode="json"),
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


def proposal_approval(state: Mapping[str, object]) -> dict[str, object]:
    existing = state.get("approval_ref")
    if existing is not None:
        ref = EvidenceArtifactRefV1.model_validate(existing)
        return {"human_action": "approve", "approval_ref": ref.model_dump(mode="json")}
    raw = interrupt(
        {
            "reason": "fix_proposal_requires_approval",
            "actions": list(PROPOSAL_APPROVAL_ACTIONS),
            "interrupt_id": "fix-proposal-approval",
            "ordinal": 0,
            "proposal_ref": state.get("proposal_ref"),
            "proposal": state.get("proposal_result"),
        }
    )
    decision = ProposalApprovalDecision.model_validate(raw)
    return {
        "human_action": decision.action,
        "approval_ref": (
            None if decision.approval_ref is None else decision.approval_ref.model_dump(mode="json")
        ),
    }


def _public_terminal(state: Mapping[str, object], status: HealingRepairOutcome | str) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "kind": state.get("kind"),
        "change_id": state.get("change_id"),
        "rounds_used": state.get("rounds_used", 0),
        "rounds_budget": state.get("rounds_budget", 0),
        "effect_refs": state.get("effect_refs") or [],
    }
    repair_result = state.get("repair_result")
    if isinstance(repair_result, Mapping):
        payload["repair_result"] = dict(repair_result)
    return payload


def terminal_done(state: HealingState) -> dict[str, object]:
    status = state.get("status")
    if state.get("kind") == "failure":
        if status != "applied":
            raise ValueError("failure repair can complete only with an applied repair")
        return _public_terminal(state, "applied")
    return _public_terminal(state, _closed_repair_status(status, kind=state.get("kind")))


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "exhausted")


def terminal_not_eligible(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "not_eligible")


def terminal_failed(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "failed")


def terminal_needs_review(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "needs_review")


__all__ = [
    "COVERAGE_REVIEW_ACTIONS",
    "PROPOSAL_APPROVAL_ACTIONS",
    "CoverageReviewDecision",
    "ProposalApprovalDecision",
    "activation_repair",
    "admit_passthrough",
    "advance_repair_round_node",
    "coverage_review",
    "publish_repair",
    "publish_applied_repair",
    "publish_proposal",
    "proposal_approval",
    "select_application",
    "select_coverage",
    "select_failure",
    "terminal_done",
    "terminal_exhausted",
    "terminal_failed",
    "terminal_not_eligible",
    "terminal_needs_review",
]
