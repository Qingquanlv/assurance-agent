from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase4.conformance import execute_task

from assurance_execution.operations.selection import SelectHandler
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    as_object,
    select_request,
    write_test,
)


@pytest.mark.asyncio
async def test_select_builds_closed_mapping_from_codegen_mappings(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    (tmp_path / "workflow-state.json").write_text("{}", encoding="utf-8")
    outcome = await execute_task(SelectHandler(), select_request(), tmp_path)
    assert outcome.status == "succeeded"
    mapping = as_object(as_object(outcome.output)["mapping"])
    assert mapping["selected"] == ["tests/generated_test.py"]
    assert as_object(mapping["mappings"][0])["case_id"] == "TC_A"
    assert as_object(mapping["mappings"][0])["capability"] == "entities.item.create"
    assert "tests/legacy_test.py" not in mapping["selected"]


@pytest.mark.asyncio
async def test_select_rejects_unknown_leaf_or_case(tmp_path: Path) -> None:
    payload = select_request()
    payload["reviewed_cases"]["added"][0]["trace"] = {"auth.fake": {"covered": True}}
    unknown_leaf = await execute_task(SelectHandler(), payload, tmp_path)
    assert unknown_leaf.status == "failed"
    assert unknown_leaf.failure is not None
    assert unknown_leaf.failure.kind == "invalid_input"
    missing_case = select_request()
    missing_case["mappings"][0]["entries"][0]["case_id"] = "TC_MISSING"
    unknown_case = await execute_task(SelectHandler(), missing_case, tmp_path)
    assert unknown_case.status == "failed"
    assert unknown_case.failure is not None
    assert unknown_case.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_select_ignores_unselected_layer_mapping(tmp_path: Path) -> None:
    payload = select_request(
        extra_mappings=[
            {
                "schema_version": "1",
                "layer": "e2e",
                "entries": [
                    {
                        "case_id": "TC_A",
                        "symbol": "test_tc_a_001__ok",
                        "target_file": "tests/e2e/test_legacy.py",
                    }
                ],
            }
        ]
    )
    outcome = await execute_task(SelectHandler(), payload, tmp_path)
    assert outcome.status == "succeeded"
    assert as_object(as_object(outcome.output)["mapping"])["selected"] == ["tests/generated_test.py"]
