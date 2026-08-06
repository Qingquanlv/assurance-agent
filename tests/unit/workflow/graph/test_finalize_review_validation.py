"""Engine-level freeze validation of must_compat registry artifacts.

Gate expressions read artifact fields straight from the file (freeze only checks
existence), so a skill that omits a gate-critical field would otherwise slip
through and dead-end at a terminal `stop`. finalize now runs the registry
pydantic model for every declared `change:` single-file output whose spec is
`must_compat`, failing such artifacts as `invalid_output`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.graph.finalize import _validate_registry_outputs
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.issues.identity import candidate_document_digest


def _workspace(tmp_path: Path) -> TaskWorkspace:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    return TaskWorkspace(
        task_id="t1",
        root=tmp_path,
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        base_tree_id="",
    )


def _write(ws: TaskWorkspace, rel: str, payload: object) -> None:
    path = ws.change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if rel.endswith((".yaml", ".yml")):
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


def test_project_candidate_document_is_validated_at_agent_boundary(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    path = ws.project_root / "qa" / "retro" / "retro-1" / "proposal-candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "2",
                "retro_id": "retro-1",
                "context_sha256": "sha256:context",
                "candidates": [{"candidate_id": "IMP-CAND-1"}],
            }
        ),
        encoding="utf-8",
    )

    result = _validate_registry_outputs(
        workspace=ws,
        outputs=("project:qa/retro/retro-1/proposal-candidates.json",),
    )

    assert result is not None
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "improvement_candidate_document" in (result.error or "")


_BASE_API_REVIEW = {
    "schema_version": "1.0",
    "review_type": "api-plan",
    "change_id": "CH-1",
    "decision": "pass",
    "codegen_readiness": "ready",
    "auto_fix_allowed": False,
    "human_review_required": False,
    "risk_level": "low",
    "findings": [],
    "auto_fix_plan": [],
    "next_action": "continue",
}


def test_api_plan_review_missing_required_capabilities_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "review/api-plan-review.json", dict(_BASE_API_REVIEW))
    result = _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",))
    assert result is not None
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "required_capabilities" in (result.error or "")


def test_api_plan_review_empty_required_capabilities_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "review/api-plan-review.json", {**_BASE_API_REVIEW, "required_capabilities": []})
    result = _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",))
    assert result is not None
    assert result.error_kind == "invalid_output"


def test_api_plan_review_with_capabilities_passes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "review/api-plan-review.json",
        {**_BASE_API_REVIEW, "required_capabilities": ["auth.api_admin_token"]},
    )
    assert _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",)) is None


@pytest.mark.parametrize(
    "field",
    (
        "review_type",
        "change_id",
        "codegen_readiness",
        "auto_fix_allowed",
        "human_review_required",
        "risk_level",
        "auto_fix_plan",
        "next_action",
    ),
)
def test_api_plan_review_missing_cross_skill_field_is_invalid_output(tmp_path: Path, field: str) -> None:
    ws = _workspace(tmp_path)
    payload = {**_BASE_API_REVIEW, "required_capabilities": ["auth.api_admin_token"]}
    del payload[field]
    _write(ws, "review/api-plan-review.json", payload)

    result = _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",))

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert field in (result.error or "")


def test_api_plan_review_finding_without_id_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "review/api-plan-review.json",
        {
            **_BASE_API_REVIEW,
            "required_capabilities": ["auth.api_admin_token"],
            "findings": [{"severity": "high"}],
        },
    )

    result = _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",))

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "findings[0].id" in (result.error or "")


def test_case_review_with_independent_source_verification_passes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "review/case-review.json",
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-1",
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["app/api.py"],
                "verified_claims": [{"claim": "route exists", "evidence_files": ["app/api.py"]}],
            },
        },
    )
    assert _validate_registry_outputs(workspace=ws, outputs=("change:review/case-review.json",)) is None


def test_case_review_without_source_verification_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "review/case-review.json",
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-1",
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
        },
    )

    result = _validate_registry_outputs(workspace=ws, outputs=("change:review/case-review.json",))

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "source_verification" in (result.error or "")


def _write_project(ws: TaskWorkspace, rel: str, payload: object) -> Path:
    path = ws.project_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_RUNTIME_SIGNAL_DOC = {
    "schema_version": "3",
    "retro_id": "retro-1",
    "domain": "eval",
    "analysis_status": "failed",
    "failure_reason": "retro_pipeline_failure:collect:internal",
    "analyzer": "operation:retro-pipeline-fallback",
    "signals": [],
    "slice_sha256": "sha256:" + "a" * 64,
}


def test_completed_signal_document_keeps_engine_owned_digest(tmp_path: Path) -> None:
    """The draft ban on ``slice_sha256`` must not outlive runtime completion.

    Signals reach finalize after the runtime backfilled the digest, whether the
    author was an analyzer skill or a fallback operation.
    """
    ws = _workspace(tmp_path)
    _write_project(ws, "qa/retro/retro-1/signals/eval.json", _RUNTIME_SIGNAL_DOC)

    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=("project:qa/retro/retro-1/signals/eval.json",),
        )
        is None
    )


def test_completed_signal_document_still_validates_canonical_shape(tmp_path: Path) -> None:
    """Skipping the draft model must not skip the canonical model."""
    ws = _workspace(tmp_path)
    _write_project(
        ws,
        "qa/retro/retro-1/signals/eval.json",
        {**_RUNTIME_SIGNAL_DOC, "analysis_status": "failed", "failure_reason": None},
    )

    result = _validate_registry_outputs(
        workspace=ws,
        outputs=("project:qa/retro/retro-1/signals/eval.json",),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "failure_reason" in (result.error or "")


def test_invalid_json_review_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    path = ws.change_dir / "review" / "api-plan-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    result = _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",))
    assert result is not None
    assert result.error_kind == "invalid_output"


def test_advisory_must_compat_is_validated(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "explore/advisory.json", {"schema_version": "1.0"})  # missing required lists
    result = _validate_registry_outputs(workspace=ws, outputs=("change:explore/advisory.json",))
    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "advisory" in (result.error or "")


def test_fix_proposal_and_apply_summary_must_compat_pass(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "healing/fix-proposal.json",
        {"schema_version": "1.0", "summary": {"eligible_count": 0}, "proposals": []},
    )
    _write(
        ws,
        "healing/api-apply-summary.json",
        {"schema_version": "1.0", "target": "api", "applied": True},
    )
    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=(
                "change:healing/fix-proposal.json",
                "change:healing/api-apply-summary.json",
            ),
        )
        is None
    )


def test_qa_yaml_must_compat_missing_targets_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        ".qa.yaml",
        {
            "schema_version": "1.0",
            "schema": "case-driven",
            "created_at": "2026-07-24T00:00:00Z",
            "change": {
                "change_id": "CH-1",
                "requirement_id": "R1",
                "feature_name": "feat",
                "status": "draft",
            },
            # targets omitted — must_compat QaYaml requires it
        },
    )
    result = _validate_registry_outputs(workspace=ws, outputs=("change:.qa.yaml",))
    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "targets" in (result.error or "")


def test_issue_candidate_with_unknown_surface_kind_is_invalid_output(tmp_path: Path) -> None:
    """Analyzer output is rejected before reconcile when its enum violates the contract."""
    ws = _workspace(tmp_path)
    _write(
        ws,
        "inspect/issue-candidates.json",
        {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "batch-1",
            "evidence_bundle_digest": "sha256:evidence",
            "candidates": [
                {
                    "candidate_id": "CAND-001",
                    "observation_ids": ["OBS-001"],
                    "proposed": {
                        "title": "Knowledge fixture is missing",
                        "classification": "workflow_issue",
                        "severity": "medium",
                        "root_cause_hypothesis": "The fixture contract is incomplete",
                    },
                    "affected_surface": {"kind": "knowledge", "value": "auth.token"},
                    "fingerprint_inputs": {
                        "surface": "auth.token",
                        "symptom": "fixture_missing",
                    },
                    "possible_problem_ids": [],
                    "confidence": 0.9,
                    "recommended_action": "update the fixture contract",
                }
            ],
        },
    )

    result = _validate_registry_outputs(
        workspace=ws,
        outputs=("change:inspect/issue-candidates.json",),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "affected_surface.kind" in (result.error or "")


def test_issue_analysis_status_rejects_noncanonical_candidate_digest(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    candidate_document = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "evidence_bundle_digest": "sha256:evidence",
        "candidates": [],
    }
    _write(ws, "inspect/issue-candidates.json", candidate_document)
    _write(
        ws,
        "inspect/issue-analysis-status.json",
        {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "batch-1",
            "status": "completed",
            "evidence_bundle_digest": "sha256:evidence",
            "candidate_count": 0,
            "candidate_digest": "sha256:raw-file-bytes",
        },
    )

    result = _validate_registry_outputs(
        workspace=ws,
        outputs=(
            "change:inspect/issue-candidates.json",
            "change:inspect/issue-analysis-status.json",
        ),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "candidate_digest" in (result.error or "")
    assert "canonical" in (result.error or "")


def test_issue_analysis_status_accepts_authored_json_digest_without_model_defaults(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    candidate_document = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-1",
        "evidence_bundle_digest": "sha256:evidence",
        "candidates": [
            {
                "candidate_id": "CAND-001",
                "observation_ids": ["OBS-001"],
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
    _write(ws, "inspect/issue-candidates.json", candidate_document)
    _write(
        ws,
        "inspect/issue-analysis-status.json",
        {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "batch-1",
            "status": "completed",
            "evidence_bundle_digest": "sha256:evidence",
            "candidate_count": 1,
            "candidate_digest": candidate_document_digest(candidate_document),
        },
    )

    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=(
                "change:inspect/issue-candidates.json",
                "change:inspect/issue-analysis-status.json",
            ),
        )
        is None
    )


def test_versioned_and_directory_outputs_are_skipped(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    # versioned: execution-manifest — not must_compat
    _write(ws, "execution/execution-manifest.yaml", {"broken": True})
    # directory output — not expanded
    (ws.change_dir / "cases").mkdir(parents=True, exist_ok=True)
    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=("change:execution/execution-manifest.yaml", "change:cases/"),
        )
        is None
    )


def test_non_change_outputs_are_ignored(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=("repo:review/api-plan-review.json", "change:plans/api-plan.md"),
        )
        is None
    )
