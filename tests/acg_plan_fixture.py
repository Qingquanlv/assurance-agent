"""Small, real frozen-plan fixture shared by cross-capability tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_intake.contracts.common import TestFamily
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.plan import (
    BusinessVerificationPolicyV1,
    PlanBudgetsV1,
    ResolvePlanInputV1,
    ResolvedAssurancePlan,
    TestFamilyPolicyV1,
    plan_artifact_ref,
    plan_bytes,
)
from assurance_intake.operations.plan_artifacts import prepare_quality_goal
from assurance_intake.operations.resolve_plan import resolve_plan


DEFAULT_POLICY: dict[str, object] = {
    "coverage_floor_by_tier": {
        "low": 0.7,
        "medium": 0.8,
        "high": 0.9,
        "critical": 1.0,
    },
    "evidence_sufficiency": {
        "recency_hours": 24,
        "require_current_batch": True,
    },
    "test_family_policy": {
        "required": [],
        "allowed": ["api", "e2e", "fuzz", "performance"],
    },
}


def _write(root: Path, relative: str, data: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def install_plan(
    root: Path,
    change_id: str,
    *,
    capability_leafs: tuple[str, ...] = ("entities.item.constraints.name",),
    journeys: tuple[str, ...] = (),
    minimum_required_coverage: Mapping[str, object] | None = None,
    candidates: tuple[TestFamily, ...] = ("api",),
    proposed: tuple[TestFamily, ...] = ("api",),
    policy: Mapping[str, object] = DEFAULT_POLICY,
    verification_policy: BusinessVerificationPolicyV1 | None = None,
) -> tuple[ResolvedAssurancePlan, dict[str, str]]:
    capability_leafs = tuple(sorted(set(capability_leafs)))
    policy_bytes = yaml.safe_dump(dict(policy), sort_keys=True).encode()
    policy_digest = _write(root, ".aa/policy.yaml", policy_bytes)
    catalog_bytes = canonical_json_bytes(cast(JSONValue, {"typed_leafs": list(capability_leafs)}))
    catalog_digest = _write(root, ".aa/capability-catalog.json", catalog_bytes)
    knowledge_bytes = yaml.safe_dump({"journeys": list(journeys)}, sort_keys=True).encode()
    knowledge_digest = _write(root, ".aa/data-knowledge.yaml", knowledge_bytes)
    if verification_policy is not None:
        verification_bytes = yaml.safe_dump(
            {"validation_profile": verification_policy.validation_profile},
            sort_keys=True,
        ).encode()
        verification_digest = _write(root, ".aa/verification-policy.yaml", verification_bytes)
        verification_policy = verification_policy.model_copy(update={"digest": verification_digest})

    coverage = (
        dict(minimum_required_coverage)
        if minimum_required_coverage is not None
        else {"api": [capability_leafs[0]]}
    )
    recommended = set(proposed)
    exploration = {
        "schema_version": "1",
        "change_id": change_id,
        "context_ref": "explore/context.json",
        "generated_at": "2026-09-05T00:00:00Z",
        "executive_summary": "test plan fixture",
        "watchlist": [],
        "evidence_inventory": {"available": [], "missing": [], "not_inspected": []},
        "source_code_evidence": [],
        "case_design_guidance": {
            "priority_hints": [],
            "suggested_scenarios": [],
            "regression_focus": [],
        },
        "minimum_required_coverage": coverage,
        "open_questions_for_case_design": [],
        "test_strategy": {
            "scope": {"in_scope": [], "out_of_scope": []},
            "data_focus": [],
            "depth": "core",
            "layer_recommendation": [
                {
                    "layer": label,
                    "recommended": family in recommended,
                    "rationale": f"{family} fixture rationale",
                    "evidence_ids": [],
                }
                for label, family in (
                    ("API", "api"),
                    ("E2E", "e2e"),
                    ("Fuzz", "fuzz"),
                    ("Performance", "performance"),
                )
            ],
        },
    }
    exploration_bytes = canonical_json_bytes(cast(JSONValue, exploration))
    exploration_path = f"qa/changes/{change_id}/explore/exploration.json"
    exploration_digest = _write(root, exploration_path, exploration_bytes)
    source_digests = (
        ("assurance.product.configuration.capability-catalog", catalog_digest),
        ("assurance.product.configuration.data-knowledge", knowledge_digest),
    )
    family_policy = TestFamilyPolicyV1.model_validate(policy["test_family_policy"])
    request = ResolvePlanInputV1(
        change_id=change_id,
        requirement_digest=hashlib.sha256(b"requirement").hexdigest(),
        candidate_test_families=candidates,
        budgets=PlanBudgetsV1(
            review_rounds=2,
            coverage_rounds=2,
            healing_rounds=1,
            execution_retries=1,
        ),
        policy_resource_id="assurance.product.configuration.product-policy",
        policy_digest=policy_digest,
        family_policy=family_policy,
        exploration_ref=EvidenceArtifactRefV1(
            path=exploration_path,
            digest=exploration_digest,
        ),
        source_resource_digests=source_digests,
        capability_leafs=capability_leafs,
        verification_policy=verification_policy,
    )
    advisory, quality_goal = prepare_quality_goal(request, project_root=root)
    selected = tuple(
        family
        for label, family in (
            ("API", "api"),
            ("E2E", "e2e"),
            ("Fuzz", "fuzz"),
            ("Performance", "performance"),
        )
        if any(row.layer == label and row.recommended for row in advisory.test_strategy.layer_recommendation)
    )
    plan = resolve_plan(
        request=request, proposed=cast(tuple[TestFamily, ...], selected), quality_goal=quality_goal
    )
    ref = plan_artifact_ref(plan)
    _write(root, ref.path, plan_bytes(plan))
    return plan, ref.model_dump(mode="json")


__all__ = ["DEFAULT_POLICY", "install_plan"]
