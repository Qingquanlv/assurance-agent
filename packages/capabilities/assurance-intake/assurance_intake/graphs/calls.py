from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from graph_engine.artifacts import ArtifactRef, coerce_artifact_ref
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.stategraph.ledger import NamedWrite, ledger_refs, matches_named_write
from graph_engine.stategraph.publish import output_mapping

from assurance_intake.contracts.explore import EXPLORATION_PATH
from assurance_intake.contracts.impact import INVENTORY_PATH
from assurance_intake.contracts.plan import ResolvePlanInputV1, ResolvePlanOutputV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.graphs.state import as_int
from assurance_intake.ops.case_design import CaseDesignInputV1
from assurance_intake.ops.case_repair import CaseRepairInputV1
from assurance_intake.ops.case_review import CaseReviewInputV1
from assurance_intake.ops.explore import ExploreInputV1
from assurance_intake.ops.intake import IntakeInputV1

activation_one_shot = BusinessActivation.one_shot()
CASE_LEDGER_NAME = "intake.case"


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
                item for item in _preparation_refs(state) if item["path"] == EXPLORATION_PATH
            ),
            "impact_inventory_ref": next(
                item for item in _preparation_refs(state) if item["path"] == INVENTORY_PATH
            ),
            "source_resource_digests": (
                (catalog["resource_id"], catalog["sha256"]),
                (knowledge["resource_id"], knowledge["sha256"]),
            ),
            "capability_leafs": state["capability_leafs"],
        }
    )


def _rebind_artifact_path(
    items: object,
    ref: EvidenceArtifactRefV1,
) -> list[dict[str, object]] | None:
    """Replace one path with the digest of the bytes now on disk."""
    mappings = _mapping_items(items)
    if not any(item.get("path") == ref.path for item in mappings):
        return None
    dumped = ref.model_dump(mode="json")
    rebound: list[dict[str, object]] = []
    replaced = False
    for item in mappings:
        if item.get("path") != ref.path:
            rebound.append(dict(item))
            continue
        if replaced:
            continue
        rebound.append(dumped)
        replaced = True
    return rebound


def publish_plan(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    resolved = ResolvePlanOutputV1.model_validate(output)
    current = state.get("plan_digest")
    if current is not None and current != resolved.plan.plan_digest:
        raise ValueError("frozen assurance plan cannot be replaced")
    refs = _mapping_items(state.get("preparation_refs"))
    refs.append(resolved.plan.exploration_ref.model_dump(mode="json"))
    refs.append(resolved.plan_ref.model_dump(mode="json"))
    by_path = {str(item["path"]): item for item in refs}
    published: dict[str, object] = {
        "selected_test_families": list(resolved.plan.selected_test_families),
        "plan_digest": resolved.plan.plan_digest,
        "plan_ref": resolved.plan_ref.model_dump(mode="json"),
        "preparation_refs": [by_path[path] for path in sorted(by_path)],
    }
    # Plan resolution rewrites exploration.json with bound obligation keys.
    # Keep the artifact list on that digest so later readers do not authenticate
    # the pre-bind bytes.
    rebound = _rebind_artifact_path(state.get("artifacts"), resolved.plan.exploration_ref)
    if rebound is not None:
        published["artifacts"] = rebound
    return published


def case_refs_from_ledger(state: Mapping[str, object]) -> list[dict[str, str]]:
    return ledger_refs(state.get("artifact_ledger"), CASE_LEDGER_NAME)


def _case_delta_paths(state: Mapping[str, object]) -> object:
    refs = case_refs_from_ledger(state)
    if refs:
        return [item["path"] for item in refs]
    return state["case_delta_paths"]


def _case_delta_payload(state: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        **_skill_payload(state),
        "plan_digest": state["plan_digest"],
        "plan_ref": state["plan_ref"],
        "selected_test_families": state["selected_test_families"],
        "case_delta_paths": _case_delta_paths(state),
        "preparation_refs": state.get("preparation_refs", ()),
    }
    if state.get("ui_exploration_ref") is not None:
        payload["ui_exploration_ref"] = state["ui_exploration_ref"]
    if state.get("api_discovery_ref") is not None:
        payload["api_discovery_ref"] = state["api_discovery_ref"]
    return payload


def select_case_design(state: Mapping[str, object]) -> CaseDesignInputV1:
    return CaseDesignInputV1.model_validate(
        {
            **_case_delta_payload(state),
            "coverage_epoch": state.get("coverage_epoch", 0),
            "case_rework_context": state.get("case_rework_context"),
        }
    )


def select_case_repair(state: Mapping[str, object]) -> CaseRepairInputV1:
    return CaseRepairInputV1.model_validate(_case_delta_payload(state))


def select_case_review(state: Mapping[str, object]) -> CaseReviewInputV1:
    return CaseReviewInputV1.model_validate(
        {
            **_skill_payload(state),
            "plan_digest": state["plan_digest"],
            "plan_ref": state["plan_ref"],
            "case_delta_paths": _case_delta_paths(state),
            "coverage_epoch": state.get("coverage_epoch", 0),
            "review_round": state.get("rounds_used", 0),
            "preparation_refs": state.get("preparation_refs", ()),
            "case_refs": case_refs_from_ledger(state),
        }
    )


def activation_review_round(state: Mapping[str, object]) -> BusinessActivation:
    return BusinessActivation.for_round(as_int(state["rounds_used"], name="rounds_used"))


def _output_payload(output: object) -> dict[str, object]:
    return output_mapping(output)


def _committed_refs(
    committed: Sequence[ArtifactRef | Mapping[str, object]],
) -> tuple[ArtifactRef, ...]:
    return tuple(coerce_artifact_ref(item) for item in committed)


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def publish_artifacts(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    current_refs = _mapping_items(state.get("preparation_refs"))
    artifact_items = _mapping_items(payload.get("artifacts"))
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
    }


def publish_case_design(
    state: Mapping[str, object],
    output: object,
    receipt: object,
    *,
    committed: Sequence[ArtifactRef | Mapping[str, object]] = (),
) -> dict[str, object]:
    del output, receipt
    # Sealed refs, not the agent artifact list, decide which files moved.
    refs = _committed_refs(committed)
    case_write = _case_named_write()
    cases = tuple(ref for ref in refs if matches_named_write(case_write, ref.path))
    others = tuple(ref for ref in refs if ref not in cases)
    preparation_refs = [
        EvidenceArtifactRefV1.model_validate(item).model_dump(mode="json")
        for item in (
            *_mapping_items(state.get("preparation_refs")),
            *(ref.model_dump(mode="json") for ref in others),
        )
        if isinstance(item, Mapping)
        and isinstance(item.get("digest"), str)
        and not matches_named_write(case_write, str(item.get("path", "")))
    ]
    preparation_by_path = {str(item["path"]): item for item in preparation_refs}
    published_cases = sorted(
        (ref.model_dump(mode="json") for ref in cases),
        key=lambda item: (item["path"], item["digest"]),
    )
    update: dict[str, object] = {
        "artifacts": [ref.model_dump(mode="json") for ref in refs],
        "preparation_refs": [preparation_by_path[path] for path in sorted(preparation_by_path)],
    }
    if published_cases:
        update["case_delta_paths"] = [item["path"] for item in published_cases]
    return update


def _case_named_write() -> NamedWrite:
    from assurance_intake.ops.case_design import op as case_design_op

    return next(item for item in case_design_op.ledger_writes() if item.name == "case")


def publish_case_review(state: Mapping[str, object], output: object, receipt: object) -> dict[str, object]:
    from assurance_intake.graphs.case import project_case_review

    return project_case_review(state, output, receipt)


__all__ = [
    "activation_one_shot",
    "activation_review_round",
    "case_refs_from_ledger",
    "publish_artifacts",
    "publish_case_design",
    "publish_case_review",
    "publish_plan",
    "select_case_design",
    "select_case_repair",
    "select_case_review",
    "select_explore",
    "select_intake",
    "select_resolve_plan",
]
