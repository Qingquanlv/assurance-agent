from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).parents[3]
_REPORTER = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "benchmark_specialty_report.py"
_HELPERS = _REPORTER.with_name("cursor-loop-helpers.sh")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _install_frozen_item(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "sut"
    change_id = "CH-REPORT-1"
    change = project / "qa" / "changes" / change_id

    knowledge = {
        "version": 1,
        "accounts": {},
        "auth": {"api_admin_token": {"method": "token"}},
        "entities": {},
        "capabilities": {
            "domain_factories": {},
            "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
            "cleanup": {},
        },
    }
    knowledge_path = project / ".aa" / "data-knowledge.yaml"
    knowledge_path.parent.mkdir(parents=True)
    knowledge_path.write_text(yaml.safe_dump(knowledge), encoding="utf-8")

    _write_json(
        change / "review" / "api-plan-review.json",
        {
            "schema_version": "1",
            "change_id": change_id,
            "decision": "pass",
            "codegen_readiness": "ready",
            "required_capabilities": ["auth.api_admin_token"],
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "findings": [],
        },
    )
    _write_json(
        change / "review" / "api-plan-checks.json",
        {
            "schema_version": "1",
            "status": "fail",
            "checks": [
                {"check_id": "l1_path", "status": "pass", "findings": [], "refs": []},
                {
                    "check_id": "assert_ideal",
                    "status": "fail",
                    "findings": [
                        {
                            "locator": "TC_API_001",
                            "expected": "4xx rejection",
                            "actual": "missing token",
                        }
                    ],
                    "refs": ["plans/api-plan.md"],
                },
            ],
        },
    )
    events = [
        {
            "seq": 1,
            "type": "graph_invocation_started",
            "invocation_id": "inv-api-review",
            "graph_id": "api-plan-cycle",
            "graph_digest": "graph-frozen",
            "policy_digest": "policy-frozen",
        },
        {
            "seq": 2,
            "type": "task_attempt_started",
            "invocation_id": "inv-api-review",
            "node_id": "mechanical-plan-checks",
            "contract_digest": "mechanical-contract",
        },
        {
            "seq": 3,
            "type": "task_attempt_started",
            "invocation_id": "inv-api-review",
            "node_id": "review",
            "contract_digest": "review-contract",
        },
    ]
    events_path = change / "events.jsonl"
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

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
        "change_id": change_id,
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
    trace_path = tmp_path / "trace.json"
    _write_json(trace_path, trace)

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
    _write_json(change / "inspect" / "trace-projection.json", reconciled)
    reconciled_digest = hashlib.sha256(
        json.dumps(reconciled, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    quality_gate = {
        "final_status": "FAIL",
        "schema_version": "1.0",
        "change_id": change_id,
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
    _write_json(change / "execution" / "quality-gate-result.json", quality_gate)

    verify_path = tmp_path / "verify.json"
    _write_json(
        verify_path,
        {
            "verdict": "fail",
            "change_id": change_id,
            "phase": "reconciled",
            "as_of": "2026-07-30T00:00:00Z",
            "policy_digest": "verify-policy",
            "projection_digest": reconciled_digest,
            "blocking_gaps": [],
            "open_problem_ids": ["PROB-1", "PROB-2"],
            "insufficient": [],
        },
    )
    return project, trace_path, verify_path


def test_collect_freezes_capability_policy_replay_and_trace_evidence(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--schema-root",
            str(_ROOT),
            "--change-id",
            "CH-REPORT-1",
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            "9",
            "--verify-exit",
            "40",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    capability = report["capability_contract_policy"]
    assert capability["mechanical_checks"] == {
        "status": "fail",
        "finding_count": 1,
        "by_check": {
            "assert_ideal": {"status": "fail", "finding_count": 1},
            "l1_path": {"status": "pass", "finding_count": 0},
        },
    }
    assert capability["capabilities"] == {
        "required": ["auth.api_admin_token"],
        "missing": [],
    }
    assert capability["policy"]["source"] == "packaged_default"
    assert capability["policy"]["plan_check_action"] == "warn"
    assert capability["policy"]["recorded_digest"] == "policy-frozen"
    assert [(row["action"], row["verdict"]) for row in capability["policy_replay"]] == [
        ("warn", "pass"),
        ("block", "reject"),
        ("require_human", "needs_human_review"),
    ]
    assert capability["contracts"]["mechanical_execution_contract_digest"] == "mechanical-contract"
    assert capability["contracts"]["agent_execution_contract_digests"] == []
    assert capability["contracts"]["rendered_output_contract_count"] == 0
    assert capability["contracts"]["prompt_observability"] == "schema_digest_mismatch"

    evidence = report["traceability_evidence"]
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


def test_render_emits_both_specialty_sections_and_policy_matrix(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    collect = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--schema-root",
            str(_ROOT),
            "--change-id",
            "CH-REPORT-1",
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            "0",
            "--verify-exit",
            "40",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
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
    assert (
        "| `CH-REPORT-1` | fail | assert_ideal=fail(1), l1_path=pass(0) | 1 | 1/0 | "
        "0 | 0 | schema_digest_mismatch | packaged_default/warn | no |"
    ) in rendered.stdout
    assert "### Policy Replay Matrix" in rendered.stdout
    assert "| `CH-REPORT-1` | pass | reject | needs_human_review |" in rendered.stdout
    assert "## Traceability / Evidence Projection" in rendered.stdout
    assert "| `CH-REPORT-1` | execution | batch-1 | degraded | 2 | 2 | 1 | 1 |" in rendered.stdout
    assert (
        "| `CH-REPORT-1` | 1 | 1 | missing_fuzz_run:1, never_run:1 | PASS | 48.35 | 2.58 | FAIL |"
        in rendered.stdout
    )
    assert "reported insufficient=0; observed insufficient=1" in rendered.stdout


def test_collect_rejects_structurally_invalid_trace_instead_of_reporting_zero(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["rows"] = {"not": "a list"}
    _write_json(trace_path, trace)

    result = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--schema-root",
            str(_ROOT),
            "--change-id",
            "CH-REPORT-1",
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            "0",
            "--verify-exit",
            "40",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "validation error" in result.stderr
    assert not output.exists()


def test_frozen_specialty_report_reconstructs_trace_gate_row_without_cli_rerun(
    tmp_path: Path,
) -> None:
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    collect = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--schema-root",
            str(_ROOT),
            "--change-id",
            "CH-REPORT-1",
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            "9",
            "--verify-exit",
            "40",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collect.returncode == 0, collect.stderr

    row = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "evidence-row",
            "--change-id",
            "CH-REPORT-1",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert row.returncode == 0, row.stderr
    assert row.stdout == "CH-REPORT-1|9|degraded|1|40|fail|0|0\n"


def test_evidence_row_rejects_report_for_a_different_resume_item(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
    output = tmp_path / "specialty.json"
    collect = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--schema-root",
            str(_ROOT),
            "--change-id",
            "CH-REPORT-1",
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            "0",
            "--verify-exit",
            "40",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
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
            "CH-REPORT-1",
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
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
    change = project / "qa" / "changes" / "CH-REPORT-1"
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

    result = subprocess.run(
        [
            sys.executable,
            str(_REPORTER),
            "collect",
            "--project-root",
            str(project),
            "--schema-root",
            str(_ROOT),
            "--change-id",
            "CH-REPORT-1",
            "--trace",
            str(trace_path),
            "--verify",
            str(verify_path),
            "--trace-exit",
            "0",
            "--verify-exit",
            "40",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
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


def test_cursor_helper_collects_and_renders_real_specialty_report(tmp_path: Path) -> None:
    project, trace_path, verify_path = _install_frozen_item(tmp_path)
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
            "CH-REPORT-1",
            shlex.quote(str(trace_path)),
            shlex.quote(str(verify_path)),
            shlex.quote(str(output)),
            shlex.quote(str(log)),
            "9",
            "40",
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
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["change_id"] == "CH-REPORT-1"
    assert report["traceability_evidence"]["command_status"] == {
        "trace_exit": 9,
        "verify_exit": 40,
    }
    assert "## Capability + Contract + Policy" in result.stdout
    assert "### Policy Replay Matrix" in result.stdout
    assert "## Traceability / Evidence Projection" in result.stdout
