import hashlib
import json

import pytest

from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    ProblemFingerprint,
    ProblemFingerprintPreimage,
)
from assurance_agent.workflow.issues.identity import (
    DIGEST_PREFIX_LENGTH,
    ObservationIdentityInput,
    candidate_document_digest,
    fingerprint_digest_for_version,
    occurrence_id,
    observation_id,
    problem_fingerprint,
    problem_id,
    reconciliation_idempotency_key,
)


def _canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _observation_input(**overrides: object) -> ObservationIdentityInput:
    base = {
        "change_id": "RET-dept-management",
        "batch_id": "20260725-124844",
        "kind": "test_failure",
        "target": "api",
        "case_id": "API-DEPT-NEG-001",
        "source_artifact": "execution/runs/20260725-124844/api-result.json",
        "source_json_pointer": "/cases/3",
        "signature": "http_500_on_empty_department_name",
    }
    merged: dict[str, str] = {**base, **{k: str(v) for k, v in overrides.items()}}
    return ObservationIdentityInput(**merged)


def test_observation_id_is_deterministic_and_prefixed() -> None:
    obs_input = _observation_input()
    first = observation_id(obs_input)
    second = observation_id(obs_input)

    assert first == second
    assert first.startswith("OBS-")
    assert len(first) == len("OBS-") + DIGEST_PREFIX_LENGTH


def test_observation_id_ignores_whitespace_in_signature() -> None:
    base = _observation_input(signature="http_500_on_empty_department_name")
    spaced = _observation_input(signature="  http_500_on_empty_department_name  ")

    assert observation_id(base) == observation_id(spaced)


def test_observation_id_changes_with_source_location() -> None:
    left = _observation_input(source_json_pointer="/cases/3")
    right = _observation_input(source_json_pointer="/cases/4")

    assert observation_id(left) != observation_id(right)


def test_occurrence_and_reconciliation_keys_are_stable() -> None:
    change_id = "RET-dept-management"
    batch_id = "20260725-124844"
    candidate_digest = "sha256:candidate"

    occ = occurrence_id(change_id, batch_id, candidate_digest)
    key = reconciliation_idempotency_key(change_id, batch_id, candidate_digest)

    assert occ.startswith("OCC-")
    assert len(occ) == len("OCC-") + DIGEST_PREFIX_LENGTH
    assert key == _canonical_sha256(
        {
            "change_id": change_id,
            "batch_id": batch_id,
            "candidate_digest": candidate_digest,
        }
    )


def test_problem_fingerprint_ignores_title_root_cause_confidence_severity_change_id() -> None:
    surface = AffectedSurface(kind="endpoint", value="POST /api/v1/dept")
    inputs = FingerprintInputs(
        surface="POST /api/v1/dept",
        symptom="invalid_empty_name_returns_500",
        qualifiers=["negative_case"],
    )

    baseline = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=inputs,
    )
    noisy = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=inputs,
        title="Different title",
        root_cause_hypothesis="Different root cause",
        confidence=0.12,
        severity="low",
        change_id="OTHER-change",
    )

    assert baseline == noisy
    assert baseline.digest.startswith("sha256:")
    assert baseline.version == "1"


def test_problem_fingerprint_changes_with_surface_kind_value_symptom_qualifiers_version() -> None:
    base_surface = AffectedSurface(kind="endpoint", value="POST /api/v1/dept")
    base_inputs = FingerprintInputs(
        surface="POST /api/v1/dept",
        symptom="invalid_empty_name_returns_500",
        qualifiers=["negative_case"],
    )
    baseline = problem_fingerprint(
        affected_surface=base_surface,
        fingerprint_inputs=base_inputs,
    )

    other_kind = problem_fingerprint(
        affected_surface=AffectedSurface(kind="module", value="dept_service"),
        fingerprint_inputs=FingerprintInputs(
            surface="dept_service",
            symptom="invalid_empty_name_returns_500",
            qualifiers=["negative_case"],
        ),
    )
    other_value = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept/other"),
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept/other",
            symptom="invalid_empty_name_returns_500",
            qualifiers=["negative_case"],
        ),
    )
    other_symptom = problem_fingerprint(
        affected_surface=base_surface,
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept",
            symptom="invalid_empty_name_returns_502",
            qualifiers=["negative_case"],
        ),
    )
    other_qualifiers = problem_fingerprint(
        affected_surface=base_surface,
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept",
            symptom="invalid_empty_name_returns_500",
            qualifiers=["positive_case"],
        ),
    )
    other_version_digest = fingerprint_digest_for_version(
        affected_surface=base_surface,
        fingerprint_inputs=base_inputs,
        version="2",
    )

    assert baseline != other_kind
    assert baseline != other_value
    assert baseline != other_symptom
    assert baseline != other_qualifiers
    assert baseline.digest.removeprefix("sha256:") != other_version_digest


def test_problem_fingerprint_normalizes_endpoint_module_and_symptom_tokens() -> None:
    left = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="post  /api/v1/dept/"),
        fingerprint_inputs=FingerprintInputs(
            surface="post  /api/v1/dept/",
            symptom="Invalid Empty Name Returns 500",
        ),
    )
    right = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept"),
        fingerprint_inputs=FingerprintInputs(
            surface="POST /api/v1/dept",
            symptom="invalid_empty_name_returns_500",
        ),
    )

    assert left == right


def test_problem_fingerprint_rejects_empty_normalized_values() -> None:
    with pytest.raises(ValueError, match="symptom"):
        problem_fingerprint(
            affected_surface=AffectedSurface(kind="endpoint", value="POST /api/v1/dept"),
            fingerprint_inputs=FingerprintInputs(surface="POST /api/v1/dept", symptom="   "),
        )

    with pytest.raises(ValueError, match="surface"):
        problem_fingerprint(
            affected_surface=AffectedSurface(kind="module", value="   "),
            fingerprint_inputs=FingerprintInputs(surface="   ", symptom="missing_module"),
        )


def test_problem_id_uses_fingerprint_digest_prefix() -> None:
    fingerprint = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
    derived = problem_id(fingerprint)

    assert derived == f"PROB-{'a' * DIGEST_PREFIX_LENGTH}"


def test_problem_fingerprint_canonical_key_order_is_stable() -> None:
    surface = AffectedSurface(kind="endpoint", value="POST /api/v1/dept")
    inputs = FingerprintInputs(
        surface="POST /api/v1/dept",
        symptom="invalid_empty_name_returns_500",
        qualifiers=["b", "a"],
    )
    fingerprint = problem_fingerprint(
        affected_surface=surface,
        fingerprint_inputs=inputs,
    )

    expected_digest = "sha256:" + _canonical_sha256(
        {
            "version": "1",
            "surface_kind": "endpoint",
            "surface_identity": "POST /api/v1/dept",
            "symptom": "invalid_empty_name_returns_500",
            "qualifiers": ["a", "b"],
        }
    )
    assert fingerprint.digest == expected_digest


def test_problem_fingerprint_exposes_verified_canonical_preimage_for_future_reuse() -> None:
    fingerprint = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value="post  /api/v1/dept/"),
        fingerprint_inputs=FingerprintInputs(
            surface="ignored duplicate surface",
            symptom="Closure Not Rebuilt On Reparent",
            qualifiers=["reparent", "dept closure"],
        ),
    )

    assert fingerprint.preimage == ProblemFingerprintPreimage(
        version="1",
        surface_kind="endpoint",
        surface_identity="POST /api/v1/dept",
        symptom="closure_not_rebuilt_on_reparent",
        qualifiers=["dept_closure", "reparent"],
    )


def test_problem_fingerprint_rejects_preimage_that_does_not_match_digest() -> None:
    with pytest.raises(ValueError, match="preimage"):
        ProblemFingerprint(
            version="1",
            digest="sha256:" + "a" * 64,
            preimage=ProblemFingerprintPreimage(
                version="1",
                surface_kind="endpoint",
                surface_identity="POST /api/v1/dept",
                symptom="http_500",
                qualifiers=[],
            ),
        )


def test_candidate_document_digest_hashes_authored_json_without_inserting_defaults() -> None:
    authored = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "B-1",
        "evidence_bundle_digest": "sha256:evidence",
        "candidates": [
            {
                "candidate_id": "CAND-1",
                "observation_ids": ["OBS-1"],
                "proposed": {
                    "title": "Endpoint fails",
                    "classification": "product_bug",
                    "severity": "high",
                    "root_cause_hypothesis": "Unhandled input",
                },
                "affected_surface": {"kind": "endpoint", "value": "POST /api/items"},
                "fingerprint_inputs": {"surface": "POST /api/items", "symptom": "http_500"},
                "possible_problem_ids": [],
                "confidence": 1,
                "recommended_action": "investigate",
            }
        ],
    }
    expected = (
        "sha256:"
        + hashlib.sha256(
            (json.dumps(authored, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
    )

    assert candidate_document_digest(authored) == expected
