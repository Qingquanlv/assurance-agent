from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS, STATE_SCHEMA_VERSION, digest
from assurance_quality.contracts.assessment import AssessmentInputsV1


def test_current_revision_rejects_the_previous_state_schema() -> None:
    assert STATE_SCHEMA_VERSION == "4"
    for contract in ENTRYPOINT_CONTRACTS.values():
        assert contract.state_schema_version == "4"
        previous = replace(contract, state_schema_version="3")
        assert digest(previous) != digest(contract)


def test_missing_obligation_assessment_is_not_filled_by_defaults() -> None:
    payload = {
        "change_id": "CH-1",
        "coverage_epoch": 0,
        "batch_id": "B-1",
        "plan_digest": "a" * 64,
        "plan_ref": {
            "path": f"qa/results/plan/{'a' * 64}/resolved-assurance-plan.json",
            "digest": "a" * 64,
        },
        "scope": {
            "change_id": "CH-1",
            "coverage_epoch": 0,
            "required_case_ids": ["C1"],
            "selected_families": ["api"],
            "applicable_goals": ["constraint_coverage"],
            "applicability_refs": [{"path": "qa/requirement.md", "digest": "a" * 64}],
            "risk_tier": "high",
            "policy_digest": "a" * 64,
        },
        "policy": {"coverage_floor_by_tier": {"low": 0.7, "medium": 0.8, "high": 0.9, "critical": 1.0}},
        "trace_ref": {"path": "qa/results/inspect/trace.json", "digest": "a" * 64},
        "gaps_ref": {"path": "qa/results/inspect/gaps.json", "digest": "a" * 64},
        "metrics_ref": {"path": "qa/results/inspect/metrics.json", "digest": "a" * 64},
        "sufficiency_ref": {"path": "qa/results/inspect/sufficiency.json", "digest": "a" * 64},
        "execution_ref": {"path": "qa/results/execution/result.json", "digest": "a" * 64},
        "observations_ref": {"path": "qa/results/inspect/observations.json", "digest": "a" * 64},
        "issue_evidence_manifest_ref": {"path": "qa/results/inspect/manifest.json", "digest": "a" * 64},
        "owned_evidence_ids": ["OBS-1"],
        "evidence_bundle_digest": "sha256:" + "a" * 64,
    }
    with pytest.raises(ValidationError, match="obligation"):
        AssessmentInputsV1.model_validate(payload)
