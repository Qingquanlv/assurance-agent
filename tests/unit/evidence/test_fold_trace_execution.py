"""Task 3: execution-phase `fold_trace` semantics.

Covers the dual `current` mode, the canonical logical manifest view digest (D6),
result identity fail-closed (P1-7), performance's separate on-disk shape and
capability join (D7), multi-batch latest/freshest/skipped/presence semantics, the
timezone policy (P1-5), determinism and source coverage.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.execution import SelectedTargets
from assurance_agent.artifacts.models.trace import TraceProjectionLike as TraceProjection
from assurance_agent.artifacts.models.trace import TraceRow, TraceSource
from assurance_agent.change_location import ChangeNotFoundError
from assurance_agent.evidence.trace import ExecutionFoldInput, fold_trace
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-TRACE-001"
BATCH_OLD = "20260701-101010"
BATCH_NEW = "20260702-111111"
EXECUTED_AT = datetime(2026, 7, 2, 19, 30, 0, tzinfo=timezone(timedelta(hours=8)))
MANIFEST_FOLD_VIEW_SOURCE = "execution/execution-manifest.json#fold-view"
TESTS_TREE_SCAN_SOURCE = "tests/#tree-digest"


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #


def _change_dir(project_root: Path, change_id: str = CHANGE_ID) -> Path:
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / change_id
    change_dir.mkdir(parents=True, exist_ok=True)
    return change_dir


def _case(
    case_id: str,
    *,
    case_type: str = "API",
    module: str = "system.api",
    required: bool = True,
    capability: str | None = None,
) -> dict[str, object]:
    automation: dict[str, object] = {"required": required}
    if capability is not None:
        automation["performance"] = {"scenario": {"capability": capability}}
    return {
        "case_id": case_id,
        "module": module,
        "type": case_type,
        "assertions": ["an assertion"],
        "automation": automation,
    }


def _write_cases(
    change_dir: Path,
    cases: list[dict[str, object]],
    *,
    rel: str = "cases/system/api/case.yaml",
) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": "1.0", "added": cases, "modified": [], "removed": []}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_result(
    change_dir: Path,
    batch_id: str,
    target: str,
    rows: list[tuple[str, str]],
    *,
    change_id: str = CHANGE_ID,
    doc_batch_id: str | None = None,
    doc_target: str | None = None,
    unmapped: list[tuple[str, str]] | None = None,
) -> Path:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": doc_batch_id or batch_id,
        "target": doc_target or target,
        "status": "passed",
        "command": f"pytest tests/{target}",
        "source": {"framework": "pytest", "raw_log": f"raw/{target}.log", "report_json": ""},
        "total": len(rows),
        "passed": sum(1 for _, status in rows if status == "passed"),
        "failed": sum(1 for _, status in rows if status == "failed"),
        "skipped": sum(1 for _, status in rows if status == "skipped"),
        "cases": [
            {
                "case_id": case_id,
                "status": status,
                "file": f"tests/{target}/test_x.py",
                "test_name": f"test_{case_id.lower()}__scenario",
                "duration_ms": 12,
                "message": "",
                "raw_log_ref": "/abs/raw.log",
                "trace": "",
                "screenshot": "",
                "video": "",
            }
            for case_id, status in rows
        ],
        "unmapped_tests": [
            {
                "case_id": "",
                "status": "passed",
                "file": file,
                "test_name": test_name,
                "duration_ms": 3,
                "message": "",
                "raw_log_ref": "",
            }
            for file, test_name in (unmapped or [])
        ],
    }
    path = batch_dir / f"{target}-result.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _write_performance_result(
    change_dir: Path,
    batch_id: str,
    scenarios: list[tuple[str, str]],
    *,
    change_id: str = CHANGE_ID,
    doc_batch_id: str | None = None,
    kind: str = "performance",
) -> Path:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": doc_batch_id or batch_id,
        "kind": kind,
        "available": True,
        "status": "PASS",
        "scenarios": [
            {
                "capability": capability,
                "endpoint": "/api/v1/thing/list",
                "measured_p95_ms": 8.0,
                "threshold_p95_ms": 2000.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": verdict,
            }
            for capability, verdict in scenarios
        ],
        "command": "uv run locust -f tests/perf/locustfile_thing.py",
        "source": {"raw_log": "/abs/performance.log"},
    }
    path = batch_dir / "performance-result.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _write_raw_result(change_dir: Path, batch_id: str, filename: str, document: dict[str, object]) -> Path:
    """Write a result document verbatim, including omitted or malformed keys."""
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / filename
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _manifest_document(
    batch_id: str,
    *,
    change_id: str = CHANGE_ID,
    targets: dict[str, bool] | None = None,
    executed_at: str | None = None,
    test_files: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    selected = {"api": True, "e2e": False, "fuzz": False, "performance": False}
    selected.update(targets or {})
    document: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "selected_targets": selected,
        "result_files": {"api": f"runs/{batch_id}/api-result.json"},
        "test_files_sha256": test_files,
        "final_status": "PASS",
    }
    if executed_at is not None:
        document["executed_at"] = executed_at
    document.update(extra or {})
    return document


def _write_manifest(change_dir: Path, batch_id: str, **kwargs: object) -> Path:
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    path = execution_dir / "execution-manifest.json"
    path.write_text(
        json.dumps(_manifest_document(batch_id, **kwargs), sort_keys=False),  # type: ignore[arg-type]
        encoding="utf-8",
    )
    return path


def _write_batch_manifest(change_dir: Path, batch_id: str, **kwargs: object) -> Path:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / "execution-manifest.json"
    path.write_text(
        json.dumps(_manifest_document(batch_id, **kwargs), sort_keys=False),  # type: ignore[arg-type]
        encoding="utf-8",
    )
    return path


def _fold_input(
    batch_id: str = BATCH_NEW,
    *,
    executed_at: datetime = EXECUTED_AT,
    targets: dict[str, bool] | None = None,
    test_files: dict[str, str] | None = None,
) -> ExecutionFoldInput:
    selected = {"api": True, "e2e": False, "fuzz": False, "performance": False}
    selected.update(targets or {})
    return ExecutionFoldInput(
        batch_id=batch_id,
        executed_at=executed_at,
        selected_targets=SelectedTargets(**selected),
        test_files_sha256=test_files if test_files is not None else {},
    )


def _row(projection: TraceProjection, case_id: str) -> TraceRow:
    rows = [row for row in projection.rows if row.case_id == case_id]
    assert rows, f"no row for {case_id}"
    return rows[0]


def _gap_codes(projection: TraceProjection) -> list[str]:
    return [gap.code for gap in projection.gaps]


def _source(projection: TraceProjection, path: str) -> TraceSource:
    matches = [source for source in projection.sources if source.path == path]
    assert matches, f"no source entry for {path}"
    return matches[0]


def _simple_change(project_root: Path) -> Path:
    change_dir = _change_dir(project_root)
    _write_cases(change_dir, [_case("TC_API_001")])
    return change_dir


def _write_mapped_test(project_root: Path, case_id: str = "TC_API_001") -> dict[str, str]:
    """Write the test `_write_result` claims to have run, and hash it.

    Task 4 derives coverage from the current tree, so a batch whose mapped test
    does not exist there is a real `mapped_test_missing_from_tree` gap. Tests
    that need a gap-free fold have to put the test on disk and record its hash
    as the batch's `test_files_sha256` baseline.
    """
    source = f"def test_{case_id.lower()}__scenario() -> None:\n    assert True\n"
    path = project_root / "tests" / "api" / "test_x.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return {"tests/api/test_x.py": hashlib.sha256(source.encode("utf-8")).hexdigest()}


# --------------------------------------------------------------------------- #
# current dual mode
# --------------------------------------------------------------------------- #


def test_current_none_takes_batch_facts_from_the_top_level_manifest(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.phase == "execution"
    assert projection.authoritative_batch_id == BATCH_NEW
    assert "manifest_missing" not in _gap_codes(projection)
    assert _row(projection, "TC_API_001").presence_in_current_batch == "executed"


def test_current_none_without_top_level_manifest_produces_manifest_missing_gap(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "manifest_missing" in _gap_codes(projection)
    missing = [gap for gap in projection.gaps if gap.code == "manifest_missing"]
    assert [gap.source for gap in missing] == [MANIFEST_FOLD_VIEW_SOURCE]
    view_source = _source(projection, MANIFEST_FOLD_VIEW_SOURCE)
    assert view_source.exists is False
    assert view_source.sha256 is None
    assert projection.authoritative_batch_id == ""


def test_injected_current_ignores_the_top_level_manifest_entirely(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    current = _fold_input()

    without_manifest = fold_trace(tmp_path, CHANGE_ID, current=current)
    _write_manifest(
        change_dir,
        BATCH_OLD,
        targets={"api": False, "e2e": True},
        test_files={"tests/e2e/test_other.py": "deadbeef"},
    )
    with_manifest = fold_trace(tmp_path, CHANGE_ID, current=current)

    assert without_manifest.model_dump_json() == with_manifest.model_dump_json()
    assert with_manifest.authoritative_batch_id == BATCH_NEW


def test_injected_current_never_produces_manifest_missing(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID, current=_fold_input())

    assert "manifest_missing" not in _gap_codes(projection)
    assert _source(projection, MANIFEST_FOLD_VIEW_SOURCE).exists is True


def test_fold_view_digest_is_the_canonical_json_sha256_of_the_five_fold_fields(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    test_files = {"tests/api/test_x.py": "ab" * 32}

    projection = fold_trace(tmp_path, CHANGE_ID, current=_fold_input(test_files=test_files))

    expected = hashlib.sha256(
        canonical_json_bytes(
            {
                "change_id": CHANGE_ID,
                "batch_id": BATCH_NEW,
                "executed_at": EXECUTED_AT.isoformat(),
                "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
                "test_files_sha256": test_files,
            }
        )
    ).hexdigest()
    assert _source(projection, MANIFEST_FOLD_VIEW_SOURCE).sha256 == expected


def test_fold_view_digest_ignores_manifest_fields_outside_the_view(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    _write_manifest(change_dir, BATCH_NEW)
    baseline = _source(fold_trace(tmp_path, CHANGE_ID), MANIFEST_FOLD_VIEW_SOURCE).sha256

    _write_manifest(
        change_dir,
        BATCH_NEW,
        extra={
            "final_status": "FAIL",
            "product_tree_sha256": "ff" * 32,
            "tests_tree_sha256": "ee" * 32,
            "result_files": {"api": f"runs/{BATCH_NEW}/api-result.json", "summary": "x"},
        },
    )
    assert _source(fold_trace(tmp_path, CHANGE_ID), MANIFEST_FOLD_VIEW_SOURCE).sha256 == baseline


def test_legacy_manifest_view_digest_differs_from_an_injected_view(tmp_path: Path) -> None:
    """Byte parity is a precondition on Task 8's *writer*, not on this fold.

    A manifest published without ``executed_at`` and ``test_files_sha256``
    projects onto a different logical view than the runner's injected one, so the
    two folds cannot agree. Task 8 must publish both fields non-null; this test
    is the marker that makes the requirement fail loudly rather than silently.
    """
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    _write_manifest(change_dir, BATCH_NEW)

    on_disk = _source(fold_trace(tmp_path, CHANGE_ID), MANIFEST_FOLD_VIEW_SOURCE)
    injected = _source(fold_trace(tmp_path, CHANGE_ID, current=_fold_input()), MANIFEST_FOLD_VIEW_SOURCE)

    assert on_disk.sha256 != injected.sha256


def test_manifest_and_injected_views_agree_on_the_same_facts(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    test_files = {"tests/api/test_x.py": "cd" * 32}
    _write_manifest(
        change_dir,
        BATCH_NEW,
        executed_at=EXECUTED_AT.isoformat(),
        test_files=test_files,
    )

    on_disk = fold_trace(tmp_path, CHANGE_ID)
    injected = fold_trace(tmp_path, CHANGE_ID, current=_fold_input(test_files=test_files))

    assert on_disk.model_dump_json() == injected.model_dump_json()


# --------------------------------------------------------------------------- #
# result identity (P1-7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("doc_change_id", "doc_batch_id", "doc_target", "detail_field"),
    [
        ("CH-OTHER-999", None, None, "change_id"),
        (CHANGE_ID, BATCH_OLD, None, "batch_id"),
        (CHANGE_ID, None, "e2e", "target"),
    ],
    ids=["change_id", "batch_id", "target"],
)
def test_result_identity_mismatch_skips_the_document_and_gaps(
    tmp_path: Path,
    doc_change_id: str,
    doc_batch_id: str | None,
    doc_target: str | None,
    detail_field: str,
) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(
        change_dir,
        BATCH_NEW,
        "api",
        [("TC_API_001", "passed")],
        change_id=doc_change_id,
        doc_batch_id=doc_batch_id,
        doc_target=doc_target,
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    mismatches = [gap for gap in projection.gaps if gap.code == "result_identity_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].source == f"execution/runs/{BATCH_NEW}/api-result.json"
    assert detail_field in mismatches[0].detail
    row = _row(projection, "TC_API_001")
    assert row.latest_execution is None
    assert row.freshest_pass is None
    assert row.presence_in_current_batch == "not_in_current_batch"


def test_corrupt_result_json_produces_result_corrupt_gap(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    batch_dir = change_dir / "execution" / "runs" / BATCH_NEW
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "api-result.json").write_text("{not json", encoding="utf-8")

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "result_corrupt" in _gap_codes(projection)
    assert "result_missing" not in _gap_codes(projection)
    assert _row(projection, "TC_API_001").latest_execution is None


def test_selected_target_without_a_result_file_produces_result_missing_gap(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "e2e": True})

    projection = fold_trace(tmp_path, CHANGE_ID)

    missing = [gap for gap in projection.gaps if gap.code == "result_missing"]
    assert sorted(gap.target or "" for gap in missing) == ["api", "e2e"]
    assert all(gap.batch_id == BATCH_NEW for gap in missing)
    api_source = _source(projection, f"execution/runs/{BATCH_NEW}/api-result.json")
    assert api_source.exists is False
    assert api_source.sha256 is None


def test_unselected_targets_do_not_produce_result_missing_gaps(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False})

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "result_missing" not in _gap_codes(projection)


# --------------------------------------------------------------------------- #
# multi-batch semantics
# --------------------------------------------------------------------------- #


def test_latest_execution_is_the_max_batch_and_freshest_pass_is_independent(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_OLD, "api", [("TC_API_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "failed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert (row.latest_execution.batch_id, row.latest_execution.status) == (BATCH_NEW, "failed")
    assert row.freshest_pass is not None
    assert (row.freshest_pass.batch_id, row.freshest_pass.status) == (BATCH_OLD, "passed")


def test_skipped_counts_as_latest_but_never_as_a_pass(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "skipped")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert row.latest_execution.status == "skipped"
    assert row.freshest_pass is None
    assert row.presence_in_current_batch == "executed"


def test_presence_is_executed_not_in_current_batch_or_target_not_selected(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [
            _case("TC_API_001"),
            _case("TC_API_002"),
            _case("TC_E2E_001", case_type="E2E", module="system.e2e"),
        ],
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "e2e": False})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _row(projection, "TC_API_001").presence_in_current_batch == "executed"
    assert _row(projection, "TC_API_002").presence_in_current_batch == "not_in_current_batch"
    assert _row(projection, "TC_E2E_001").presence_in_current_batch == "target_not_selected"


def test_current_batch_presence_ignores_historical_batches(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_OLD, "api", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.presence_in_current_batch == "not_in_current_batch"
    assert row.latest_execution is not None
    assert row.latest_execution.batch_id == BATCH_OLD


def test_missing_current_batch_facts_leave_presence_conservative(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.presence_in_current_batch == "not_in_current_batch"


# --------------------------------------------------------------------------- #
# V2 target isolation
# --------------------------------------------------------------------------- #


def test_same_batch_cross_target_latest_prefers_the_cases_own_target(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "failed")])
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert (row.latest_execution.target, row.latest_execution.status) == ("api", "failed")


def test_same_batch_cross_target_freshest_pass_prefers_the_cases_own_target(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.freshest_pass is not None
    assert row.freshest_pass.target == "api"


def test_freshest_pass_does_not_borrow_another_targets_pass(
    tmp_path: Path,
) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "failed")])
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.freshest_pass is None


def test_own_target_preference_beats_fixed_target_order(tmp_path: Path) -> None:
    """A Fuzz case is answered by the fuzz suite even though api sorts first.

    An API case cannot tell these two rules apart (api is both its own target and
    the first in fixed order), so the discriminating case has to be a case type
    whose own target sorts later.
    """
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_FUZZ_001", case_type="Fuzz", module="system.fuzz")],
        rel="cases/system/fuzz/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_FUZZ_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_FUZZ_001", "failed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_FUZZ_001")

    assert row.latest_execution is not None
    assert (row.latest_execution.target, row.latest_execution.status) == ("fuzz", "failed")
    assert row.freshest_pass is None


def test_own_target_preference_also_applies_to_freshest_pass(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_FUZZ_001", case_type="Fuzz", module="system.fuzz")],
        rel="cases/system/fuzz/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_FUZZ_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_FUZZ_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_FUZZ_001")

    assert row.freshest_pass is not None
    assert row.freshest_pass.target == "fuzz"


def test_cross_target_results_are_ignored_when_the_own_target_is_absent(
    tmp_path: Path,
) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_E2E_001", case_type="E2E", module="system.e2e")],
        rel="cases/system/e2e/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_E2E_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "api", [("TC_E2E_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_E2E_001")

    assert row.latest_execution is None
    assert row.freshest_pass is None


# --------------------------------------------------------------------------- #
# tolerant row lists, strict identity
# --------------------------------------------------------------------------- #


def test_target_document_without_unmapped_tests_is_accepted(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_raw_result(
        change_dir,
        BATCH_NEW,
        "api-result.json",
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_NEW,
            "target": "api",
            "cases": [
                {
                    "case_id": "TC_API_001",
                    "status": "passed",
                    "file": "tests/api/test_x.py",
                    "test_name": "test_tc_api_001__scenario",
                }
            ],
        },
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "result_corrupt" not in _gap_codes(projection)
    assert _row(projection, "TC_API_001").latest_execution is not None
    assert projection.unmapped_tests == ()


def test_target_document_without_any_rows_is_accepted_as_empty_evidence(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_raw_result(
        change_dir,
        BATCH_NEW,
        "api-result.json",
        {"change_id": CHANGE_ID, "batch_id": BATCH_NEW, "target": "api"},
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.gaps == ()
    row = _row(projection, "TC_API_001")
    assert row.latest_execution is None
    assert row.presence_in_current_batch == "not_in_current_batch"


def test_performance_document_without_scenarios_is_accepted_as_empty_evidence(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_PERF_001", case_type="Performance", module="system.perf", capability="wanted")],
        rel="cases/system/perf/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "performance": True})
    _write_raw_result(
        change_dir,
        BATCH_NEW,
        "performance-result.json",
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_NEW,
            "kind": "performance",
            "available": False,
            "status": "SKIPPED",
        },
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "result_corrupt" not in _gap_codes(projection)
    row = _row(projection, "TC_PERF_001")
    assert row.latest_execution is None
    assert row.atemporal_kinds_present == ()


def test_target_document_missing_identity_fields_is_still_rejected(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_raw_result(
        change_dir, BATCH_NEW, "api-result.json", {"batch_id": BATCH_NEW, "target": "api", "cases": []}
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    mismatches = [gap for gap in projection.gaps if gap.code == "result_identity_mismatch"]
    assert len(mismatches) == 1
    assert "change_id" in mismatches[0].detail


def test_dto_validation_failure_is_result_corrupt_not_identity_mismatch(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_raw_result(
        change_dir,
        BATCH_NEW,
        "api-result.json",
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_NEW,
            "target": "api",
            "cases": [
                {
                    "case_id": "TC_API_001",
                    "status": "exploded",
                    "file": "tests/api/test_x.py",
                    "test_name": "test_tc_api_001__scenario",
                }
            ],
        },
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _gap_codes(projection) == ["result_corrupt"]
    assert _row(projection, "TC_API_001").latest_execution is None


# --------------------------------------------------------------------------- #
# aggregation, duplicate declarations, presence precedence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("statuses", "expected", "passes"),
    [
        # A case with a failed row did not pass that batch, even if another of
        # its rows passed — so worst-wins also suppresses `freshest_pass`.
        (["failed", "passed"], "failed", False),
        (["passed", "failed"], "failed", False),
        (["passed", "skipped"], "passed", True),
        (["skipped", "passed"], "passed", True),
        (["skipped", "skipped"], "skipped", False),
    ],
)
def test_duplicate_case_rows_in_one_target_aggregate_worst_wins(
    tmp_path: Path, statuses: list[str], expected: str, passes: bool
) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", status) for status in statuses])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert row.latest_execution.status == expected
    assert (row.freshest_pass is not None) is passes


def test_duplicate_case_id_keeps_the_first_path_sorted_declaration(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_API_001", module="system.aaa")],
        rel="cases/aaa/case.yaml",
    )
    _write_cases(
        change_dir,
        [_case("TC_API_001", module="system.zzz")],
        rel="cases/zzz/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW)

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert [row.case_id for row in projection.rows] == ["TC_API_001"]
    assert projection.rows[0].module == "system.aaa"


def test_presence_ignores_execution_from_a_different_target(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_E2E_001", case_type="E2E", module="system.e2e")],
        rel="cases/system/e2e/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "e2e": False})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_E2E_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_E2E_001")

    assert row.presence_in_current_batch == "target_not_selected"


def test_multi_batch_performance_latest_and_freshest_use_the_capability_join(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_PERF_001", case_type="Performance", module="system.perf", capability="wanted")],
        rel="cases/system/perf/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "performance": True})
    _write_performance_result(change_dir, BATCH_OLD, [("wanted", "PASS")])
    _write_performance_result(change_dir, BATCH_NEW, [("wanted", "FAIL"), ("other", "PASS")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_PERF_001")

    assert row.latest_execution is not None
    assert (row.latest_execution.batch_id, row.latest_execution.status) == (BATCH_NEW, "failed")
    assert row.freshest_pass is not None
    assert (row.freshest_pass.batch_id, row.freshest_pass.status) == (BATCH_OLD, "passed")
    assert "perf_run" in row.atemporal_kinds_present


# --------------------------------------------------------------------------- #
# timezone policy (P1-5)
# --------------------------------------------------------------------------- #


def test_injected_executed_at_takes_priority_over_the_batch_id(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID, current=_fold_input()), "TC_API_001")

    assert row.latest_execution is not None
    assert row.latest_execution.ts == EXECUTED_AT
    assert row.latest_execution.ts_source == "executed_at"


def test_manifest_executed_at_takes_priority_over_the_batch_id(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, executed_at=EXECUTED_AT.isoformat())
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert row.latest_execution.ts == EXECUTED_AT
    assert row.latest_execution.ts_source == "executed_at"


def test_legacy_batch_id_only_is_parsed_and_stamped_utc(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert row.latest_execution.ts == datetime(2026, 7, 2, 11, 11, 11, tzinfo=UTC)
    assert row.latest_execution.ts_source == "batch_id_legacy_utc"


def test_historical_batch_executed_at_comes_from_its_own_batch_manifest(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    historical_ts = datetime(2026, 7, 1, 18, 10, 10, tzinfo=timezone(timedelta(hours=8)))
    _write_batch_manifest(change_dir, BATCH_OLD, executed_at=historical_ts.isoformat())
    _write_result(change_dir, BATCH_OLD, "api", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert row.latest_execution is not None
    assert row.latest_execution.ts == historical_ts
    assert row.latest_execution.ts_source == "executed_at"


def test_every_execution_timestamp_is_timezone_aware(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_OLD, "api", [("TC_API_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "failed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    for execution in (row.latest_execution, row.freshest_pass):
        assert execution is not None
        assert execution.ts.utcoffset() is not None


def test_nanosecond_batch_id_is_folded_as_execution_evidence(tmp_path: Path) -> None:
    batch_id = "20260702-111111-000000123"
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, batch_id)
    _write_result(change_dir, batch_id, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "batch_id_unparseable" not in _gap_codes(projection)
    latest = _row(projection, "TC_API_001").latest_execution
    assert latest is not None
    assert latest.batch_id == batch_id


def test_execution_fold_input_rejects_naive_executed_at() -> None:
    with pytest.raises(ValueError, match="aware"):
        ExecutionFoldInput(
            batch_id=BATCH_NEW,
            executed_at=datetime(2026, 7, 2, 11, 11, 11),
            selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
            test_files_sha256={},
        )


def test_execution_fold_input_copies_test_files_sha256(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    caller_owned = {"tests/api/test_x.py": "ab" * 32}
    current = _fold_input(test_files=caller_owned)
    before = _source(fold_trace(tmp_path, CHANGE_ID, current=current), MANIFEST_FOLD_VIEW_SOURCE)

    caller_owned["tests/api/test_sneaked_in.py"] = "cd" * 32

    assert current.test_files_sha256 == {"tests/api/test_x.py": "ab" * 32}
    after = _source(fold_trace(tmp_path, CHANGE_ID, current=current), MANIFEST_FOLD_VIEW_SOURCE)
    assert after.sha256 == before.sha256


def test_manifest_with_naive_executed_at_is_rejected_fail_closed(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, executed_at="2026-07-02T11:11:11")
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "manifest_missing" in _gap_codes(projection)
    assert _source(projection, MANIFEST_FOLD_VIEW_SOURCE).exists is False


def test_manifest_for_another_change_is_rejected_fail_closed(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, change_id="CH-OTHER-999")
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    missing = [gap for gap in projection.gaps if gap.code == "manifest_missing"]
    assert len(missing) == 1
    assert "CH-OTHER-999" in missing[0].detail
    assert projection.authoritative_batch_id == ""


def test_unparseable_authoritative_batch_id_produces_a_gap(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, "not-a-batch")

    projection = fold_trace(tmp_path, CHANGE_ID)

    unparseable = [gap for gap in projection.gaps if gap.code == "batch_id_unparseable"]
    assert [gap.source for gap in unparseable] == [MANIFEST_FOLD_VIEW_SOURCE]
    assert "result_missing" not in _gap_codes(projection)


def test_unparseable_batch_directory_name_gaps_and_skips_its_results(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, "not-a-batch", "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    unparseable = [gap for gap in projection.gaps if gap.code == "batch_id_unparseable"]
    assert len(unparseable) == 1
    assert unparseable[0].batch_id == "not-a-batch"
    assert _row(projection, "TC_API_001").latest_execution is None


# --------------------------------------------------------------------------- #
# performance (D7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdict", "status"),
    [("PASS", "passed"), ("FAIL", "failed"), ("SKIPPED", "skipped")],
)
def test_performance_capability_join_maps_each_verdict(tmp_path: Path, verdict: str, status: str) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [
            _case(
                "TC_PERF_001",
                case_type="Performance",
                module="system.perf",
                capability="dept-list-tree-query",
            )
        ],
        rel="cases/system/perf/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "performance": True})
    _write_performance_result(change_dir, BATCH_NEW, [("dept-list-tree-query", verdict)])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_PERF_001")

    assert row.latest_execution is not None
    assert (row.latest_execution.target, row.latest_execution.status) == ("performance", status)
    assert row.presence_in_current_batch == "executed"
    assert ("perf_run" in row.atemporal_kinds_present) is (verdict != "SKIPPED")
    assert (row.freshest_pass is not None) is (verdict == "PASS")


def test_performance_scenario_without_a_matching_capability_is_not_joined(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_PERF_001", case_type="Performance", module="system.perf", capability="wanted")],
        rel="cases/system/perf/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "performance": True})
    _write_performance_result(change_dir, BATCH_NEW, [("something-else", "PASS")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_PERF_001")

    assert row.latest_execution is None
    assert row.atemporal_kinds_present == ()
    assert row.presence_in_current_batch == "not_in_current_batch"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"change_id": "CH-OTHER-999"},
        {"doc_batch_id": BATCH_OLD},
        {"kind": "coverage"},
    ],
    ids=["change_id", "batch_id", "kind"],
)
def test_performance_identity_mismatch_skips_the_document_and_gaps(
    tmp_path: Path, kwargs: dict[str, str]
) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_PERF_001", case_type="Performance", module="system.perf", capability="wanted")],
        rel="cases/system/perf/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "performance": True})
    _write_performance_result(change_dir, BATCH_NEW, [("wanted", "PASS")], **kwargs)

    projection = fold_trace(tmp_path, CHANGE_ID)

    mismatches = [gap for gap in projection.gaps if gap.code == "result_identity_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].source == f"execution/runs/{BATCH_NEW}/performance-result.json"
    assert _row(projection, "TC_PERF_001").latest_execution is None


def test_performance_document_extra_on_disk_fields_are_ignored(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_PERF_001", case_type="Performance", module="system.perf", capability="wanted")],
        rel="cases/system/perf/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "performance": True})
    _write_performance_result(change_dir, BATCH_NEW, [("wanted", "PASS")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "result_corrupt" not in _gap_codes(projection)
    assert _row(projection, "TC_PERF_001").latest_execution is not None


# --------------------------------------------------------------------------- #
# atemporal kinds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected"),
    [("passed", True), ("failed", True), ("skipped", False)],
)
def test_fuzz_run_derives_only_from_a_non_skipped_fuzz_result(
    tmp_path: Path, status: str, expected: bool
) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_FUZZ_001", case_type="Fuzz", module="system.fuzz")],
        rel="cases/system/fuzz/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": False, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_FUZZ_001", status)])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_FUZZ_001")

    assert ("fuzz_run" in row.atemporal_kinds_present) is expected


def test_fuzz_run_is_absent_for_non_fuzz_case_types(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "fuzz": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "fuzz", [("TC_API_001", "passed")])

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_API_001")

    assert "fuzz_run" not in row.atemporal_kinds_present


def test_coverage_state_without_a_tree_scan_hit_is_uncovered_or_not_required(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        [_case("TC_API_001", required=True), _case("TC_API_002", required=False)],
    )
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _row(projection, "TC_API_001").coverage_state == "uncovered"
    assert _row(projection, "TC_API_002").coverage_state == "not_required"
    assert all(row.covering_tests == () for row in projection.rows)
    assert all("covered" not in row.atemporal_kinds_present for row in projection.rows)


# --------------------------------------------------------------------------- #
# unmapped, sources, determinism, integrity
# --------------------------------------------------------------------------- #


def test_unmapped_tests_come_from_the_authoritative_batch(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(
        change_dir,
        BATCH_OLD,
        "api",
        [("TC_API_001", "passed")],
        unmapped=[("tests/api/test_stale.py", "test_stale")],
    )
    _write_result(
        change_dir,
        BATCH_NEW,
        "api",
        [("TC_API_001", "passed")],
        unmapped=[("tests/api/test_x.py", "test_orphan")],
    )

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert [(item.file, item.test_name) for item in projection.unmapped_tests] == [
        ("tests/api/test_x.py", "test_orphan")
    ]


def test_sources_cover_every_input_actually_read(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_batch_manifest(change_dir, BATCH_OLD)
    _write_result(change_dir, BATCH_OLD, "api", [("TC_API_001", "passed")])
    result_path = _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert [source.path for source in projection.sources] == [
        "cases/system/api/case.yaml",
        MANIFEST_FOLD_VIEW_SOURCE,
        f"execution/runs/{BATCH_OLD}/api-result.json",
        f"execution/runs/{BATCH_OLD}/execution-manifest.json",
        f"execution/runs/{BATCH_NEW}/api-result.json",
        TESTS_TREE_SCAN_SOURCE,
    ]
    assert all(source.exists for source in projection.sources)
    current = _source(projection, f"execution/runs/{BATCH_NEW}/api-result.json")
    assert current.sha256 == hashlib.sha256(result_path.read_bytes()).hexdigest()


def test_current_batch_manifest_is_not_read_as_a_separate_source(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_batch_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    paths = [source.path for source in projection.sources]
    assert f"execution/runs/{BATCH_NEW}/execution-manifest.json" not in paths


def test_repeated_folds_are_byte_identical(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir, [_case("TC_API_002"), _case("TC_API_001")])
    _write_cases(
        change_dir,
        [_case("TC_E2E_001", case_type="E2E", module="system.e2e")],
        rel="cases/system/e2e/case.yaml",
    )
    _write_manifest(change_dir, BATCH_NEW, targets={"api": True, "e2e": True})
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])
    _write_result(change_dir, BATCH_NEW, "e2e", [("TC_E2E_001", "failed")])

    first = fold_trace(tmp_path, CHANGE_ID)
    second = fold_trace(tmp_path, CHANGE_ID)

    assert first.model_dump_json() == second.model_dump_json()
    assert [row.case_id for row in first.rows] == ["TC_API_001", "TC_API_002", "TC_E2E_001"]


def test_integrity_is_complete_without_gaps(tmp_path: Path) -> None:
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW, test_files=_write_mapped_test(tmp_path))
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.gaps == ()
    assert projection.integrity == "complete"


def test_a_blocking_gap_is_incomplete_even_when_rows_survive(tmp_path: Path) -> None:
    """Task 4's derivation, replacing Task 3's interim `complete_with_gaps`.

    Only ``mapped_test_missing_from_tree`` may leave integrity non-``incomplete``
    (it describes the rows, not a broken input). A missing manifest means the
    fold could not establish what the current batch even was, so no sufficiency
    policy may soften it. The three-level derivation itself is pinned in
    ``test_tree_scan.py``.
    """
    change_dir = _simple_change(tmp_path)
    _write_mapped_test(tmp_path)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _gap_codes(projection) == ["manifest_missing"]
    assert projection.integrity == "incomplete"


def test_integrity_is_incomplete_without_any_case_document(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "passed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.rows == ()
    assert projection.integrity == "incomplete"


def test_case_unreadable_gaps_are_propagated_from_the_case_loader(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    path = change_dir / "cases" / "system" / "api" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("added:\n- case_id: [unterminated\n", encoding="utf-8")
    _write_manifest(change_dir, BATCH_NEW)

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "case_unreadable" in _gap_codes(projection)
    assert projection.integrity == "incomplete"


def test_unknown_change_is_fail_closed(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    with pytest.raises(ChangeNotFoundError, match="not found"):
        fold_trace(tmp_path, "CH-DOES-NOT-EXIST")


def test_the_execution_phase_leaves_the_reconciled_row_fields_empty(tmp_path: Path) -> None:
    """Reconciled facts are Task 10's; this phase may not invent them.

    The enrichment itself — and the invariant that it changes no field asserted
    in this module — lives in ``test_fold_trace_reconciled.py``.
    """
    change_dir = _simple_change(tmp_path)
    _write_manifest(change_dir, BATCH_NEW)
    _write_result(change_dir, BATCH_NEW, "api", [("TC_API_001", "failed")])

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.phase == "execution"
    assert _row(projection, "TC_API_001").failures == ()
    assert _row(projection, "TC_API_001").open_problem_ids == ()
