from __future__ import annotations

import pytest

from agent_runtime_contracts.ops import OutputError
from assurance_execution.contracts.observations import CollectorDocumentV1
from assurance_execution.operations.observation_run import (
    RunnerUnsupported,
    build_family_argv,
    normalize_collector_report,
)


def test_incomplete_collector_never_normalizes_to_success() -> None:
    document = CollectorDocumentV1.model_validate(
        {
            "protocol_version": "1",
            "collection_token": "unit-test-token",
            "identity": {
                "plan_digest": "a" * 64,
                "method_plan_refs": [],
                "mapping_digest": "b" * 64,
                "batch_id": "B-1",
                "baseline_tree_id": "c" * 64,
                "runner_profile_digest": "d" * 64,
            },
            "complete": False,
            "pytest_exitstatus": 2,
            "collected_nodeids": [],
            "collection_errors": ["collection_failed"],
            "observations": [],
            "report": {
                "summary": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0},
                "tests": [],
            },
        }
    )
    with pytest.raises(OutputError, match="collector_incomplete"):
        normalize_collector_report(document)


def test_complete_empty_collection_normalizes() -> None:
    document = CollectorDocumentV1.model_validate(
        {
            "protocol_version": "1",
            "collection_token": "unit-test-token",
            "identity": {
                "plan_digest": "a" * 64,
                "method_plan_refs": [],
                "mapping_digest": "b" * 64,
                "batch_id": "B-1",
                "baseline_tree_id": "c" * 64,
                "runner_profile_digest": "d" * 64,
            },
            "complete": True,
            "pytest_exitstatus": 0,
            "collected_nodeids": [],
            "collection_errors": [],
            "observations": [],
            "report": {
                "summary": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0},
                "tests": [],
            },
        }
    )
    report = normalize_collector_report(document)
    assert report.summary.collected == 0


def test_performance_and_missing_family_are_unsupported() -> None:
    with pytest.raises(RunnerUnsupported, match="performance"):
        build_family_argv("performance", ("qa/tests/perf.py",), batch_id="B-1")
    argv = build_family_argv("e2e", ("qa/tests/e2e/test_a.py",), batch_id="B-9")
    assert "--output=/tmp/aa-playwright-B-9" in argv
    assert "aa_observe" in argv
