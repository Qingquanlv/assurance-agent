"""Authenticated source loading and quality-goal materialization for plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.plan import (
    PreparedQualityGoalV1,
    ResolvePlanInputV1,
    LoadPlanInputV1,
    ResolvePlanOutputV1,
    TestFamilyPolicyV1,
    decode_plan,
    plan_artifact_ref,
    plan_bytes,
)
from assurance_intake.contracts.quality_goals import (
    COVERAGE_GOAL_ORDER,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
    journey_keys_from_document,
    normalize_goal_obligations,
    required_goal_families,
)
from assurance_intake.operations.resolve_plan import derive_family_proposal, resolve_plan

_RESOURCE_PATHS = {
    "assurance.product.configuration.capability-catalog": ".aa/capability-catalog.json",
    "assurance.product.configuration.data-knowledge": ".aa/data-knowledge.yaml",
}
_POLICY_PATH = ".aa/policy.yaml"
_VERIFICATION_POLICY_PATH = ".aa/verification-policy.yaml"


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


def prepare_quality_goal(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
) -> tuple[ExploreAdvisoryV1, PreparedQualityGoalV1]:
    exploration_data = _read_regular_bytes(
        project_root,
        request.exploration_ref.path,
        request.exploration_ref.digest,
    )
    try:
        advisory = ExploreAdvisoryV1.model_validate(json.loads(exploration_data))
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("exploration artifact is invalid") from error
    if advisory.change_id != request.change_id:
        raise ValueError("exploration change_id does not match plan input")
    expected_context = "explore/context.json"
    if advisory.context_ref != expected_context:
        raise ValueError("exploration context_ref does not match the current change")

    policy = _mapping_yaml(
        _read_regular_bytes(project_root, _POLICY_PATH, request.policy_digest),
        "product policy",
    )
    family_policy = TestFamilyPolicyV1.model_validate(policy.get("test_family_policy"))
    if family_policy != request.family_policy:
        raise ValueError("admitted family policy does not match authenticated policy")

    if request.verification_policy is not None:
        verification = _mapping_yaml(
            _read_regular_bytes(
                project_root,
                _VERIFICATION_POLICY_PATH,
                request.verification_policy.digest,
            ),
            "verification policy",
        )
        if verification.get("validation_profile") != request.verification_policy.validation_profile:
            raise ValueError("validation profile does not match authenticated verification policy")

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
    obligations = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset(request.capability_leafs),
        journey_keys=frozenset(journey_keys_from_document(knowledge)),
    )
    goal = PreparedQualityGoalV1(
        obligations_ref=request.exploration_ref,
        source_resource_digests=request.source_resource_digests,
        required_test_families=required_goal_families(obligations),
        metric_catalog=COVERAGE_GOAL_ORDER,
        coverage_policy=CoverageGoalPolicyV1.from_product_policy(policy),
        sufficiency_policy=SufficiencyPolicyV1.from_product_policy(policy),
    )
    return advisory, goal


def _write_plan(output: ResolvePlanOutputV1, write_root: Path) -> None:
    destination = write_root.joinpath(*output.plan_ref.path.split("/"))
    resolved_root = write_root.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.resolve(strict=False).relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("plan output path escapes write_root") from error
    data = plan_bytes(output.plan)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != data:
            raise ValueError("plan output already exists with different bytes")
        return
    destination.write_bytes(data)


def resolve_plan_artifact(
    request: ResolvePlanInputV1,
    *,
    project_root: Path,
    write_root: Path,
) -> ResolvePlanOutputV1:
    advisory, goal = prepare_quality_goal(request, project_root=project_root)
    plan = resolve_plan(
        request=request,
        proposed=derive_family_proposal(advisory.test_strategy),
        quality_goal=goal,
    )
    output = ResolvePlanOutputV1(plan=plan, plan_ref=plan_artifact_ref(plan))
    _write_plan(output, write_root)
    return output


def load_plan_artifact(
    request: LoadPlanInputV1,
    *,
    project_root: Path,
) -> ResolvePlanOutputV1:
    data = _read_regular_bytes(
        project_root,
        request.resolved_plan_ref.path,
        request.resolved_plan_ref.digest,
    )
    plan = decode_plan(data, request.resolved_plan_ref)
    if plan.change_id != request.change_id:
        raise ValueError("plan change_id does not match loader input")
    if plan.requirement_digest != request.requirement_digest:
        raise ValueError("plan requirement_digest does not match loader input")
    if plan.resolved_budgets != request.budgets:
        raise ValueError("plan budgets do not match loader input")
    if plan.policy_resource_id != request.policy_resource_id or plan.policy_digest != request.policy_digest:
        raise ValueError("plan policy identity does not match loader input")
    if plan.quality_goal.source_resource_digests != request.source_resource_digests:
        raise ValueError("plan source identities do not match loader input")
    if plan.verification_policy != request.verification_policy:
        raise ValueError("plan verification policy identity does not match loader input")

    policy = _mapping_yaml(
        _read_regular_bytes(project_root, _POLICY_PATH, request.policy_digest),
        "product policy",
    )
    family_policy = TestFamilyPolicyV1.model_validate(policy.get("test_family_policy"))
    resolve_input = ResolvePlanInputV1(
        change_id=request.change_id,
        requirement_digest=request.requirement_digest,
        candidate_test_families=plan.candidate_test_families,
        budgets=request.budgets,
        policy_resource_id=request.policy_resource_id,
        policy_digest=request.policy_digest,
        family_policy=family_policy,
        exploration_ref=plan.exploration_ref,
        source_resource_digests=request.source_resource_digests,
        capability_leafs=request.capability_leafs,
        verification_policy=request.verification_policy,
    )
    advisory, goal = prepare_quality_goal(resolve_input, project_root=project_root)
    expected = resolve_plan(
        request=resolve_input,
        proposed=derive_family_proposal(advisory.test_strategy),
        quality_goal=goal,
    )
    if expected != plan:
        raise ValueError("stored plan does not match its authenticated inputs")
    return ResolvePlanOutputV1(plan=plan, plan_ref=request.resolved_plan_ref)


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
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


class LoadPlanHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            validated = LoadPlanInputV1.model_validate(request.input)
            output = load_plan_artifact(validated, project_root=context.project_root)
        except (ValueError, ValidationError, OSError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


__all__ = [
    "LoadPlanHandler",
    "ResolvePlanHandler",
    "load_plan_artifact",
    "prepare_quality_goal",
    "resolve_plan_artifact",
]
