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
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

import assurance_agent.workflow.graph.finalize as finalize
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.finalize import (
    _validate_registry_outputs,
)
from assurance_agent.workflow.graph.invariants import (
    run_cross_artifact_invariants,
    validate_case_design_approved_automation as _validate_case_design_approved_automation,
    validate_case_design_source_verification as _validate_case_design_source_verification,
    validate_case_review_minimum_coverage_payloads,
    validate_issue_candidate_digest,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.task_inputs import TaskInputSnapshotV1, _entries_input_sha256
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WriteSet,
)
from assurance_agent.workflow.issues.identity import candidate_document_digest
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


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


def _write_init_config(ws: TaskWorkspace, answers: InitAnswers) -> None:
    config_path = ws.project_root / ".aa" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(build_config_yaml(answers), encoding="utf-8")


def test_case_design_rejects_approved_layer_without_automated_case() -> None:
    result = _validate_case_design_approved_automation(
        {
            "change:.qa.yaml": {"approval": {"approved_approach": "API + E2E + Fuzz + Performance"}},
            "change:cases/system/dept/case.yaml": {
                "added": [
                    {"case_id": "TC_API_001", "type": "API", "automation": {"required": True}},
                    {"case_id": "TC_E2E_001", "type": "E2E", "automation": {"required": False}},
                ],
                "modified": [],
            },
        }
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "E2E, Fuzz, Performance" in (result.error or "")


def test_case_design_accepts_automated_case_for_each_approved_layer() -> None:
    entries = [
        {"case_id": f"TC_{case_type}_001", "type": case_type, "automation": {"required": True}}
        for case_type in ("API", "E2E", "Fuzz", "Performance")
    ]
    assert (
        _validate_case_design_approved_automation(
            {
                "change:.qa.yaml": {"approval": {"approved_approach": "API / E2E / Fuzz / Performance"}},
                "change:cases/system/dept/case.yaml": {"added": entries, "modified": []},
            }
        )
        is None
    )


def _case_design_dir_claims() -> ResourceClaims:
    parsed = tuple(ResourcePath.parse(p) for p in ("change:.qa.yaml", "change:cases/**"))
    return ResourceClaims(writes=parsed, authorization_writes=parsed)


def _empty_input_snapshot(base_tree_id: str) -> TaskInputSnapshotV1:
    return TaskInputSnapshotV1(
        schema_version="1",
        invocation_id="inv-1",
        task_id="case-design",
        attempt_id="case-design-a1",
        base_tree_id=base_tree_id,
        materialized_tree_id=base_tree_id,
        input_sha256=_entries_input_sha256([]),
        runtime_context_sha256=None,
        contract_digest="sha256:" + "c" * 64,
        claims_digest="sha256:" + "e" * 64,
        entries=[],
    )


def _freeze_case_design_directory_output(
    tmp_path: Path,
    *,
    case_types: tuple[str, ...],
) -> tuple[TreeStore, WriteSet]:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id="case-design",
        base_tree_id=store.capture(project),
        store=store,
    )
    qa = {
        "schema_version": "1.0",
        "schema": "case-driven",
        "created_at": "2026-08-17T13:53:49.000Z",
        "change": {
            "change_id": "CH-1",
            "requirement_id": "RET-dept",
            "feature_name": "dept-management",
            "status": "draft",
        },
        "approval": {
            "mode": "autonomous",
            "approved_by": "aa-workflow",
            "approved_approach": "API + E2E + Fuzz + Performance",
            "approved_at": "2026-08-17T13:53:49.000Z",
        },
    }
    (workspace.change_dir / ".qa.yaml").write_text(yaml.safe_dump(qa, sort_keys=False), encoding="utf-8")
    case_dir = workspace.change_dir / "cases" / "system" / "dept"
    case_dir.mkdir(parents=True)
    case_dir.joinpath("case.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": f"TC_DEPT_{case_type}_001",
                        "type": case_type,
                        "automation": {"required": True},
                    }
                    for case_type in case_types
                ],
                "modified": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    write_set = store.freeze_write_set(
        workspace,
        claims=_case_design_dir_claims(),
        outputs=("change:.qa.yaml", "change:cases/"),
    )
    return store, write_set


def test_cross_artifact_reads_cases_under_declared_directory_output(tmp_path: Path) -> None:
    store, write_set = _freeze_case_design_directory_output(
        tmp_path, case_types=("API", "E2E", "Fuzz", "Performance")
    )

    assert "change:cases/" in write_set.outputs_sha256
    assert "change:cases/system/dept/case.yaml" not in write_set.outputs_sha256
    assert any(entry.logical_path.endswith("cases/system/dept/case.yaml") for entry in write_set.entries)

    ran = run_cross_artifact_invariants(
        store=store,
        write_set=write_set,
        input_snapshot=_empty_input_snapshot(write_set.base_tree_id),
        project_root=tmp_path / "proj",
    )

    assert "qa_yaml_case_automation" in ran


def test_cross_artifact_still_rejects_directory_output_missing_automated_layer(
    tmp_path: Path,
) -> None:
    store, write_set = _freeze_case_design_directory_output(tmp_path, case_types=("API",))

    with pytest.raises(ValueError, match="E2E, Fuzz, Performance"):
        run_cross_artifact_invariants(
            store=store,
            write_set=write_set,
            input_snapshot=_empty_input_snapshot(write_set.base_tree_id),
            project_root=tmp_path / "proj",
        )


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
        "trace/minimum-coverage-matrix.json",
        [
            {
                "mrc_id": "MRC-API-001",
                "key": "list_users",
                "required": True,
                "covered_by_cases": ["TC-1"],
                "status": "covered",
            },
            {
                "mrc_id": "MRC-NEGATIVE-001",
                "key": "missing_required_fields",
                "required": True,
                "covered_by_cases": [],
                "status": "skipped_by_scope",
                "skip_reason": "not applicable to this change",
            },
        ],
    )
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
            "minimum_coverage": {
                "total_required": 2,
                "covered": 1,
                "skipped_by_scope": 1,
                "missing": ["missing_required_fields"],
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["app/api.py"],
                "verified_claims": [{"claim": "route exists", "evidence_files": ["app/api.py"]}],
            },
        },
    )
    assert _validate_registry_outputs(workspace=ws, outputs=("change:review/case-review.json",)) is None


def test_case_review_minimum_coverage_must_match_frozen_matrix(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "trace/minimum-coverage-matrix.json",
        [
            {
                "mrc_id": "MRC-API-001",
                "key": "list_users",
                "required": True,
                "covered_by_cases": ["TC-1"],
                "status": "covered",
            },
            {
                "mrc_id": "MRC-E2E-001",
                "key": "admin_resets_password",
                "required": True,
                "covered_by_cases": [],
                "status": "skipped_by_scope",
                "skip_reason": "out of scope",
            },
        ],
    )
    _write(
        ws,
        "review/case-review.json",
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-1",
            "decision": "needs_human_review",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "human_review",
            "auto_fix_allowed": False,
            "human_review_required": True,
            "risk_level": "medium",
            "minimum_coverage": {
                "total_required": 1,
                "covered": 1,
                "skipped_by_scope": 0,
                "missing": [],
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["app/api.py"],
                "verified_claims": [{"claim": "route exists", "evidence_files": ["app/api.py"]}],
            },
        },
    )

    result = validate_case_review_minimum_coverage_payloads(
        review_raw=json.loads((ws.change_dir / "review/case-review.json").read_text(encoding="utf-8")),
        matrix_raw=json.loads(
            (ws.change_dir / "trace/minimum-coverage-matrix.json").read_text(encoding="utf-8")
        ),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "minimum_coverage" in (result.error or "")
    assert "total_required=2" in (result.error or "")
    assert "skipped_by_scope=1" in (result.error or "")
    assert "admin_resets_password" in (result.error or "")
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


def test_case_design_proposal_without_own_source_verification_is_invalid_output(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    (ws.change_dir / "proposal.md").write_text(
        "# Proposal: CH-1\n\n## Why\n\nExercise the API.\n",
        encoding="utf-8",
    )

    result = _validate_case_design_source_verification(ws)

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "Product Source Verification" in (result.error or "")


def test_case_design_source_verification_error_includes_exact_repair_shape(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    source = ws.project_root / "app" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text("def route(): ...\n", encoding="utf-8")
    (ws.change_dir / "proposal.md").write_text(
        """# Proposal: CH-1

## Product Source Verification

- reviewed_source_files:
  - `app/api.py`
""",
        encoding="utf-8",
    )

    result = _validate_case_design_source_verification(ws)

    assert result is not None
    assert "- independently_read: true" in (result.error or "")
    assert "- reviewed_source_files:\n  - `<project-relative product source path>`" in (result.error or "")


def test_case_design_proposal_with_existing_product_source_passes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    source = ws.project_root / "app" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text("def route(): ...\n", encoding="utf-8")
    (ws.change_dir / "proposal.md").write_text(
        """# Proposal: CH-1

## Product Source Verification

- independently_read: true
- reviewed_source_files:
  - `app/api.py`

| Claim | Source file | Evidence checked |
|---|---|---|
| route exists | `app/api.py` | `route` handler |
""",
        encoding="utf-8",
    )

    assert _validate_case_design_source_verification(ws) is None


@pytest.mark.parametrize(
    ("answers", "source_path"),
    (
        (InitAnswers(), "backend/api.py"),
        (
            InitAnswers(frontend_path="./client/ui", backend_path="./services/api"),
            "services/api/routes.py",
        ),
    ),
)
def test_case_design_accepts_sources_declared_by_project_config(
    tmp_path: Path,
    answers: InitAnswers,
    source_path: str,
) -> None:
    ws = _workspace(tmp_path)
    _write_init_config(ws, answers)
    source = ws.project_root / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def route(): ...\n", encoding="utf-8")
    (ws.change_dir / "proposal.md").write_text(
        f"""# Proposal: CH-1

## Product Source Verification

- independently_read: true
- reviewed_source_files:
  - `{source_path}`
""",
        encoding="utf-8",
    )

    assert _validate_case_design_source_verification(ws) is None


def test_case_design_rejects_existing_file_outside_declared_source_roots(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write_init_config(
        ws,
        InitAnswers(frontend_path="./client/ui", backend_path="./services/api"),
    )
    source = ws.project_root / "docs" / "api.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def route(): ...\n", encoding="utf-8")
    (ws.change_dir / "proposal.md").write_text(
        """# Proposal: CH-1

## Product Source Verification

- independently_read: true
- reviewed_source_files:
  - `docs/api.py`
""",
        encoding="utf-8",
    )

    result = _validate_case_design_source_verification(ws)

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "reviewed_source_files" in (result.error or "")


def test_case_design_finalize_reports_registry_and_source_verification_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "cases/system/api/case.yaml", {"broken": True})
    (ws.change_dir / "proposal.md").write_text(
        "# Proposal: CH-1\n\n## Why\n\nExercise the API.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(finalize, "_ensure_outputs_frozen", lambda **kwargs: kwargs["result"])
    monkeypatch.setattr(finalize, "_ingest_frozen_outputs", lambda **kwargs: kwargs["result"])
    monkeypatch.setattr(finalize, "_apply_subgraph_exports", lambda **kwargs: kwargs["result"])
    task = cast(
        ExecutableTask,
        SimpleNamespace(
            graph_id="main",
            node_id="case-design",
            target="skill:aa-case-design",
            input={"outputs": ["change:cases/", "change:proposal.md"]},
        ),
    )
    context = RuntimeContext(
        project_root=ws.project_root,
        repo_root=ws.repo_root,
        change_dir=ws.change_dir,
        change_id="CH-1",
    )

    result = finalize.finalize_task_result(
        compiled=cast(CompiledWorkflow, SimpleNamespace(graphs={})),
        store=cast(TreeStore, SimpleNamespace()),
        task=task,
        result=TaskResult(status="succeeded"),
        workspace=ws,
        context=context,
    )

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "case_yaml" in (result.error or "")


@pytest.mark.parametrize(
    "source_path",
    (
        "/tmp/api.py",
        "../api.py",
        "qa/changes/CH-1/explore/advisory.json",
        "app/missing.py",
    ),
)
def test_case_design_proposal_rejects_non_product_source(
    tmp_path: Path,
    source_path: str,
) -> None:
    ws = _workspace(tmp_path)
    advisory = ws.change_dir / "explore" / "advisory.json"
    advisory.parent.mkdir(parents=True)
    advisory.write_text("{}\n", encoding="utf-8")
    (ws.change_dir / "proposal.md").write_text(
        f"""# Proposal: CH-1

## Product Source Verification

- independently_read: true
- reviewed_source_files:
  - `{source_path}`
""",
        encoding="utf-8",
    )

    result = _validate_case_design_source_verification(ws)

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "reviewed_source_files" in (result.error or "")


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

    result = validate_issue_candidate_digest(
        {
            "change:inspect/issue-candidates.json": candidate_document,
            "change:inspect/issue-analysis-status.json": {
                "schema_version": "1.0",
                "change_id": "CH-1",
                "batch_id": "batch-1",
                "status": "completed",
                "evidence_bundle_digest": "sha256:evidence",
                "candidate_count": 0,
                "candidate_digest": "sha256:raw-file-bytes",
            },
        }
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


def test_versioned_outputs_are_skipped(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    # versioned: execution-manifest — not must_compat
    _write(ws, "execution/execution-manifest.json", {"broken": True})
    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=("change:execution/execution-manifest.json",),
        )
        is None
    )


def test_directory_output_expands_and_validates_registered_artifacts(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(ws, "cases/system/api/case.yaml", {"broken": True})

    result = _validate_registry_outputs(workspace=ws, outputs=("change:cases/",))

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "case_yaml" in (result.error or "")


def test_undeclared_authored_proposal_is_validated_from_frozen_write_set(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _write(
        ws,
        "plans/data-knowledge.proposal.api.yaml",
        {
            "version": 1,
            "change_id": "CH-1",
            "proposal_kind": "delta",
            "formal_knowledge": ".aa/data-knowledge.yaml",
            "capabilities": {"fixtures": {}, "auth": {}, "cleanup": {}},
            "promotion_required": True,
        },
    )

    result = _validate_registry_outputs(
        workspace=ws,
        outputs=("change:plans/api-plan.md",),
        written_outputs=("change:plans/data-knowledge.proposal.api.yaml",),
    )

    assert result is not None
    assert result.error_kind == "invalid_output"
    assert "data_knowledge_proposal" in (result.error or "")


def test_non_change_outputs_are_ignored(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    assert (
        _validate_registry_outputs(
            workspace=ws,
            outputs=("repo:review/api-plan-review.json", "change:plans/api-plan.md"),
        )
        is None
    )
