from __future__ import annotations

from typing import Any


from assurance_execution.contracts import ClosedMappingV1
from assurance_execution.operations.normalize import normalize_evidence
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    PLAN_DIGEST,
    PLAN_REF,
    closed_mapping,
)


def _normalize_input(
    *,
    selected: list[str],
    tests: list[dict[str, object]],
    exit_code: int = 0,
) -> dict[str, Any]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": PLAN_DIGEST,
        "plan_ref": PLAN_REF,
        "batch_id": "20260822T000000Z",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": closed_mapping(selected),
        "capability_leafs": ["auth.session.create", "entities.item.create"],
        "case_ids": ["TC_A", "TC_B"],
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "command": ["pytest", *selected],
        "exit_code": exit_code,
        "report": {
            "tests": tests,
            "exitcode": exit_code,
            "summary": {"collected": len(tests)},
        },
    }


def test_normalize_accepts_the_public_mapping_form_of_selected_targets() -> None:
    selected = ["tests/generated_test.py"]
    mapping = ClosedMappingV1.model_validate(closed_mapping(selected))

    evidence = normalize_evidence(
        change_id="CH-DEMO-001",
        plan_digest=PLAN_DIGEST,
        plan_ref=PLAN_REF,
        batch_id="20260822T000000Z",
        selected_targets={"api": True, "e2e": False, "fuzz": False, "performance": False},
        mapping=mapping,
        capability_leafs=frozenset({"auth.session.create", "entities.item.create"}),
        case_ids=frozenset({"TC_A", "TC_B"}),
        baseline_tree_id="b" * 64,
        runner_profile_digest="c" * 64,
        command=("pytest", *selected),
        exit_code=0,
        report={
            "tests": [
                {
                    "nodeid": "tests/generated_test.py::test_tc_a_001__ok",
                    "outcome": "passed",
                    "call": {"outcome": "passed", "duration": 0.01},
                }
            ],
            "exitcode": 0,
            "summary": {"collected": 1, "passed": 1, "failed": 0, "skipped": 0},
        },
    )

    assert tuple(command.family for command in evidence.receipt.commands) == ("api",)
