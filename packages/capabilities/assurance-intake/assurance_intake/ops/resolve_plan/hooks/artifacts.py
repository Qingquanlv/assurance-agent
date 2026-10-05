"""Authenticated source loading and quality-goal materialization for plans."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import RootModel

from agent_runtime_contracts.ops import ArtifactHandle, InputError
from graph_engine.artifacts import ArtifactReadError, open_artifact, read_workspace_file, stage_json_artifact

from assurance_intake.contracts.explore import (
    EXPLORATION_PATH,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
    ExploreAdvisoryV1,
    PreparedExploreV1,
)
from assurance_intake.contracts.impact import INVENTORY_PATH, ChangeImpactInventoryV1
from assurance_intake.contracts.obligations import PreparedObligationV1, SourceRefV1
from assurance_intake.contracts.plan import (
    PREPARATION_REFS_PATH,
    PreparationRefsDocumentV1,
    PreparedQualityGoalV1,
    ResolvePlanInputV1,
    ResolvePlanOutputV1,
    TestFamilyPolicyV1,
    plan_artifact_ref,
)
from assurance_intake.contracts.quality_goals import (
    COVERAGE_GOAL_ORDER,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.impact_validation import validate_inventory_closed_keys
from assurance_intake.domain.obligations import (
    apply_scope_exclusions,
    journey_keys_from_document,
    normalize_goal_obligations,
    required_goal_families,
)
from assurance_intake.ops.resolve_plan.hooks.plan import derive_family_proposal, resolve_plan

_MARKER_PATH = "qa/.qa.yaml"
_CONTEXT_PATH = "qa/results/explore/context.json"
_EXPLORATION_DRAFT_PATH = "qa/results/explore/exploration-draft.json"
_ON_DISK_PREPARATION = (
    _MARKER_PATH,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
    _CONTEXT_PATH,
    _EXPLORATION_DRAFT_PATH,
    EXPLORATION_PATH,
    INVENTORY_PATH,
)

_RESOURCE_PATHS = {
    "assurance.product.configuration.capability-catalog": ".aa/capability-catalog.json",
    "assurance.product.configuration.data-knowledge": ".aa/data-knowledge.yaml",
}
_POLICY_PATH = ".aa/policy.yaml"


class _MappingDocument(RootModel[dict[str, object]]):
    """Typed mapping envelope for authenticated product JSON/YAML sources."""


def _source_ref(request: ResolvePlanInputV1, resource_id: str) -> dict[str, str]:
    try:
        return {
            "path": _RESOURCE_PATHS[resource_id],
            "digest": dict(request.source_resource_digests)[resource_id],
        }
    except KeyError as error:
        raise InputError(f"goal source is missing: {resource_id}") from error


def _exploration_loader(root: Path, ref: object) -> ExploreAdvisoryV1 | PreparedExploreV1:
    return load_exploration_document(open_artifact(root, cast(Any, ref)))


EXPLORATION = ArtifactHandle(
    "intake.exploration", slot="exploration_ref", loader=_exploration_loader, same=("change_id",)
)
INVENTORY = ArtifactHandle(
    "intake.inventory", slot="impact_inventory_ref", model=ChangeImpactInventoryV1, same=("change_id",)
)
POLICY = ArtifactHandle(
    "intake.resolve.policy",
    ref=lambda business: {"path": _POLICY_PATH, "digest": getattr(business, "policy_digest")},
    model=_MappingDocument,
    format="yaml",
)
CATALOG = ArtifactHandle(
    "intake.resolve.catalog",
    ref=lambda business: _source_ref(
        cast(ResolvePlanInputV1, business), "assurance.product.configuration.capability-catalog"
    ),
    model=_MappingDocument,
    format="json",
)
KNOWLEDGE = ArtifactHandle(
    "intake.resolve.knowledge",
    ref=lambda business: _source_ref(
        cast(ResolvePlanInputV1, business), "assurance.product.configuration.data-knowledge"
    ),
    model=_MappingDocument,
    format="yaml",
)
RESOLVE_DEPENDS = (EXPLORATION, INVENTORY, POLICY, CATALOG, KNOWLEDGE)


def _exclusion_basis(project_root: Path) -> SourceRefV1 | None:
    try:
        data = read_workspace_file(project_root, RUN_SPEC_SNAPSHOT_PATH)
    except ArtifactReadError as error:
        if error.reason != "missing":
            raise InputError(f"invalid run-spec snapshot: {error}") from error
        return None
    digest = hashlib.sha256(data).hexdigest()
    return SourceRefV1(
        kind="decision",
        artifact=EvidenceArtifactRefV1(path=RUN_SPEC_SNAPSHOT_PATH, digest=digest),
        locator="/candidate_test_families",
    )


def prepare_quality_goal(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
    sources: Mapping[str, object] | None = None,
) -> tuple[
    ExploreAdvisoryV1 | PreparedExploreV1,
    ChangeImpactInventoryV1,
    PreparedQualityGoalV1,
    tuple[PreparedObligationV1, ...],
]:
    if set(dict(request.source_resource_digests)) != set(_RESOURCE_PATHS):
        raise ValueError("goal sources must contain catalog and data knowledge")
    if sources is None:
        sources = {handle.ledger_key: handle.load(project_root, request) for handle in RESOLVE_DEPENDS}
    advisory = cast(ExploreAdvisoryV1 | PreparedExploreV1, sources[EXPLORATION.ledger_key])
    inventory = cast(ChangeImpactInventoryV1, sources[INVENTORY.ledger_key])
    policy = cast(_MappingDocument, sources[POLICY.ledger_key]).root
    catalog = cast(_MappingDocument, sources[CATALOG.ledger_key]).root
    knowledge = cast(_MappingDocument, sources[KNOWLEDGE.ledger_key]).root
    expected_context = "explore/context.json"
    if advisory.context_ref != expected_context:
        raise ValueError("exploration context_ref does not match the current change")
    family_policy = TestFamilyPolicyV1.model_validate(policy.get("test_family_policy"))
    if family_policy != request.family_policy:
        raise ValueError("admitted family policy does not match authenticated policy")

    if catalog.get("typed_leafs") != list(request.capability_leafs):
        raise ValueError("capability catalog does not match capability_leafs")
    journey_keys = frozenset(journey_keys_from_document(knowledge))
    validate_inventory_closed_keys(inventory, journey_keys=journey_keys)
    admissible = frozenset(request.candidate_test_families)
    obligations = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset(request.capability_leafs),
        journey_keys=journey_keys,
        admissible_families=admissible,
    )
    basis = _exclusion_basis(project_root)
    if basis is not None:
        obligations = apply_scope_exclusions(
            obligations,
            candidate_families=admissible,
            policy_required_families=frozenset(request.family_policy.required),
            exclusion_basis=basis,
        )
    goal = PreparedQualityGoalV1(
        obligations_ref=request.exploration_ref,
        source_resource_digests=request.source_resource_digests,
        required_test_families=required_goal_families(obligations, admissible_families=admissible),
        metric_catalog=COVERAGE_GOAL_ORDER,
        coverage_policy=CoverageGoalPolicyV1.from_product_policy(policy),
        sufficiency_policy=SufficiencyPolicyV1.from_product_policy(policy),
    )
    return advisory, inventory, goal, obligations


def write_bound_exploration(
    write_root: Path,
    document: ExploreAdvisoryV1 | PreparedExploreV1,
    obligations: tuple[PreparedObligationV1, ...],
    current_ref: EvidenceArtifactRefV1,
) -> EvidenceArtifactRefV1:
    """Stage plan-bound keys for the Kernel to commit with the plan."""
    if not isinstance(document, PreparedExploreV1):
        return current_ref
    updated = document.model_copy(update={"minimum_required_coverage": obligations})
    staged = stage_json_artifact(write_root, current_ref.path, updated)
    if staged.digest == current_ref.digest:
        return current_ref
    return EvidenceArtifactRefV1.model_validate(staged.model_dump(mode="json"))


def resolve_plan_artifact(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
    write_root: Path,
    sources: Mapping[str, object] | None = None,
) -> ResolvePlanOutputV1:
    advisory, inventory, goal, obligations = prepare_quality_goal(
        request, project_root=project_root, sources=sources
    )
    exploration_ref = write_bound_exploration(
        write_root,
        advisory,
        obligations,
        request.exploration_ref,
    )
    goal = goal.model_copy(update={"obligations_ref": exploration_ref})
    plan = resolve_plan(
        request=request,
        proposed=derive_family_proposal(advisory.test_strategy),  # type: ignore[union-attr]
        quality_goal=goal,
        inventory=inventory,
        exploration_ref=exploration_ref,
    )
    preparation_refs = _preparation_refs(
        request,
        project_root=project_root,
        exploration_ref=exploration_ref,
        plan_ref=plan_artifact_ref(plan),
    )
    output = ResolvePlanOutputV1(
        plan=plan,
        plan_ref=plan_artifact_ref(plan),
        preparation_refs=preparation_refs,
    )
    stage_json_artifact(
        write_root,
        PREPARATION_REFS_PATH,
        PreparationRefsDocumentV1(preparation_refs=preparation_refs),
    )
    staged = stage_json_artifact(write_root, output.plan_ref.path, output.plan, trailing_newline=False)
    if staged.digest != output.plan_ref.digest:
        raise ValueError("staged resolved plan digest differs from plan_ref")
    return output


def _preparation_refs(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
    exploration_ref: EvidenceArtifactRefV1,
    plan_ref: EvidenceArtifactRefV1,
) -> tuple[EvidenceArtifactRefV1, ...]:
    """Filtered caller artifacts, on-disk intake/explore outputs, then the rewritten plan files."""
    collected: list[EvidenceArtifactRefV1] = [
        ref for ref in request.artifacts if "/cases/" not in ref.path and "/review/" not in ref.path
    ]
    for path in _ON_DISK_PREPARATION:
        try:
            data = read_workspace_file(project_root, path)
        except ArtifactReadError as error:
            if error.reason != "missing":
                raise InputError(f"invalid preparation file {path}: {error}") from error
            continue
        collected.append(EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(data).hexdigest()))
    collected.extend((request.impact_inventory_ref, exploration_ref, plan_ref))
    by_path: dict[str, EvidenceArtifactRefV1] = {}
    for ref in collected:
        by_path[ref.path] = ref
    return tuple(by_path[path] for path in sorted(by_path))


__all__ = [
    "prepare_quality_goal",
    "RESOLVE_DEPENDS",
    "resolve_plan_artifact",
]
