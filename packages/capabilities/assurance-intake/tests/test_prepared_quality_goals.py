from __future__ import annotations

import hashlib
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.plan import LoadPlanInputV1, ResolvePlanInputV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.quality_goals import (
    PreparedObligationV1,
    normalize_goal_obligations,
    required_goal_families,
)
from assurance_intake.operations.plan_artifacts import (
    LoadPlanHandler,
    ResolvePlanHandler,
    prepare_quality_goal,
)
from tests.product.test_change_local_output_routing import execute_task


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _advisory(
    mrc: dict[str, object],
    *,
    recommended: tuple[str, ...] = ("API", "E2E"),
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-1",
        "context_ref": "explore/context.json",
        "generated_at": "2026-09-05T00:00:00Z",
        "executive_summary": "Checkout changes",
        "watchlist": [],
        "evidence_inventory": {"available": [], "missing": [], "not_inspected": []},
        "source_code_evidence": [],
        "case_design_guidance": {
            "priority_hints": [],
            "suggested_scenarios": [],
            "regression_focus": [],
        },
        "minimum_required_coverage": mrc,
        "open_questions_for_case_design": [],
        "test_strategy": {
            "scope": {"in_scope": ["checkout"], "out_of_scope": []},
            "data_focus": [],
            "depth": "core",
            "layer_recommendation": [
                {
                    "layer": layer,
                    "recommended": layer in recommended,
                    "rationale": "Required by the change",
                    "evidence_ids": [],
                }
                for layer in ("API", "E2E", "Fuzz", "Performance")
            ],
        },
    }


def test_required_goal_families_are_derived_before_the_proposal() -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            {
                "negative": [
                    {
                        "id": "MRC-NEGATIVE-001",
                        "key": "entities.item.constraints.name",
                        "category": "negative",
                        "required": True,
                        "layer": "both",
                    }
                ],
                "e2e": ["checkout"],
            }
        )
    )
    rows = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset({"entities.item.constraints.name"}),
        journey_keys=frozenset({"checkout"}),
    )
    assert rows == (
        PreparedObligationV1(
            mrc_id="MRC-E2E-001",
            key="checkout",
            category="e2e",
            required=True,
            layer="e2e",
        ),
        PreparedObligationV1(
            mrc_id="MRC-NEGATIVE-001",
            key="entities.item.constraints.name",
            category="negative",
            required=True,
            layer="both",
        ),
    )
    assert required_goal_families(rows) == ("api", "e2e")


def test_empty_legacy_condition_does_not_make_resolved_obligations_unresolved() -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            {
                "api": ["create_item"],
                "e2e_if_enabled": [],
                "negative": ["entities.item.constraints.name"],
                "data_integrity": ["entities.item.constraints.parent"],
            },
            recommended=("API",),
        )
    )
    rows = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset({"entities.item.constraints.name", "entities.item.constraints.parent"}),
        journey_keys=frozenset(),
    )
    assert [(row.key, row.layer, row.required) for row in rows] == [
        ("create_item", "api", True),
        ("entities.item.constraints.parent", "api", True),
        ("entities.item.constraints.name", "api", True),
    ]
    assert required_goal_families(rows) == ("api",)


@pytest.mark.parametrize(
    ("mrc", "message"),
    [
        ({"negative": ["entities.item.constraints.unknown"]}, "unknown"),
        ({"e2e_if_enabled": ["checkout"]}, "unresolved"),
        (
            {
                "api": [{"id": "MRC-1", "key": "create", "category": "e2e"}],
            },
            "category",
        ),
    ],
)
def test_goal_normalization_fails_closed(mrc: dict[str, object], message: str) -> None:
    advisory = ExploreAdvisoryV1.model_validate(_advisory(mrc))
    with pytest.raises(ValueError, match=message):
        normalize_goal_obligations(
            advisory,
            capability_leafs=frozenset({"entities.item.constraints.name"}),
            journey_keys=frozenset({"checkout"}),
        )


@pytest.mark.parametrize("empty_legacy_category", [False, True])
def test_prepare_quality_goal_authenticates_every_source(
    tmp_path: Path,
    empty_legacy_category: bool,
) -> None:
    project = tmp_path
    explore_path = project / "qa/results/explore/exploration.json"
    explore_path.parent.mkdir(parents=True)
    mrc: dict[str, object] = {
        "api": ["create_item"],
        "e2e": ["checkout"],
        "negative": ["entities.item.constraints.name"],
        "data_integrity": ["entities.item.constraints.parent"],
    }
    if empty_legacy_category:
        mrc["e2e_if_enabled"] = []
    explore_bytes = json.dumps(
        _advisory(mrc, recommended=("API",)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    explore_path.write_bytes(explore_bytes)

    aa = project / ".aa"
    aa.mkdir()
    policy_bytes = yaml.safe_dump(
        {
            "schema_version": "1",
            "test_family_policy": {"required": [], "allowed": ["api", "e2e"]},
            "coverage_floor_by_tier": {
                "low": 0.7,
                "medium": 0.8,
                "high": 0.9,
                "critical": 1.0,
            },
            "evidence_sufficiency": {"recency_hours": 24, "require_current_batch": True},
        },
        sort_keys=True,
    ).encode()
    knowledge_bytes = yaml.safe_dump(
        {"schema_version": "1", "journeys": ["checkout"]},
        sort_keys=True,
    ).encode()
    catalog_bytes = json.dumps(
        {
            "schema_version": "1",
            "typed_leafs": ["entities.item.constraints.name", "entities.item.constraints.parent"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (aa / "policy.yaml").write_bytes(policy_bytes)
    (aa / "data-knowledge.yaml").write_bytes(knowledge_bytes)
    (aa / "capability-catalog.json").write_bytes(catalog_bytes)

    source_digests = (
        ("assurance.product.configuration.capability-catalog", _sha(catalog_bytes)),
        ("assurance.product.configuration.data-knowledge", _sha(knowledge_bytes)),
    )
    request = ResolvePlanInputV1.model_validate(
        {
            "change_id": "CH-1",
            "requirement_digest": "a" * 64,
            "candidate_test_families": ("api", "e2e"),
            "budgets": {
                "review_rounds": 1,
                "coverage_rounds": 2,
                "healing_rounds": 1,
                "execution_retries": 0,
            },
            "policy_resource_id": "assurance.product.configuration.product-policy",
            "policy_digest": _sha(policy_bytes),
            "family_policy": {"required": (), "allowed": ("api", "e2e")},
            "exploration_ref": {
                "path": explore_path.relative_to(project).as_posix(),
                "digest": _sha(explore_bytes),
            },
            "source_resource_digests": source_digests,
            "capability_leafs": (
                "entities.item.constraints.name",
                "entities.item.constraints.parent",
            ),
        }
    )

    advisory, goal = prepare_quality_goal(request, project_root=project)
    assert advisory.change_id == "CH-1"
    assert advisory.test_strategy.layer_recommendation[1].recommended is False
    assert goal.required_test_families == ("api", "e2e")
    assert goal.source_resource_digests == source_digests
    assert goal.coverage_policy.coverage_floor_by_tier.critical == 1.0

    resolve_stage = project / "resolve-stage"
    resolve_stage.mkdir()
    resolved = asyncio.run(
        execute_task(
            ResolvePlanHandler(),
            request.model_dump(mode="json"),
            workspace=project,
            write_root=resolve_stage,
            capability_id="assurance.intake.resolve-plan",
        )
    )
    assert resolved.outcome.status == "succeeded"
    output = resolved.outcome.output
    assert isinstance(output, dict)
    plan = output["plan"]
    assert isinstance(plan, dict)
    assert plan["proposed_test_families"] == ["api"]
    assert plan["selected_test_families"] == ["api", "e2e"]
    quality_goal = plan["quality_goal"]
    assert isinstance(quality_goal, dict)
    assert quality_goal["required_test_families"] == ["api", "e2e"]
    plan_ref = output["plan_ref"]
    assert isinstance(plan_ref, dict)
    relative = plan_ref["path"]
    assert isinstance(relative, str)
    staged_plan = resolve_stage / relative
    assert staged_plan.is_file()
    committed_plan = project / relative
    committed_plan.parent.mkdir(parents=True)
    committed_plan.write_bytes(staged_plan.read_bytes())

    load_input = LoadPlanInputV1(
        change_id=request.change_id,
        requirement_digest=request.requirement_digest,
        resolved_plan_ref=EvidenceArtifactRefV1.model_validate(plan_ref),
        budgets=request.budgets,
        policy_resource_id=request.policy_resource_id,
        policy_digest=request.policy_digest,
        source_resource_digests=request.source_resource_digests,
        capability_leafs=request.capability_leafs,
    )
    load_stage = project / "load-stage"
    load_stage.mkdir()
    loaded = asyncio.run(
        execute_task(
            LoadPlanHandler(),
            load_input.model_dump(mode="json"),
            workspace=project,
            write_root=load_stage,
            capability_id="assurance.intake.load-plan",
        )
    )
    assert loaded.outcome.status == "succeeded"
    assert loaded.outcome.output == output
    assert list(load_stage.rglob("*")) == []

    (aa / "policy.yaml").write_bytes(policy_bytes + b"\n")
    with pytest.raises(ValueError, match="digest"):
        prepare_quality_goal(request, project_root=project)
