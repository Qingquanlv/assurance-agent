"""Task 4: the current tests-tree scan and the fold facts derived from it.

Two layers are pinned here:

- ``scan_test_tree`` itself — line-based ``def`` / ``async def`` extraction (so a
  case id named in a docstring or comment cannot fabricate a mapping), the
  performance capability literal hit, the plain per-file sha256 and the
  evidence-owned tree digest.
- what ``fold_trace`` does with that scan — coverage and ``covering_tests``, the
  ``mapped_test_missing_from_tree`` gap when an executed mapping disappears, the
  per-file manifest-vs-current drift check (identical in both fold modes), and
  the exact three-level ``integrity`` derivation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.execution import SelectedTargets
from assurance_agent.artifacts.models.trace import TraceProjectionLike as TraceProjection
from assurance_agent.artifacts.models.trace import TraceRow, TraceSource
from assurance_agent.evidence.trace import ExecutionFoldInput, fold_trace
from assurance_agent.evidence.tree_scan import TreeScanResult, scan_test_tree
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-TRACE-004"
BATCH_ID = "20260702-111111"
EXECUTED_AT = datetime(2026, 7, 2, 19, 30, 0, tzinfo=timezone(timedelta(hours=8)))
MANIFEST_FOLD_VIEW_SOURCE = "execution/execution-manifest.yaml#fold-view"
TESTS_TREE_SCAN_SOURCE = "tests/#tree-digest"

MAPPED_TEST_FILE = "tests/api/test_x.py"
MAPPED_TEST_NAME = "test_tc_api_001__scenario"


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def _write_file(project_root: Path, rel: str, text: str) -> Path:
    path = project_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mapped_test_source(test_name: str = MAPPED_TEST_NAME) -> str:
    return f"def {test_name}() -> None:\n    assert True\n"


def _change_dir(project_root: Path) -> Path:
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    return change_dir


def _write_cases(
    change_dir: Path,
    *,
    case_id: str = "TC_API_001",
    case_type: str = "API",
    module: str = "system.api",
    capability: str | None = None,
    required: bool = True,
) -> None:
    automation: dict[str, object] = {"required": required}
    if capability is not None:
        automation["performance"] = {"scenario": {"capability": capability}}
    document = {
        "schema_version": "1.0",
        "added": [
            {
                "case_id": case_id,
                "module": module,
                "type": case_type,
                "assertions": ["an assertion"],
                "automation": automation,
            }
        ],
        "modified": [],
        "removed": [],
    }
    path = change_dir / "cases" / "system" / "api" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_api_result(
    change_dir: Path,
    *,
    case_id: str = "TC_API_001",
    file: str = MAPPED_TEST_FILE,
    test_name: str = MAPPED_TEST_NAME,
    batch_id: str = BATCH_ID,
) -> None:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "target": "api",
        "cases": [{"case_id": case_id, "status": "passed", "file": file, "test_name": test_name}],
        "unmapped_tests": [],
    }
    (batch_dir / "api-result.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def _write_manifest(
    change_dir: Path,
    *,
    test_files: dict[str, str] | None,
    batch_id: str = BATCH_ID,
) -> None:
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "executed_at": EXECUTED_AT.isoformat(),
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "result_files": {"api": f"runs/{batch_id}/api-result.json"},
        "test_files_sha256": test_files,
        "final_status": "PASS",
    }
    path = change_dir / "execution" / "execution-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _fold_input(test_files: dict[str, str], *, batch_id: str = BATCH_ID) -> ExecutionFoldInput:
    return ExecutionFoldInput(
        batch_id=batch_id,
        executed_at=EXECUTED_AT,
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        test_files_sha256=test_files,
    )


def _gap_codes(projection: TraceProjection) -> list[str]:
    return [gap.code for gap in projection.gaps]


def _row(projection: TraceProjection, case_id: str = "TC_API_001") -> TraceRow:
    rows = [row for row in projection.rows if row.case_id == case_id]
    assert rows, f"no row for {case_id}"
    return rows[0]


def _source(projection: TraceProjection, path: str) -> TraceSource:
    matches = [source for source in projection.sources if source.path == path]
    assert matches, f"no source entry for {path}"
    return matches[0]


def _covered_change(project_root: Path) -> Path:
    """A change whose one case is covered by a real test file, with no drift."""
    change_dir = _change_dir(project_root)
    _write_cases(change_dir)
    _write_file(project_root, MAPPED_TEST_FILE, _mapped_test_source())
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(project_root).file_sha256))
    return change_dir


# --------------------------------------------------------------------------- #
# scan: function-name mapping
# --------------------------------------------------------------------------- #


def test_a_test_function_name_maps_its_case_id(tmp_path: Path) -> None:
    _write_file(tmp_path, "tests/api/test_dept.py", "def test_tc_dept_001__list() -> None:\n    pass\n")

    result = scan_test_tree(tmp_path)

    assert result.case_ids == frozenset({"TC_DEPT_001"})
    assert [(ref.file, ref.test_name) for ref in result.covering_tests["TC_DEPT_001"]] == [
        ("tests/api/test_dept.py", "test_tc_dept_001__list")
    ]


def test_async_test_functions_are_scanned_too(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "tests/e2e/test_menu.py",
        "class TestMenu:\n    async def test_tc_menu_002__open(self) -> None:\n        pass\n",
    )

    result = scan_test_tree(tmp_path)

    assert result.case_ids == frozenset({"TC_MENU_002"})
    assert result.covering_tests["TC_MENU_002"][0].test_name == "test_tc_menu_002__open"


def test_case_ids_outside_a_def_line_never_map(tmp_path: Path) -> None:
    """Docstring case ranges and commented-out tests are the classic false mappings."""
    _write_file(
        tmp_path,
        "tests/api/test_role.py",
        '"""Role suite.\n\nCases: TC_ROLE_001 - TC_ROLE_003\n"""\n\n'
        "# def test_tc_role_004__disabled() -> None:\n"
        "#     pass\n\n"
        "def test_tc_role_005__real() -> None:\n"
        '    """Covers TC_ROLE_006."""\n'
        "    pass\n",
    )

    result = scan_test_tree(tmp_path)

    assert result.case_ids == frozenset({"TC_ROLE_005"})


def test_non_test_functions_are_not_scanned(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "tests/api/conftest.py",
        "def helper_tc_api_007__seed() -> None:\n    pass\n\ndef check_tc_api_008() -> None:\n    pass\n",
    )

    result = scan_test_tree(tmp_path)

    assert result.case_ids == frozenset()
    assert result.functions == ()


def test_every_test_function_is_reported_sorted_by_file_and_name(tmp_path: Path) -> None:
    """Discovery order is a filesystem accident; the reported order is explicit."""
    _write_file(
        tmp_path,
        "tests/api/test_misc.py",
        "def test_tc_api_009__mapped() -> None:\n    pass\n\ndef test_orphan() -> None:\n    pass\n",
    )
    _write_file(tmp_path, "tests/api/test_aaa.py", "def test_zzz() -> None:\n    pass\n")

    result = scan_test_tree(tmp_path)

    assert [(ref.file, ref.test_name) for ref in result.functions] == [
        ("tests/api/test_aaa.py", "test_zzz"),
        ("tests/api/test_misc.py", "test_orphan"),
        ("tests/api/test_misc.py", "test_tc_api_009__mapped"),
    ]
    assert set(result.covering_tests) == {"TC_API_009"}


def test_the_same_case_id_in_several_files_records_every_covering_test(tmp_path: Path) -> None:
    """Covering tests are sorted by `(file, test_name)`, not by where they were found."""
    _write_file(tmp_path, "tests/api/test_b.py", "def test_tc_api_010__two() -> None:\n    pass\n")
    _write_file(
        tmp_path,
        "tests/api/test_a.py",
        "def test_tc_api_010__zzz() -> None:\n    pass\n\ndef test_tc_api_010__aaa() -> None:\n    pass\n",
    )

    result = scan_test_tree(tmp_path)

    assert [(ref.file, ref.test_name) for ref in result.covering_tests["TC_API_010"]] == [
        ("tests/api/test_a.py", "test_tc_api_010__aaa"),
        ("tests/api/test_a.py", "test_tc_api_010__zzz"),
        ("tests/api/test_b.py", "test_tc_api_010__two"),
    ]


# --------------------------------------------------------------------------- #
# scan: performance capability literals
# --------------------------------------------------------------------------- #


def test_performance_capability_is_a_literal_name_hit_in_a_locustfile(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "tests/perf/locustfile_dept.py",
        "class DeptUser(HttpUser):\n"
        "    @task\n"
        "    def tc_dept_perf_001__list(self) -> None:\n"
        '        self.client.get("/api/v1/dept/list", name="dept-list-tree-query")\n',
    )

    result = scan_test_tree(tmp_path)

    assert result.perf_capabilities == frozenset({"dept-list-tree-query"})


def test_performance_capability_resolves_a_module_string_constant(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "tests/perf/locustfile_user.py",
        'CAPABILITY = "user-list-query"\n\n'
        "class User(HttpUser):\n"
        "    @task\n"
        "    def list_users(self) -> None:\n"
        '        self.client.get("/api/v1/user/list", name=CAPABILITY)\n',
    )

    result = scan_test_tree(tmp_path)

    assert result.perf_capabilities == frozenset({"user-list-query"})


def test_a_capability_named_only_in_a_comment_is_not_a_hit(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "tests/perf/locustfile_dept.py",
        '# TODO: add name="dept-list-tree-query" once the endpoint lands\n'
        '        self.client.get("/api/v1/dept/list", name="dept-detail-query")  # name="stale"\n',
    )

    result = scan_test_tree(tmp_path)

    assert result.perf_capabilities == frozenset({"dept-detail-query"})


@pytest.mark.parametrize("keyword", ["username", "user_name", "file_name", "codename"])
def test_a_keyword_merely_ending_in_name_is_not_a_capability(tmp_path: Path, keyword: str) -> None:
    """``name=`` is a whole keyword argument, not a suffix of a longer one."""
    _write_file(
        tmp_path,
        "tests/perf/locustfile_dept.py",
        f'    self.client.post("/api/v1/user/create", json={{"x": 1}}, {keyword}="admin")\n',
    )

    result = scan_test_tree(tmp_path)

    assert result.perf_capabilities == frozenset()


def test_a_hash_inside_a_url_does_not_hide_the_capability(tmp_path: Path) -> None:
    """Comment stripping is quote-aware, so a fragment in a path is not a comment."""
    _write_file(
        tmp_path,
        "tests/perf/locustfile_dept.py",
        '    self.client.get("/api/v1/dept/list#tree", name="dept-list-tree-query")\n',
    )

    result = scan_test_tree(tmp_path)

    assert result.perf_capabilities == frozenset({"dept-list-tree-query"})


def test_capability_literals_outside_tests_perf_locustfiles_are_ignored(tmp_path: Path) -> None:
    _write_file(tmp_path, "tests/api/test_x.py", 'client.get("/x", name="not-a-capability")\n')
    _write_file(tmp_path, "tests/perf/helpers.py", 'NAME = dict(name="also-not-a-capability")\n')

    result = scan_test_tree(tmp_path)

    assert result.perf_capabilities == frozenset()


# --------------------------------------------------------------------------- #
# scan: per-file sha256 and the evidence-owned tree digest
# --------------------------------------------------------------------------- #


def test_file_sha256_is_the_plain_sha256_of_every_tests_file(tmp_path: Path) -> None:
    source = "def test_tc_api_011__x() -> None:\n    pass\n"
    fixture = '{"seed": 1}\n'
    _write_file(tmp_path, "tests/api/test_x.py", source)
    _write_file(tmp_path, "tests/data/seed.json", fixture)
    _write_file(tmp_path, "app/main.py", "print('not a test')\n")

    result = scan_test_tree(tmp_path)

    assert dict(result.file_sha256) == {
        "tests/api/test_x.py": _sha256(source),
        "tests/data/seed.json": _sha256(fixture),
    }


def test_pycache_artifacts_are_neither_hashed_nor_scanned(tmp_path: Path) -> None:
    _write_file(tmp_path, "tests/api/test_x.py", "def test_tc_api_012__x() -> None:\n    pass\n")
    _write_file(tmp_path, "tests/api/__pycache__/test_x.cpython-311.pyc", "stale bytes\n")
    _write_file(tmp_path, "tests/api/test_x.pyc", "stale bytes\n")

    result = scan_test_tree(tmp_path)

    assert list(result.file_sha256) == ["tests/api/test_x.py"]


def test_tree_digest_is_the_canonical_json_sha256_of_the_sorted_file_map(tmp_path: Path) -> None:
    first = "def test_tc_api_013__a() -> None:\n    pass\n"
    second = "def test_tc_api_014__b() -> None:\n    pass\n"
    _write_file(tmp_path, "tests/api/test_b.py", second)
    _write_file(tmp_path, "tests/api/test_a.py", first)

    result = scan_test_tree(tmp_path)

    expected = hashlib.sha256(
        canonical_json_bytes({"tests/api/test_a.py": _sha256(first), "tests/api/test_b.py": _sha256(second)})
    ).hexdigest()
    assert result.tree_digest == expected


def test_tree_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    path = _write_file(tmp_path, "tests/api/test_x.py", "def test_tc_api_015__x() -> None:\n    pass\n")
    before = scan_test_tree(tmp_path)

    assert scan_test_tree(tmp_path).tree_digest == before.tree_digest

    path.write_text("def test_tc_api_015__x() -> None:\n    assert True\n", encoding="utf-8")
    assert scan_test_tree(tmp_path).tree_digest != before.tree_digest


def test_a_missing_tests_directory_scans_to_an_empty_tree(tmp_path: Path) -> None:
    result = scan_test_tree(tmp_path)

    assert result == TreeScanResult(
        case_ids=frozenset(),
        perf_capabilities=frozenset(),
        functions=(),
        covering_tests={},
        file_sha256={},
        tree_digest=hashlib.sha256(canonical_json_bytes({})).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# fold: coverage from the current tree
# --------------------------------------------------------------------------- #


def test_a_current_tree_hit_makes_the_case_covered(tmp_path: Path) -> None:
    _covered_change(tmp_path)

    row = _row(fold_trace(tmp_path, CHANGE_ID))

    assert row.coverage_state == "covered"
    assert [(ref.file, ref.test_name) for ref in row.covering_tests] == [(MAPPED_TEST_FILE, MAPPED_TEST_NAME)]
    assert "covered" in row.atemporal_kinds_present


def test_a_performance_case_is_covered_by_a_capability_literal(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(
        change_dir,
        case_id="TC_PERF_001",
        case_type="Performance",
        module="system.perf",
        capability="dept-list-tree-query",
    )
    _write_file(
        tmp_path,
        "tests/perf/locustfile_dept.py",
        '    self.client.get("/api/v1/dept/list", name="dept-list-tree-query")\n',
    )
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    row = _row(fold_trace(tmp_path, CHANGE_ID), "TC_PERF_001")

    assert row.coverage_state == "covered"
    assert row.covering_tests == ()


# --------------------------------------------------------------------------- #
# fold: mapped test disappearance
# --------------------------------------------------------------------------- #


def test_a_mapped_test_missing_from_the_tree_is_uncovered_and_gaps(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    # The file still exists, but its function no longer carries the case id.
    renamed = "def test_renamed__scenario() -> None:\n    assert True\n"
    _write_file(tmp_path, MAPPED_TEST_FILE, renamed)
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _gap_codes(projection) == ["mapped_test_missing_from_tree"]
    gap = projection.gaps[0]
    assert gap.source == TESTS_TREE_SCAN_SOURCE
    assert gap.batch_id == BATCH_ID
    assert f"{MAPPED_TEST_FILE}::{MAPPED_TEST_NAME}" in gap.detail
    assert "TC_API_001" in gap.detail
    row = _row(projection)
    assert row.coverage_state == "uncovered"
    assert row.covering_tests == ()


def test_renaming_a_test_that_keeps_the_case_id_keeps_the_mapping(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, _mapped_test_source("test_tc_api_001__renamed"))
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.gaps == ()
    assert _row(projection).coverage_state == "covered"


def test_the_missing_mapping_is_the_newest_batchs(tmp_path: Path) -> None:
    """An older batch's mapping is history; the newest one is what is claimed now."""
    older = "20260701-101010"
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, "def test_renamed__scenario() -> None:\n    pass\n")
    _write_api_result(
        change_dir, file="tests/api/test_old.py", test_name="test_tc_api_001__old", batch_id=older
    )
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    missing = [gap for gap in projection.gaps if gap.code == "mapped_test_missing_from_tree"]
    assert len(missing) == 1
    assert missing[0].batch_id == BATCH_ID
    assert missing[0].detail.count("::") == 1
    assert f"{MAPPED_TEST_FILE}::{MAPPED_TEST_NAME}" in missing[0].detail


def test_a_test_that_still_exists_but_no_longer_maps_is_not_a_disappearance(tmp_path: Path) -> None:
    """The runner resolves a case id from the whole nodeid, the tree scan only from
    the function name (spec §7.1). A test whose case id lives in its *path* is
    therefore uncovered — but it did not disappear, so it is not this gap.
    """
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    path_mapped = "tests/api/test_tc_api_001.py"
    _write_file(tmp_path, path_mapped, "def test_scenario() -> None:\n    pass\n")
    _write_api_result(change_dir, file=path_mapped, test_name="test_scenario")
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.gaps == ()
    assert _row(projection).coverage_state == "uncovered"


def test_a_case_that_does_not_require_automation_never_reports_a_missing_mapping(
    tmp_path: Path,
) -> None:
    """``not_required`` is a deliberate choice, not a lost mapping.

    The case ran once and no longer maps a test, but nobody asked it to be
    automated — reporting a gap would let an opt-out degrade integrity.
    """
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir, required=False)
    _write_file(tmp_path, MAPPED_TEST_FILE, "def test_renamed__scenario() -> None:\n    pass\n")
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _row(projection).coverage_state == "not_required"
    assert projection.gaps == ()
    assert projection.integrity == "complete"


def test_a_case_never_executed_produces_no_missing_mapping_gap(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_manifest(change_dir, test_files={})

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert "mapped_test_missing_from_tree" not in _gap_codes(projection)
    assert _row(projection).coverage_state == "uncovered"


# --------------------------------------------------------------------------- #
# fold: per-file manifest-vs-current drift
# --------------------------------------------------------------------------- #


def _drift_change(tmp_path: Path, kind: str) -> tuple[Path, dict[str, str]]:
    """Build a covered change whose manifest baseline drifts from the tree by `kind`."""
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, _mapped_test_source())
    _write_api_result(change_dir)
    baseline = dict(scan_test_tree(tmp_path).file_sha256)
    if kind == "added":
        _write_file(tmp_path, "tests/api/test_new.py", "def test_tc_api_002__new() -> None:\n    pass\n")
    elif kind == "deleted":
        baseline["tests/api/test_gone.py"] = "ab" * 32
    else:
        baseline[MAPPED_TEST_FILE] = "cd" * 32
    return change_dir, baseline


@pytest.mark.parametrize(
    ("kind", "expected_detail"),
    [
        ("added", "added: tests/api/test_new.py"),
        ("deleted", "deleted: tests/api/test_gone.py"),
        ("changed", f"changed: {MAPPED_TEST_FILE}"),
    ],
)
def test_each_kind_of_per_file_drift_produces_a_tree_digest_mismatch(
    tmp_path: Path, kind: str, expected_detail: str
) -> None:
    change_dir, baseline = _drift_change(tmp_path, kind)
    _write_manifest(change_dir, test_files=baseline)

    projection = fold_trace(tmp_path, CHANGE_ID)

    mismatches = [gap for gap in projection.gaps if gap.code == "tests_tree_digest_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].source == TESTS_TREE_SCAN_SOURCE
    assert mismatches[0].batch_id == BATCH_ID
    assert expected_detail in mismatches[0].detail


def test_all_three_drift_kinds_fold_into_one_mismatch_with_a_fixed_section_order(
    tmp_path: Path,
) -> None:
    """One gap per fold, whose detail is byte-stable: added, then deleted, then changed."""
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, _mapped_test_source())
    _write_file(tmp_path, "tests/api/test_edited.py", "def test_tc_api_003__old() -> None:\n    pass\n")
    _write_api_result(change_dir)
    baseline = dict(scan_test_tree(tmp_path).file_sha256)
    baseline["tests/api/test_edited.py"] = "cd" * 32
    baseline["tests/api/test_gone.py"] = "ab" * 32
    _write_file(tmp_path, "tests/api/test_new.py", "def test_tc_api_002__new() -> None:\n    pass\n")
    _write_manifest(change_dir, test_files=baseline)

    projection = fold_trace(tmp_path, CHANGE_ID)

    mismatches = [gap for gap in projection.gaps if gap.code == "tests_tree_digest_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].detail == (
        "added: tests/api/test_new.py; deleted: tests/api/test_gone.py; changed: tests/api/test_edited.py"
    )


@pytest.mark.parametrize("kind", ["added", "deleted", "changed"])
def test_the_injected_mode_runs_the_same_per_file_comparison(tmp_path: Path, kind: str) -> None:
    change_dir, baseline = _drift_change(tmp_path, kind)
    _write_manifest(change_dir, test_files=baseline)

    on_disk = fold_trace(tmp_path, CHANGE_ID)
    injected = fold_trace(tmp_path, CHANGE_ID, current=_fold_input(baseline))

    assert on_disk.model_dump_json() == injected.model_dump_json()
    assert "tests_tree_digest_mismatch" in _gap_codes(injected)


def test_an_identical_tree_produces_no_mismatch_in_either_mode(tmp_path: Path) -> None:
    _covered_change(tmp_path)
    baseline = dict(scan_test_tree(tmp_path).file_sha256)

    on_disk = fold_trace(tmp_path, CHANGE_ID)
    injected = fold_trace(tmp_path, CHANGE_ID, current=_fold_input(baseline))

    assert on_disk.gaps == ()
    assert injected.gaps == ()


def test_platform_metadata_is_not_part_of_the_evidence_owned_test_tree(tmp_path: Path) -> None:
    _covered_change(tmp_path)
    baseline = dict(scan_test_tree(tmp_path).file_sha256)
    _write_file(tmp_path, "tests/.DS_Store", "macOS metadata")

    projection = fold_trace(tmp_path, CHANGE_ID, current=_fold_input(baseline))

    assert "tests/.DS_Store" not in scan_test_tree(tmp_path).file_sha256
    assert "tests_tree_digest_mismatch" not in _gap_codes(projection)


def test_nightly_metrics_directory_is_not_parsed_as_an_execution_batch(tmp_path: Path) -> None:
    change_dir = _covered_change(tmp_path)
    nightly = change_dir / "execution" / "runs" / "nightly"
    nightly.mkdir(parents=True)
    (nightly / "metrics.json").write_text("{}\n", encoding="utf-8")

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert not any(
        gap.code == "batch_id_unparseable" and gap.batch_id == "nightly" for gap in projection.gaps
    )


def test_a_manifest_without_per_file_hashes_claims_an_empty_tree(tmp_path: Path) -> None:
    """A published manifest that omits ``test_files_sha256`` cannot vouch for the tree."""
    change_dir = _covered_change(tmp_path)
    _write_manifest(change_dir, test_files=None)

    projection = fold_trace(tmp_path, CHANGE_ID)

    mismatches = [gap for gap in projection.gaps if gap.code == "tests_tree_digest_mismatch"]
    assert len(mismatches) == 1
    assert f"added: {MAPPED_TEST_FILE}" in mismatches[0].detail


def test_manifest_missing_only_when_neither_view_source_exists(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, _mapped_test_source())
    _write_api_result(change_dir)

    without_any_view = fold_trace(tmp_path, CHANGE_ID)
    injected = fold_trace(tmp_path, CHANGE_ID, current=_fold_input({}))

    assert _gap_codes(without_any_view) == ["manifest_missing"]
    assert "tests_tree_digest_mismatch" not in _gap_codes(without_any_view)
    assert _gap_codes(injected) == ["tests_tree_digest_mismatch"]


# --------------------------------------------------------------------------- #
# fold: the tests tree as a source
# --------------------------------------------------------------------------- #


def test_sources_record_the_evidence_owned_tree_digest(tmp_path: Path) -> None:
    _covered_change(tmp_path)

    projection = fold_trace(tmp_path, CHANGE_ID)

    source = _source(projection, TESTS_TREE_SCAN_SOURCE)
    assert source.exists is True
    assert source.sha256 == scan_test_tree(tmp_path).tree_digest
    assert source.sha256 != _source(projection, MANIFEST_FOLD_VIEW_SOURCE).sha256


# --------------------------------------------------------------------------- #
# fold: three-level integrity
# --------------------------------------------------------------------------- #


def test_integrity_is_complete_when_the_tree_backs_every_fact(tmp_path: Path) -> None:
    _covered_change(tmp_path)

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.gaps == ()
    assert projection.integrity == "complete"


def test_only_a_missing_mapped_test_leaves_integrity_degraded(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, "def test_renamed__scenario() -> None:\n    pass\n")
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _gap_codes(projection) == ["mapped_test_missing_from_tree"]
    assert projection.integrity == "degraded"


@pytest.mark.parametrize("blocking", ["tests_tree_digest_mismatch", "manifest_missing"])
def test_any_blocking_gap_makes_integrity_incomplete(tmp_path: Path, blocking: str) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, _mapped_test_source())
    _write_api_result(change_dir)
    if blocking == "tests_tree_digest_mismatch":
        _write_manifest(change_dir, test_files={"tests/api/test_gone.py": "ab" * 32})

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert blocking in _gap_codes(projection)
    assert projection.integrity == "incomplete"


def test_a_blocking_gap_is_not_softened_by_a_degraded_one(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_cases(change_dir)
    _write_file(tmp_path, MAPPED_TEST_FILE, "def test_renamed__scenario() -> None:\n    pass\n")
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files={"tests/api/test_gone.py": "ab" * 32})

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert set(_gap_codes(projection)) == {"mapped_test_missing_from_tree", "tests_tree_digest_mismatch"}
    assert projection.integrity == "incomplete"


def test_zero_case_rows_is_incomplete_even_with_a_clean_tree(tmp_path: Path) -> None:
    change_dir = _change_dir(tmp_path)
    _write_file(tmp_path, MAPPED_TEST_FILE, _mapped_test_source())
    _write_api_result(change_dir)
    _write_manifest(change_dir, test_files=dict(scan_test_tree(tmp_path).file_sha256))

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert projection.rows == ()
    assert projection.gaps == ()
    assert projection.integrity == "incomplete"
