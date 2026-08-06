"""Promotion track Task 1: RegressionCandidate / TestPromotionManifest / PromotionReceipt."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import (
    ALLOWED_DELIVERIES,
    DeliveryKind,
    ImprovementCandidate,
    ImprovementKind,
)
from assurance_agent.artifacts.models.promotion import (
    PromotionMapping,
    PromotionReceipt,
    RegressionCandidate,
    TestPromotionManifest,
    WriteSetEntry,
)
from assurance_agent.artifacts.registry import match_artifact


def _sha(char: str = "a") -> str:
    return "sha256:" + (char * 64)


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
            "discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py": _sha("b"),
        },
        minimization_status="minimized",
        purpose="Stable regression for cross-tenant read denial",
        evidence_refs=("discovery/counterexamples/CE-auth-tenant-001/replay/attempt-0.json",),
    )
    base.update(overrides)
    return RegressionCandidate(**base)


def _mapping(**overrides: Any) -> PromotionMapping:
    base: dict[str, Any] = dict(
        source="discovery/candidates/RC-auth-tenant-001/files/test_tenant_boundary.py",
        target="tests/api/test_tenant_boundary.py",
        source_sha256=_sha("b"),
        target_base_sha256=None,
        must_not_exist=True,
    )
    base.update(overrides)
    return PromotionMapping(**base)


def _manifest(**overrides: Any) -> TestPromotionManifest:
    mapping = _mapping()
    base: dict[str, Any] = dict(
        schema_version="1",
        improvement_id="IMP-promo-001",
        candidate_id="RC-auth-tenant-001",
        mappings=(mapping,),
        problem_id="PROB-auth-tenant-001",
        counterexample_id="CE-auth-tenant-001",
        oracle_id="ORACLE-auth-tenant-isolation",
        replay_refs=("discovery/counterexamples/CE-auth-tenant-001/replay/attempt-0.json",),
        before_fix_revision="deadbeef",
        after_fix_revision="cafebabe",
        isolation_strategy=None,
        write_authorization=("tests/api/test_tenant_boundary.py",),
        rollback_manifest=("tests/api/test_tenant_boundary.py",),
        digests={"candidate": _sha("c"), "review_subject": _sha("d")},
    )
    base.update(overrides)
    return TestPromotionManifest(**base)


def _receipt(**overrides: Any) -> PromotionReceipt:
    base: dict[str, Any] = dict(
        schema_version="1",
        receipt_id="PROM-RCPT-001",
        improvement_id="IMP-promo-001",
        candidate_id="RC-auth-tenant-001",
        applied_at="2026-08-05T06:00:00Z",
        write_set=(
            WriteSetEntry(
                path="tests/api/test_tenant_boundary.py",
                before_sha256=None,
                after_sha256=_sha("e"),
            ),
        ),
        status="applied",
        source_digests={"candidate": _sha("c")},
        write_authorization=("tests/api/test_tenant_boundary.py",),
    )
    base.update(overrides)
    return PromotionReceipt(**base)


def test_regression_candidate_happy_path_round_trips() -> None:
    candidate = _candidate()
    restored = RegressionCandidate.model_validate(candidate.model_dump(mode="json"))
    assert restored == candidate
    assert restored.surface == "api"
    assert restored.proposed_targets[0].startswith("tests/api/")


def test_regression_candidate_accepts_testdata_target() -> None:
    candidate = _candidate(
        proposed_targets=("tests/testdata/domain/api.py",),
        source_files={
            "discovery/candidates/RC-auth-tenant-001/files/api.py": _sha("b"),
        },
    )
    assert candidate.proposed_targets == ("tests/testdata/domain/api.py",)


def test_regression_candidate_rejects_unsafe_and_out_of_phase1_paths() -> None:
    with pytest.raises(ValidationError, match="safe project-relative path"):
        _candidate(proposed_targets=("/tmp/evil.py",))
    with pytest.raises(ValidationError, match="safe project-relative path"):
        _candidate(proposed_targets=("tests/api/../escape.py",))
    with pytest.raises(ValidationError, match="tests/api/\\*\\*|tests/testdata/\\*\\*"):
        _candidate(proposed_targets=("tests/e2e/test_login.py",))
    with pytest.raises(ValidationError, match="safe project-relative path"):
        _candidate(
            source_files={"/abs/source.py": _sha("b")},
            proposed_targets=("tests/api/test_ok.py",),
        )


def test_promotion_manifest_happy_path_round_trips() -> None:
    manifest = _manifest()
    restored = TestPromotionManifest.model_validate(manifest.model_dump(mode="json"))
    assert restored == manifest
    assert restored.mappings[0].must_not_exist is True
    assert restored.mappings[0].target_base_sha256 is None


def test_promotion_manifest_accepts_existing_target_base_digest() -> None:
    manifest = _manifest(
        mappings=(
            _mapping(
                must_not_exist=False,
                target_base_sha256=_sha("f"),
            ),
        ),
    )
    assert manifest.mappings[0].target_base_sha256 == _sha("f")
    assert manifest.mappings[0].must_not_exist is False


def test_promotion_manifest_rejects_must_not_exist_xor_conflict() -> None:
    with pytest.raises(ValidationError, match="must_not_exist"):
        _mapping(must_not_exist=True, target_base_sha256=_sha("f"))
    with pytest.raises(ValidationError, match="must_not_exist"):
        _mapping(must_not_exist=False, target_base_sha256=None)


def test_promotion_manifest_rejects_unauthorized_mapping_target() -> None:
    with pytest.raises(ValidationError, match="write_authorization"):
        _manifest(
            write_authorization=("tests/api/other.py",),
            rollback_manifest=("tests/api/other.py",),
        )


def test_promotion_manifest_rejects_unsafe_mapping_paths() -> None:
    with pytest.raises(ValidationError, match="safe project-relative path"):
        _mapping(target="tests/api/../../etc/passwd")
    with pytest.raises(ValidationError, match="tests/api/\\*\\*|tests/testdata/\\*\\*"):
        _mapping(target="tests/fuzz/test_fuzz.py", must_not_exist=True, target_base_sha256=None)


def test_promotion_receipt_happy_path_round_trips() -> None:
    receipt = _receipt()
    restored = PromotionReceipt.model_validate(receipt.model_dump(mode="json"))
    assert restored == receipt
    assert restored.status == "applied"
    assert restored.write_set[0].before_sha256 is None


def test_promotion_receipt_rejects_write_set_outside_authorization() -> None:
    with pytest.raises(ValidationError, match="write_authorization"):
        _receipt(
            write_set=(
                WriteSetEntry(
                    path="tests/api/unauthorized.py",
                    before_sha256=None,
                    after_sha256=_sha("e"),
                ),
            ),
        )


def test_test_and_fixture_accept_test_promotion_delivery() -> None:
    for kind in (ImprovementKind.TEST, ImprovementKind.FIXTURE):
        assert DeliveryKind.TEST_PROMOTION in ALLOWED_DELIVERIES[kind]
        candidate = ImprovementCandidate.model_validate(
            {
                "candidate_id": f"IMP-{kind.value}",
                "kind": kind.value,
                "delivery": "test_promotion",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": "tests/api/test_tenant_boundary.py",
                "rationale": "Promote confirmed CE to canonical regression",
                "proposed_change": "Add minimized API regression from discovery candidate",
                "verification": {
                    "suites": ["api"],
                    "success_criteria": "Fails before fix, passes after fix",
                },
                "risk": "medium",
                "confidence": "high",
            }
        )
        assert candidate.delivery is DeliveryKind.TEST_PROMOTION


def test_prompt_rejects_test_promotion_delivery() -> None:
    assert DeliveryKind.TEST_PROMOTION not in ALLOWED_DELIVERIES[ImprovementKind.PROMPT]
    with pytest.raises(ValidationError, match="cannot use"):
        ImprovementCandidate.model_validate(
            {
                "candidate_id": "IMP-prompt-promo",
                "kind": "prompt_improvement",
                "delivery": "test_promotion",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": ".aa/memory/aa-api-plan.md",
                "rationale": "Should not promote via test_promotion",
                "proposed_change": "n/a",
                "verification": {"suites": ["unit"], "success_criteria": "n/a"},
                "risk": "low",
                "confidence": "low",
            }
        )


def test_promotion_registry_patterns_are_change_relative() -> None:
    """Phase 1 vertical slice keeps promotion artifacts change-local under discovery/.

    Choice: ``discovery/candidates/<id>/candidate.yaml``,
    ``promotion-manifest.yaml``, and ``promotion-receipt.json`` (not
    ``qa/improvements/**``) so candidates, manifests, and receipts share one
    directory with the design §5.1 layout and archive as a unit.
    """
    expected = {
        "discovery/candidates/RC-001/candidate.yaml": "discovery_regression_candidate",
        "discovery/candidates/RC-001/promotion-manifest.yaml": "discovery_promotion_manifest",
        "discovery/candidates/RC-001/promotion-receipt.json": "discovery_promotion_receipt",
    }
    for path, artifact_type in expected.items():
        spec = match_artifact(path)
        assert spec is not None, path
        assert spec.artifact_type == artifact_type
        assert spec.compat == "must_compat"

    assert match_artifact("discovery/candidates/RC-001/files/test.py") is None
    assert match_artifact("qa/improvements/promotions/RC-001/receipt.json") is None
    assert match_artifact("inspect/promotion-receipts/RC-001.json") is None
