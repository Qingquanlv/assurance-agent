from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.assurance import LAYER_NAMES
from assurance_agent.eval.specialty_models import (
    LegacySpecialtyReportV1,
    SpecialtyReportV2,
    build_capability_replay_v2,
    load_specialty_report,
)
from assurance_agent.eval.specialty_render import render_specialty_sections
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.definition_pinning import policy_snapshot_relpath
from assurance_agent.workflow.graph.replay_binding import normalize_logical_path
from assurance_agent.workflow.graph.workspace import TreeStore
from tests.unit.workflow.graph.test_replay_binding import (
    _CHANGE_ID,
    _ENTRYPOINT,
    _ROOT_INV,
    _build_fixture,
)

_ROOT = Path(__file__).parents[3]
_REPORTER = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "benchmark_specialty_report.py"
_HELPERS = _REPORTER.with_name("cursor-loop-helpers.sh")
_GOLDEN_SHA256 = "78d6d13361ab7210cb1fa57b252e51df3fc3b11c57a438b0a6fe1a64944ce640"


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


def _mutate_root_started_field(change_dir: Path, field: str, value: str) -> None:
    events = _read_events(change_dir)
    for event in events:
        if event.get("type") == "graph_invocation_started" and event.get("invocation_id") == _ROOT_INV:
            event[field] = value
    _write_events(change_dir, events)


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
            gate_report = dict(event.get("gate_report") or {})
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
            "--schema-root",
            str(_ROOT),
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
    assert capability.semantics == "counterfactual_plan_check_actions/v1"
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


@pytest.mark.parametrize(
    ("failure_code", "mutator", "collect_overrides"),
    [
        ("root_invocation_unbound", lambda _change_dir: None, {"root_invocation_id": "missing-root"}),
        ("policy_snapshot_missing", _drop_policy_snapshot, {}),
        ("pinned_schema_missing", _drop_pinned_schema, {}),
        ("pinned_schema_digest_mismatch", _corrupt_pinned_schema_digest, {}),
        ("gate_semantics_mismatch", lambda cd: _mutate_root_started_field(cd, "gate_semantics_digest", "0" * 64), {}),
        (
            "assurance_profile_mismatch",
            lambda cd: _mutate_root_started_field(cd, "assurance_profile_digest", "0" * 64),
            {},
        ),
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
    assert "| change_id | layer | status | applicability | mechanical | findings | capabilities req/miss | contract digest |" in rendered
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
    assert "| `CH-MATRIX` | performance | mechanical_producer_unbound | mechanical_producer_unbound | mechanical_producer_unbound |" in rendered

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
    api_rows = [line for line in matrix_section.splitlines() if " | api | " in line and line.startswith("| `CH-")]
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
            shlex.quote(str(_ROOT)),
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
