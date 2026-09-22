from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_quality.operations.sufficiency import build_sufficiency_facts
from assurance_quality.operations.trace import TraceCaseInput, TraceOperationInput, project_trace

_AS_OF = datetime(2026, 9, 5, tzinfo=UTC)
_SHA = "a" * 64


def _evidence(*, case_id: str = "TC_A", case_type: str = "api") -> ExecutionEvidenceV1:
    selected_targets = {
        "api": case_type == "api",
        "e2e": case_type == "e2e",
        "fuzz": case_type == "fuzz",
        "performance": case_type == "performance",
    }
    return ExecutionEvidenceV1.model_validate(
        {
            "schema_version": "1",
            "status": "passed",
            "change_id": "CH-1",
            "plan_digest": _SHA,
            "plan_ref": {
                "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
                "digest": _SHA,
            },
            "batch_id": "B-1",
            "selected_targets": selected_targets,
            "family_outcomes": [{"family": case_type, "state": "executed"}],
            "mapping": {
                "schema_version": "1",
                "selected": ("tests/test_a.py::test_a",),
                "mappings": (
                    {
                        "test": "tests/test_a.py::test_a",
                        "case_id": case_id,
                        "capability": "entities.item.create",
                        "layer": case_type,
                    },
                ),
            },
            "mapping_digest": _SHA,
            "baseline_tree_id": _SHA,
            "runner_profile_digest": _SHA,
            "receipt_digest": _SHA,
            "receipt": {
                "commands": [
                    {
                        "family": "api",
                        **{
                            "command": ("pytest",),
                            "exit_code": 0,
                            "collected": 1,
                            "passed": 1,
                            "failed": 0,
                            "skipped": 0,
                        },
                    }
                ]
            },
            "results": (
                {
                    "test": "tests/test_a.py::test_a",
                    "status": "passed",
                    "duration_ms": 1,
                    "case_id": case_id,
                },
            ),
        },
        context={"capability_leafs": frozenset({"entities.item.create"}), "case_ids": frozenset({case_id})},
    )


def _projection(*, case_type: str = "API", evidence: ExecutionEvidenceV1 | None = None, at=None):
    return project_trace(
        TraceOperationInput(
            change_id="CH-1",
            batch_id="B-1",
            closed_mapping=("tests/test_a.py::test_a",),
            cases=(
                TraceCaseInput(
                    case_id="TC_A",
                    case_type=case_type,  # type: ignore[arg-type]
                    capability="entities.item.create",
                ),
            ),
            capability_leafs=("entities.item.create",),
            case_ids=("TC_A",),
            execution_evidence=evidence,
            executed_at=at or _AS_OF,
        )
    )


def test_unexecuted_required_case_preserves_all_actionable_reasons() -> None:
    facts = build_sufficiency_facts(_projection(), policy_digest=_SHA, as_of=_AS_OF, recency_hours=24)
    assert facts.sufficient is False
    assert facts.insufficient_cases[0].reason_codes == (
        "not_in_current_batch",
        "uncovered",
        "never_run",
        "no_pass",
    )


def test_current_fresh_passing_execution_is_sufficient() -> None:
    facts = build_sufficiency_facts(
        _projection(evidence=_evidence()),
        policy_digest=_SHA,
        as_of=_AS_OF,
        recency_hours=24,
    )
    assert facts.sufficient is True
    assert facts.insufficient_cases == ()


def test_stale_execution_and_pass_are_both_retained() -> None:
    facts = build_sufficiency_facts(
        _projection(evidence=_evidence(), at=_AS_OF - timedelta(hours=25)),
        policy_digest=_SHA,
        as_of=_AS_OF,
        recency_hours=24,
    )
    assert facts.insufficient_cases[0].reason_codes == ("execution_stale", "pass_stale")


def test_fuzz_and_performance_require_family_specific_execution() -> None:
    fuzz = build_sufficiency_facts(
        _projection(case_type="Fuzz"), policy_digest=_SHA, as_of=_AS_OF, recency_hours=24
    )
    performance = build_sufficiency_facts(
        _projection(case_type="Performance"),
        policy_digest=_SHA,
        as_of=_AS_OF,
        recency_hours=24,
    )
    assert "fuzz_run_missing" in fuzz.insufficient_cases[0].reason_codes
    assert "perf_run_missing" in performance.insufficient_cases[0].reason_codes
