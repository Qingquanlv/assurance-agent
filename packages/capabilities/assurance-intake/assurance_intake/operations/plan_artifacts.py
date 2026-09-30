"""Authenticated source loading and quality-goal materialization for plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.explore import (
    ExploreAdvisoryV1,
    PreparedExploreV1,
    RUN_SPEC_SNAPSHOT_PATH,
    TestStrategyV1,
)
from assurance_intake.contracts.obligations import SourceRefV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.obligations import (
    apply_scope_exclusions,
    journey_keys_from_document,
    normalize_goal_obligations,
    required_goal_families,
)
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.operations.impact_validation import validate_inventory_closed_keys
from assurance_intake.contracts.plan import (
    PreparedQualityGoalV1,
    ResolvePlanInputV1,
    ResolvePlanOutputV1,
    TestFamilyPolicyV1,
    plan_artifact_ref,
    plan_bytes,
)
from assurance_intake.contracts.obligations import PreparedObligationV1
from assurance_intake.contracts.quality_goals import (
    COVERAGE_GOAL_ORDER,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_intake.operations.resolve_plan import derive_family_proposal, resolve_plan
from assurance_intake.domain.explore_context import load_exploration_document

_RESOURCE_PATHS = {
    "assurance.product.configuration.capability-catalog": ".aa/capability-catalog.json",
    "assurance.product.configuration.data-knowledge": ".aa/data-knowledge.yaml",
}
_POLICY_PATH = ".aa/policy.yaml"


def _read_regular_bytes(root: Path, relative: str, expected_digest: str) -> bytes:
    candidate = root.joinpath(*relative.split("/"))
    if candidate.is_symlink():
        raise ValueError(f"source must not be a symlink: {relative}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        before = resolved.stat()
    except (OSError, ValueError) as error:
        raise ValueError(f"source is unavailable or outside the project: {relative}") from error
    if not resolved.is_file() or before.st_nlink != 1:
        raise ValueError(f"source must be one regular file: {relative}")
    data = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError(f"source changed while being read: {relative}")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError(f"source digest does not match: {relative}")
    return data


def _mapping_yaml(data: bytes, label: str) -> Mapping[str, object]:
    try:
        value = yaml.safe_load(data)
    except yaml.YAMLError as error:
        raise ValueError(f"{label} is invalid YAML") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _load_exploration(data: bytes) -> tuple[str, str, TestStrategyV1, ExploreAdvisoryV1 | PreparedExploreV1]:
    document = load_exploration_document(data)
    return document.change_id, document.context_ref, document.test_strategy, document


def _exclusion_basis(project_root: Path) -> SourceRefV1 | None:
    path = project_root.joinpath(*RUN_SPEC_SNAPSHOT_PATH.split("/"))
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SourceRefV1(
        kind="decision",
        artifact=EvidenceArtifactRefV1(path=RUN_SPEC_SNAPSHOT_PATH, digest=digest),
        locator="/candidate_test_families",
    )


def prepare_quality_goal(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
) -> tuple[
    ExploreAdvisoryV1 | PreparedExploreV1,
    ChangeImpactInventoryV1,
    PreparedQualityGoalV1,
    tuple[PreparedObligationV1, ...],
]:
    exploration_data = _read_regular_bytes(
        project_root,
        request.exploration_ref.path,
        request.exploration_ref.digest,
    )
    try:
        change_id, context_ref, _strategy, advisory = _load_exploration(exploration_data)
    except ValueError as error:
        raise ValueError("exploration artifact is invalid") from error
    if change_id != request.change_id:
        raise ValueError("exploration change_id does not match plan input")
    expected_context = "explore/context.json"
    if context_ref != expected_context:
        raise ValueError("exploration context_ref does not match the current change")

    inventory_data = _read_regular_bytes(
        project_root,
        request.impact_inventory_ref.path,
        request.impact_inventory_ref.digest,
    )
    try:
        inventory = ChangeImpactInventoryV1.model_validate(json.loads(inventory_data))
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("impact inventory artifact is invalid") from error
    if inventory.change_id != request.change_id:
        raise ValueError("impact inventory change_id does not match plan input")

    policy = _mapping_yaml(
        _read_regular_bytes(project_root, _POLICY_PATH, request.policy_digest),
        "product policy",
    )
    family_policy = TestFamilyPolicyV1.model_validate(policy.get("test_family_policy"))
    if family_policy != request.family_policy:
        raise ValueError("admitted family policy does not match authenticated policy")

    source_bytes: dict[str, bytes] = {}
    for resource_id, digest in request.source_resource_digests:
        try:
            relative = _RESOURCE_PATHS[resource_id]
        except KeyError as error:
            raise ValueError(f"unsupported goal source resource: {resource_id}") from error
        source_bytes[resource_id] = _read_regular_bytes(project_root, relative, digest)
    if set(source_bytes) != set(_RESOURCE_PATHS):
        raise ValueError("goal sources must contain catalog and data knowledge")

    try:
        catalog = json.loads(source_bytes["assurance.product.configuration.capability-catalog"])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("capability catalog is invalid JSON") from error
    if not isinstance(catalog, Mapping) or catalog.get("typed_leafs") != list(request.capability_leafs):
        raise ValueError("capability catalog does not match capability_leafs")
    knowledge = _mapping_yaml(
        source_bytes["assurance.product.configuration.data-knowledge"],
        "data knowledge",
    )
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
    data = canonical_json_bytes(cast(JSONValue, updated.model_dump(mode="json"))) + b"\n"
    digest = hashlib.sha256(data).hexdigest()
    if digest == current_ref.digest:
        return current_ref
    _write_artifact(write_root, current_ref.path, data)
    return EvidenceArtifactRefV1(path=current_ref.path, digest=digest)


def _write_artifact(write_root: Path, relative: str, data: bytes) -> None:
    destination = write_root.joinpath(*relative.split("/"))
    resolved_root = write_root.resolve(strict=True)
    try:
        destination.resolve(strict=False).relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("artifact output path escapes write_root") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or destination.read_bytes() != data:
            raise ValueError("artifact output already exists with different bytes")
        return
    destination.write_bytes(data)


def resolve_plan_artifact(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
    write_root: Path,
) -> ResolvePlanOutputV1:
    advisory, inventory, goal, obligations = prepare_quality_goal(request, project_root=project_root)
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
    output = ResolvePlanOutputV1(plan=plan, plan_ref=plan_artifact_ref(plan))
    _write_artifact(write_root, output.plan_ref.path, plan_bytes(output.plan))
    return output


class ResolvePlanHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            validated = ResolvePlanInputV1.model_validate(request.input)
            output = resolve_plan_artifact(
                validated,
                project_root=context.project_root,
                write_root=context.write_root,
            )
        except (ValueError, ValidationError, OSError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=True)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


__all__ = [
    "ResolvePlanHandler",
    "prepare_quality_goal",
    "resolve_plan_artifact",
]
