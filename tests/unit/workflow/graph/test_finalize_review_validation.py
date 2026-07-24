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

import yaml

from assurance_agent.workflow.graph.finalize import _validate_registry_outputs
from assurance_agent.workflow.graph.workspace import TaskWorkspace


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


_BASE_API_REVIEW = {
    "schema_version": "1.0",
    "review_type": "api-plan",
    "decision": "pass",
    "findings": [],
}


def test_api_plan_review_missing_required_capabilities_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "review/api-plan-review.json", dict(_BASE_API_REVIEW))
    result = _validate_registry_outputs(
        workspace=ws, outputs=("change:review/api-plan-review.json",)
    )
    assert result is not None
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "required_capabilities" in (result.error or "")


def test_api_plan_review_empty_required_capabilities_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "review/api-plan-review.json", {**_BASE_API_REVIEW, "required_capabilities": []})
    result = _validate_registry_outputs(
        workspace=ws, outputs=("change:review/api-plan-review.json",)
    )
    assert result is not None
    assert result.error_kind == "invalid_output"


def test_api_plan_review_with_capabilities_passes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "review/api-plan-review.json",
        {**_BASE_API_REVIEW, "required_capabilities": ["auth.api_admin_token"]},
    )
    assert (
        _validate_registry_outputs(workspace=ws, outputs=("change:review/api-plan-review.json",))
        is None
    )


def test_non_plan_review_without_capabilities_passes(tmp_path: Path) -> None:
    """case-review is not a capability-gated review_type — omission is fine."""
    ws = _workspace(tmp_path)
    _write(
        ws,
        "review/case-review.json",
        {"schema_version": "1.0", "review_type": "case", "decision": "pass", "findings": []},
    )
    assert (
        _validate_registry_outputs(workspace=ws, outputs=("change:review/case-review.json",))
        is None
    )


def test_invalid_json_review_is_invalid_output(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    path = ws.change_dir / "review" / "api-plan-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    result = _validate_registry_outputs(
        workspace=ws, outputs=("change:review/api-plan-review.json",)
    )
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
