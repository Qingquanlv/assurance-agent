from __future__ import annotations

from pathlib import Path

import pytest
from graph_engine.plugin_api import CandidateWriteSet

from tests.phase4.conformance import execute_task

from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.trace import MaterializeTraceHandler
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    LEAF,
    catalog_leafs,
    trace_input,
    validation_context,
    write_set,
)


@pytest.mark.asyncio
async def test_trace_operation_excludes_unmapped_old_tests(tmp_path: Path) -> None:
    outcome = await execute_task(
        MaterializeTraceHandler(),
        trace_input(closed_mapping=["tests/generated.py"], observed=["tests/generated.py", "tests/old.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    trace = TraceProjectionV2.model_validate(
        outcome.output,
        context={"capability_leafs": frozenset(catalog_leafs())},
    )
    assert tuple(row.test_path for row in trace.rows) == ("tests/generated.py",)


@pytest.mark.asyncio
async def test_trace_rejects_capability_outside_frozen_leafs(tmp_path: Path) -> None:
    payload = trace_input(
        closed_mapping=["tests/generated.py"],
        observed=["tests/generated.py"],
        capability_leafs=["auth.session.create"],
    )
    payload["cases"] = [
        {
            "case_id": "TC_A",
            "module": "menus",
            "case_type": "API",
            "automation_required": True,
            "capability": LEAF,
        }
    ]
    outcome = await execute_task(MaterializeTraceHandler(), payload, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "frozen typed leaf" in outcome.failure.message


def test_trace_validator_requires_catalog_closure() -> None:
    from assurance_quality.validators.trace import TraceValidator

    validator = TraceValidator(
        capability_leafs=frozenset(catalog_leafs()),
        case_ids=frozenset({"TC_A"}),
        file_bytes={"inspect/trace-projection.json": b"{}"},
    )
    result = validator.validate(
        write_set("inspect/trace-projection.json"),
        validation_context(),
    )
    assert result.accepted is False
    assert result.reason


def test_plugin_trace_validator_is_path_only() -> None:
    from assurance_quality.plugin import QualityPlugin
    from graph_engine import ENGINE_API_VERSION, RegistryPorts

    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validator = contribution.commit_validators["assurance.quality.validator.trace.v2"]
    accepted = validator.validate(write_set("inspect/trace-projection.json"), validation_context())
    rejected = validator.validate(write_set("src/app.py"), validation_context())
    assert accepted.accepted is True
    assert rejected.accepted is False
    empty = CandidateWriteSet(baseline_tree_id=HEX_A, candidate_tree_id="1" * 64, files=())
    assert validator.validate(empty, validation_context()).accepted is True
