"""aa verify — reconciled-phase evidence verdict CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.policy import EvidenceSufficiency, Policy
from assurance_agent.artifacts.models.trace import TraceExecution, TraceGap, TraceProjection, TraceRow
from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.cli import main
from assurance_agent.evidence.sufficiency import (
    SufficiencyReport,
    build_evidence_coverage_evaluation,
    evaluate_sufficiency,
)
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.evidence.verify import VERIFY_BLOCKING_GAP_CODES
from assurance_agent.commands import verify_cmd
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-VERIFY-1"
BATCH_ID = "20260729-120000"
EXECUTED_AT = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
AS_OF = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
CASE_ID = "TC_DEPT_API_001"

RECONCILED_BLOCKING_GAP_CODES = tuple(sorted(VERIFY_BLOCKING_GAP_CODES))
RECONCILED_INPUT_GAP_CODES = frozenset(
    {
        "failure_analysis_missing",
        "issues_snapshot_missing",
        "problems_snapshot_missing",
    }
)


def _write_api_case(change_dir: Path, case_id: str = CASE_ID) -> None:
    path = change_dir / "cases/dept/case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: API
    title: create
    status: active
    priority: P0
    severity: blocker
    automation:
      required: true
modified: []
removed: []
""",
        encoding="utf-8",
    )


def _write_manifest(
    change_dir: Path,
    *,
    test_files_sha256: dict[str, str] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": EXECUTED_AT.isoformat(),
        "selected_targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=False).model_dump(),
        "result_files": {},
    }
    if test_files_sha256 is not None:
        payload["test_files_sha256"] = test_files_sha256
    manifest_path = change_dir / "execution/execution-manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_api_result(change_dir: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "target": "api",
        "status": "passed",
        "command": "cmd",
        "source": {"framework": "pytest", "raw_log": "raw.log"},
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cases": [
            {
                "case_id": CASE_ID,
                "status": "passed",
                "file": "tests/api/test_dept.py",
                "test_name": "test_tc_dept_api_001__ok",
                "duration_ms": 1,
                "message": "",
            }
        ],
        "unmapped_tests": [],
    }
    batch_dir = change_dir / "execution/runs" / BATCH_ID
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "api-result.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_test_file(project_root: Path) -> str:
    rel = "tests/api/test_dept.py"
    path = project_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def test_tc_dept_api_001__ok():\n    assert True\n",
        encoding="utf-8",
    )
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_failure_analysis(change_dir: Path) -> None:
    inspect_dir = change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "source_manifest": "execution/execution-manifest.yaml",
        "inspection_status": "completed",
        "batch_id": BATCH_ID,
        "source_batch_id": BATCH_ID,
        "final_status": "PASS",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "failures": [],
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }
    (inspect_dir / "failure-analysis.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_issues_snapshot(change_dir: Path) -> None:
    issues_dir = change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "authoritative_batch_id": BATCH_ID,
        "observations": [],
        "occurrences": [],
        "project_sync_status": "completed",
        "batches": [BATCH_ID],
    }
    (issues_dir / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_problems(project_root: Path) -> None:
    problems_dir = project_root / "qa" / "issues"
    problems_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-07-29T12:00:00Z",
        "problems": [],
    }
    (problems_dir / "problems.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_policy(project_root: Path, *, on_insufficient: str = "require_human") -> None:
    aa = project_root / ".aa"
    aa.mkdir(parents=True, exist_ok=True)
    policy = Policy(
        version=1,
        human_review_risk_levels=["high"],
        force_continue_allowed=True,
        plan_checks={
            "l1_path": "warn",
            "shared_factory": "warn",
            "assert_ideal": "warn",
            "capability_keys": "warn",
        },
        coverage_floor={"risk_high": 0.9, "risk_medium": 0.7},
        fuzz={"required_when_endpoint_has_auth": True},
        healing={"auth_module": "require_human"},
        evidence_sufficiency=EvidenceSufficiency(
            recency_hours=72,
            required_kinds={
                "API": ["covered", "execution_recent"],
                "E2E": ["covered", "execution_recent"],
                "Fuzz": ["covered", "fuzz_run"],
                "Performance": ["covered", "perf_run"],
            },
            on_insufficient=on_insufficient,  # type: ignore[arg-type]
        ),
    )
    (aa / "policy.yaml").write_text(
        yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )


def _strip_test_from_tree(project_root: Path, change_dir: Path) -> None:
    """Remove mapped test from tree and sync manifest digests (avoids blocking mismatch)."""
    (project_root / "tests/api/test_dept.py").unlink(missing_ok=True)
    payload = yaml.safe_load((change_dir / "execution/execution-manifest.yaml").read_text(encoding="utf-8"))
    payload["test_files_sha256"] = {}
    (change_dir / "execution/execution-manifest.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _seed_reconciled_happy_path(
    project_root: Path,
    *,
    on_insufficient: str = "require_human",
    write_failure_analysis: bool = True,
    write_issues: bool = True,
    write_problems: bool = True,
) -> Path:
    write_aa_config(project_root)
    _write_policy(project_root, on_insufficient=on_insufficient)
    change_dir = project_root / "qa/changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_api_case(change_dir)
    digest = _write_test_file(project_root)
    _write_manifest(change_dir, test_files_sha256={"tests/api/test_dept.py": digest})
    _write_api_result(change_dir)
    if write_failure_analysis:
        _write_failure_analysis(change_dir)
    if write_issues:
        _write_issues_snapshot(change_dir)
    if write_problems:
        _write_problems(project_root)
    return change_dir


def _execution(
    *,
    ts: datetime | None = None,
    status: str = "passed",
) -> TraceExecution:
    return TraceExecution(
        batch_id=BATCH_ID,
        target="api",
        status=status,  # type: ignore[arg-type]
        ts=ts or AS_OF - timedelta(hours=1),
        ts_source="executed_at",
    )


def _row(
    *,
    coverage_state: str = "covered",
    latest_execution: TraceExecution | None = None,
    open_problem_ids: tuple[str, ...] = (),
) -> TraceRow:
    return TraceRow(
        case_id=CASE_ID,
        module="system.dept",
        case_type="API",
        automation_required=True,
        coverage_state=coverage_state,  # type: ignore[arg-type]
        latest_execution=latest_execution,
        freshest_pass=latest_execution,
        presence_in_current_batch="executed",
        atemporal_kinds_present=("covered",),
        open_problem_ids=open_problem_ids,
    )


def _policy(*, on_insufficient: str = "require_human") -> Policy:
    return Policy(
        version=1,
        human_review_risk_levels=["high"],
        force_continue_allowed=True,
        plan_checks={
            "l1_path": "warn",
            "shared_factory": "warn",
            "assert_ideal": "warn",
            "capability_keys": "warn",
        },
        coverage_floor={"risk_high": 0.9, "risk_medium": 0.7},
        fuzz={"required_when_endpoint_has_auth": True},
        healing={"auth_module": "require_human"},
        evidence_sufficiency=EvidenceSufficiency(
            recency_hours=72,
            required_kinds={
                "API": ["covered", "execution_recent"],
                "E2E": ["covered", "execution_recent"],
                "Fuzz": ["covered", "fuzz_run"],
                "Performance": ["covered", "perf_run"],
            },
            on_insufficient=on_insufficient,  # type: ignore[arg-type]
        ),
    )


def _report(*, sufficient: bool = True) -> SufficiencyReport:
    latest = _execution()
    return evaluate_sufficiency(
        TraceProjection(
            change_id=CHANGE_ID,
            phase="reconciled",
            authoritative_batch_id=BATCH_ID,
            rows=(_row(latest_execution=latest if sufficient else None),),
            integrity="complete",
        ),
        _policy(),
        as_of=AS_OF,
    )


@pytest.fixture
def project():
    runner = CliRunner()
    ctx = runner.isolated_filesystem()
    root = Path(ctx.__enter__())
    yield runner, root
    ctx.__exit__(None, None, None)


def test_missing_change_exits_40(project) -> None:
    runner, root = project
    write_aa_config(root)
    (root / "qa/changes/CH-OTHER").mkdir(parents=True)
    result = runner.invoke(main, ["verify", "--change", "NOPE"])
    assert result.exit_code == 40
    assert "NOPE" in result.stderr


def test_pass_verdict_json(project) -> None:
    runner, root = project
    _seed_reconciled_happy_path(root)
    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["verdict"] == "pass"
    assert doc["phase"] == "reconciled"
    assert doc["scope"]["cases"] == [CASE_ID]
    assert doc["scope"]["batch"] == BATCH_ID
    assert doc["scope"]["policy_digest"] == doc["policy_digest"]
    assert doc["scope"]["projection_digest"] == doc["projection_digest"]


def test_needs_human_verdict(project) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root, on_insufficient="require_human")
    _strip_test_from_tree(root, change_dir)
    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 30, result.output
    doc = json.loads(result.stdout)
    assert doc["verdict"] == "needs_human"
    assert doc["insufficient"]


def test_open_problem_ids_fail(project) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root)
    obs = {
        "observation_id": "OBS-1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "kind": "test_failure",
        "target": "api",
        "case_id": CASE_ID,
        "source": {"artifact": "inspect/failure-analysis.json", "json_pointer": "/failures/0"},
        "evidence_refs": ["evidence:1"],
        "signature": "sig-1",
        "observed_at": "2026-07-29T12:00:00Z",
    }
    occ = {
        "occurrence_id": "OCC-1",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "observation_ids": ["OBS-1"],
        "problem_id": "PROB-1",
        "provisional_assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "hypothesis",
        },
        "analysis": {
            "evidence_bundle_digest": "sha256:evidence",
            "analyzer": "test-analyzer",
            "prompt_version": "1.0",
            "candidate_digest": "sha256:candidate",
        },
    }
    problem = {
        "problem_id": "PROB-1",
        "fingerprint": {"version": "1", "digest": "sha256:prob-1"},
        "title": "Problem 1",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "hypothesis",
        },
        "status": "detected",
        "first_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-1"},
        "occurrences": ["OCC-1"],
        "verification_request": None,
        "resolution": None,
        "version": 1,
    }
    (change_dir / "issues/snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "authoritative_batch_id": BATCH_ID,
                "observations": [obs],
                "occurrences": [occ],
                "project_sync_status": "completed",
                "batches": [BATCH_ID],
            }
        ),
        encoding="utf-8",
    )
    (root / "qa/issues/problems.json").write_text(
        json.dumps({"schema_version": "1.0", "generated_at": "2026-07-29T12:00:00Z", "problems": [problem]}),
        encoding="utf-8",
    )
    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 40, result.output
    doc = json.loads(result.stdout)
    assert doc["verdict"] == "fail"
    assert doc["open_problem_ids"] == ["PROB-1"]


@pytest.mark.parametrize("gap_code", RECONCILED_BLOCKING_GAP_CODES)
def test_blocking_gap_codes_fail(gap_code: str) -> None:
    gap = TraceGap(code=gap_code, source=f"src/{gap_code}")  # type: ignore[arg-type]
    projection = TraceProjection(
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        rows=(_row(latest_execution=_execution()),),
        gaps=(gap,),
        integrity="incomplete",
    )
    result = verify_cmd.evaluate_verify_verdict(
        projection,
        _policy(on_insufficient="warn"),
        _report(),
    )
    assert result.verdict == "fail"
    assert any(item.code == gap_code for item in result.blocking_gaps)


def test_blocking_gap_manifest_missing_cli(project) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root, on_insufficient="warn")
    (change_dir / "execution/execution-manifest.yaml").unlink()
    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 40, result.output
    doc = json.loads(result.stdout)
    assert doc["verdict"] == "fail"
    assert any(item["code"] == "manifest_missing" for item in doc["blocking_gaps"])


@pytest.mark.parametrize(
    ("gap_code", "mutator"),
    [
        (
            "case_unreadable",
            lambda root, change_dir: (change_dir / "cases/dept/case.yaml").write_text(
                "added: not-a-list\n", encoding="utf-8"
            ),
        ),
        (
            "result_corrupt",
            lambda root, change_dir: (
                change_dir / "execution/runs" / BATCH_ID / "api-result.json"
            ).write_text("{bad", encoding="utf-8"),
        ),
        (
            "result_identity_mismatch",
            lambda root, change_dir: (
                change_dir / "execution/runs" / BATCH_ID / "api-result.json"
            ).write_text(
                json.dumps(
                    {
                        **json.loads(
                            (change_dir / "execution/runs" / BATCH_ID / "api-result.json").read_text()
                        ),
                        "change_id": "OTHER",
                    }
                )
            ),
        ),
        (
            "failure_analysis_missing",
            lambda root, change_dir: (change_dir / "inspect/failure-analysis.json").unlink(),
        ),
        (
            "issues_snapshot_missing",
            lambda root, change_dir: (change_dir / "issues/snapshot.json").unlink(),
        ),
        (
            "problems_snapshot_missing",
            lambda root, change_dir: (root / "qa/issues/problems.json").unlink(),
        ),
        (
            "tests_tree_digest_mismatch",
            lambda root, change_dir: (
                lambda payload: (
                    payload.update({"test_files_sha256": {"tests/api/test_dept.py": "deadbeef" * 8}}),
                    (change_dir / "execution/execution-manifest.yaml").write_text(
                        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
                    ),
                )
            )(yaml.safe_load((change_dir / "execution/execution-manifest.yaml").read_text(encoding="utf-8"))),
        ),
    ],
)
def test_blocking_gap_codes_fail_cli(project, gap_code: str, mutator) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root, on_insufficient="warn")
    mutator(root, change_dir)
    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 40, result.output
    doc = json.loads(result.stdout)
    assert doc["verdict"] == "fail"
    assert any(item["code"] == gap_code for item in doc["blocking_gaps"])


@pytest.mark.parametrize("gap_code", sorted(RECONCILED_INPUT_GAP_CODES))
def test_reconciled_input_gaps_fail_even_when_on_insufficient_warn(project, gap_code: str) -> None:
    runner, root = project
    _seed_reconciled_happy_path(root, on_insufficient="warn")
    change_dir = root / "qa/changes" / CHANGE_ID
    if gap_code == "failure_analysis_missing":
        (change_dir / "inspect/failure-analysis.json").unlink()
    elif gap_code == "issues_snapshot_missing":
        (change_dir / "issues/snapshot.json").unlink()
    else:
        (root / "qa/issues/problems.json").unlink()

    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 40, result.output
    doc = json.loads(result.stdout)
    assert doc["verdict"] == "fail"
    assert any(item["code"] == gap_code for item in doc["blocking_gaps"])


def test_mapped_test_missing_from_tree_follows_sufficiency_policy(project) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root, on_insufficient="warn")
    _strip_test_from_tree(root, change_dir)

    warn_result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert warn_result.exit_code == 0, warn_result.output
    warn_doc = json.loads(warn_result.stdout)
    assert warn_doc["verdict"] == "pass"
    assert warn_doc["warnings"]
    assert not warn_doc["blocking_gaps"]
    projection = fold_trace(root, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "mapped_test_missing_from_tree" for gap in projection.gaps)

    _write_policy(root, on_insufficient="block")
    block_result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert block_result.exit_code == 40, block_result.output
    block_doc = json.loads(block_result.stdout)
    assert block_doc["verdict"] == "fail"
    assert block_doc["insufficient"]


def test_policy_digest_changes_when_policy_changes(project) -> None:
    runner, root = project
    _seed_reconciled_happy_path(root, on_insufficient="warn")
    first = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert first.exit_code == 0
    first_digest = json.loads(first.stdout)["policy_digest"]

    _write_policy(root, on_insufficient="block")
    second = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    second_digest = json.loads(second.stdout)["policy_digest"]
    assert first_digest != second_digest
    assert second_digest == policy_digest(load_policy(root))


def test_runner_and_verify_share_sufficiency_evaluation(project) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root, on_insufficient="warn")
    _strip_test_from_tree(root, change_dir)

    projection = fold_trace(root, CHANGE_ID, phase="reconciled")
    policy = load_policy(root)
    evaluation = build_evidence_coverage_evaluation(projection, policy, as_of=AS_OF)
    gate = build_quality_gate(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        api=None,
        e2e=None,
        coverage=None,
        evidence_coverage=evaluation,
    )
    verify_result = verify_cmd.evaluate_verify_verdict(
        projection,
        policy,
        evaluation.report,  # type: ignore[arg-type]
    )
    assert gate.dimensions.coverage.status == "PASS_WITH_WARNINGS"
    assert verify_result.verdict == "pass"
    assert verify_result.warnings
    _ = runner


def test_naive_as_of_is_rejected_by_run_verify(project) -> None:
    _seed_reconciled_happy_path(project[1])
    code = verify_cmd.run_verify(
        CHANGE_ID,
        as_json=False,
        as_of=datetime(2026, 7, 30, 12, 0, 0),
    )
    assert code == 40


def test_evaluate_verify_verdict_unit_cases() -> None:
    policy = _policy(on_insufficient="block")
    report = _report(sufficient=False)
    projection = TraceProjection(
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        rows=(_row(latest_execution=None),),
        integrity="complete",
    )
    blocked = verify_cmd.evaluate_verify_verdict(projection, policy, report)
    assert blocked.verdict == "fail"
    assert blocked.insufficient

    human_policy = _policy(on_insufficient="require_human")
    needs_human = verify_cmd.evaluate_verify_verdict(projection, human_policy, report)
    assert needs_human.verdict == "needs_human"

    gap_projection = TraceProjection(
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        rows=(_row(latest_execution=_execution()),),
        gaps=(TraceGap(code="manifest_missing", source="execution/execution-manifest.yaml"),),
        integrity="incomplete",
    )
    blocked_gap = verify_cmd.evaluate_verify_verdict(
        gap_projection,
        _policy(on_insufficient="warn"),
        _report(),
    )
    assert blocked_gap.verdict == "fail"
    assert blocked_gap.blocking_gaps[0].code == "manifest_missing"


def test_on_disk_projection_matches_verify_fold_shared_fields(project) -> None:
    runner, root = project
    change_dir = _seed_reconciled_happy_path(root)
    live = fold_trace(root, CHANGE_ID, phase="reconciled")
    authoritative_path = change_dir / "inspect/trace-projection.json"
    authoritative_path.parent.mkdir(parents=True, exist_ok=True)
    authoritative_path.write_text(live.model_dump_json(indent=2), encoding="utf-8")

    shared_fields = (
        "case_id",
        "module",
        "case_type",
        "automation_required",
        "covering_tests",
        "coverage_state",
        "latest_execution",
        "freshest_pass",
        "presence_in_current_batch",
        "atemporal_kinds_present",
    )
    on_disk = json.loads(authoritative_path.read_text(encoding="utf-8"))
    result = runner.invoke(main, ["verify", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 0, result.output
    verify_projection = fold_trace(root, CHANGE_ID, phase="reconciled")
    for disk_row, verify_row in zip(on_disk["rows"], verify_projection.rows, strict=True):
        for field in shared_fields:
            expected = getattr(verify_row, field)
            if hasattr(expected, "model_dump"):
                expected = expected.model_dump(mode="json")
            elif isinstance(expected, tuple) and expected and hasattr(expected[0], "model_dump"):
                expected = [item.model_dump(mode="json") for item in expected]
            elif isinstance(expected, tuple):
                expected = list(expected)
            assert disk_row[field] == expected


def test_help_lists_verify_command() -> None:
    result = CliRunner().invoke(main, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--change" in result.output
