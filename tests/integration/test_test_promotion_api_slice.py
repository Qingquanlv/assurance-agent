"""Promotion track Task 3 — e2e CE→candidate→manifest→apply→receipt acceptance.

Scenarios A–C exercise the vertical slice with Fake gate injectables.
Scenario D exposes a pure C2 feedstock counter only (no M4 MetricKey fold).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.discovery import (
    Counterexample,
    CounterexampleReplay,
    MinimizationInfo,
)
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementState,
    ImprovementVerification,
)
from assurance_agent.artifacts.models.promotion import (
    PromotionMapping,
    PromotionReceipt,
    RegressionCandidate,
    TestPromotionManifest,
)
from assurance_agent.evidence.promotion import count_promoted_counterexamples
from assurance_agent.verification.promotion_gate import SuiteResults
from assurance_agent.workflow.discovery.candidates import build_regression_candidate
from assurance_agent.workflow.improvements.promotion_delivery import (
    PromotionDeliveryError,
    apply_test_promotion,
    as_approved_view,
    rollback_test_promotion,
)

CHANGE_ID = "CH-PROMO-P1"
CAMPAIGN_ID = "CAM-PROMO-P1"
CE_ID = "CE-auth-tenant-001"
CANDIDATE_ID = "RC-auth-tenant-001"
PROBLEM_ID = "PROB-auth-tenant-001"
ORACLE_ID = "ORACLE-auth-tenant-isolation"
IMP_ID = "IMP-promo-001"
TARGET_REL = "tests/api/test_tenant_boundary.py"
SOURCE_REL = f"discovery/candidates/{CANDIDATE_ID}/files/test_tenant_boundary.py"
CE_REL = f"discovery/counterexamples/{CE_ID}.yaml"
REVIEW = "sha256:" + ("d" * 64)
SOURCE_BYTES = b"def test_tenant_boundary():\n    assert deny_cross_tenant()\n"


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _confirmed_ce(*, source_digest: str) -> Counterexample:
    return Counterexample(
        schema_version="1",
        counterexample_id=CE_ID,
        campaign_id=CAMPAIGN_ID,
        round_id="R0001",
        surface="api",
        technique="auth.tenant-boundary",
        obligation_ids=("OBL-tenant-isolation",),
        oracle_id=ORACLE_ID,
        oracle_kind="hard_oracle",
        environment_digest="sha256:" + ("a" * 64),
        generated_file_digests={SOURCE_REL: source_digest},
        setup={},
        actions=({"method": "GET", "path": "/api/tenants/other/resource"},),
        observed={"status": 200},
        expected={"status": 403},
        seed=7,
        minimization=MinimizationInfo(status="minimized", parent_counterexample_id=None),
        replay=CounterexampleReplay(
            attempts=2,
            reproduced=2,
            artifact_refs=(
                f"discovery/counterexamples/{CE_ID}/replay/attempt-0.json",
                f"discovery/counterexamples/{CE_ID}/replay/attempt-1.json",
            ),
        ),
        finding_status="confirmed",
    )


def _fake_approved_improvement() -> ImprovementProjection:
    """Fake ApprovedImprovement: TEST + test_promotion + approved."""
    return ImprovementProjection(
        improvement_id=IMP_ID,
        fingerprint="fp-promo-001",
        fingerprint_version="1",
        kind=ImprovementKind.TEST,
        delivery=DeliveryKind.TEST_PROMOTION,
        source_refs=ImprovementSourceRefs(problem_ids=(PROBLEM_ID,)),
        target=TARGET_REL,
        rationale="Promote confirmed tenant-boundary CE to canonical regression",
        proposed_change=f"Promote {CANDIDATE_ID} via test_promotion",
        knowledge_delta=None,
        verification=ImprovementVerification(
            suites=("api",),
            required_cases=(),
            success_criteria="canonical target written; gate green",
        ),
        risk="medium",
        confidence="high",
        state=ImprovementState.APPROVED,
        version=2,
        proposed_by_retro_ids=("RETRO-promo-001",),
        supersedes=None,
        last_event_id="EVT-promo-001",
        review_subject_sha256=REVIEW,
        approval_source="human",
        last_auto_review=None,
    )


def _manifest(
    candidate: RegressionCandidate,
    *,
    source_digest: str,
    **overrides: Any,
) -> TestPromotionManifest:
    mapping = PromotionMapping(
        source=SOURCE_REL,
        target=TARGET_REL,
        source_sha256=source_digest,
        target_base_sha256=None,
        must_not_exist=True,
    )
    base: dict[str, Any] = dict(
        schema_version="1",
        improvement_id=IMP_ID,
        candidate_id=CANDIDATE_ID,
        mappings=(mapping,),
        problem_id=PROBLEM_ID,
        counterexample_id=CE_ID,
        oracle_id=ORACLE_ID,
        replay_refs=(f"discovery/counterexamples/{CE_ID}/replay/attempt-0.json",),
        before_fix_revision="deadbeef",
        after_fix_revision="cafebabe",
        isolation_strategy="quarantine_until_fix",
        write_authorization=(TARGET_REL,),
        rollback_manifest=(TARGET_REL,),
        digests={
            "candidate": sha256_bytes(canonical_json_bytes(candidate)),
            "review_subject": REVIEW,
        },
    )
    base.update(overrides)
    return TestPromotionManifest(**base)


def _seed_ce_and_candidate(change_dir: Path) -> tuple[Counterexample, RegressionCandidate, bytes]:
    source_digest = sha256_bytes(SOURCE_BYTES)
    ce = _confirmed_ce(source_digest=source_digest)
    _write(
        change_dir / CE_REL,
        yaml.safe_dump(ce.model_dump(mode="json"), sort_keys=True),
    )

    candidate = build_regression_candidate(
        ce,
        change_id=CHANGE_ID,
        candidate_id=CANDIDATE_ID,
        proposed_targets=(TARGET_REL,),
        source_files={SOURCE_REL: source_digest},
        purpose="Stable regression for cross-tenant read denial",
        problem_id=PROBLEM_ID,
    )
    cand_dir = change_dir / "discovery" / "candidates" / CANDIDATE_ID
    _write(
        cand_dir / "candidate.yaml",
        yaml.safe_dump(candidate.model_dump(mode="json"), sort_keys=True),
    )
    _write(change_dir / SOURCE_REL, SOURCE_BYTES)
    return ce, candidate, SOURCE_BYTES


def _apply_kwargs(manifest: TestPromotionManifest) -> dict[str, Any]:
    return dict(
        approved_improvement=as_approved_view(_fake_approved_improvement()),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
        before_fix_failed=True,
        after_fix_passed=True,
        authorized_targets=manifest.write_authorization,
        clock=lambda: "2026-08-05T06:00:00Z",
    )


def test_scenario_a_promote_confirmed_ce_asset(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    (project / "tests" / "api").mkdir(parents=True)
    change.mkdir(parents=True)

    _ce, candidate, source = _seed_ce_and_candidate(change)
    source_digest = sha256_bytes(source)
    manifest = _manifest(candidate, source_digest=source_digest)
    _write(
        change / f"discovery/candidates/{CANDIDATE_ID}/promotion-manifest.yaml",
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=True),
    )

    receipt = apply_test_promotion(project, change, manifest, **_apply_kwargs(manifest))

    target = project / TARGET_REL
    assert target.is_file()
    assert target.read_bytes() == source
    assert sha256_bytes(target.read_bytes()) == source_digest

    assert receipt.status == "applied"
    assert len(receipt.write_set) == 1
    assert receipt.write_set[0].path == TARGET_REL
    assert receipt.write_set[0].before_sha256 is None
    assert receipt.write_set[0].after_sha256 == source_digest

    receipt_path = change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json"
    assert receipt_path.is_file()
    loaded = PromotionReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
    assert loaded.status == "applied"
    assert loaded.write_set[0].path == TARGET_REL

    # Promotion must not delete change-local discovery CE.
    ce_path = change / CE_REL
    assert ce_path.is_file()
    still = Counterexample.model_validate(yaml.safe_load(ce_path.read_text(encoding="utf-8")))
    assert still.counterexample_id == CE_ID
    assert still.finding_status == "confirmed"


def test_scenario_b_rollback_after_apply(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    (project / "tests" / "api").mkdir(parents=True)
    change.mkdir(parents=True)

    _ce, candidate, source = _seed_ce_and_candidate(change)
    manifest = _manifest(candidate, source_digest=sha256_bytes(source))
    receipt = apply_test_promotion(project, change, manifest, **_apply_kwargs(manifest))
    assert (project / TARGET_REL).is_file()

    rolled = rollback_test_promotion(project, receipt, change_dir=change)
    assert rolled.status == "rolled_back"
    assert not (project / TARGET_REL).exists()

    loaded = PromotionReceipt.model_validate_json(
        (change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json").read_text(encoding="utf-8")
    )
    assert loaded.status == "rolled_back"

    # CE still present after rollback.
    assert (change / CE_REL).is_file()


def test_scenario_c_fail_closed_replay_and_must_not_exist(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    (project / "tests" / "api").mkdir(parents=True)
    change.mkdir(parents=True)

    _ce, candidate, source = _seed_ce_and_candidate(change)
    manifest = _manifest(candidate, source_digest=sha256_bytes(source))
    kwargs = _apply_kwargs(manifest)

    # C1: replay_ok=False → no canonical write, no applied receipt.
    with pytest.raises(PromotionDeliveryError) as exc_replay:
        apply_test_promotion(project, change, manifest, **{**kwargs, "replay_ok": False})
    assert "replay_failed" in exc_replay.value.reasons
    assert not (project / TARGET_REL).exists()
    assert not (change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json").exists()

    # C2: target already exists under must_not_exist → no write / no receipt.
    _write(project / TARGET_REL, b"preexisting canonical body\n")
    preexisting = (project / TARGET_REL).read_bytes()
    with pytest.raises(PromotionDeliveryError) as exc_exists:
        apply_test_promotion(project, change, manifest, **kwargs)
    assert (
        "must_not_exist_violation" in exc_exists.value.reasons
        or "semantic_duplicate" in exc_exists.value.reasons
    )
    assert (project / TARGET_REL).read_bytes() == preexisting
    assert not (change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json").exists()


def test_scenario_d_c2_feedstock_counter_from_applied_receipt(tmp_path: Path) -> None:
    """Successful receipt counts as promoted counterexample feedstock for future C2.

    Pure counter only — M4 will consume this later; no MetricKey / MetricsDocument.
    """
    project = tmp_path / "project"
    change = tmp_path / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    (project / "tests" / "api").mkdir(parents=True)
    change.mkdir(parents=True)

    _ce, candidate, source = _seed_ce_and_candidate(change)
    manifest = _manifest(candidate, source_digest=sha256_bytes(source))
    receipt = apply_test_promotion(project, change, manifest, **_apply_kwargs(manifest))

    assert count_promoted_counterexamples([receipt], [candidate]) == 1
    # Rolled-back / non-applied receipts must not count.
    rolled = receipt.model_copy(update={"status": "rolled_back"})
    assert count_promoted_counterexamples([rolled], [candidate]) == 0
    # Unknown candidate_id on receipt → skip (fail-closed, no invent).
    assert count_promoted_counterexamples([receipt], []) == 0
