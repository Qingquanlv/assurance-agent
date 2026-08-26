from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from graph_engine.canonical import JSONValue

from assurance_agent.workflow.execution.scope import resolve_test_paths
from assurance_execution.operations.normalize import NormalizeHandler
from assurance_execution.operations.runner import classify_exit
from assurance_execution.operations.selection import SelectHandler
from assurance_execution.contracts.selection import selected_test_file
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    as_object,
    execute_task,
    codegen_mapping,
    materialize_execution_view,
    select_request,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _normalize_payload(fixture: dict[str, object]) -> JSONValue:
    selected = list(cast(list[str], fixture["selected"]))
    return cast(
        JSONValue,
        {
            "change_id": "CH-DEMO-001",
            "batch_id": "20260822T000000Z",
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "mapping": {
                "selected": selected,
                "mappings": [
                    {
                        "test": path,
                        "case_id": "TC_A",
                        "capability": "entities.item.create",
                        "layer": "api",
                    }
                    for path in selected
                ],
            },
            "capability_leafs": ["auth.session.create", "entities.item.create"],
            "case_ids": ["TC_A", "TC_B"],
            "baseline_tree_id": "b" * 64,
            "runner_profile_digest": "c" * 64,
            "command": ["pytest", *selected] if selected else ["pytest"],
            "exit_code": fixture["exit_code"],
            "report": fixture["report"],
        },
    )


@pytest.mark.asyncio
async def test_legacy_and_execution_select_the_same_mapped_tests(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-DEMO-001"
    plans = change_dir / "plans"
    plans.mkdir(parents=True)
    mapping = codegen_mapping(target_file="tests/api/test_users.py")
    (plans / "api-codegen-mapping.json").write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "api" / "test_users.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "tests" / "legacy_test.py").write_text("def test_old():\n    assert True\n")
    legacy = resolve_test_paths(change_dir, "api")
    current = await execute_task(
        SelectHandler(),
        select_request(target_file="tests/api/test_users.py"),
        tmp_path,
    )
    assert current.status == "succeeded"
    selected = as_object(as_object(current.output)["mapping"])["selected"]
    assert tuple(legacy or ()) == tuple(dict.fromkeys(selected_test_file(item) for item in selected))
    assert "tests/legacy_test.py" not in selected


@pytest.mark.asyncio
async def test_normalize_pass_fail_empty_and_unmapped(tmp_path: Path) -> None:
    passed = await execute_task(NormalizeHandler(), _normalize_payload(_load("pass.json")), tmp_path)
    failed = await execute_task(NormalizeHandler(), _normalize_payload(_load("fail.json")), tmp_path)
    empty = await execute_task(NormalizeHandler(), _normalize_payload(_load("empty.json")), tmp_path)
    unmapped = await execute_task(NormalizeHandler(), _normalize_payload(_load("unmapped.json")), tmp_path)
    assert passed.status == "succeeded"
    assert as_object(as_object(passed.output)["receipt"])["passed"] == 1
    assert classify_exit(0, failed=0, collected=1) == "passed"
    assert failed.status == "succeeded"
    assert as_object(as_object(failed.output)["receipt"])["failed"] == 1
    assert as_object(as_object(failed.output)["results"][0])["message"] == "AssertionError: 200 != 500"
    assert classify_exit(1, failed=1, collected=1) == "failed"
    assert empty.status == "succeeded"
    assert as_object(empty.output)["results"] == []
    assert classify_exit(5, failed=0, collected=0) == "skipped"
    assert unmapped.status == "failed"
    assert unmapped.failure is not None
    assert "outside the closed mapping" in unmapped.failure.message


@pytest.mark.asyncio
async def test_pr_metric_input_tracks_selected_results(tmp_path: Path) -> None:
    from assurance_execution.operations.runner import RunTestsAndCollectPrMetricsHandler
    from execution_fixtures import (  # pyright: ignore[reportMissingImports]
        fake_pytest_host,
        run_request,
        write_test,
    )

    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    materialize_execution_view(tmp_path, ["tests/generated_test.py"])
    outcome = await execute_task(
        RunTestsAndCollectPrMetricsHandler(process_host=fake_pytest_host()),
        run_request(selected=["tests/generated_test.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    output = as_object(outcome.output)
    metric = as_object(output["pr_metric_input"])
    assert metric["selected"] == ["tests/generated_test.py"]
    assert [as_object(row)["test"] for row in metric["results"]] == ["tests/generated_test.py"]
    assert metric["mapping_digest"] == output["mapping_digest"]
    assert metric["receipt_digest"] == output["receipt_digest"]
