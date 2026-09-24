from __future__ import annotations

import hashlib
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from assurance_intake.contracts.explore import ExploreAdvisoryV1, PreparedExploreV1
from assurance_intake.contracts.plan import LoadPlanInputV1, ResolvePlanInputV1
from assurance_intake.contracts.quality_goals import normalize_obligation_drafts, required_goal_families
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.operations.obligations import normalize_goal_obligations
from assurance_intake.operations.plan_artifacts import (
    LoadPlanHandler,
    ResolvePlanHandler,
    prepare_quality_goal,
)
from tests.product.test_change_local_output_routing import execute_task


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _draft(
    *,
    draft_id: str,
    proposed_key: str | None,
    category: str,
    layer: str,
    statement: str | None = None,
) -> dict[str, object]:
    return {
        "draft_id": draft_id,
        "proposed_key": proposed_key,
        "category": category,
        "layer": layer,
        "statement": statement or f"{proposed_key} must hold",
        "applicability_conditions": [],
        "impact_row_ids": [],
        "proposed_profile_id": None,
        "prerequisites": [],
        "observation_goals": [],
        "basis_quotes": [],
        "open_questions": [],
    }


def _advisory(
    drafts: list[dict[str, object]],
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
        "minimum_required_coverage": drafts,
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


def test_required_goal_families_are_not_downgraded_by_candidate_set() -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            [
                _draft(draft_id="D-API", proposed_key="create_item", category="api", layer="api"),
                _draft(draft_id="D-E2E", proposed_key="checkout", category="e2e", layer="e2e"),
            ],
            recommended=("API",),
        )
    )
    rows = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset(),
        journey_keys=frozenset({"checkout"}),
        admissible_families=frozenset({"api"}),
    )
    assert [(row.key, row.required, row.layer) for row in rows] == [
        ("create_item", True, "api"),
        ("checkout", True, "e2e"),
    ]
    assert required_goal_families(rows, admissible_families=frozenset({"api"})) == ("api", "e2e")


def test_required_goal_families_are_derived_before_the_proposal() -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            [
                _draft(
                    draft_id="MRC-NEGATIVE-001",
                    proposed_key="entities.item.constraints.name",
                    category="negative",
                    layer="both",
                ),
                _draft(draft_id="MRC-E2E-001", proposed_key="checkout", category="e2e", layer="e2e"),
            ]
        )
    )
    rows = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset({"entities.item.constraints.name"}),
        journey_keys=frozenset({"checkout"}),
    )
    assert [(row.mrc_id, row.key, row.category, row.required, row.layer) for row in rows] == [
        ("MRC-E2E-001", "checkout", "e2e", True, "e2e"),
        ("MRC-NEGATIVE-001", "entities.item.constraints.name", "negative", True, "both"),
    ]
    assert required_goal_families(rows) == ("api", "e2e")


def test_empty_legacy_condition_does_not_make_resolved_obligations_unresolved() -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            [
                _draft(draft_id="D-API", proposed_key="create_item", category="api", layer="api"),
                _draft(
                    draft_id="D-NEG",
                    proposed_key="entities.item.constraints.name",
                    category="negative",
                    layer="api",
                ),
                _draft(
                    draft_id="D-DATA",
                    proposed_key="entities.item.constraints.parent",
                    category="data_integrity",
                    layer="api",
                ),
            ],
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
    ("drafts", "message"),
    [
        (
            [
                _draft(
                    draft_id="D-COND",
                    proposed_key="checkout",
                    category="e2e_if_enabled",
                    layer="e2e",
                )
            ],
            "unresolved",
        ),
    ],
)
def test_goal_normalization_fails_closed(drafts: list[dict[str, object]], message: str) -> None:
    advisory = ExploreAdvisoryV1.model_validate(_advisory(drafts))
    with pytest.raises(ValueError, match=message):
        normalize_goal_obligations(
            advisory,
            capability_leafs=frozenset({"entities.item.constraints.name"}),
            journey_keys=frozenset({"checkout"}),
        )


@pytest.mark.parametrize("category", ("negative", "data_integrity"))
def test_unresolved_closed_obligation_is_preserved_for_later_gate(category: str) -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            [
                _draft(
                    draft_id="MRC-NEGATIVE-009",
                    proposed_key=None,
                    category=category,
                    layer="api",
                    statement="Unauthorized or invalid reference must be rejected",
                )
            ]
        )
    )
    rows = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset({"entities.item.constraints.name"}),
        journey_keys=frozenset(),
    )
    assert [(row.mrc_id, row.key, row.proposed_key, row.required) for row in rows] == [
        ("MRC-NEGATIVE-009", None, None, True)
    ]
    assert required_goal_families(rows) == ("api",)


@pytest.mark.parametrize(
    ("category", "layer", "proposed_key"),
    [
        ("negative", "api", "departments.permissions.deny"),
        ("e2e", "e2e", "department-admin-journey"),
    ],
)
def test_unknown_proposed_capability_is_preserved_as_unresolved(
    category: str,
    layer: str,
    proposed_key: str,
) -> None:
    advisory = ExploreAdvisoryV1.model_validate(
        _advisory(
            [
                _draft(
                    draft_id="MRC-UNRESOLVED-001",
                    proposed_key=proposed_key,
                    category=category,
                    layer=layer,
                )
            ]
        )
    )

    rows = normalize_goal_obligations(
        advisory,
        capability_leafs=frozenset({"entities.item.constraints.name"}),
        journey_keys=frozenset({"checkout"}),
    )

    assert [(row.key, row.proposed_key, row.required) for row in rows] == [(None, proposed_key, True)]


@pytest.mark.parametrize("empty_legacy_category", [False, True])
def test_prepare_quality_goal_authenticates_every_source(
    tmp_path: Path,
    empty_legacy_category: bool,
) -> None:
    project = tmp_path
    explore_path = project / "qa/results/explore/exploration.json"
    explore_path.parent.mkdir(parents=True)
    drafts = [
        _draft(draft_id="D-API", proposed_key="create_item", category="api", layer="api"),
        _draft(draft_id="D-E2E", proposed_key="checkout", category="e2e", layer="e2e"),
        _draft(
            draft_id="D-NEG",
            proposed_key="entities.item.constraints.name",
            category="negative",
            layer="api",
        ),
        _draft(
            draft_id="D-DATA",
            proposed_key="entities.item.constraints.parent",
            category="data_integrity",
            layer="api",
        ),
        _draft(
            draft_id="MRC-NEGATIVE-009",
            proposed_key=None,
            category="negative",
            layer="api",
        ),
    ]
    del empty_legacy_category
    explore_bytes = json.dumps(
        _advisory(drafts, recommended=("API",)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    explore_path.write_bytes(explore_bytes)
    inventory_bytes = json.dumps(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "context_ref": "explore/context.json",
            "rows": [],
            "exclusions": [],
        }
    ).encode()
    inventory_path = project / "qa/results/explore/impact-inventory.json"
    inventory_path.write_bytes(inventory_bytes)

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
            "impact_inventory_ref": {
                "path": "qa/results/explore/impact-inventory.json",
                "digest": _sha(inventory_bytes),
            },
            "source_resource_digests": source_digests,
            "capability_leafs": (
                "entities.item.constraints.name",
                "entities.item.constraints.parent",
            ),
        }
    )

    advisory, _inventory, goal, _obligations = prepare_quality_goal(request, project_root=project)
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


def test_prepare_quality_goal_does_not_require_e2e_outside_the_candidate_set(
    tmp_path: Path,
) -> None:
    project = tmp_path
    explore_path = project / "qa/results/explore/exploration.json"
    explore_path.parent.mkdir(parents=True)
    explore_bytes = json.dumps(
        _advisory(
            [
                _draft(draft_id="D-API", proposed_key="create_item", category="api", layer="api"),
                _draft(draft_id="D-E2E", proposed_key="checkout", category="e2e", layer="e2e"),
            ],
            recommended=("API",),
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    explore_path.write_bytes(explore_bytes)
    inventory_bytes = json.dumps(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "context_ref": "explore/context.json",
            "rows": [
                {
                    "row_id": "IR-001",
                    "change_evidence_ids": ["CF-001"],
                    "affected_behavior": {"kind": "journey", "key": "checkout"},
                    "obligation": "the checkout journey stays visible",
                    "expected_basis_ids": [],
                    "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
                    "disposition": "add",
                    "gap_reason": None,
                    "confidence": "medium",
                }
            ],
            "exclusions": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    inventory_path = project / "qa/results/explore/impact-inventory.json"
    inventory_path.write_bytes(inventory_bytes)

    aa = project / ".aa"
    aa.mkdir()
    policy_bytes = yaml.safe_dump(
        {
            "schema_version": "1",
            "test_family_policy": {"required": ["api"], "allowed": ["api", "e2e"]},
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
        {"schema_version": "1", "typed_leafs": []},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (aa / "policy.yaml").write_bytes(policy_bytes)
    (aa / "data-knowledge.yaml").write_bytes(knowledge_bytes)
    (aa / "capability-catalog.json").write_bytes(catalog_bytes)

    request = ResolvePlanInputV1.model_validate(
        {
            "change_id": "CH-1",
            "requirement_digest": "a" * 64,
            "candidate_test_families": ("api",),
            "budgets": {
                "review_rounds": 1,
                "coverage_rounds": 2,
                "healing_rounds": 1,
                "execution_retries": 0,
            },
            "policy_resource_id": "assurance.product.configuration.product-policy",
            "policy_digest": _sha(policy_bytes),
            "family_policy": {"required": ("api",), "allowed": ("api", "e2e")},
            "exploration_ref": {
                "path": explore_path.relative_to(project).as_posix(),
                "digest": _sha(explore_bytes),
            },
            "impact_inventory_ref": {
                "path": "qa/results/explore/impact-inventory.json",
                "digest": _sha(inventory_bytes),
            },
            "source_resource_digests": (
                ("assurance.product.configuration.capability-catalog", _sha(catalog_bytes)),
                ("assurance.product.configuration.data-knowledge", _sha(knowledge_bytes)),
            ),
            "capability_leafs": (),
        }
    )

    _advisory_doc, _inventory, goal, _obligations = prepare_quality_goal(request, project_root=project)
    assert goal.required_test_families == ("api", "e2e")

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
    assert resolved.outcome.status == "failed"


def test_resolve_plan_writes_bound_keys_into_sealed_exploration(tmp_path: Path) -> None:
    project = tmp_path
    drafts = [
        _draft(draft_id="MRC-API-001", proposed_key="create_item", category="api", layer="api"),
        _draft(
            draft_id="MRC-NEGATIVE-001",
            proposed_key="entities.item.constraints.name",
            category="negative",
            layer="api",
        ),
        _draft(
            draft_id="MRC-DATA-001",
            proposed_key="entities.item.constraints.missing",
            category="data_integrity",
            layer="api",
        ),
    ]
    advisory = ExploreAdvisoryV1.model_validate(_advisory(drafts, recommended=("API",)))
    sealed = PreparedExploreV1(
        schema_version="1",
        change_id=advisory.change_id,
        context_ref=advisory.context_ref,
        generated_at=advisory.generated_at,
        executive_summary=advisory.executive_summary,
        watchlist=tuple(advisory.watchlist),
        evidence_inventory=advisory.evidence_inventory,
        source_code_evidence=tuple(advisory.source_code_evidence),
        case_design_guidance=advisory.case_design_guidance,
        minimum_required_coverage=normalize_obligation_drafts(
            advisory.minimum_required_coverage,
            resolved_quotes={},
        ),
        open_questions_for_case_design=tuple(advisory.open_questions_for_case_design),
        test_strategy=advisory.test_strategy,
    )
    assert all(row.key is None for row in sealed.minimum_required_coverage)
    explore_path = project / "qa/results/explore/exploration.json"
    explore_path.parent.mkdir(parents=True)
    explore_bytes = json.dumps(sealed.model_dump(mode="json")).encode()
    explore_path.write_bytes(explore_bytes)
    inventory_bytes = json.dumps(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "context_ref": "explore/context.json",
            "rows": [],
            "exclusions": [],
        }
    ).encode()
    (project / "qa/results/explore/impact-inventory.json").write_bytes(inventory_bytes)
    aa = project / ".aa"
    aa.mkdir()
    policy_bytes = yaml.safe_dump(
        {
            "schema_version": "1",
            "test_family_policy": {"required": [], "allowed": ["api"]},
            "coverage_floor_by_tier": {"low": 0.7, "medium": 0.8, "high": 0.9, "critical": 1.0},
            "evidence_sufficiency": {"recency_hours": 24, "require_current_batch": True},
        },
        sort_keys=True,
    ).encode()
    knowledge_bytes = yaml.safe_dump({"schema_version": "1", "journeys": []}, sort_keys=True).encode()
    catalog_bytes = json.dumps(
        {"schema_version": "1", "typed_leafs": ["entities.item.constraints.name"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (aa / "policy.yaml").write_bytes(policy_bytes)
    (aa / "data-knowledge.yaml").write_bytes(knowledge_bytes)
    (aa / "capability-catalog.json").write_bytes(catalog_bytes)
    request = ResolvePlanInputV1.model_validate(
        {
            "change_id": "CH-1",
            "requirement_digest": "a" * 64,
            "candidate_test_families": ("api",),
            "budgets": {
                "review_rounds": 1,
                "coverage_rounds": 2,
                "healing_rounds": 1,
                "execution_retries": 0,
            },
            "policy_resource_id": "assurance.product.configuration.product-policy",
            "policy_digest": _sha(policy_bytes),
            "family_policy": {"required": (), "allowed": ("api",)},
            "exploration_ref": {
                "path": "qa/results/explore/exploration.json",
                "digest": _sha(explore_bytes),
            },
            "impact_inventory_ref": {
                "path": "qa/results/explore/impact-inventory.json",
                "digest": _sha(inventory_bytes),
            },
            "source_resource_digests": (
                ("assurance.product.configuration.capability-catalog", _sha(catalog_bytes)),
                ("assurance.product.configuration.data-knowledge", _sha(knowledge_bytes)),
            ),
            "capability_leafs": ("entities.item.constraints.name",),
        }
    )
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
    assert resolved.outcome.status == "succeeded", resolved.outcome.failure
    written = json.loads(explore_path.read_bytes())
    rows = {row["mrc_id"]: row for row in written["minimum_required_coverage"]}
    assert rows["MRC-API-001"]["key"] == "create_item"
    assert rows["MRC-API-001"]["proposed_key"] == "create_item"
    assert rows["MRC-NEGATIVE-001"]["key"] == "entities.item.constraints.name"
    assert rows["MRC-DATA-001"]["key"] is None
    assert rows["MRC-DATA-001"]["proposed_key"] == "entities.item.constraints.missing"
    output = resolved.outcome.output
    assert isinstance(output, dict)
    plan = output["plan"]
    assert isinstance(plan, dict)
    exploration_ref = plan["exploration_ref"]
    assert isinstance(exploration_ref, dict)
    assert exploration_ref["digest"] == _sha(explore_path.read_bytes())
    quality_goal = plan["quality_goal"]
    assert isinstance(quality_goal, dict)
    assert quality_goal["obligations_ref"] == exploration_ref
