"""operation:materialize-trace-projection — the reconciled projection plus the
facts the independent trace gate routes on.

The node runs after issue reconciliation, so it is the first place a *reconciled*
projection can be published for the change as a whole. It writes two documents
and judges nothing else: routing is the ``trace-sufficiency-gate``'s job, and
this operation may not touch the execution manifest or the quality gate, whose
``final_status`` states what the execution found.

Two properties get most of the attention here because they are what a consumer
would otherwise have to trust:

- **The ``as_of`` is the batch's own instant**, never a wall clock, so the same
  evidence yields the same verdict on every replay.
- **Failures are recorded, not raised**, wherever a document can still be
  written: a policy that cannot be applied is a fact the gate must see, and a
  task failure would deny it the chance.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models import TraceProjection, TraceSufficiencyFacts
from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.evidence.sufficiency import SufficiencyReport
from assurance_agent.workflow.graph.handlers import trace_projection
from assurance_agent.workflow.graph.handlers.trace_projection import (
    TRACE_PROJECTION_REL,
    TRACE_SUFFICIENCY_REL,
    materialize_trace_projection,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-TRACE-011"
BATCH_ID = "20260702-111111"
CASE_ID = "TC_API_001"
EXECUTED_AT = datetime(2026, 7, 2, 11, 11, 11, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _write_cases(change_dir: Path) -> None:
    document = {
        "schema_version": "1.0",
        "added": [
            {
                "case_id": CASE_ID,
                "module": "system.api",
                "type": "API",
                "assertions": ["an assertion"],
                "automation": {"required": True},
            }
        ],
        "modified": [],
        "removed": [],
    }
    path = change_dir / "cases" / "system" / "api" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_tests(project_root: Path) -> dict[str, str]:
    source = f"def test_{CASE_ID.lower()}__scenario() -> None:\n    assert True\n"
    path = project_root / "tests" / "api" / "test_x.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return {"tests/api/test_x.py": hashlib.sha256(source.encode("utf-8")).hexdigest()}


def _write_manifest(
    change_dir: Path,
    test_files: dict[str, str],
    *,
    executed_at: str | None = None,
    batch_id: str = BATCH_ID,
) -> None:
    document: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "result_files": {"api": f"runs/{batch_id}/api-result.json"},
        "test_files_sha256": test_files,
        "final_status": "PASS",
    }
    if executed_at is not None:
        document["executed_at"] = executed_at
    path = change_dir / "execution" / "execution-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_api_result(change_dir: Path, *, status: str = "passed", batch_id: str = BATCH_ID) -> None:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "target": "api",
        "cases": [
            {
                "case_id": CASE_ID,
                "status": status,
                "file": "tests/api/test_x.py",
                "test_name": f"test_{CASE_ID.lower()}__scenario",
            }
        ],
        "unmapped_tests": [],
    }
    (batch_dir / "api-result.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def _write_failure_analysis(change_dir: Path) -> None:
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "source_manifest": "execution/execution-manifest.yaml",
        "inspection_status": "completed",
        "batch_id": BATCH_ID,
        "source_batch_id": BATCH_ID,
        "final_status": "PASS",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "no_failures",
        "failures": [],
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }
    path = change_dir / "inspect" / "failure-analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _write_clean_snapshot(change_dir: Path) -> None:
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "authoritative_batch_id": BATCH_ID,
        "observations": [],
        "occurrences": [],
        "analysis_status": None,
        "project_sync_status": "completed",
        "batches": [BATCH_ID],
    }
    path = change_dir / "issues" / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _write_problem_ledger(project_root: Path, problems: list[dict[str, object]] | None = None) -> None:
    document = {
        "schema_version": "1.0",
        "generated_at": "2026-07-02T12:00:00Z",
        "problems": problems or [],
    }
    path = project_root / "qa" / "issues" / "problems.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _project(tmp_path: Path, *, executed_at: str | None = None) -> Path:
    """A reconciled-clean change: cases, mapped tests, manifest, result, inspect,
    a clean issue snapshot and an empty problem ledger."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    _write_cases(change_dir)
    _write_manifest(change_dir, _write_tests(project_root), executed_at=executed_at)
    _write_api_result(change_dir)
    _write_failure_analysis(change_dir)
    _write_clean_snapshot(change_dir)
    _write_problem_ledger(project_root)
    return project_root


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="materialize-trace-projection",
        graph_id="inspect-with-issues",
        target="operation:materialize-trace-projection",
        input={},
    )


def _context() -> RuntimeContext:
    return RuntimeContext.model_construct(change_id=CHANGE_ID, params={})


def _run(project_root: Path) -> TaskResult:
    return materialize_trace_projection(_task(), _workspace(project_root), _context())


def _projection(project_root: Path) -> TraceProjection:
    path = project_root / "qa" / "changes" / CHANGE_ID / TRACE_PROJECTION_REL
    return TraceProjection.model_validate_json(path.read_bytes())


def _facts(project_root: Path) -> TraceSufficiencyFacts:
    path = project_root / "qa" / "changes" / CHANGE_ID / TRACE_SUFFICIENCY_REL
    return TraceSufficiencyFacts.model_validate_json(path.read_bytes())


def _write_policy(project_root: Path, *, on_insufficient: str = "require_human", recency: int = 72) -> None:
    document = {
        "version": 1,
        "human_review_risk_levels": ["high"],
        "force_continue_allowed": False,
        "plan_checks": {
            "l1_path": "block",
            "shared_factory": "warn",
            "assert_ideal": "warn",
            "capability_keys": "block",
        },
        "coverage_floor": {"risk_high": 80.0, "risk_medium": 60.0},
        "fuzz": {"required_when_endpoint_has_auth": True},
        "healing": {"auth_module": "require_human"},
        "evidence_sufficiency": {
            "recency_hours": recency,
            "required_kinds": {
                "API": ["covered", "execution_recent"],
                "E2E": ["covered", "execution_recent"],
                "Fuzz": ["covered", "fuzz_run"],
                "Performance": ["covered", "perf_run"],
            },
            "on_insufficient": on_insufficient,
        },
    }
    (project_root / ".aa" / "policy.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


# --------------------------------------------------------------------------- #
# both documents, published together
# --------------------------------------------------------------------------- #


def test_the_reconciled_projection_and_its_facts_are_both_published(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())

    result = _run(project_root)

    assert result.status == "succeeded"
    projection = _projection(project_root)
    assert projection.phase == "reconciled"
    assert projection.change_id == CHANGE_ID
    assert projection.authoritative_batch_id == BATCH_ID
    assert [row.case_id for row in projection.rows] == [CASE_ID]
    facts = _facts(project_root)
    assert facts.change_id == CHANGE_ID
    assert facts.authoritative_batch_id == BATCH_ID


def test_the_facts_echo_the_projections_integrity_and_gaps(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())

    _run(project_root)

    projection = _projection(project_root)
    facts = _facts(project_root)
    assert facts.integrity == projection.integrity == "complete"
    assert facts.integrity_blocks_routing is False
    assert facts.gap_codes == ()


def test_the_result_value_names_both_documents_without_a_verdict(tmp_path: Path) -> None:
    """The node's value is for edges and operators; the verdict lives in the gate."""
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())

    value = _run(project_root).value

    assert isinstance(value, dict)
    assert value["integrity"] == "complete"
    assert value["sufficient"] is True
    assert value["error_code"] is None
    assert "verdict" not in value


# --------------------------------------------------------------------------- #
# as_of comes from the authoritative batch, never the clock
# --------------------------------------------------------------------------- #


def test_as_of_is_the_manifests_executed_at(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())

    _run(project_root)

    assert _facts(project_root).as_of == EXECUTED_AT


def test_as_of_falls_back_to_the_batch_ids_utc_instant(tmp_path: Path) -> None:
    """A manifest predating ``executed_at`` still pins a deterministic instant —
    the same approximation the fold records as ``batch_id_legacy_utc``."""
    project_root = _project(tmp_path)

    _run(project_root)

    assert _facts(project_root).as_of == datetime(2026, 7, 2, 11, 11, 11, tzinfo=UTC)


def test_the_verdict_does_not_move_with_the_wall_clock(tmp_path: Path) -> None:
    """The decisive property: an old batch stays judged at its own instant.

    Evaluating at ``now`` would make the same bytes sufficient today and stale
    tomorrow, so a replay of one batch could route differently every time.
    """
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    _write_policy(project_root, recency=72)

    _run(project_root)

    facts = _facts(project_root)
    assert facts.as_of == EXECUTED_AT
    assert datetime.now(UTC) - EXECUTED_AT > timedelta(hours=72), "fixture must be older than recency"
    assert facts.sufficient is True


def test_the_two_documents_are_byte_identical_across_two_runs(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    change_dir = project_root / "qa" / "changes" / CHANGE_ID

    _run(project_root)
    first = (
        (change_dir / TRACE_PROJECTION_REL).read_bytes(),
        (change_dir / TRACE_SUFFICIENCY_REL).read_bytes(),
    )
    _run(project_root)

    assert first == (
        (change_dir / TRACE_PROJECTION_REL).read_bytes(),
        (change_dir / TRACE_SUFFICIENCY_REL).read_bytes(),
    )


# --------------------------------------------------------------------------- #
# the policy it was judged under
# --------------------------------------------------------------------------- #


def test_the_facts_record_the_policy_digest_they_were_judged_under(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    _write_policy(project_root)

    _run(project_root)

    assert _facts(project_root).policy_digest == policy_digest(load_policy(project_root))


def test_an_insufficient_row_is_named_with_its_reasons(tmp_path: Path) -> None:
    """A covered case the batch never executed: one shortfall, with its reason."""
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    _write_policy(project_root)
    batch_dir = project_root / "qa" / "changes" / CHANGE_ID / "execution" / "runs" / BATCH_ID
    document = json.loads((batch_dir / "api-result.json").read_text(encoding="utf-8"))
    document["cases"] = []
    (batch_dir / "api-result.json").write_text(json.dumps(document, indent=2), encoding="utf-8")

    _run(project_root)

    facts = _facts(project_root)
    assert facts.sufficient is False
    assert [case.case_id for case in facts.insufficient_cases] == [CASE_ID]
    assert facts.insufficient_cases[0].reason_codes == ("never_run",)


def test_open_problems_are_reported_as_one_routing_fact(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    (change_dir / "issues" / "snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "authoritative_batch_id": BATCH_ID,
                "observations": [
                    {
                        "observation_id": "OBS-1",
                        "change_id": CHANGE_ID,
                        "batch_id": BATCH_ID,
                        "kind": "test_failure",
                        "target": "api",
                        "case_id": CASE_ID,
                        "source": {
                            "artifact": f"execution/runs/{BATCH_ID}/api-result.json",
                            "json_pointer": "/cases/0",
                        },
                        "evidence_refs": [f"execution/runs/{BATCH_ID}/api-result.json"],
                        "signature": "HTTP 500",
                        "observed_at": "2026-07-02T11:11:11Z",
                    }
                ],
                "occurrences": [
                    {
                        "occurrence_id": "OCC-1",
                        "change_id": CHANGE_ID,
                        "batch_id": BATCH_ID,
                        "observation_ids": ["OBS-1"],
                        "problem_id": "PROB-1",
                        "provisional_assessment": {
                            "classification": "product_bug",
                            "severity": "high",
                            "confidence": 0.9,
                            "rationale": "500 from the endpoint",
                        },
                        "analysis": {
                            "summary": "endpoint returns 500",
                            "suspected_component": "api",
                            "reproduction": "call the endpoint",
                        },
                    }
                ],
                "analysis_status": {
                    "schema_version": "1.0",
                    "change_id": CHANGE_ID,
                    "batch_id": BATCH_ID,
                    "status": "completed",
                    "evidence_bundle_digest": "sha256:" + "a" * 64,
                    "candidate_count": 1,
                    "candidate_digest": "sha256:" + "b" * 64,
                },
                "project_sync_status": "completed",
                "batches": [BATCH_ID],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_problem_ledger(
        project_root,
        [
            {
                "problem_id": "PROB-1",
                "fingerprint": {"version": "1", "digest": "sha256:" + "c" * 64},
                "title": "endpoint 500",
                "assessment": {
                    "classification": "product_bug",
                    "severity": "high",
                    "authority": "llm_provisional",
                },
                "status": "detected",
                "first_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-1"},
                "last_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-1"},
                "occurrences": ["OCC-1"],
                "version": 1,
            }
        ],
    )

    _run(project_root)

    assert _facts(project_root).has_open_problems is True


# --------------------------------------------------------------------------- #
# missing inputs are a gap projection, not a task failure
# --------------------------------------------------------------------------- #


def test_a_change_with_no_evidence_yields_a_gap_projection_and_succeeds(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    (project_root / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)

    result = _run(project_root)

    assert result.status == "succeeded"
    projection = _projection(project_root)
    assert projection.rows == ()
    assert "manifest_missing" in {gap.code for gap in projection.gaps}
    assert projection.integrity == "incomplete"


def test_a_vacuously_sufficient_projection_cannot_be_read_as_a_pass(tmp_path: Path) -> None:
    """Zero rows makes ``sufficient`` vacuously true; ``integrity`` is what says
    the fold read nothing, and the facts must carry the blocking flag so no gate
    can route the emptiness as a pass."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    (project_root / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)

    _run(project_root)

    facts = _facts(project_root)
    assert facts.integrity == "incomplete"
    assert facts.integrity_blocks_routing is True
    assert facts.error_code == "evidence_projection_missing"
    assert facts.sufficient is False


def test_an_unresolvable_change_is_a_task_failure(tmp_path: Path) -> None:
    """The one input whose absence leaves nothing to publish.

    Without a project config the change directory cannot even be located, so no
    projection — not even an empty one — can be attributed to a change.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()

    result = _run(project_root)

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


# --------------------------------------------------------------------------- #
# a policy that cannot be applied is recorded, not raised
# --------------------------------------------------------------------------- #


def test_a_broken_policy_is_recorded_as_policy_error(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    (project_root / ".aa" / "policy.yaml").write_text(
        "version: 1\nevidence_sufficiency:\n  recency_hours: -5\n", encoding="utf-8"
    )

    result = _run(project_root)

    assert result.status == "succeeded", "the gate must get to see the failure"
    facts = _facts(project_root)
    assert facts.error_code == "policy_error"
    assert facts.sufficient is False
    assert facts.policy_digest is None
    assert facts.as_of is None
    # The projection is still authoritative: only the judgement is missing.
    assert [row.case_id for row in _projection(project_root).rows] == [CASE_ID]


def test_a_policy_missing_a_case_type_is_a_policy_error(tmp_path: Path) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    _write_policy(project_root)
    policy = yaml.safe_load((project_root / ".aa" / "policy.yaml").read_text(encoding="utf-8"))
    del policy["evidence_sufficiency"]["required_kinds"]["API"]
    (project_root / ".aa" / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")

    result = _run(project_root)

    assert result.status == "succeeded"
    assert _facts(project_root).error_code == "policy_error"


def test_an_unattributable_batch_is_recorded_as_a_projection_error(tmp_path: Path) -> None:
    """No orderable batch instant means no ``as_of``, so nothing can be judged —
    and inventing ``now`` instead is exactly the wall-clock dependency the node
    must not have."""
    project_root = _project(tmp_path)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    _write_manifest(change_dir, _write_tests(project_root), batch_id="not-a-batch-id")

    result = _run(project_root)

    assert result.status == "succeeded"
    facts = _facts(project_root)
    assert facts.error_code == "evidence_projection_missing"
    assert facts.as_of is None


# --------------------------------------------------------------------------- #
# the node stays out of the execution verdict
# --------------------------------------------------------------------------- #


def test_the_node_writes_nothing_but_its_two_documents(tmp_path: Path) -> None:
    """A trace verdict may not become an execution verdict.

    ``QualityGateResult.final_status`` and the execution manifest state what the
    execution found; this node adds a second, independent judgement and must
    leave both byte-identical.
    """
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    gate_path = change_dir / "inspect" / "quality-gate-result.json"
    gate_path.write_text(json.dumps({"final_status": "PASS"}), encoding="utf-8")
    before = {
        path.relative_to(change_dir).as_posix(): path.read_bytes()
        for path in sorted(change_dir.rglob("*"))
        if path.is_file()
    }

    _run(project_root)

    after = {
        path.relative_to(change_dir).as_posix(): path.read_bytes()
        for path in sorted(change_dir.rglob("*"))
        if path.is_file()
    }
    assert set(after) - set(before) == {TRACE_PROJECTION_REL, TRACE_SUFFICIENCY_REL}
    assert {rel: payload for rel, payload in after.items() if rel in before} == before


def test_a_self_contradicting_judgement_is_not_published_as_a_policy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two error codes describe *inputs*, so neither may absorb a producer bug.

    ``policy_error`` tells an operator to go and audit the policy. If the code used
    it for "the report and the projection disagreed about integrity" — a state the
    evaluator cannot reach, since it copies the projection's own level — an operator
    would audit a policy that was fine, and the contradiction would be published as
    a fact about it. So a document the facts model refuses is left to raise: there is
    no input to blame and nothing truthful to write.
    """
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())

    def _lying(projection: TraceProjection, policy: object, *, as_of: datetime) -> SufficiencyReport:
        return SufficiencyReport(
            change_id=projection.change_id,
            as_of=as_of,
            recency_hours=72,
            # Disagrees with `projection.integrity`, which is "complete" here.
            integrity="incomplete",
            rows=(),
        )

    monkeypatch.setattr(trace_projection, "evaluate_sufficiency", _lying)

    with pytest.raises(ValidationError):
        _run(project_root)

    facts_path = project_root / "qa" / "changes" / CHANGE_ID / TRACE_SUFFICIENCY_REL
    assert not facts_path.exists(), "nothing truthful could be written, so nothing was"


@pytest.mark.parametrize("rel", [TRACE_PROJECTION_REL, TRACE_SUFFICIENCY_REL])
def test_no_temporary_file_survives_a_publication(tmp_path: Path, rel: str) -> None:
    project_root = _project(tmp_path, executed_at=EXECUTED_AT.isoformat())

    _run(project_root)

    inspect_dir = project_root / "qa" / "changes" / CHANGE_ID / "inspect"
    assert [path.name for path in inspect_dir.glob(".*")] == []
    assert (inspect_dir.parent / rel).is_file()
