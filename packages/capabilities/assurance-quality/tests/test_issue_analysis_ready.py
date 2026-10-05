"""Issue-analysis prepare reads both ledger documents or neither."""

from __future__ import annotations

import pytest

from agent_runtime_contracts.ops import InputError

from assurance_quality.contracts.agent import IssueAnalysisSkillInputV1
from assurance_quality.contracts.assessment import AssessmentInputsV1, InspectionOutcomeV1
from assurance_quality.ops.issue_analysis.hooks import require_issue_analysis_ready


def _business(**overrides: object) -> IssueAnalysisSkillInputV1:
    payload: dict[str, object] = {
        "inspection_disposition": "analysis_required",
        "owned_evidence_ids": ("obs-1",),
        "evidence_bundle_digest": "sha256:" + "a" * 64,
    }
    payload.update(overrides)
    return IssueAnalysisSkillInputV1.model_construct(**payload)  # type: ignore[arg-type]


def _inspection(**overrides: object) -> InspectionOutcomeV1:
    payload: dict[str, object] = {
        "change_id": "CH-A",
        "batch_id": "B-1",
        "coverage_epoch": 1,
        "disposition": "blocked",
    }
    payload.update(overrides)
    return InspectionOutcomeV1.model_construct(**payload)  # type: ignore[arg-type]


def _assessment(**overrides: object) -> AssessmentInputsV1:
    payload: dict[str, object] = {
        "change_id": "CH-A",
        "batch_id": "B-1",
        "coverage_epoch": 1,
        "owned_evidence_ids": ("obs-1",),
        "evidence_bundle_digest": "sha256:" + "a" * 64,
    }
    payload.update(overrides)
    return AssessmentInputsV1.model_construct(**payload)  # type: ignore[arg-type]


def test_absent_ledger_documents_pass_for_a_thin_or_direct_call() -> None:
    require_issue_analysis_ready(_business(inspection_disposition="satisfied"), None, None)
    require_issue_analysis_ready(
        _business(inspection_disposition=None, owned_evidence_ids=(), evidence_bundle_digest=None),
        None,
        None,
    )


def test_one_ledger_document_is_an_input_failure() -> None:
    with pytest.raises(InputError, match="read together"):
        require_issue_analysis_ready(_business(), _inspection(), None)
    with pytest.raises(InputError, match="read together"):
        require_issue_analysis_ready(_business(), None, _assessment())


def test_ledger_documents_must_agree_on_identity_and_the_bundle() -> None:
    require_issue_analysis_ready(_business(), _inspection(), _assessment())
    with pytest.raises(InputError, match="diagnostic-ready"):
        require_issue_analysis_ready(
            _business(),
            _inspection(),
            _assessment(coverage_epoch=2),
        )


def test_a_satisfied_pair_is_an_input_failure() -> None:
    with pytest.raises(InputError, match="failed inspection"):
        require_issue_analysis_ready(
            _business(),
            _inspection(disposition="satisfied"),
            _assessment(),
        )
