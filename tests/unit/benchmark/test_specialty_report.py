from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast, get_args

import pytest
import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.artifacts.models.trace import TraceProjectionV2, load_trace_projection_document
from assurance_agent.artifacts.policy import load_policy_bytes, policy_digest
from assurance_agent.eval.specialty_models import (
    CompleteTraceabilityEvidenceV3,
    IncompleteTraceabilityEvidenceV3,
    LegacySpecialtyReportV1,
    SpecialtyReportV2,
    SpecialtyReportV3,
    TraceCollectionFailureReason,
    build_capability_replay_v2,
    load_specialty_report,
    load_specialty_report_document,
)
from assurance_agent.eval.specialty_render import render_specialty_sections
from assurance_agent.eval.specialty_replay import collect_capability_policy_replay
from assurance_agent.evidence.current_projection import load_current_reconciled_projection
from assurance_agent.evidence.layer_summary import join_layer_sufficiency, summarize_projection_by_layer
from assurance_agent.evidence.sufficiency import build_evidence_coverage_evaluation, evaluate_sufficiency
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.evidence.verify import evaluate_verify_verdict
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.execution.results import CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.graph.definition_pinning import policy_snapshot_relpath
from assurance_agent.workflow.graph.replay_binding import normalize_logical_path
from assurance_agent.workflow.graph.workspace import TreeStore
from assurance_agent.workflow.improvements.ledger import atomic_write_json
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import AWARE_NOW
from tests.unit.workflow.graph.test_replay_binding import (
    _CHANGE_ID,
    _ENTRYPOINT,
    _ROOT_INV,
    _build_fixture,
)

CHANGE_ID = _CHANGE_ID
ROOT_INVOCATION_ID = _ROOT_INV
_BATCH_ID = "20260730-120000"

_ROOT = Path(__file__).parents[3]
_REPORTER = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "benchmark_specialty_report.py"
_HELPERS = _REPORTER.with_name("cursor-loop-helpers.sh")
_CURSOR_LOOP = _REPORTER.with_name("run-workflow-loop-cursor.sh")
_GOLDEN_SHA256 = "ffecb094efafac8a4d1115ae472d94c3d9418e933407ad28021333cee8f1f72a"


def _specialty_stage_function_source() -> str:
    text = _CURSOR_LOOP.read_text(encoding="utf-8")
    start = text.index("run_specialty_report_stage() {")
    end = text.index("\nreuse_specialty_report_stage() {", start)
    return text[start:end].rstrip()


def _run_specialty_report_stage(
    tmp_path: Path,
    *,
    change_id: str,
    project: Path,
    trace_path: Path,
    verify_path: Path,
    trace_exit: str,
    verify_exit: str,
    root_invocation_id: str,
    workflow_entrypoint: str = _ENTRYPOINT,
) -> subprocess.CompletedProcess[str]:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    trace_file = run_dir / f"{change_id}.trace.json"
    verify_file = run_dir / f"{change_id}.verify.json"
    trace_file.write_text(trace_path.read_text(encoding="utf-8"), encoding="utf-8")
    verify_file.write_text(verify_path.read_text(encoding="utf-8"), encoding="utf-8")
    if root_invocation_id:
        state_file = run_dir / f"{change_id}.workflow-root.json"
        state_file.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "change_id": change_id,
                    "entrypoint": workflow_entrypoint,
                    "root_invocation_id": root_invocation_id,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    evidence_row = f"{change_id}|{trace_exit}|degraded|1|{verify_exit}|fail|0|0"
    stage_fn = _specialty_stage_function_source()
    script = f"""
set -uo pipefail
source {shlex.quote(str(_HELPERS))}
RUN_DIR={shlex.quote(str(run_dir))}
PROJECT_ROOT={shlex.quote(str(project))}
AA_REPO_ROOT={shlex.quote(str(_ROOT))}
AA_PYTHON_BIN={shlex.quote(sys.executable)}
SPECIALTY_REPORT_PY={shlex.quote(str(_REPORTER))}
DO_SPECIALTY_REPORT=true
DO_TRACE_VERIFY=true
DRIVER_ENTRYPOINT={shlex.quote(workflow_entrypoint)}
declare -a EVIDENCE_ROWS=({shlex.quote(evidence_row)})
declare -a SPECIALTY_REPORT_FILES=()
SPECIALTY_REPORT_FAILED=false
stage_exit=0
log() {{ printf '%s\\n' "$*"; }}
{stage_fn}
run_specialty_report_stage {shlex.quote(change_id)} || stage_exit=$?
printf 'stage_exit=%s\\n' "$stage_exit"
printf 'SPECIALTY_REPORT_FAILED=%s\\n' "$SPECIALTY_REPORT_FAILED"
if ((${{#SPECIALTY_REPORT_FILES[@]}})); then
  printf 'SPECIALTY_REPORT_FILES=%s\\n' "${{SPECIALTY_REPORT_FILES[@]}}"
else
  printf 'SPECIALTY_REPORT_FILES=\\n'
fi
exit "$stage_exit"
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _install_strict_frozen_item(tmp_path: Path) -> tuple[Path, Path, Path]:
    fixture = _build_fixture(tmp_path)
    change_dir = fixture.change_dir
    store = TreeStore(change_dir)
    profile = get_layer_assurance_profile("api")
    tree = fixture.gate_trees["api"]
    for art in [profile.review_artifact, profile.checks_artifact]:
        resolved = store.read_json(tree, normalize_logical_path(f"change:{art}"))
        path = change_dir / art
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(resolved.value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    trace_rows = [
        {
            "case_id": "TC_API_001",
            "module": "system.api",
            "case_type": "API",
            "automation_required": True,
            "assertions": ["returns 200"],
            "covering_tests": [{"file": "tests/api/test_api.py", "function": "test_api"}],
            "coverage_state": "covered",
            "latest_execution": None,
            "freshest_pass": None,
            "presence_in_current_batch": "executed",
            "atemporal_kinds_present": ["covered"],
            "failures": [],
            "open_problem_ids": [],
        },
        {
            "case_id": "TC_FUZZ_001",
            "module": "system.api",
            "case_type": "Fuzz",
            "automation_required": True,
            "assertions": ["does not crash"],
            "covering_tests": [],
            "coverage_state": "uncovered",
            "latest_execution": None,
            "freshest_pass": None,
            "presence_in_current_batch": "not_in_current_batch",
            "atemporal_kinds_present": [],
            "failures": [],
            "open_problem_ids": [],
        },
    ]
    trace = {
        "schema_version": "1",
        "change_id": _CHANGE_ID,
        "phase": "execution",
        "authoritative_batch_id": "batch-1",
        "integrity": "degraded",
        "sources": [
            {"path": "case.yaml", "exists": True, "sha256": "case-digest"},
            {"path": "api-result.json", "exists": True, "sha256": "result-digest"},
        ],
        "rows": trace_rows,
        "unmapped_tests": [{"file": "tests/api/test_extra.py", "test_name": "test_extra"}],
        "gaps": [
            {
                "code": "mapped_test_missing_from_tree",
                "source": "tests",
                "detail": "missing generated test",
            }
        ],
    }
    reconciled = {
        **trace,
        "phase": "reconciled",
        "integrity": "complete",
        "sources": [
            {"path": "case.yaml", "exists": True, "sha256": "case-digest"},
            {"path": "api-result.json", "exists": True, "sha256": "result-digest"},
            {"path": "issues/snapshot.json", "exists": True, "sha256": "issue-digest"},
        ],
        "gaps": [],
        "rows": [
            {
                **trace_rows[0],
                "failures": [
                    {"category": "assertion_failure", "severity": "high"},
                    {"category": "contract_failure", "severity": "medium"},
                ],
                "open_problem_ids": ["PROB-1", "PROB-2"],
            },
            trace_rows[1],
        ],
    }
    trace_path = tmp_path / "trace.json"
    _write_json(trace_path, trace)
    _write_json(change_dir / "inspect" / "trace-projection.json", reconciled)
    reconciled_digest = hashlib.sha256(
        json.dumps(reconciled, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    quality_gate = {
        "final_status": "FAIL",
        "schema_version": "1.0",
        "change_id": _CHANGE_ID,
        "batch_id": "batch-1",
        "dimensions": {
            "functional": {
                "status": "FAIL",
                "api": {"total": 1, "passed": 0, "failed": 1},
                "e2e": {"total": 0, "passed": 0, "failed": 0},
                "fuzz": {"total": 0, "passed": 0, "failed": 0},
                "unmapped_tests": 1,
            },
            "coverage": {
                "status": "PASS",
                "available": True,
                "line_coverage": 48.35,
                "branch_coverage": 2.58,
                "threshold": {"line": 70.0, "branch": 60.0},
                "scope": None,
                "evidence": {
                    "as_of": "2026-07-30T00:00:00Z",
                    "recency_hours": 72,
                    "verdicts": [
                        {
                            "case_id": "TC_API_001",
                            "sufficient": True,
                            "missing_kinds": [],
                            "reason_codes": [],
                            "execution_state": "fresh",
                        },
                        {
                            "case_id": "TC_FUZZ_001",
                            "sufficient": False,
                            "missing_kinds": ["fuzz_run"],
                            "reason_codes": ["missing_fuzz_run", "never_run"],
                            "execution_state": "never_run",
                        },
                    ],
                },
            },
        },
    }
    _write_json(change_dir / "execution" / "quality-gate-result.json", quality_gate)

    verify_path = tmp_path / "verify.json"
    _write_json(
        verify_path,
        {
            "verdict": "fail",
            "change_id": _CHANGE_ID,
            "phase": "reconciled",
            "as_of": "2026-07-30T00:00:00Z",
            "policy_digest": "verify-policy",
            "projection_digest": reconciled_digest,
            "blocking_gaps": [],
            "open_problem_ids": ["PROB-1", "PROB-2"],
            "insufficient": [],
        },
    )
    return fixture.project, trace_path, verify_path


def _load_reporter_module() -> Any:
    name = "benchmark_specialty_report_under_test"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, _REPORTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_case_yaml(
    change_dir: Path,
    *,
    rel: str,
    case_id: str,
    case_type: str,
) -> None:
    performance_block = ""
    if case_type == "Performance":
        performance_block = """      performance:
        scenario:
          capability: dept_list
"""
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: {case_type}
    title: t
    status: active
    priority: P0
    severity: blocker
    automation:
      required: true
{performance_block}modified: []
removed: []
""",
        encoding="utf-8",
    )


def _write_target_result(
    change_dir: Path,
    *,
    target: str,
    case_id: str,
    batch_id: str = _BATCH_ID,
) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "target": target,
        "status": "passed",
        "command": "cmd",
        "source": {"framework": "pytest", "raw_log": "raw.log"},
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cases": [
            {
                "case_id": case_id,
                "status": "passed",
                "file": f"tests/{target}/test_x.py",
                "test_name": f"test_{case_id.lower()}__ok",
                "duration_ms": 1,
                "message": "",
            }
        ],
        "unmapped_tests": [],
    }
    path = change_dir / "execution" / "runs" / batch_id / f"{target}-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_performance_result(change_dir: Path, *, batch_id: str = _BATCH_ID) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "kind": "performance",
        "available": True,
        "status": "PASS",
        "scenarios": [
            {
                "capability": "dept_list",
                "endpoint": "/dept",
                "measured_p95_ms": 100.0,
                "threshold_p95_ms": 200.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": "PASS",
            }
        ],
        "command": "",
        "source": {},
    }
    path = change_dir / "execution" / "runs" / batch_id / "performance-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _install_authority_valid_complete_item(
    tmp_path: Path,
    *,
    include_layers: frozenset[str] | None = None,
) -> tuple[Path, Path, Path]:
    """Build a fully authority-valid item via production constructors (no digest patches)."""
    layers = include_layers or frozenset({"api", "e2e", "fuzz", "performance"})
    fixture = _build_fixture(tmp_path)
    change_dir = fixture.change_dir
    project = fixture.project

    if "api" in layers:
        _write_case_yaml(change_dir, rel="cases/api/case.yaml", case_id="TC_API_001", case_type="API")
        _write_target_result(change_dir, target="api", case_id="TC_API_001")
    if "e2e" in layers:
        _write_case_yaml(change_dir, rel="cases/e2e/case.yaml", case_id="TC_E2E_001", case_type="E2E")
        _write_target_result(change_dir, target="e2e", case_id="TC_E2E_001")
    if "fuzz" in layers:
        _write_case_yaml(change_dir, rel="cases/fuzz/case.yaml", case_id="TC_FUZZ_001", case_type="Fuzz")
        _write_target_result(change_dir, target="fuzz", case_id="TC_FUZZ_001")
    if "performance" in layers:
        _write_case_yaml(
            change_dir, rel="cases/perf/case.yaml", case_id="TC_PERF_001", case_type="Performance"
        )
        _write_performance_result(change_dir)

    selected = SelectedTargets(
        api="api" in layers,
        e2e="e2e" in layers,
        fuzz="fuzz" in layers,
        performance="performance" in layers,
    )
    manifest = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": _BATCH_ID,
        "executed_at": AWARE_NOW.isoformat(),
        "selected_targets": selected.model_dump(),
        "result_files": {},
    }
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    execution = fold_trace(project, CHANGE_ID, phase="execution")
    reconciled = fold_trace(project, CHANGE_ID, phase="reconciled")
    assert isinstance(execution, TraceProjectionV2)
    assert isinstance(reconciled, TraceProjectionV2)

    execution_trace_path = tmp_path / "trace.json"
    verify_path = tmp_path / "verify.json"
    atomic_write_json(execution_trace_path, execution.model_dump(mode="json"))
    atomic_write_json(
        change_dir / "inspect" / "trace-projection.json",
        reconciled.model_dump(mode="json"),
    )
    # Prove the persisted reconciled artifact is byte-current before collection.
    assert load_current_reconciled_projection(project, CHANGE_ID).model_dump(
        mode="json"
    ) == reconciled.model_dump(mode="json")

    capability = collect_capability_policy_replay(
        change_dir=change_dir,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        expected_entrypoint=_ENTRYPOINT,
    )
    binding = capability.definition_binding
    assert binding is not None
    policy = load_policy_bytes(
        (change_dir / policy_snapshot_relpath(binding.baseline_policy_digest)).read_bytes(),
        origin="pinned",
    )
    assert policy_digest(policy) == binding.baseline_policy_digest

    sufficiency = evaluate_sufficiency(
        execution,
        policy,
        as_of=AWARE_NOW,
        require_current_batch=True,
    )
    evidence_coverage = build_evidence_coverage_evaluation(execution, policy, as_of=AWARE_NOW)
    source = ResultSource(framework="pytest", raw_log="")
    api = (
        TargetResult(
            change_id=CHANGE_ID,
            batch_id=_BATCH_ID,
            target="api",
            status="passed",
            command="cmd",
            source=source,
            total=1,
            passed=1,
            failed=0,
            skipped=0,
            cases=[],
            unmapped_tests=[],
        )
        if "api" in layers
        else None
    )
    e2e = (
        TargetResult(
            change_id=CHANGE_ID,
            batch_id=_BATCH_ID,
            target="e2e",
            status="passed",
            command="cmd",
            source=source,
            total=1,
            passed=1,
            failed=0,
            skipped=0,
            cases=[],
            unmapped_tests=[],
        )
        if "e2e" in layers
        else None
    )
    fuzz = (
        TargetResult(
            change_id=CHANGE_ID,
            batch_id=_BATCH_ID,
            target="fuzz",
            status="passed",
            command="cmd",
            source=source,
            total=1,
            passed=1,
            failed=0,
            skipped=0,
            cases=[],
            unmapped_tests=[],
        )
        if "fuzz" in layers
        else None
    )
    coverage = CoverageResult(
        change_id=CHANGE_ID,
        batch_id=_BATCH_ID,
        available=True,
        line_coverage=80.0,
        branch_coverage=70.0,
        threshold=CoverageThreshold(line=70.0, branch=60.0),
        status="PASS",
    )
    quality = build_quality_gate(
        change_id=CHANGE_ID,
        batch_id=_BATCH_ID,
        api=api,
        e2e=e2e,
        coverage=coverage,
        evidence_coverage=evidence_coverage,
        fuzz=fuzz,
    )
    atomic_write_json(
        change_dir / "execution" / "quality-gate-result.json",
        quality.model_dump(mode="json"),
    )
    verify = evaluate_verify_verdict(reconciled, policy, sufficiency)
    atomic_write_json(verify_path, verify.model_dump(mode="json"))
    return project, execution_trace_path, verify_path


def _change_dir(project: Path) -> Path:
    return project / "qa" / "changes" / _CHANGE_ID


def _read_events(change_dir: Path) -> list[dict[str, object]]:
    path = change_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_events(change_dir: Path, events: list[dict[str, object]]) -> None:
    (change_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def _root_event_field(change_dir: Path, field: str) -> str:
    for event in _read_events(change_dir):
        if event.get("type") == "graph_invocation_started" and event.get("invocation_id") == _ROOT_INV:
            return str(event[field])
    raise AssertionError(f"missing root event field {field!r}")


def _drop_policy_snapshot(change_dir: Path) -> None:
    digest = _root_event_field(change_dir, "policy_digest")
    (change_dir / policy_snapshot_relpath(digest)).unlink()


def _drop_pinned_schema(change_dir: Path) -> None:
    digest = _root_event_field(change_dir, "graph_digest")
    (change_dir / ".graph-runtime" / "schemas" / f"{digest}.json").unlink()


def _corrupt_pinned_schema_digest(change_dir: Path) -> None:
    digest = _root_event_field(change_dir, "graph_digest")
    schema_path = change_dir / ".graph-runtime" / "schemas" / f"{digest}.json"
    schema_path.write_text('{"schema_version":"2","name":"bad"}\n', encoding="utf-8")


def _corrupt_policy_snapshot_bytes(change_dir: Path) -> None:
    digest = _root_event_field(change_dir, "policy_digest")
    policy_path = change_dir / policy_snapshot_relpath(digest)
    policy_path.write_bytes(b"policy: corrupted\n")


def _corrupt_api_mechanical_outputs(change_dir: Path) -> None:
    events = _read_events(change_dir)
    profile = get_layer_assurance_profile("api")
    checks_path = normalize_logical_path(f"change:{profile.checks_artifact}")
    for event in events:
        task_id = str(event.get("task_id", ""))
        if event.get("type") == "task_attempt_succeeded" and task_id.endswith(":mechanical-plan-checks"):
            if "api-plan-cycle" not in task_id:
                continue
            event["outputs_sha256"] = {checks_path: "0" * 64}
    _write_events(change_dir, events)


def _strip_api_gate_reads_sha256(change_dir: Path) -> None:
    events = _read_events(change_dir)
    for event in events:
        task_id = str(event.get("task_id", ""))
        if event.get("type") == "task_attempt_succeeded" and task_id.endswith(":review-gate"):
            if "api-plan-cycle" not in task_id:
                continue
            gate_report = dict(cast(dict[str, object], event.get("gate_report") or {}))
            gate_report.pop("reads_sha256", None)
            event["gate_report"] = gate_report
    _write_events(change_dir, events)


def _assert_incomplete_v2_collect(
    result: subprocess.CompletedProcess[str],
    output: Path,
    *,
    definition_failure: str | None = None,
    layer_reason: tuple[str, str] | None = None,
) -> SpecialtyReportV2:
    assert result.returncode != 0
    report = load_specialty_report(json.loads(output.read_text(encoding="utf-8")))
    assert isinstance(report, SpecialtyReportV2)
    assert report.schema_version == "2"
    capability = report.capability_contract_policy
    assert capability.integrity == "incomplete"
    if definition_failure is not None:
        assert capability.definition_failure == definition_failure
        assert capability.definition_binding is None
        assert all(row.status == "incomplete" for row in capability.rows)
    if layer_reason is not None:
        layer, reason_code = layer_reason
        assert capability.definition_binding is not None
        assert capability.definition_failure is None
        by_layer = {row.layer: row for row in capability.rows}
        assert by_layer[layer].status == "incomplete"
        assert by_layer[layer].reason_code == reason_code
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []
    return report


def _collect_command(
    *,
    project: Path,
    trace_path: Path,
    verify_path: Path,
    output: Path,
    trace_exit: str = "0",
    verify_exit: str = "40",
    change_id: str = _CHANGE_ID,
    root_invocation_id: str = _ROOT_INV,
    workflow_entrypoint: str = _ENTRYPOINT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--change-id",
            change_id,
            "--root-invocation-id",
            root_invocation_id,
            "--workflow-entrypoint",
            workflow_entrypoint,
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            trace_exit,
            "--verify-exit",
            verify_exit,
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_collect_freezes_capability_policy_replay_and_trace_evidence(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"

    first = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
        trace_exit="9",
        verify_exit="40",
    )
    second = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=tmp_path / "specialty-2.json",
        trace_exit="9",
        verify_exit="40",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_bytes = output.read_bytes()
    second_bytes = (tmp_path / "specialty-2.json").read_bytes()
    assert first_bytes == second_bytes
    assert hashlib.sha256(first_bytes).hexdigest() == _GOLDEN_SHA256

    report = load_specialty_report(json.loads(first_bytes.decode("utf-8")))
    assert isinstance(report, SpecialtyReportV2)
    assert report.schema_version == "2"
    capability = report.capability_contract_policy
    assert capability.semantics == "counterfactual_plan_check_actions/v2"
    assert capability.integrity == "complete"
    assert capability.definition_binding is not None
    assert capability.definition_binding.root_invocation_id == _ROOT_INV
    assert capability.definition_binding.gate_definition_source == "pinned_schema"
    assert [row.layer for row in capability.rows] == ["api", "e2e", "fuzz", "performance"]
    assert [row.status for row in capability.rows] == [
        "complete",
        "complete",
        "not_selected",
        "not_selected",
    ]

    api_row = capability.rows[0]
    assert api_row.status == "complete"
    assert [item.action for item in api_row.scenarios] == ["warn", "block", "require_human"]
    assert [(item.action, item.verdict) for item in api_row.scenarios] == [
        ("warn", "pass"),
        ("block", "reject"),
        ("require_human", "needs_human_review"),
    ]
    assert [item.check_id for item in api_row.mechanical_checks.checks] == [
        "l1_path",
        "shared_factory",
        "assert_ideal",
        "capability_keys",
    ]
    assert api_row.mechanical_checks.status == "fail"
    assert api_row.mechanical_checks.finding_count == 1

    evidence = report.traceability_evidence
    assert evidence["command_status"] == {"trace_exit": 9, "verify_exit": 40}
    assert evidence["execution_projection"] == {
        "phase": "execution",
        "batch_id": "batch-1",
        "integrity": "degraded",
        "row_count": 2,
        "source_count": 2,
        "gap_count": 1,
        "unmapped_test_count": 1,
    }
    assert evidence["reconciled_projection"] == {
        "phase": "reconciled",
        "batch_id": "batch-1",
        "integrity": "complete",
        "row_count": 2,
        "source_count": 3,
        "gap_count": 0,
        "unmapped_test_count": 1,
        "failure_row_count": 1,
        "failure_link_count": 2,
        "open_problem_row_count": 1,
        "open_problem_link_count": 2,
        "unique_open_problem_count": 2,
    }
    assert evidence["sufficiency"] == {
        "sufficient_count": 1,
        "insufficient_count": 1,
        "reason_counts": {"missing_fuzz_run": 1, "never_run": 1},
    }
    assert evidence["coverage"] == {
        "status": "PASS",
        "line": 48.35,
        "branch": 2.58,
        "final_status": "FAIL",
    }
    assert evidence["verify"]["open_problem_count"] == 2
    assert evidence["verify"]["reported_insufficient_count"] == 0
    assert evidence["verify"]["observed_insufficient_count"] == 1


def _mutate_all_started_field(change_dir: Path, field: str, value: str) -> None:
    events = _read_events(change_dir)
    for event in events:
        if event.get("type") == "graph_invocation_started":
            event[field] = value
    _write_events(change_dir, events)


@pytest.mark.parametrize(
    ("failure_code", "mutator", "collect_overrides"),
    [
        ("root_invocation_unbound", lambda _change_dir: None, {"root_invocation_id": "missing-root"}),
        ("policy_snapshot_missing", _drop_policy_snapshot, {}),
        ("pinned_schema_missing", _drop_pinned_schema, {}),
        ("pinned_schema_digest_mismatch", _corrupt_pinned_schema_digest, {}),
        ("policy_digest_mismatch", _corrupt_policy_snapshot_bytes, {}),
    ],
)
def test_collect_writes_incomplete_v2_for_definition_integrity_failures(
    tmp_path: Path,
    failure_code: str,
    mutator: object,
    collect_overrides: dict[str, str],
) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    change_dir = _change_dir(project)
    mutator(change_dir)  # type: ignore[operator]
    output = tmp_path / f"specialty-def-{failure_code}.json"

    result = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
        **collect_overrides,
    )

    _assert_incomplete_v2_collect(result, output, definition_failure=failure_code)


@pytest.mark.parametrize(
    ("layer", "failure_code", "mutator"),
    [
        ("api", "mechanical_producer_unbound", _corrupt_api_mechanical_outputs),
        ("api", "gate_evidence_unbound", _strip_api_gate_reads_sha256),
        (
            "api",
            "gate_semantics_mismatch",
            lambda cd: _mutate_all_started_field(cd, "gate_semantics_digest", "0" * 64),
        ),
        (
            "api",
            "profile_definition_incompatible",
            lambda cd: _mutate_all_started_field(cd, "assurance_profile_digest", "0" * 64),
        ),
    ],
)
def test_collect_writes_incomplete_v2_for_evidence_integrity_failures(
    tmp_path: Path,
    layer: str,
    failure_code: str,
    mutator: object,
) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    mutator(_change_dir(project))  # type: ignore[operator]
    output = tmp_path / f"specialty-evidence-{failure_code}.json"

    result = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
    )

    _assert_incomplete_v2_collect(result, output, layer_reason=(layer, failure_code))


def test_collect_incomplete_report_is_byte_stable_and_leaves_no_temp_files(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty-incomplete.json"

    first = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
        root_invocation_id="missing-root",
    )
    second = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=tmp_path / "specialty-incomplete-2.json",
        root_invocation_id="missing-root",
    )

    assert first.returncode != 0
    assert second.returncode != 0
    assert output.read_bytes() == (tmp_path / "specialty-incomplete-2.json").read_bytes()
    assert list(output.parent.glob(".*.tmp")) == []


def test_v1_report_loads_for_evidence_row_but_rejects_v2_validation(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    v1_output = tmp_path / "legacy-v1.json"
    _write_json(
        v1_output,
        {
            "schema_version": "1",
            "change_id": _CHANGE_ID,
            "capability_contract_policy": {"capabilities": {"required": [], "missing": []}},
            "traceability_evidence": {
                "command_status": {"trace_exit": 9, "verify_exit": 40},
                "execution_projection": {
                    "integrity": "degraded",
                    "gap_count": 1,
                },
                "verify": {
                    "verdict": "fail",
                    "blocking_gap_count": 0,
                    "reported_insufficient_count": 0,
                },
            },
        },
    )

    loaded = load_specialty_report(json.loads(v1_output.read_text(encoding="utf-8")))
    assert isinstance(loaded, LegacySpecialtyReportV1)
    with pytest.raises(ValidationError):
        SpecialtyReportV2.model_validate(loaded.model_dump(mode="json"))

    row = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "evidence-row",
            "--change-id",
            _CHANGE_ID,
            str(v1_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert row.returncode == 0, row.stderr
    assert row.stdout == f"{_CHANGE_ID}|9|degraded|1|40|fail|0|0\n"


def _minimal_traceability(*, change_id: str) -> dict[str, object]:
    return {
        "command_status": {"trace_exit": 9, "verify_exit": 40},
        "execution_projection": {
            "phase": "execution",
            "batch_id": "batch-1",
            "integrity": "degraded",
            "row_count": 2,
            "source_count": 2,
            "gap_count": 1,
            "unmapped_test_count": 1,
        },
        "reconciled_projection": {
            "phase": "reconciled",
            "batch_id": "batch-1",
            "integrity": "complete",
            "row_count": 2,
            "source_count": 3,
            "gap_count": 0,
            "unmapped_test_count": 1,
            "failure_row_count": 1,
            "failure_link_count": 2,
            "open_problem_row_count": 1,
            "open_problem_link_count": 2,
            "unique_open_problem_count": 2,
        },
        "sufficiency": {
            "sufficient_count": 1,
            "insufficient_count": 1,
            "reason_counts": {"missing_fuzz_run": 1, "never_run": 1},
        },
        "coverage": {
            "status": "PASS",
            "line": 48.35,
            "branch": 2.58,
            "final_status": "FAIL",
        },
        "verify": {
            "verdict": "fail",
            "blocking_gap_count": 0,
            "open_problem_count": 2,
            "reported_insufficient_count": 0,
            "observed_insufficient_count": 1,
        },
    }


def _legacy_v1_report(*, change_id: str = "CH-LEGACY") -> LegacySpecialtyReportV1:
    return LegacySpecialtyReportV1.model_validate(
        {
            "schema_version": "1",
            "change_id": change_id,
            "capability_contract_policy": {
                "capabilities": {"required": ["cap.a"], "missing": ["cap.b"]},
                "contracts": {
                    "agent_execution_contract_digests": ["exec-1"],
                    "rendered_output_contract_count": 2,
                    "prompt_observability": "derived_not_recorded",
                },
                "mechanical_checks": {
                    "status": "fail",
                    "finding_count": 1,
                    "by_check": {
                        "l1_path": {"status": "pass", "finding_count": 0},
                        "shared_factory": {"status": "fail", "finding_count": 1},
                        "assert_ideal": {"status": "pass", "finding_count": 0},
                        "capability_keys": {"status": "pass", "finding_count": 0},
                    },
                },
                "policy": {
                    "source": "project",
                    "plan_checks": {
                        "assert_ideal": "warn",
                        "capability_keys": "block",
                        "l1_path": "require_human",
                        "shared_factory": "warn",
                    },
                    "digest": "policy-current",
                    "recorded_digest": "policy-current",
                },
                "policy_replay": [
                    {"action": "warn", "verdict": "pass"},
                    {"action": "block", "verdict": "reject"},
                    {"action": "require_human", "verdict": "needs_human_review"},
                ],
            },
            "traceability_evidence": _minimal_traceability(change_id=change_id),
        }
    )


def _synthetic_v2_report(*, change_id: str) -> SpecialtyReportV2:
    rows: list[dict[str, object]] = [
        {
            "layer": "api",
            "case_type": "API",
            "status": "complete",
            "reason_code": None,
            "applicability": "applicable",
            "gate_id": "api-plan-review-gate",
            "review_artifact": "review/api-plan-review.json",
            "checks_artifact": "review/api-plan-checks.json",
            "capabilities": {"required": ["cap.a", "cap.b"], "missing": ["cap.c"]},
            "mechanical_checks": {
                "status": "fail",
                "finding_count": 2,
                "checks": [
                    {"check_id": "l1_path", "status": "pass", "finding_count": 0},
                    {"check_id": "shared_factory", "status": "fail", "finding_count": 2},
                    {"check_id": "assert_ideal", "status": "pass", "finding_count": 0},
                    {"check_id": "capability_keys", "status": "pass", "finding_count": 0},
                ],
            },
            "mechanical_execution_contract_digest": "mech-api",
            "evidence_digests": {
                "review": "review-api",
                "checks": "checks-api",
                "data_knowledge": "l1-api",
            },
            "scenarios": [
                {
                    "action": "warn",
                    "policy_digest": "d-warn",
                    "verdict": "pass",
                    "route": "pass",
                    "matched_rule": "pass_when",
                    "reason": "ok",
                    "missing_capabilities": [],
                    "policy_effect": "no_failed_checks",
                },
                {
                    "action": "block",
                    "policy_digest": "d-block",
                    "verdict": "reject",
                    "route": "reject",
                    "matched_rule": "reject_when",
                    "reason": "failed",
                    "missing_capabilities": [],
                    "policy_effect": "applied",
                },
                {
                    "action": "require_human",
                    "policy_digest": "d-human",
                    "verdict": "needs_human_review",
                    "route": "needs_human_review",
                    "matched_rule": "human_when",
                    "reason": "review",
                    "missing_capabilities": [],
                    "policy_effect": "applied",
                },
            ],
        },
        {
            "layer": "e2e",
            "case_type": "E2E",
            "status": "complete",
            "reason_code": None,
            "applicability": "not_applicable",
            "gate_id": "e2e-plan-review-gate",
            "review_artifact": "review/plan-review.json",
            "checks_artifact": "review/e2e-plan-checks.json",
            "capabilities": None,
            "mechanical_checks": {
                "status": "pass",
                "finding_count": 0,
                "checks": [
                    {"check_id": check_id, "status": "not_applicable", "finding_count": 0}
                    for check_id in ("l1_path", "shared_factory", "assert_ideal", "capability_keys")
                ],
            },
            "mechanical_execution_contract_digest": "mech-e2e",
            "evidence_digests": {"review": None, "checks": "checks-e2e", "data_knowledge": None},
            "scenarios": [
                {
                    "action": action,
                    "policy_digest": f"d-{action}",
                    "verdict": "skip",
                    "route": "skip",
                    "matched_rule": None,
                    "reason": "layer_not_applicable",
                    "missing_capabilities": [],
                    "policy_effect": "no_failed_checks",
                }
                for action in ("warn", "block", "require_human")
            ],
        },
        {"layer": "fuzz", "case_type": "Fuzz", "status": "not_selected", "reason_code": None},
        {
            "layer": "performance",
            "case_type": "Performance",
            "status": "incomplete",
            "reason_code": "mechanical_producer_unbound",
        },
    ]
    return SpecialtyReportV2(
        change_id=change_id,
        capability_contract_policy=build_capability_replay_v2(
            definition_binding={
                "root_invocation_id": f"root-{change_id}",
                "assurance_invocation_id": f"assurance-{change_id}",
                "graph_digest": f"graph-{change_id}",
                "gate_definition_source": "pinned_schema",
                "baseline_policy_digest": "policy-digest",
                "policy_source": "pinned_runtime_snapshot",
                "policy_origin": "project",
                "gate_semantics_digest": "semantics-digest",
                "assurance_profile_digest": "profile-digest",
            },
            rows=rows,
        ),
        traceability_evidence=_minimal_traceability(change_id=change_id),
    )


def test_render_layer_assurance_matrix_shows_four_layer_fields() -> None:
    report = _synthetic_v2_report(change_id="CH-MATRIX")
    rendered = render_specialty_sections([report])

    assert "### Layer Assurance Matrix" in rendered
    assert (
        "| change_id | layer | status | applicability | mechanical | findings | capabilities req/miss | contract digest |"
        in rendered
    )
    assert "| `CH-MATRIX` | api | complete | applicable | fail | 2 | 2/1 | yes |" in rendered
    assert "| `CH-MATRIX` | e2e | complete | not_applicable | pass | 0 | - | yes |" in rendered
    assert "| `CH-MATRIX` | fuzz | not_selected | - | - | - | - | - |" in rendered
    assert "| `CH-MATRIX` | performance | incomplete | - | - | - | - | - |" in rendered


def test_render_policy_replay_matrix_shows_verdicts_statuses_and_reasons() -> None:
    report = _synthetic_v2_report(change_id="CH-MATRIX")
    rendered = render_specialty_sections([report])

    assert "### Policy Replay Matrix" in rendered
    assert "| `CH-MATRIX` | api | pass | reject | needs_human_review |" in rendered
    assert "| `CH-MATRIX` | e2e | skip | skip | skip |" in rendered
    assert "| `CH-MATRIX` | fuzz | not_selected | not_selected | not_selected |" in rendered
    assert (
        "| `CH-MATRIX` | performance | mechanical_producer_unbound | mechanical_producer_unbound | mechanical_producer_unbound |"
        in rendered
    )

    not_wired = _synthetic_v2_report(change_id="CH-NW")
    not_wired_rows: list[dict[str, object]] = [
        not_wired.capability_contract_policy.rows[0].model_dump(mode="json"),
        not_wired.capability_contract_policy.rows[1].model_dump(mode="json"),
        {"layer": "fuzz", "case_type": "Fuzz", "status": "not_wired", "reason_code": None},
        not_wired.capability_contract_policy.rows[3].model_dump(mode="json"),
    ]
    binding = not_wired.capability_contract_policy.definition_binding
    assert binding is not None
    not_wired_report = SpecialtyReportV2(
        change_id="CH-NW",
        capability_contract_policy=build_capability_replay_v2(
            definition_binding=binding.model_dump(mode="json"),
            rows=not_wired_rows,
        ),
        traceability_evidence=not_wired.traceability_evidence,
    )
    not_wired_rendered = render_specialty_sections([not_wired_report])
    assert "| `CH-NW` | fuzz | not_wired | not_wired | not_wired |" in not_wired_rendered


def test_render_sorts_v2_reports_by_change_id_and_profile_order() -> None:
    first = _synthetic_v2_report(change_id="CH-A")
    second = _synthetic_v2_report(change_id="CH-B")
    rendered = render_specialty_sections([second, first])

    matrix_section = rendered.split("### Layer Assurance Matrix", maxsplit=1)[1].split(
        "### Policy Replay Matrix", maxsplit=1
    )[0]
    api_rows = [
        line for line in matrix_section.splitlines() if " | api | " in line and line.startswith("| `CH-")
    ]
    assert api_rows == [
        "| `CH-A` | api | complete | applicable | fail | 2 | 2/1 | yes |",
        "| `CH-B` | api | complete | applicable | fail | 2 | 2/1 | yes |",
    ]
    ch_a_rows = [line for line in matrix_section.splitlines() if line.startswith("| `CH-A` |")]
    assert [row.split("|")[2].strip() for row in ch_a_rows] == list(LAYER_NAMES)


def test_render_v1_alone_labels_legacy_and_skips_layer_matrix_rows() -> None:
    report = _legacy_v1_report()
    rendered = render_specialty_sections([report])

    assert "### legacy_api_only" in rendered
    assert "### Layer Assurance Matrix" not in rendered
    assert "| `CH-LEGACY` | fail |" in rendered
    assert "| `CH-LEGACY` | legacy_api_only | pass | reject | needs_human_review |" in rendered
    assert "| `CH-LEGACY` | api |" not in rendered
    assert "| `CH-LEGACY` | e2e |" not in rendered
    assert "| `CH-LEGACY` | fuzz |" not in rendered
    assert "| `CH-LEGACY` | performance |" not in rendered


def test_render_mixed_v1_v2_preserves_legacy_label_and_four_layer_rows() -> None:
    v1 = _legacy_v1_report(change_id="CH-LEGACY")
    v2 = _synthetic_v2_report(change_id="CH-V2")
    rendered = render_specialty_sections([v2, v1])

    assert "### Layer Assurance Matrix" in rendered
    assert "### legacy_api_only" in rendered
    assert "| `CH-V2` | api | complete | applicable |" in rendered
    assert "| `CH-LEGACY` | legacy_api_only | pass | reject | needs_human_review |" in rendered
    assert "| `CH-LEGACY` | api |" not in rendered


def test_render_mixed_v1_v2_policy_replay_sorts_by_change_id() -> None:
    v1 = _legacy_v1_report(change_id="CH-A")
    v2 = _synthetic_v2_report(change_id="CH-B")
    rendered = render_specialty_sections([v2, v1])

    replay_section = rendered.split("### Policy Replay Matrix", maxsplit=1)[1].split("##", maxsplit=1)[0]
    data_rows = [line for line in replay_section.splitlines() if line.startswith("| `CH-")]
    assert data_rows[0] == "| `CH-A` | legacy_api_only | pass | reject | needs_human_review |"
    assert data_rows[1].startswith("| `CH-B` | api |")


def test_render_emits_both_specialty_sections_and_policy_matrix(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    collect = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
        trace_exit="0",
        verify_exit="40",
    )
    assert collect.returncode == 0, collect.stderr

    rendered = subprocess.run(
        [sys.executable, str(_REPORTER), "render", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert rendered.returncode == 0, rendered.stderr
    assert "## Capability + Contract + Policy" in rendered.stdout
    assert "### Layer Assurance Matrix" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | api | complete | applicable | fail | 1 |" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | fuzz | not_selected | - | - | - | - | - |" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | performance | not_selected | - | - | - | - | - |" in rendered.stdout
    assert "### Policy Replay Matrix" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | api | pass | reject | needs_human_review |" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | fuzz | not_selected | not_selected | not_selected |" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | performance | not_selected | not_selected | not_selected |" in rendered.stdout
    assert "## Traceability / Evidence Projection" in rendered.stdout
    assert f"| `{_CHANGE_ID}` | execution | batch-1 | degraded | 2 | 2 | 1 | 1 |" in rendered.stdout
    assert (
        f"| `{_CHANGE_ID}` | 1 | 1 | missing_fuzz_run:1, never_run:1 | PASS | 48.35 | 2.58 | FAIL |"
        in rendered.stdout
    )
    assert "reported insufficient=0; observed insufficient=1" in rendered.stdout


def test_collect_rejects_structurally_invalid_trace_instead_of_reporting_zero(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["rows"] = {"not": "a list"}
    _write_json(trace_path, trace)

    result = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
    )

    assert result.returncode != 0
    assert "validation error" in result.stderr
    assert not output.exists()


def test_frozen_specialty_report_reconstructs_trace_gate_row_without_cli_rerun(
    tmp_path: Path,
) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    collect = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
        trace_exit="9",
        verify_exit="40",
    )
    assert collect.returncode == 0, collect.stderr

    row = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "evidence-row",
            "--change-id",
            _CHANGE_ID,
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert row.returncode == 0, row.stderr
    assert row.stdout == f"{_CHANGE_ID}|9|degraded|1|40|fail|0|0\n"


def test_evidence_row_rejects_report_for_a_different_resume_item(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    collect = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
    )
    assert collect.returncode == 0, collect.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    report["change_id"] = "CH-OTHER"
    _write_json(output, report)

    row = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "evidence-row",
            "--change-id",
            _CHANGE_ID,
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert row.returncode != 0
    assert "specialty report change_id mismatch" in row.stderr


@pytest.mark.parametrize(
    ("artifact", "updates", "message"),
    [
        ("review", {"change_id": "CH-OTHER"}, "review change_id mismatch"),
        ("trace", {"change_id": "CH-OTHER"}, "execution trace change_id mismatch"),
        ("trace", {"phase": "reconciled"}, "execution trace phase mismatch"),
        ("reconciled", {"phase": "execution"}, "reconciled trace phase mismatch"),
        (
            "reconciled",
            {"authoritative_batch_id": "batch-other"},
            "trace batch mismatch",
        ),
        ("quality", {"change_id": "CH-OTHER"}, "quality gate change_id mismatch"),
        ("quality", {"batch_id": "batch-other"}, "quality gate batch mismatch"),
        ("verify", {"change_id": "CH-OTHER"}, "verify change_id mismatch"),
        ("verify", {"phase": "execution"}, "verify phase mismatch"),
        ("verify", {"projection_digest": "stale-digest"}, "verify projection digest mismatch"),
        (
            "verify",
            {
                "scope": {
                    "cases": ["TC_API_001", "TC_FUZZ_001"],
                    "batch": "batch-other",
                    "policy_digest": "verify-policy",
                    "projection_digest": "scope-projection",
                }
            },
            "verify scope batch mismatch",
        ),
    ],
)
def test_collect_rejects_cross_artifact_identity_phase_and_batch_mismatch(
    tmp_path: Path,
    artifact: str,
    updates: dict[str, str],
    message: str,
) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    change = project / "qa" / "changes" / _CHANGE_ID
    paths = {
        "review": change / "review" / "api-plan-review.json",
        "trace": trace_path,
        "reconciled": change / "inspect" / "trace-projection.json",
        "quality": change / "execution" / "quality-gate-result.json",
        "verify": verify_path,
    }
    payload = json.loads(paths[artifact].read_text(encoding="utf-8"))
    payload.update(updates)
    _write_json(paths[artifact], payload)
    output = tmp_path / "specialty.json"

    result = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()


def test_resume_reuses_frozen_report_and_rejects_post_archive_collection(tmp_path: Path) -> None:
    report = tmp_path / "run" / "CH-1.specialty-report.json"
    archive = tmp_path / "sut" / "qa" / "archive" / "CH-1"
    command = (
        f"source {shlex.quote(str(_HELPERS))}; "
        f"benchmark_specialty_resume_action {shlex.quote(str(report))} "
        f"{shlex.quote(str(archive))}"
    )

    fresh = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
    archive.mkdir(parents=True)
    archived_without_report = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False
    )
    report.parent.mkdir(parents=True)
    report.write_text("{}\n", encoding="utf-8")
    archived_with_report = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False
    )

    assert (fresh.returncode, fresh.stdout) == (0, "collect")
    assert (archived_without_report.returncode, archived_without_report.stdout) == (
        1,
        "missing_after_archive",
    )
    assert (archived_with_report.returncode, archived_with_report.stdout) == (0, "reuse")


def test_finalize_registers_valid_incomplete_report_before_failure(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty-incomplete.json"
    collect = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
        root_invocation_id="missing-root",
    )
    assert collect.returncode != 0

    command = (
        f"source {shlex.quote(str(_HELPERS))}; "
        f"finalize_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(_REPORTER))} {_CHANGE_ID} {shlex.quote(str(output))} 1"
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "registered=true" in result.stdout
    assert str(output) in result.stdout.splitlines()[0]
    report = load_specialty_report(json.loads(output.read_text(encoding="utf-8")))
    assert isinstance(report, SpecialtyReportV2)
    assert report.capability_contract_policy.integrity == "incomplete"


def test_finalize_skips_invalid_report_on_collector_failure(tmp_path: Path) -> None:
    output = tmp_path / "specialty-invalid.json"
    output.write_text("{not-json}\n", encoding="utf-8")
    command = (
        f"source {shlex.quote(str(_HELPERS))}; "
        f"finalize_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(_REPORTER))} {_CHANGE_ID} {shlex.quote(str(output))} 1"
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert result.stdout.strip() == "registered=false"


def test_run_specialty_report_stage_registers_incomplete_v2_and_fails(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    result = _run_specialty_report_stage(
        tmp_path,
        change_id=_CHANGE_ID,
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        trace_exit="9",
        verify_exit="40",
        root_invocation_id="missing-root",
    )

    assert result.returncode == 1, result.stderr
    assert "SPECIALTY_REPORT_FAILED=true" in result.stdout
    assert "stage_exit=1" in result.stdout
    files_line = next(
        line for line in result.stdout.splitlines() if line.startswith("SPECIALTY_REPORT_FILES=")
    )
    report_path = Path(files_line.removeprefix("SPECIALTY_REPORT_FILES=").strip())
    assert report_path.is_file()
    report = load_specialty_report(json.loads(report_path.read_text(encoding="utf-8")))
    assert isinstance(report, SpecialtyReportV2)
    assert report.capability_contract_policy.integrity == "incomplete"
    assert report.capability_contract_policy.definition_failure == "root_invocation_unbound"

    rendered = subprocess.run(
        [sys.executable, str(_REPORTER), "render", str(report_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert "### Layer Assurance Matrix" in rendered.stdout
    assert "root_invocation_unbound" in rendered.stdout


def test_run_specialty_report_stage_skips_invalid_report_on_collector_failure(
    tmp_path: Path,
) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["rows"] = {"not": "a list"}
    bad_trace = tmp_path / "bad-trace.json"
    _write_json(bad_trace, trace)
    result = _run_specialty_report_stage(
        tmp_path,
        change_id=_CHANGE_ID,
        project=project,
        trace_path=bad_trace,
        verify_path=verify_path,
        trace_exit="9",
        verify_exit="40",
        root_invocation_id=_ROOT_INV,
    )

    assert result.returncode == 1, result.stderr
    assert "SPECIALTY_REPORT_FAILED=true" in result.stdout
    assert "stage_exit=1" in result.stdout
    files_line = next(
        line for line in result.stdout.splitlines() if line.startswith("SPECIALTY_REPORT_FILES=")
    )
    assert files_line.removeprefix("SPECIALTY_REPORT_FILES=").strip() == ""
    report_file = tmp_path / "run" / f"{_CHANGE_ID}.specialty-report.json"
    assert not report_file.exists()


def test_cursor_helper_collects_and_renders_real_specialty_report(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    log = tmp_path / "specialty.log"
    command = " ".join(
        [
            f"source {shlex.quote(str(_HELPERS))};",
            "collect_benchmark_specialty_report",
            shlex.quote(sys.executable),
            shlex.quote(str(_REPORTER)),
            shlex.quote(str(project)),
            _CHANGE_ID,
            shlex.quote(str(trace_path)),
            shlex.quote(str(verify_path)),
            shlex.quote(str(output)),
            shlex.quote(str(log)),
            "9",
            "40",
            shlex.quote(_ROOT_INV),
            shlex.quote(_ENTRYPOINT),
            ";",
            "render_benchmark_specialty_sections",
            shlex.quote(sys.executable),
            shlex.quote(str(_REPORTER)),
            shlex.quote(str(output)),
        ]
    )

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = load_specialty_report(json.loads(output.read_text(encoding="utf-8")))
    assert isinstance(report, SpecialtyReportV2)
    assert report.change_id == _CHANGE_ID
    assert report.traceability_evidence["command_status"] == {
        "trace_exit": 9,
        "verify_exit": 40,
    }
    assert "## Capability + Contract + Policy" in result.stdout
    assert "### Layer Assurance Matrix" in result.stdout
    assert "### Policy Replay Matrix" in result.stdout
    assert "## Traceability / Evidence Projection" in result.stdout


def test_collect_complete_traceability_happy_path_and_v3_round_trip(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    inputs = reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )
    trace = reporter._collect_complete_traceability(inputs, capability)
    assert trace.status == "complete"
    assert [row.layer for row in trace.execution.facts.layers] == [
        "api",
        "e2e",
        "fuzz",
        "performance",
    ]
    assert trace.execution.overview.projection_digest == trace.sufficiency.source_projection_digest
    assert trace.reconciled.overview.projection_digest == trace.verify.projection_digest
    binding = capability.definition_binding
    assert binding is not None
    assert (
        trace.sufficiency.source_policy_digest == binding.baseline_policy_digest == trace.verify.policy_digest
    )
    # Business recovery gaps keep reconciled integrity incomplete while collection is complete.
    assert trace.reconciled.overview.integrity == "incomplete"
    assert trace.status == "complete"

    report = SpecialtyReportV3(
        change_id=CHANGE_ID,
        capability_contract_policy=capability,
        traceability_evidence=trace,
    )
    reloaded = load_specialty_report_document(report.model_dump(mode="json"))
    assert isinstance(reloaded, SpecialtyReportV3)
    assert isinstance(reloaded.traceability_evidence, CompleteTraceabilityEvidenceV3)
    assert reloaded.model_dump(mode="json") == report.model_dump(mode="json")


def test_complete_collection_keeps_zero_row_layers_present(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(
        tmp_path,
        include_layers=frozenset({"api"}),
    )
    inputs = reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )
    trace = reporter._collect_complete_traceability(inputs, capability)
    assert [row.layer for row in trace.execution.facts.layers] == list(LAYER_NAMES)
    by_layer = {row.layer: row for row in trace.execution.facts.layers}
    assert by_layer["api"].total == 1
    assert by_layer["e2e"].total == 0
    assert by_layer["fuzz"].total == 0
    assert by_layer["performance"].total == 0


def test_complete_collection_global_gaps_stay_out_of_layer_rows(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    inputs = reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )
    trace = reporter._collect_complete_traceability(inputs, capability)
    assert trace.reconciled.facts.global_gaps.total > 0
    global_codes = set(trace.reconciled.facts.global_gaps.by_code)
    for layer in trace.reconciled.facts.layers:
        assert global_codes.isdisjoint(layer.gaps.by_code)


def test_complete_collection_sufficiency_view_can_change_without_mutating_facts(
    tmp_path: Path,
) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    inputs = reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )
    trace = reporter._collect_complete_traceability(inputs, capability)
    exec_facts = trace.execution.facts.model_dump(mode="json")
    rec_facts = trace.reconciled.facts.model_dump(mode="json")

    execution = load_trace_projection_document(json.loads(execution_trace_path.read_text(encoding="utf-8")))
    assert isinstance(execution, TraceProjectionV2)
    binding = capability.definition_binding
    assert binding is not None
    policy = load_policy_bytes(
        (inputs.change_dir / policy_snapshot_relpath(binding.baseline_policy_digest)).read_bytes(),
        origin="pinned",
    )
    alt_report = evaluate_sufficiency(
        execution,
        policy,
        as_of=AWARE_NOW + timedelta(hours=100),
        require_current_batch=True,
    )
    alt_joined = join_layer_sufficiency(
        execution,
        summarize_projection_by_layer(execution),
        alt_report,
        expected_policy_digest=binding.baseline_policy_digest,
    )
    assert alt_joined.as_of != trace.sufficiency.as_of
    assert alt_joined.model_dump(mode="json") != trace.sufficiency.model_dump(mode="json")
    assert summarize_projection_by_layer(execution).model_dump(mode="json") == exec_facts
    reconciled = load_current_reconciled_projection(project, CHANGE_ID)
    assert summarize_projection_by_layer(reconciled).model_dump(mode="json") == rec_facts


def test_public_collect_report_and_cli_still_emit_specialty_report_v2(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    report = reporter.collect_report(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    assert isinstance(report, SpecialtyReportV2)
    assert report.schema_version == "2"
    assert "layers" not in report.traceability_evidence
    assert "sufficient_count" in report.traceability_evidence["sufficiency"]

    output = tmp_path / "specialty-public-v2.json"
    result = _collect_command(
        project=project,
        trace_path=trace_path,
        verify_path=verify_path,
        output=output,
    )
    assert result.returncode == 0, result.stderr
    loaded = load_specialty_report(json.loads(output.read_text(encoding="utf-8")))
    assert isinstance(loaded, SpecialtyReportV2)


def test_complete_collection_path_not_reachable_from_main() -> None:
    reporter = _load_reporter_module()
    main_source = inspect.getsource(reporter.main)
    assert "_collect_complete_traceability" not in main_source
    assert "collect_v3_report" not in main_source
    assert "SpecialtyReportV3" not in main_source
    source = _REPORTER.read_text(encoding="utf-8")
    assert "schema_root" not in source
    assert "TraceCollectionInputs" in source


@pytest.mark.parametrize(
    ("attr", "module_path"),
    [
        ("load_current_reconciled_projection", "assurance_agent.evidence.current_projection"),
        ("validate_trace_phase_pair", "assurance_agent.evidence.layer_summary"),
        ("summarize_projection_by_layer", "assurance_agent.eval.specialty_models"),
        ("join_layer_sufficiency", "assurance_agent.evidence.layer_summary"),
        ("load_quality_gate_result_document", "assurance_agent.artifacts.models.inspect"),
    ],
)
def test_complete_collection_uses_shared_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attr: str,
    module_path: str,
) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    inputs = reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )

    import importlib

    module = importlib.import_module(module_path)
    original = getattr(module, attr)
    calls: list[object] = []

    if attr == "validate_trace_phase_pair":

        def wrapper(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return original(*args, **kwargs)

    elif attr == "summarize_projection_by_layer":

        def wrapper(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            calls.append(result)
            return result

    elif attr == "join_layer_sufficiency":

        def wrapper(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            calls.append(result)
            return result

    elif attr == "load_current_reconciled_projection":

        def wrapper(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            calls.append(result)
            return result

    else:

        def wrapper(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            calls.append(result)
            return result

    monkeypatch.setattr(module, attr, wrapper)
    # Also patch the reporter's bound name when it imported the symbol directly.
    if hasattr(reporter, attr):
        monkeypatch.setattr(reporter, attr, wrapper)
    if attr == "summarize_projection_by_layer":
        monkeypatch.setattr(
            "assurance_agent.eval.specialty_models.summarize_projection_by_layer",
            wrapper,
        )
    if attr == "join_layer_sufficiency" and hasattr(reporter, "join_layer_sufficiency"):
        monkeypatch.setattr(reporter, "join_layer_sufficiency", wrapper)
    if attr == "load_current_reconciled_projection" and hasattr(
        reporter, "load_current_reconciled_projection"
    ):
        monkeypatch.setattr(reporter, "load_current_reconciled_projection", wrapper)
    if attr == "validate_trace_phase_pair" and hasattr(reporter, "validate_trace_phase_pair"):
        monkeypatch.setattr(reporter, "validate_trace_phase_pair", wrapper)
    if attr == "load_quality_gate_result_document" and hasattr(reporter, "load_quality_gate_result_document"):
        monkeypatch.setattr(reporter, "load_quality_gate_result_document", wrapper)

    trace = reporter._collect_complete_traceability(inputs, capability)
    assert calls, f"expected shared seam {attr} to be called"
    if attr == "load_current_reconciled_projection":
        assert calls[0] is not None
        assert trace.reconciled.overview.projection_digest
    elif attr == "join_layer_sufficiency":
        assert trace.sufficiency == calls[0]
    elif attr == "summarize_projection_by_layer":
        assert any(
            cast(Any, call).source_projection_digest
            in {
                trace.execution.overview.projection_digest,
                trace.reconciled.overview.projection_digest,
            }
            for call in calls
        )
    elif attr == "load_quality_gate_result_document":
        quality = cast(Any, calls[0])
        assert quality.schema_version == "2.0"
        assert trace.coverage.final_status == quality.final_status
    else:
        assert calls


def test_complete_collector_ast_forbids_local_aggregation_and_legacy_parses() -> None:
    reporter = _load_reporter_module()
    source = inspect.getsource(reporter._collect_complete_traceability)
    tree = ast.parse(source)
    assert isinstance(tree.body[0], ast.FunctionDef)
    forbidden_substrings = (
        "_projection_summary",
        "_quality_summary",
        "Counter(",
        "TraceProjection.model_validate",
        "QualityGateResult.model_validate",
        "SufficiencyReport.model_validate",
        "json.loads",
    )
    for token in forbidden_substrings:
        assert token not in source, token


def test_trace_collection_inputs_rejects_negative_exits_and_resolves_change(
    tmp_path: Path,
) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    with pytest.raises(ValueError, match="nonnegative"):
        reporter.TraceCollectionInputs(
            project_root=project,
            change_id=CHANGE_ID,
            root_invocation_id=ROOT_INVOCATION_ID,
            workflow_entrypoint=_ENTRYPOINT,
            trace_path=execution_trace_path,
            verify_path=verify_path,
            trace_exit=-1,
            verify_exit=0,
        )
    inputs = reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id="",
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    assert inputs.change_dir == _change_dir(project)
    assert inputs.command_status.trace_exit == 0
    assert inputs.root_invocation_id == ""
    assert not hasattr(inputs, "schema_root")


def _v3_inputs(
    reporter: Any,
    project: Path,
    execution_trace_path: Path,
    verify_path: Path,
) -> Any:
    return reporter.TraceCollectionInputs(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=execution_trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )


def _assert_incomplete_v3_report(
    report: SpecialtyReportV3,
    *,
    reason_code: TraceCollectionFailureReason,
) -> IncompleteTraceabilityEvidenceV3:
    reloaded = load_specialty_report_document(report.model_dump(mode="json"))
    assert isinstance(reloaded, SpecialtyReportV3)
    evidence = reloaded.traceability_evidence
    assert isinstance(evidence, IncompleteTraceabilityEvidenceV3)
    assert evidence.status == "incomplete"
    assert evidence.reason_code == reason_code
    assert isinstance(evidence.detail, str)
    assert "Traceback" not in evidence.detail
    assert "Exception" not in evidence.detail
    assert "Error(" not in evidence.detail
    dumped = evidence.model_dump(mode="json")
    for complete_key in (
        "execution",
        "reconciled",
        "sufficiency",
        "coverage",
        "verify",
    ):
        assert complete_key not in dumped
    return evidence


def _mutate_execution_json(
    execution_trace_path: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    payload = json.loads(execution_trace_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    mutator(payload)
    _write_json(execution_trace_path, payload)


def _mutate_reconciled_json(
    project: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    path = _change_dir(project) / "inspect" / "trace-projection.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    mutator(payload)
    _write_json(path, payload)


def _mutate_quality_json(
    project: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    path = _change_dir(project) / "execution" / "quality-gate-result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    mutator(payload)
    _write_json(path, payload)


def _mutate_verify_json(
    verify_path: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    payload = json.loads(verify_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    mutator(payload)
    _write_json(verify_path, payload)


def _apply_incomplete_reason_mutation(
    *,
    reason: TraceCollectionFailureReason,
    project: Path,
    execution_trace_path: Path,
    verify_path: Path,
) -> None:
    change_dir = _change_dir(project)
    if reason == "execution_projection_missing":
        execution_trace_path.unlink()
        return
    if reason == "execution_projection_invalid":
        execution_trace_path.write_bytes(b"\xff\xfe not-json")
        return
    if reason == "reconciled_projection_missing":
        (change_dir / "inspect" / "trace-projection.json").unlink()
        return
    if reason == "reconciled_projection_invalid":
        (change_dir / "inspect" / "trace-projection.json").write_text("{", encoding="utf-8")
        return
    if reason == "reconciled_projection_stale":

        def drift_persisted_digest(payload: dict[str, Any]) -> None:
            # Keep raw identity; change bytes so current-loader digest_mismatch → stale.
            rows = payload["rows"]
            assert isinstance(rows, list) and rows
            first = rows[0]
            assert isinstance(first, dict)
            first["assertions"] = ["persisted-no-longer-current"]

        _mutate_reconciled_json(project, drift_persisted_digest)
        return
    if reason == "projection_identity_mismatch":

        def wrong_execution_change(payload: dict[str, Any]) -> None:
            payload["change_id"] = "CH-OTHER"
            payload["rows"] = "not-a-list"  # compound: identity precedes model defects

        _mutate_execution_json(execution_trace_path, wrong_execution_change)
        return
    if reason == "projection_phase_pair_mismatch":

        def drift_execution_row(payload: dict[str, Any]) -> None:
            rows = payload["rows"]
            assert isinstance(rows, list) and rows
            first = rows[0]
            assert isinstance(first, dict)
            first["assertions"] = ["mutated-for-phase-pair"]

        _mutate_execution_json(execution_trace_path, drift_execution_row)
        return
    if reason == "quality_gate_missing":
        (change_dir / "execution" / "quality-gate-result.json").unlink()
        return
    if reason == "quality_gate_invalid":
        (change_dir / "execution" / "quality-gate-result.json").write_text(
            '["not-an-object"]\n',
            encoding="utf-8",
        )
        return
    if reason == "quality_gate_binding_mismatch":

        def to_v2_error_arm(payload: dict[str, Any]) -> None:
            coverage = payload["dimensions"]["coverage"]
            assert isinstance(coverage, dict)
            coverage["evidence"] = {
                "kind": "error",
                "error_code": "evidence_projection_missing",
            }

        _mutate_quality_json(project, to_v2_error_arm)
        return
    if reason == "sufficiency_binding_mismatch":

        def duplicate_case_id(payload: dict[str, Any]) -> None:
            report = payload["dimensions"]["coverage"]["evidence"]["report"]
            verdicts = report["verdicts"]
            assert isinstance(verdicts, list) and verdicts
            first = dict(verdicts[0])
            verdicts.append(first)

        _mutate_quality_json(project, duplicate_case_id)
        return
    if reason == "verify_result_missing":
        verify_path.unlink()
        return
    if reason == "verify_result_invalid":
        verify_path.write_text("{not-json", encoding="utf-8")
        return
    if reason == "verify_binding_mismatch":

        def pass_without_scope(payload: dict[str, Any]) -> None:
            payload["verdict"] = "pass"
            payload["scope"] = None
            payload["phase"] = "execution"  # compound: raw identity precedes model/arm checks
            payload["blocking_gaps"] = "bad"

        _mutate_verify_json(verify_path, pass_without_scope)
        return
    if reason == "layer_summary_invalid":

        def lie_about_integrity(payload: dict[str, Any]) -> None:
            # Parsed V2 stays model-valid; shared summary rejects integrity misstatement.
            current = payload.get("integrity")
            payload["integrity"] = "complete" if current != "complete" else "degraded"

        _mutate_execution_json(execution_trace_path, lie_about_integrity)
        return
    raise AssertionError(f"unmapped reason fixture: {reason}")


@pytest.mark.parametrize("reason", list(get_args(TraceCollectionFailureReason)))
def test_collect_v3_report_maps_every_closed_failure_reason(
    tmp_path: Path,
    reason: TraceCollectionFailureReason,
) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    _apply_incomplete_reason_mutation(
        reason=reason,
        project=project,
        execution_trace_path=execution_trace_path,
        verify_path=verify_path,
    )
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    _assert_incomplete_v3_report(report, reason_code=reason)
    assert report.capability_contract_policy.integrity in {"complete", "incomplete"}


def test_collect_v3_ordered_mapping_prefers_missing_over_invalid(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    execution_trace_path.write_bytes(b"\xff\xfe")
    execution_trace_path.unlink()
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    _assert_incomplete_v3_report(report, reason_code="execution_projection_missing")


def test_collect_v3_ordered_mapping_prefers_identity_over_model_defect(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["phase"] = "reconciled"
        payload["schema_version"] = "1"
        payload["rows"] = None

    _mutate_execution_json(execution_trace_path, mutate)
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    _assert_incomplete_v3_report(report, reason_code="projection_identity_mismatch")


def test_collect_v3_quality_duplicate_case_id_is_sufficiency_binding_mismatch(
    tmp_path: Path,
) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        report = payload["dimensions"]["coverage"]["evidence"]["report"]
        # Keep digests/semantics correct; only defect is duplicate case_id.
        report["verdicts"] = [report["verdicts"][0], dict(report["verdicts"][0])]

    _mutate_quality_json(project, mutate)
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    _assert_incomplete_v3_report(report, reason_code="sufficiency_binding_mismatch")


def test_collect_v3_quality_binding_scalar_precedes_duplicate_case_id(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        report = payload["dimensions"]["coverage"]["evidence"]["report"]
        report["source_policy_digest"] = "0" * 64
        report["verdicts"] = [report["verdicts"][0], dict(report["verdicts"][0])]

    _mutate_quality_json(project, mutate)
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    _assert_incomplete_v3_report(report, reason_code="sufficiency_binding_mismatch")
    assert "policy" in report.traceability_evidence.detail


def test_collect_v3_quality_identity_precedes_sufficiency_preflight(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)

    def mutate(payload: dict[str, Any]) -> None:
        payload["batch_id"] = "wrong-batch"
        report = payload["dimensions"]["coverage"]["evidence"]["report"]
        report["source_policy_digest"] = "0" * 64
        report["verdicts"] = [report["verdicts"][0], dict(report["verdicts"][0])]

    _mutate_quality_json(project, mutate)
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    _assert_incomplete_v3_report(report, reason_code="quality_gate_binding_mismatch")


def test_collect_v3_capability_incomplete_with_trace_complete(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    _corrupt_api_mechanical_outputs(_change_dir(project))
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    reloaded = load_specialty_report_document(report.model_dump(mode="json"))
    assert isinstance(reloaded, SpecialtyReportV3)
    assert reloaded.capability_contract_policy.integrity == "incomplete"
    assert reloaded.capability_contract_policy.definition_binding is not None
    assert isinstance(reloaded.traceability_evidence, CompleteTraceabilityEvidenceV3)
    assert reloaded.traceability_evidence.status == "complete"


def test_collect_v3_trace_incomplete_with_capability_complete(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    execution_trace_path.unlink()
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    assert report.capability_contract_policy.integrity == "complete"
    _assert_incomplete_v3_report(report, reason_code="execution_projection_missing")


def test_collect_v3_both_capability_and_trace_incomplete(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    _corrupt_api_mechanical_outputs(_change_dir(project))
    execution_trace_path.unlink()
    report = reporter.collect_v3_report(
        _v3_inputs(reporter, project, execution_trace_path, verify_path)
    )
    reloaded = load_specialty_report_document(report.model_dump(mode="json"))
    assert isinstance(reloaded, SpecialtyReportV3)
    assert reloaded.capability_contract_policy.integrity == "incomplete"
    _assert_incomplete_v3_report(reloaded, reason_code="execution_projection_missing")


def test_collect_v3_unexpected_runtime_error_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = _load_reporter_module()
    project, execution_trace_path, verify_path = _install_authority_valid_complete_item(tmp_path)
    inputs = _v3_inputs(reporter, project, execution_trace_path, verify_path)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("programmer-bug")

    monkeypatch.setattr(reporter, "_collect_complete_traceability", boom)
    with pytest.raises(RuntimeError, match="programmer-bug"):
        reporter.collect_v3_report(inputs)


def test_tasks_16_17_keep_public_collect_atomic_activation_guard(tmp_path: Path) -> None:
    reporter = _load_reporter_module()
    project, trace_path, verify_path = _install_strict_frozen_item(tmp_path)
    public = reporter.collect_report(
        project_root=project,
        change_id=CHANGE_ID,
        root_invocation_id=ROOT_INVOCATION_ID,
        workflow_entrypoint=_ENTRYPOINT,
        trace_path=trace_path,
        verify_path=verify_path,
        trace_exit=0,
        verify_exit=0,
    )
    assert isinstance(public, SpecialtyReportV2)
    assert public.schema_version == "2"

    main_source = inspect.getsource(reporter.main)
    collect_source = inspect.getsource(reporter.collect_report)
    assert "collect_v3_report" not in main_source
    assert "collect_v3_report" not in collect_source
    assert "SpecialtyReportV3" not in main_source
    assert callable(reporter.collect_v3_report)
