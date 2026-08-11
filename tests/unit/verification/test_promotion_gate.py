"""Promotion track Task 2: pure §11.5 Promotion Gate evaluation."""

from __future__ import annotations

from typing import Any

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvements import DeliveryKind, ImprovementState
from assurance_agent.artifacts.models.promotion import (
    PromotionMapping,
    RegressionCandidate,
    TestPromotionManifest,
)
from assurance_agent.verification.promotion_gate import (
    ApprovedImprovementView,
    PromotionGateDecision,
    SuiteResults,
    evaluate_promotion_gate,
)


def _candidate(**overrides: Any) -> RegressionCandidate:
    base: dict[str, Any] = dict(
        schema_version="1",
        candidate_id="RC-auth-tenant-001",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        counterexample_id="CE-auth-tenant-001",
        problem_id="PROB-auth-tenant-001",
        oracle_id="ORACLE-auth-tenant-isolation",
        surface="api",
        proposed_targets=("tests/api/test_tenant_boundary.py",),
        source_files={
            "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py": sha256_bytes(
                b"assert deny\n"
            ),
        },
        minimization_status="minimized",
        purpose="Stable regression for cross-tenant read denial",
        evidence_refs=("discovery/counterexamples/CE-auth-tenant-001/replay/attempt-0.json",),
    )
    base.update(overrides)
    return RegressionCandidate(**base)


def _mapping(*, source_bytes: bytes = b"assert deny\n", **overrides: Any) -> PromotionMapping:
    source = "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py"
    base: dict[str, Any] = dict(
        source=source,
        target="tests/api/test_tenant_boundary.py",
        source_sha256=sha256_bytes(source_bytes),
        target_base_sha256=None,
        must_not_exist=True,
    )
    base.update(overrides)
    return PromotionMapping(**base)


def _manifest(
    candidate: RegressionCandidate,
    mapping: PromotionMapping,
    *,
    review_subject: str,
    **overrides: Any,
) -> TestPromotionManifest:
    base: dict[str, Any] = dict(
        schema_version="1",
        improvement_id="IMP-promo-001",
        candidate_id=candidate.candidate_id,
        mappings=(mapping,),
        problem_id="PROB-auth-tenant-001",
        counterexample_id="CE-auth-tenant-001",
        oracle_id="ORACLE-auth-tenant-isolation",
        replay_refs=("discovery/counterexamples/CE-auth-tenant-001/replay/attempt-0.json",),
        before_fix_revision="deadbeef",
        after_fix_revision="cafebabe",
        isolation_strategy=None,
        write_authorization=(mapping.target,),
        rollback_manifest=(mapping.target,),
        digests={
            "candidate": sha256_bytes(canonical_json_bytes(candidate)),
            "review_subject": review_subject,
        },
    )
    base.update(overrides)
    return TestPromotionManifest(**base)


def _approved(**overrides: Any) -> ApprovedImprovementView:
    base: dict[str, Any] = dict(
        improvement_id="IMP-promo-001",
        state=ImprovementState.APPROVED,
        version=2,
        review_subject_sha256="sha256:" + ("d" * 64),
        delivery=DeliveryKind.TEST_PROMOTION,
    )
    base.update(overrides)
    return ApprovedImprovementView(**base)


def _pass_inputs(
    *,
    source_bytes: bytes = b"assert deny\n",
    isolation_strategy: str | None = "quarantine_until_fix",
) -> tuple[
    TestPromotionManifest,
    RegressionCandidate,
    dict[str, bytes],
    dict[str, bytes | None],
    ApprovedImprovementView,
]:
    candidate = _candidate(
        source_files={
            "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py": sha256_bytes(
                source_bytes
            ),
        }
    )
    mapping = _mapping(source_bytes=source_bytes)
    review = "sha256:" + ("d" * 64)
    manifest = _manifest(
        candidate,
        mapping,
        review_subject=review,
        isolation_strategy=isolation_strategy,
    )
    source_map = {mapping.source: source_bytes}
    targets: dict[str, bytes | None] = {mapping.target: None}
    return manifest, candidate, source_map, targets, _approved(review_subject_sha256=review)


def test_gate_passes_when_all_checks_green() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
        before_fix_failed=False,
        after_fix_passed=False,
    )
    assert decision == PromotionGateDecision(ok=True, reasons=())


def test_gate_fails_improvement_not_approved() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    approved = ApprovedImprovementView(
        improvement_id=approved.improvement_id,
        state=ImprovementState.PROPOSED,
        version=approved.version,
        review_subject_sha256=approved.review_subject_sha256,
        delivery=approved.delivery,
    )
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("improvement_not_approved",)


def test_gate_fails_improvement_version_drift() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=1,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("improvement_version_drift",)


def test_gate_fails_candidate_digest_mismatch() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    manifest = manifest.model_copy(
        update={"digests": {**manifest.digests, "candidate": "sha256:" + ("0" * 64)}}
    )
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("candidate_digest_mismatch",)


def test_gate_fails_source_digest_mismatch() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    sources = {next(iter(sources)): b"tampered\n"}
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("source_digest_mismatch",)


def test_gate_fails_review_subject_digest_mismatch() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    approved = ApprovedImprovementView(
        improvement_id=approved.improvement_id,
        state=approved.state,
        version=approved.version,
        review_subject_sha256="sha256:" + ("e" * 64),
        delivery=approved.delivery,
    )
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("review_subject_digest_mismatch",)


def test_gate_fails_must_not_exist_violation() -> None:
    """Existing path with same content as source under must_not_exist."""
    source = b"assert deny\n"
    manifest, candidate, sources, targets, approved = _pass_inputs(source_bytes=source)
    targets = {"tests/api/test_tenant_boundary.py": source}
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("must_not_exist_violation",)


def test_gate_fails_target_base_digest_drift() -> None:
    source = b"assert deny\n"
    candidate = _candidate(
        source_files={
            "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py": sha256_bytes(source),
        }
    )
    prior = b"old body\n"
    mapping = _mapping(
        source_bytes=source,
        must_not_exist=False,
        target_base_sha256=sha256_bytes(prior),
    )
    review = "sha256:" + ("d" * 64)
    manifest = _manifest(candidate, mapping, review_subject=review, isolation_strategy="quarantine")
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes={mapping.source: source},
        target_bytes_on_disk={mapping.target: b"drifted\n"},
        approved_improvement=_approved(review_subject_sha256=review),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("target_base_digest_drift",)


def test_gate_fails_semantic_duplicate() -> None:
    """Exists with different content + must_not_exist → Phase1 semantic duplicate."""
    source = b"assert deny\n"
    manifest, candidate, sources, targets, approved = _pass_inputs(source_bytes=source)
    targets = {"tests/api/test_tenant_boundary.py": b"other test body\n"}
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("semantic_duplicate",)


def test_gate_fails_replay_failed() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=False,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("replay_failed",)


def test_gate_fails_fix_protocol_without_isolation() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs(isolation_strategy=None)
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
        before_fix_failed=False,
        after_fix_passed=False,
    )
    assert decision.ok is False
    assert decision.reasons == ("fix_protocol_failed",)


def test_gate_passes_fix_protocol_before_fail_after_pass() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs(isolation_strategy=None)
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
        before_fix_failed=True,
        after_fix_passed=True,
    )
    assert decision.ok is True


def test_gate_fails_not_minimized() -> None:
    source = b"assert deny\n"
    candidate = _candidate(
        minimization_status="raw",
        source_files={
            "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py": sha256_bytes(source),
        },
    )
    mapping = _mapping(source_bytes=source)
    review = "sha256:" + ("d" * 64)
    manifest = _manifest(candidate, mapping, review_subject=review, isolation_strategy="quarantine")
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes={mapping.source: source},
        target_bytes_on_disk={mapping.target: None},
        approved_improvement=_approved(review_subject_sha256=review),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("not_minimized",)


def test_gate_fails_change_private_dependency() -> None:
    source = b"from qa.changes.CH_DEMO import helper\nassert helper()\n"
    candidate = _candidate(
        source_files={
            "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py": sha256_bytes(source),
        }
    )
    mapping = _mapping(source_bytes=source)
    review = "sha256:" + ("d" * 64)
    manifest = _manifest(candidate, mapping, review_subject=review, isolation_strategy="quarantine")
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes={mapping.source: source},
        target_bytes_on_disk={mapping.target: None},
        approved_improvement=_approved(review_subject_sha256=review),
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
    )
    assert decision.ok is False
    assert decision.reasons == ("change_private_dependency",)


def test_gate_fails_suite_failed() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(lint_ok=False),
    )
    assert decision.ok is False
    assert decision.reasons == ("suite_failed",)


def test_gate_fails_unauthorized_write() -> None:
    manifest, candidate, sources, targets, approved = _pass_inputs()
    decision = evaluate_promotion_gate(
        manifest,
        candidate=candidate,
        source_bytes=sources,
        target_bytes_on_disk=targets,
        approved_improvement=approved,
        expected_improvement_version=2,
        replay_ok=True,
        suite_results=SuiteResults(),
        authorized_targets=("tests/api/other_file.py",),
    )
    assert decision.ok is False
    assert decision.reasons == ("unauthorized_write",)


@pytest.mark.parametrize(
    "reason",
    [
        "improvement_not_approved",
        "improvement_version_drift",
        "candidate_digest_mismatch",
        "source_digest_mismatch",
        "review_subject_digest_mismatch",
        "must_not_exist_violation",
        "target_base_digest_drift",
        "semantic_duplicate",
        "replay_failed",
        "fix_protocol_failed",
        "not_minimized",
        "change_private_dependency",
        "suite_failed",
        "unauthorized_write",
    ],
)
def test_reason_codes_are_stable_literals(reason: str) -> None:
    from assurance_agent.verification.promotion_gate import PROMOTION_GATE_REASON_CODES

    assert reason in PROMOTION_GATE_REASON_CODES
