"""Promotion track Task 2: content-addressed apply / rollback for test_promotion."""

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
from assurance_agent.artifacts.models.improvements import DeliveryKind, ImprovementState
from assurance_agent.artifacts.models.promotion import (
    PromotionMapping,
    PromotionReceipt,
    RegressionCandidate,
    TestPromotionManifest,
    WriteSetEntry,
)
from assurance_agent.verification.promotion_gate import ApprovedImprovementView, SuiteResults
from assurance_agent.workflow.discovery.candidates import build_regression_candidate
from assurance_agent.workflow.improvements.promotion_delivery import (
    PromotionDeliveryError,
    apply_test_promotion,
    rollback_test_promotion,
)
from assurance_agent.workflow.improvements import promotion_delivery

SOURCE_REL = "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py"
TARGET_REL = "tests/api/test_tenant_boundary.py"
CANDIDATE_ID = "RC-auth-tenant-001"
REVIEW = "sha256:" + ("d" * 64)


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def _candidate(source_digest: str, **overrides: Any) -> RegressionCandidate:
    base: dict[str, Any] = dict(
        schema_version="1",
        candidate_id=CANDIDATE_ID,
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        counterexample_id="CE-auth-tenant-001",
        problem_id="PROB-auth-tenant-001",
        oracle_id="ORACLE-auth-tenant-isolation",
        surface="api",
        proposed_targets=(TARGET_REL,),
        source_files={SOURCE_REL: source_digest},
        minimization_status="minimized",
        purpose="Stable regression for cross-tenant read denial",
        evidence_refs=("discovery/counterexamples/CE-auth-tenant-001/replay/attempt-0.json",),
    )
    base.update(overrides)
    return RegressionCandidate(**base)


def _manifest(candidate: RegressionCandidate, source_digest: str, **overrides: Any) -> TestPromotionManifest:
    mapping = PromotionMapping(
        source=SOURCE_REL,
        target=TARGET_REL,
        source_sha256=source_digest,
        target_base_sha256=None,
        must_not_exist=True,
    )
    base: dict[str, Any] = dict(
        schema_version="1",
        improvement_id="IMP-promo-001",
        candidate_id=CANDIDATE_ID,
        mappings=(mapping,),
        problem_id="PROB-auth-tenant-001",
        counterexample_id="CE-auth-tenant-001",
        oracle_id="ORACLE-auth-tenant-isolation",
        replay_refs=("discovery/counterexamples/CE-auth-tenant-001/replay/attempt-0.json",),
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


def _approved() -> ApprovedImprovementView:
    return ApprovedImprovementView(
        improvement_id="IMP-promo-001",
        state=ImprovementState.APPROVED,
        version=2,
        review_subject_sha256=REVIEW,
        delivery=DeliveryKind.TEST_PROMOTION,
    )


def _seed_change(
    change_dir: Path, source: bytes = b"assert deny_cross_tenant()\n"
) -> tuple[RegressionCandidate, TestPromotionManifest, bytes]:
    digest = sha256_bytes(source)
    candidate = _candidate(digest)
    manifest = _manifest(candidate, digest)
    _write(change_dir / SOURCE_REL, source)
    _write(
        change_dir / f"discovery/candidates/{CANDIDATE_ID}/candidate.yaml",
        yaml.safe_dump(candidate.model_dump(mode="json"), sort_keys=True),
    )
    _write(
        change_dir / f"discovery/candidates/{CANDIDATE_ID}/promotion-manifest.yaml",
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=True),
    )
    # Change-local copy of a generated test that must remain untouched by apply.
    _write(change_dir / "tests/api/local_only.py", b"# change-local sentinel\n")
    return candidate, manifest, source


def test_apply_writes_canonical_target_and_receipt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _candidate_obj, manifest, source = _seed_change(change)

    receipt = apply_test_promotion(
        project,
        change,
        manifest,
        approved_improvement=_approved(),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )

    target = project / TARGET_REL
    assert target.is_file()
    assert target.read_bytes() == source
    assert receipt.status == "applied"
    assert receipt.write_set[0].before_sha256 is None
    assert receipt.write_set[0].after_sha256 == sha256_bytes(source)

    receipt_path = change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json"
    assert receipt_path.is_file()
    loaded = PromotionReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
    assert loaded.status == "applied"

    # Change-local tree untouched except receipt path (and pre-existing seed files).
    assert (change / "tests/api/local_only.py").read_bytes() == b"# change-local sentinel\n"
    assert not (change / TARGET_REL).exists() or (change / TARGET_REL).read_bytes() != source


def test_apply_restores_targets_when_receipt_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _, manifest, _ = _seed_change(change)
    real_atomic_write = promotion_delivery.atomic_write_bytes

    def fail_receipt(path: Path, payload: bytes) -> None:
        if path.name == "promotion-receipt.json":
            raise OSError("receipt disk full")
        real_atomic_write(path, payload)

    monkeypatch.setattr(promotion_delivery, "atomic_write_bytes", fail_receipt)

    with pytest.raises(OSError, match="receipt disk full"):
        apply_test_promotion(
            project,
            change,
            manifest,
            approved_improvement=_approved(),
            expected_improvement_version=2,
            replay_ok=True,
            suite_results=SuiteResults(),
        )

    assert not (project / TARGET_REL).exists()
    assert not (change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json").exists()


def test_rollback_deletes_new_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _, manifest, _ = _seed_change(change)
    receipt = apply_test_promotion(
        project,
        change,
        manifest,
        approved_improvement=_approved(),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert (project / TARGET_REL).is_file()

    rolled = rollback_test_promotion(project, receipt, change_dir=change)
    assert rolled.status == "rolled_back"
    assert not (project / TARGET_REL).exists()
    loaded = PromotionReceipt.model_validate_json(
        (change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json").read_text(encoding="utf-8")
    )
    assert loaded.status == "rolled_back"


def test_rollback_restores_prior_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    prior = b"prior body\n"
    source = b"assert deny_cross_tenant()\n"
    digest = sha256_bytes(source)
    candidate = _candidate(digest)
    mapping = PromotionMapping(
        source=SOURCE_REL,
        target=TARGET_REL,
        source_sha256=digest,
        target_base_sha256=sha256_bytes(prior),
        must_not_exist=False,
    )
    manifest = _manifest(
        candidate,
        digest,
        mappings=(mapping,),
        write_authorization=(TARGET_REL,),
        rollback_manifest=(TARGET_REL,),
    )
    _write(change / SOURCE_REL, source)
    _write(
        change / f"discovery/candidates/{CANDIDATE_ID}/candidate.yaml",
        yaml.safe_dump(candidate.model_dump(mode="json"), sort_keys=True),
    )
    _write(project / TARGET_REL, prior)

    receipt = apply_test_promotion(
        project,
        change,
        manifest,
        approved_improvement=_approved(),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert (project / TARGET_REL).read_bytes() == source
    assert receipt.write_set[0].before_sha256 == sha256_bytes(prior)

    rolled = rollback_test_promotion(project, receipt, change_dir=change)
    assert rolled.status == "rolled_back"
    assert (project / TARGET_REL).read_bytes() == prior


def test_digest_drift_refuses_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _, manifest, _ = _seed_change(change)
    _write(project / TARGET_REL, b"unexpected existing\n")

    with pytest.raises(PromotionDeliveryError) as exc:
        apply_test_promotion(
            project,
            change,
            manifest,
            approved_improvement=_approved(),
            expected_improvement_version=2,
            replay_ok=True,
            suite_results=SuiteResults(),
        )
    assert "semantic_duplicate" in exc.value.reasons or "must_not_exist_violation" in exc.value.reasons
    assert (
        not (project / TARGET_REL).exists() or (project / TARGET_REL).read_bytes() == b"unexpected existing\n"
    )
    # No receipt written on failure.
    assert not (change / f"discovery/candidates/{CANDIDATE_ID}/promotion-receipt.json").exists()


def test_must_not_exist_violation_no_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    source = b"assert deny_cross_tenant()\n"
    _, manifest, _ = _seed_change(change, source=source)
    _write(project / TARGET_REL, source)

    with pytest.raises(PromotionDeliveryError) as exc:
        apply_test_promotion(
            project,
            change,
            manifest,
            approved_improvement=_approved(),
            expected_improvement_version=2,
            replay_ok=True,
            suite_results=SuiteResults(),
        )
    assert "must_not_exist_violation" in exc.value.reasons
    assert (project / TARGET_REL).read_bytes() == source


def test_replay_ok_false_no_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _, manifest, _ = _seed_change(change)

    with pytest.raises(PromotionDeliveryError) as exc:
        apply_test_promotion(
            project,
            change,
            manifest,
            approved_improvement=_approved(),
            expected_improvement_version=2,
            replay_ok=False,
            suite_results=SuiteResults(),
        )
    assert exc.value.reasons == ("replay_failed",)
    assert not (project / TARGET_REL).exists()


def test_unauthorized_path_no_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _, manifest, _ = _seed_change(change)

    with pytest.raises(PromotionDeliveryError) as exc:
        apply_test_promotion(
            project,
            change,
            manifest,
            approved_improvement=_approved(),
            expected_improvement_version=2,
            replay_ok=True,
            suite_results=SuiteResults(),
            authorized_targets=("tests/api/other.py",),
        )
    assert "unauthorized_write" in exc.value.reasons
    assert not (project / TARGET_REL).exists()


def test_refuse_reapply_when_receipt_already_applied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = tmp_path / "change"
    project.mkdir()
    change.mkdir()
    _, manifest, _ = _seed_change(change)
    apply_test_promotion(
        project,
        change,
        manifest,
        approved_improvement=_approved(),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    with pytest.raises(PromotionDeliveryError, match="already applied"):
        apply_test_promotion(
            project,
            change,
            manifest,
            approved_improvement=_approved(),
            expected_improvement_version=2,
            replay_ok=True,
            suite_results=SuiteResults(),
        )


def test_refuse_rollback_when_not_applied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    receipt = PromotionReceipt(
        schema_version="1",
        receipt_id="PROM-RCPT-001",
        improvement_id="IMP-promo-001",
        candidate_id=CANDIDATE_ID,
        applied_at="2026-08-05T06:00:00Z",
        write_set=(
            WriteSetEntry(
                path=TARGET_REL,
                before_sha256=None,
                after_sha256=sha256_bytes(b"x"),
            ),
        ),
        status="rolled_back",
        source_digests={SOURCE_REL: sha256_bytes(b"x")},
        write_authorization=(TARGET_REL,),
    )
    with pytest.raises(PromotionDeliveryError, match="not applied"):
        rollback_test_promotion(project, receipt)


def test_rollback_preflights_every_target_before_mutating_any_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    drifted_rel = "tests/api/test_drifted.py"
    valid_rel = "tests/api/test_valid.py"
    applied_a = b"applied a\n"
    applied_b = b"applied b\n"
    drifted_now = b"user changed a\n"
    _write(project / drifted_rel, drifted_now)
    _write(project / valid_rel, applied_b)
    receipt = PromotionReceipt(
        schema_version="1",
        receipt_id="PROM-RCPT-ATOMIC",
        improvement_id="IMP-promo-atomic",
        candidate_id=CANDIDATE_ID,
        applied_at="2026-08-05T06:00:00Z",
        write_set=(
            WriteSetEntry(
                path=drifted_rel,
                before_sha256=None,
                after_sha256=sha256_bytes(applied_a),
            ),
            WriteSetEntry(
                path=valid_rel,
                before_sha256=None,
                after_sha256=sha256_bytes(applied_b),
            ),
        ),
        status="applied",
        source_digests={SOURCE_REL: sha256_bytes(applied_a)},
        write_authorization=(drifted_rel, valid_rel),
    )

    with pytest.raises(PromotionDeliveryError, match="drifted"):
        rollback_test_promotion(project, receipt)

    assert (project / drifted_rel).read_bytes() == drifted_now
    assert (project / valid_rel).read_bytes() == applied_b


def test_build_regression_candidate_from_confirmed_ce() -> None:
    ce = Counterexample(
        schema_version="1",
        counterexample_id="CE-auth-tenant-001",
        campaign_id="CAM-001",
        round_id="R0001",
        surface="api",
        technique="auth.tenant-boundary",
        obligation_ids=("OBL-1",),
        oracle_id="ORACLE-auth-tenant-isolation",
        oracle_kind="hard_oracle",
        environment_digest="sha256:" + ("a" * 64),
        generated_file_digests={SOURCE_REL: sha256_bytes(b"assert True\n")},
        setup={},
        actions=({"method": "GET", "path": "/x"},),
        observed={"status": 200},
        expected={"status": 403},
        seed=7,
        minimization=MinimizationInfo(status="minimized", parent_counterexample_id=None),
        replay=CounterexampleReplay(attempts=2, reproduced=2, artifact_refs=()),
        finding_status="confirmed",
    )
    built = build_regression_candidate(
        ce,
        change_id="CH-DEMO-001",
        candidate_id=CANDIDATE_ID,
        proposed_targets=(TARGET_REL,),
        source_files=dict(ce.generated_file_digests),
        purpose="Promote confirmed tenant boundary CE",
        problem_id="PROB-1",
    )
    assert built.counterexample_id == ce.counterexample_id
    assert built.oracle_id == ce.oracle_id
    assert built.minimization_status == "minimized"
    assert built.source_files == ce.generated_file_digests
