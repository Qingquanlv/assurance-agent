"""Owner-specific mutations against the canonical assurance observing harness."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS
from assurance_agent.artifacts.models.plan_checks import CheckEvidence, LayerApplicability, PlanCheckDocument
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from tests.helpers_assurance_contract import (
    BoundaryObservation,
    CanonicalAssuranceBundle,
    boundary_map,
    check_context_for_bundle,
    load_canonical_assurance_bundle,
    non_owner_boundaries_equal,
    observe_assurance_contract,
)


def _canonical(tmp_path: Path, layer: str = "api"):
    bundle = load_canonical_assurance_bundle(layer)
    return bundle, observe_assurance_contract(bundle, tmp_path=tmp_path / "canonical")


def _rejecting(observation) -> BoundaryObservation:
    rejected = [item for item in observation.boundaries if item.status == "reject"]
    assert len(rejected) == 1, observation.boundaries
    return rejected[0]


def test_missing_skill_input_owned_by_skill_contract(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    skill = tmp_path / "missing-input" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        """## Inputs

### required

- `change:plans/api-plan.md`
- `change:plans/does-not-exist.md`
- `repo:.aa/data-knowledge.yaml`

## Outputs

### required

- `change:codegen/api-codegen-summary.md`

## State Authority

- `owner: graph_ledger`
- `agent_state_writes: forbidden`
""",
        encoding="utf-8",
    )
    mutated = observe_assurance_contract(bundle, tmp_path=tmp_path / "mut", skill_path=skill)
    owner = _rejecting(mutated)
    assert owner.boundary == "skill_contract"
    assert owner.code == "required_input_missing"
    assert owner.locator == "change:plans/does-not-exist.md"
    assert non_owner_boundaries_equal(mutated, canonical, owner="skill_contract")


def test_undeclared_read_owned_by_workspace_read(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        attempt_undeclared_read="change:review/sibling-layer-review.json",
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "workspace_read"
    assert owner.code == "undeclared_read"
    assert owner.locator == "change:review/sibling-layer-review.json"
    assert non_owner_boundaries_equal(mutated, canonical, owner="workspace_read")


def test_forbidden_write_owned_by_workspace_write(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        attempt_forbidden_write="repo:src/product.py",
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "workspace_write"
    assert owner.code == "forbidden_write"
    assert owner.locator == "repo:src/product.py"
    assert non_owner_boundaries_equal(mutated, canonical, owner="workspace_write")


def test_wrong_review_layer_owned_by_runtime_ingest(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    payload = json.loads(bundle.review_bytes.decode("utf-8"))
    payload["review_type"] = "e2e-plan"
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        review_bytes=json.dumps(payload).encode("utf-8"),
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "runtime_ingest"
    assert owner.code == "wrong_review_layer"
    assert owner.locator == get_layer_assurance_profile("api").review_artifact
    assert non_owner_boundaries_equal(mutated, canonical, owner="runtime_ingest")


def test_wrong_review_change_owned_by_runtime_ingest(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    payload = json.loads(bundle.review_bytes.decode("utf-8"))
    payload["change_id"] = "CH-OTHER"
    # Keep bundle.change_id as CH-CANONICAL so the freeze identity check fails.
    mutated = observe_assurance_contract(
        replace(bundle, change_id="CH-CANONICAL"),
        tmp_path=tmp_path / "mut",
        review_bytes=json.dumps(payload).encode("utf-8"),
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "runtime_ingest"
    assert owner.code == "wrong_review_change"
    assert non_owner_boundaries_equal(mutated, canonical, owner="runtime_ingest")


def test_stale_checks_owned_by_wire(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    fresh = run_plan_checks(check_context_for_bundle(bundle))
    stale_checks = []
    for item in fresh.checks:
        if item.check_id == "l1_path":
            stale_checks.append(
                CheckEvidence(
                    check_id=item.check_id,
                    status=item.status,
                    findings=item.findings,
                    refs=tuple(sorted({*item.refs, "plans/stale-ref.md"})),
                    applicability_reason=item.applicability_reason,
                )
            )
        else:
            stale_checks.append(item)
    assert fresh.layer is not None
    assert fresh.applicability is not None
    stale = PlanCheckDocument.from_checks(
        layer=fresh.layer,
        applicability=fresh.applicability,
        checks=tuple(stale_checks),
    )
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        checks_override=stale,
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "wire"
    assert owner.code == "stale_checks"
    assert owner.locator == get_layer_assurance_profile("api").checks_artifact
    assert non_owner_boundaries_equal(mutated, canonical, owner="wire")


def test_malformed_na_owned_by_mechanical(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "fuzz")
    fresh = run_plan_checks(check_context_for_bundle(bundle))
    malformed_checks = []
    for item in fresh.checks:
        if item.check_id == "assert_ideal":
            malformed_checks.append(CheckEvidence(check_id="assert_ideal", status="pass"))
        else:
            malformed_checks.append(item)
    assert isinstance(fresh.applicability, LayerApplicability)
    malformed = PlanCheckDocument.model_construct(
        schema_version="2",
        layer="fuzz",
        applicability=fresh.applicability,
        status="pass",
        checks=tuple(malformed_checks),
    )
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        malformed_checks=malformed,
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "mechanical"
    assert owner.code == "malformed_na"
    assert non_owner_boundaries_equal(mutated, canonical, owner="mechanical")


def test_missing_capability_owned_by_mechanical(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        required_capabilities=(
            *bundle.required_capabilities,
            "capabilities.adapters.api.account.missing_leaf",
        ),
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "mechanical"
    assert owner.code == "capability_keys"
    assert non_owner_boundaries_equal(mutated, canonical, owner="mechanical")


def test_wrong_summary_manifest_layer_owned_by_codegen_workspace(tmp_path: Path) -> None:
    bundle, canonical = _canonical(tmp_path, "api")
    mutated = observe_assurance_contract(
        bundle,
        tmp_path=tmp_path / "mut",
        manifest_layer="e2e",
    )
    owner = _rejecting(mutated)
    assert owner.boundary == "codegen_workspace"
    assert owner.code == "wrong_summary_manifest_layer"
    assert owner.locator == "change:codegen/api-generated-files.json"
    # runtime_ingest must remain the earlier ok status — single status per boundary.
    assert boundary_map(mutated)["runtime_ingest"].status == "ok"
    assert non_owner_boundaries_equal(mutated, canonical, owner="codegen_workspace")


def test_empty_dynamic_layer_emits_four_layer_not_applicable_entries() -> None:
    bundle = load_canonical_assurance_bundle("performance")
    empty_bundle = CanonicalAssuranceBundle(
        layer=bundle.layer,
        root=bundle.root,
        change_id=bundle.change_id,
        plan_texts={},
        cases=[],
        data_knowledge=deepcopy(bundle.data_knowledge),
        config_text=bundle.config_text,
        review_bytes=bundle.review_bytes,
        required_capabilities=(),
        summary_relpath=bundle.summary_relpath,
        summary_text=bundle.summary_text,
        skill_path=bundle.skill_path,
        adapter_paths=bundle.adapter_paths,
    )
    document = run_plan_checks(check_context_for_bundle(empty_bundle))
    assert tuple(item.check_id for item in document.checks) == PLAN_CHECK_IDS
    assert all(item.status == "not_applicable" for item in document.checks)
    assert all(item.applicability_reason == "layer_not_applicable" for item in document.checks)
