"""aa trace — read-only execution-phase evidence projection CLI (Task 5).

Fold semantics themselves are unit-tested exhaustively in
``tests/unit/evidence/test_fold_trace_execution.py``; this suite only covers CLI
wiring — exit codes, JSON/human framing, ``--type``/``--only-gaps`` filters, and
the fail-closed contract (missing change / no valid case rows, in its two
distinct shapes). Per the Task 5 fix-review decision, row *coverage*
(``uncovered``/``not_required``) is never a fail-closed condition on its own —
only the complete absence of rows is, and "all unmapped" means "no rows, but
every executed test landed in ``unmapped_tests``", not "every row is
uncovered".

Click 8.4: ``result.output`` mixes stdout+stderr — JSON assertions use
``result.stdout``, error-text assertions use ``result.stderr``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.commands import status_cmd
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from tests.helpers_aa import write_aa_config

_BATCH_ID = "20260801-000000"


def _write_case_doc(
    change_dir: Path,
    cases: list[dict[str, object]],
    *,
    rel: str = "cases/system/api/case.yaml",
) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": "1.0", "added": cases, "modified": [], "removed": []}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _case(case_id: str, case_type: str, *, required: bool = True, module: str = "system.api") -> dict:
    return {
        "case_id": case_id,
        "module": module,
        "type": case_type,
        "assertions": ["an assertion"],
        "automation": {"required": required},
    }


def _write_test_fn(project_root: Path, rel_path: str, fn_name: str) -> None:
    path = project_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"def {fn_name}() -> None:\n    pass\n", encoding="utf-8")


def _write_manifest(change_dir: Path, change_id: str) -> None:
    document = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": _BATCH_ID,
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "result_files": {"api": f"runs/{_BATCH_ID}/api-result.json"},
        "test_files_sha256": {},
        "final_status": "PASS",
    }
    path = change_dir / "execution" / "execution-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_unmapped_only_result(change_dir: Path, change_id: str) -> None:
    """An api-result.json with zero case rows but one unmapped executed test."""
    batch_dir = change_dir / "execution" / "runs" / _BATCH_ID
    batch_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": _BATCH_ID,
        "target": "api",
        "status": "passed",
        "command": "pytest tests/api",
        "source": {"framework": "pytest", "raw_log": "raw/api.log", "report_json": ""},
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cases": [],
        "unmapped_tests": [
            {
                "file": "tests/api/test_thing.py",
                "test_name": "test_unrelated_helper",
                "duration_ms": 3,
                "message": "",
                "raw_log_ref": "",
            }
        ],
    }
    (batch_dir / "api-result.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """relpath -> (size, mtime_ns), used to assert zero writes."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_missing_change_exits_nonzero() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        result = runner.invoke(main, ["trace", "--change", "NOPE"])
        assert result.exit_code == EXIT_ERROR
        assert "NOPE" in result.stderr


def test_no_cases_exits_nonzero_with_no_valid_case_rows_message() -> None:
    """空 cases、无任何已执行 test（rows=() 且 unmapped_tests=()）→ 稳定 'no valid case rows'。"""
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        Path("qa/changes/CH-TR-1").mkdir(parents=True)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == EXIT_ERROR
        assert "no valid case rows" in result.stderr
        assert "all executed tests are unmapped" not in result.stderr


def test_no_valid_rows_but_unmapped_tests_exist_exits_nonzero_with_distinct_message() -> None:
    """rows=() 但 unmapped_tests 非空（有执行但零映射）→ 稳定 'all executed tests are unmapped'。

    这与「全部 row uncovered」是两件不同的事：这里连一个 case 声明都没有，唯一的证据是
    执行过的、未映射到任何 case 的测试。
    """
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        # no cases/ at all
        _write_manifest(change_dir, "CH-TR-1")
        _write_unmapped_only_result(change_dir, "CH-TR-1")

        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == EXIT_ERROR
        assert "all executed tests are unmapped" in result.stderr
        assert "no valid case rows" in result.stderr  # shared stable prefix, distinct suffix


def test_all_rows_uncovered_exits_zero() -> None:
    """cases 存在、required=true，但树里没有映射测试 → coverage_state=uncovered，仍是正常结果，退出 0。

    这是本轮修复的核心语义：row 覆盖状态从不是 fail-closed 条件；只有『零 row』才是。
    """
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        # no tests/ tree at all: nothing maps this required case, but the row itself is valid.

        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert [row["case_id"] for row in doc["rows"]] == ["TC_API_001"]
        assert doc["rows"][0]["coverage_state"] == "uncovered"


def test_all_rows_not_required_exits_zero() -> None:
    """cases 存在但 required=false、无映射测试 → coverage_state=not_required，退出 0。"""
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=False)])

        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert [row["case_id"] for row in doc["rows"]] == ["TC_API_001"]
        assert doc["rows"][0]["coverage_state"] == "not_required"


def test_covered_case_exits_zero_and_reports_row() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")

        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc["change_id"] == "CH-TR-1"
        assert doc["phase"] == "execution"
        row_ids = [row["case_id"] for row in doc["rows"]]
        assert row_ids == ["TC_API_001"]
        assert doc["rows"][0]["coverage_state"] == "covered"


def test_json_output_is_deterministic_across_repeated_runs() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")

        first = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        second = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert first.exit_code == 0
        assert first.stdout == second.stdout


def test_type_filter_narrows_rows_but_keeps_gaps() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(
            change_dir,
            [
                _case("TC_API_001", "API", required=True),
                _case("TC_E2E_001", "E2E", required=True, module="system.e2e"),
            ],
            rel="cases/system/mixed/case.yaml",
        )
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")
        _write_test_fn(root, "tests/e2e/test_thing.py", "test_tc_e2e_001__basic")

        unfiltered = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        filtered = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json", "--type", "API"])
        assert unfiltered.exit_code == 0
        assert filtered.exit_code == 0
        unfiltered_doc = json.loads(unfiltered.stdout)
        filtered_doc = json.loads(filtered.stdout)
        assert {row["case_id"] for row in unfiltered_doc["rows"]} == {"TC_API_001", "TC_E2E_001"}
        assert [row["case_id"] for row in filtered_doc["rows"]] == ["TC_API_001"]
        # gaps/integrity are projection-wide facts; a display filter must not hide them.
        assert filtered_doc["gaps"] == unfiltered_doc["gaps"]
        assert filtered_doc["integrity"] == unfiltered_doc["integrity"]


def test_only_gaps_json_outputs_gap_list_directly() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")

        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json", "--only-gaps"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert isinstance(doc, list)
        # no execution/execution-manifest.yaml was written -> manifest_missing gap.
        assert any(gap["code"] == "manifest_missing" for gap in doc)


def test_type_is_ignored_when_combined_with_only_gaps() -> None:
    """--only-gaps 输出没有 row 可过滤；--type 静默无效，两次调用字节相同。"""
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(
            change_dir,
            [
                _case("TC_API_001", "API", required=True),
                _case("TC_E2E_001", "E2E", required=True, module="system.e2e"),
            ],
            rel="cases/system/mixed/case.yaml",
        )
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")
        _write_test_fn(root, "tests/e2e/test_thing.py", "test_tc_e2e_001__basic")

        no_type = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json", "--only-gaps"])
        with_type = runner.invoke(
            main, ["trace", "--change", "CH-TR-1", "--json", "--only-gaps", "--type", "API"]
        )
        assert no_type.exit_code == 0
        assert with_type.exit_code == 0
        assert no_type.stdout == with_type.stdout


def test_help_documents_type_ignored_with_only_gaps() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["trace", "--help"])
    assert result.exit_code == 0
    assert "ignored" in result.output.lower()
    assert "--only-gaps" in result.output


def test_no_rows_json_still_emits_full_projection_before_failing() -> None:
    """无 valid rows 也必须先渲染完整 projection（rows=[]），再落 stderr 稳定错误。"""
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        Path("qa/changes/CH-TR-1").mkdir(parents=True)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == EXIT_ERROR
        doc = json.loads(result.stdout)
        assert doc["change_id"] == "CH-TR-1"
        assert doc["phase"] == "execution"
        assert doc["rows"] == []
        assert result.stderr.strip() != ""


def test_no_rows_only_gaps_json_still_emits_gap_array_before_failing() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        Path("qa/changes/CH-TR-1").mkdir(parents=True)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json", "--only-gaps"])
        assert result.exit_code == EXIT_ERROR
        doc = json.loads(result.stdout)
        assert isinstance(doc, list)
        # no manifest written -> manifest_missing gap is present even though rows is empty.
        assert any(gap["code"] == "manifest_missing" for gap in doc)
        assert result.stderr.strip() != ""


def test_no_rows_human_output_still_shows_gaps_before_failing() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        Path("qa/changes/CH-TR-1").mkdir(parents=True)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1"])
        assert result.exit_code == EXIT_ERROR
        assert "manifest_missing" in result.stdout
        assert "(no rows)" in result.stdout
        assert result.stderr.strip() != ""


def test_human_output_lists_case_and_is_read_only() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")

        before = _tree_snapshot(root)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1"])
        after = _tree_snapshot(root)

        assert result.exit_code == 0, result.output
        assert "TC_API_001" in result.stdout
        assert after == before


def test_json_output_matches_fold_trace() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")

        expected = fold_trace(root, "CH-TR-1", phase="execution", current=None)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == json.loads(expected.model_dump_json())


def test_does_not_use_status_read_path(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as cwd:
        root = Path(cwd)
        write_aa_config(root)
        change_dir = root / "qa/changes/CH-TR-1"
        change_dir.mkdir(parents=True)
        _write_case_doc(change_dir, [_case("TC_API_001", "API", required=True)])
        _write_test_fn(root, "tests/api/test_thing.py", "test_tc_api_001__basic")

        def _boom(*_args, **_kwargs):
            raise AssertionError("aa trace must not call read_latest_graph_status")

        monkeypatch.setattr(status_cmd, "read_latest_graph_status", _boom)
        result = runner.invoke(main, ["trace", "--change", "CH-TR-1", "--json"])
        assert result.exit_code == 0, result.output


def test_help_lists_trace_command() -> None:
    result = CliRunner().invoke(main, ["trace", "--help"])
    assert result.exit_code == 0
    assert "--change" in result.output
    assert "--only-gaps" in result.output
