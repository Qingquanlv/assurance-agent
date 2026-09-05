"""Authenticated source loading and quality-goal materialization for plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml

from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.plan import (
    PreparedQualityGoalV1,
    ResolvePlanInputV1,
    TestFamilyPolicyV1,
)
from assurance_intake.contracts.quality_goals import (
    COVERAGE_GOAL_ORDER,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
    journey_keys_from_document,
    normalize_goal_obligations,
    required_goal_families,
)

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
    expected_context = f"qa/changes/{request.change_id}/explore/context.json"
    if advisory.context_ref != expected_context:
        raise ValueError("exploration context_ref does not match the current change")

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
        catalog = json.loads(
            source_bytes["assurance.product.configuration.capability-catalog"]
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("capability catalog is invalid JSON") from error
    if not isinstance(catalog, Mapping) or catalog.get("typed_leafs") != list(
        request.capability_leafs
    ):
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


__all__ = ["prepare_quality_goal"]
