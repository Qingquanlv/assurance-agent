from __future__ import annotations

import shlex
import stat
import subprocess
from pathlib import Path


_ROOT = Path(__file__).parents[3]
_HELPERS = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "cursor-loop-helpers.sh"


def _run_helper(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(_HELPERS))}; {command}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_retro_shell_prefers_failed_change_from_current_rows_over_stale_directory(tmp_path: Path) -> None:
    changes = tmp_path / "qa" / "changes"
    (changes / "RET-current").mkdir(parents=True)
    (changes / "RET-stale-z").mkdir()

    result = _run_helper(
        tmp_path,
        'select_retro_shell_change_id "$PWD/qa/changes" '
        "'RET-current|failed|assurance failed|archived=no'",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "RET-current"


def test_remove_generated_tree_handles_read_only_graph_runtime_directories(tmp_path: Path) -> None:
    target = tmp_path / "qa" / "changes"
    nested = target / "CH-1" / ".graph-runtime" / "views" / "view-1"
    nested.mkdir(parents=True)
    (nested / "review").mkdir()
    nested.chmod(stat.S_IRUSR | stat.S_IXUSR)

    result = _run_helper(tmp_path, 'remove_generated_artifact_tree "$PWD/qa/changes"')

    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_benchmark_gate_fails_when_any_workflow_row_failed(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code true true 0 true "
        "'RET-current|failed|assurance failed|archived=no'",
    )

    assert result.returncode == 1


def test_benchmark_gate_passes_completed_archived_workflow_and_successful_retro(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code true true 0 true "
        "'RET-current|completed|final_status=PASS|archived=yes'",
    )

    assert result.returncode == 0, result.stderr


def test_benchmark_gate_fails_when_required_archive_was_skipped(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code true true 0 true "
        "'RET-current|completed|final_status=PASS|archived=no'",
    )

    assert result.returncode == 1


def test_benchmark_gate_fails_when_retro_errors(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code true true 20 false "
        "'RET-current|completed|final_status=PASS|archived=yes'",
    )

    assert result.returncode == 1


def test_retro_gate_rejects_no_shell_exit_even_if_an_artifact_was_found(tmp_path: Path) -> None:
    result = _run_helper(tmp_path, "retro_result_is_closed 10 true")

    assert result.returncode == 1


def test_retro_gate_rejects_exit_zero_without_current_run_artifacts(tmp_path: Path) -> None:
    result = _run_helper(tmp_path, "retro_result_is_closed 0 false")

    assert result.returncode == 1


def test_retro_gate_accepts_exit_zero_with_current_run_artifacts(tmp_path: Path) -> None:
    result = _run_helper(tmp_path, "retro_result_is_closed 0 true")

    assert result.returncode == 0, result.stderr


def test_retro_artifact_contract_rejects_a_fresh_but_empty_directory(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-current"
    retro_dir.mkdir(parents=True)

    result = _run_helper(tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false')

    assert result.returncode == 1


def test_retro_artifact_contract_accepts_zero_signal_context_only(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-current"
    retro_dir.mkdir(parents=True)
    (retro_dir / "context.json").write_text('{"signal_count": 0}\n', encoding="utf-8")

    result = _run_helper(tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false')

    assert result.returncode == 0, result.stderr


def test_retro_artifact_contract_requires_full_positive_signal_receipt(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-current"
    retro_dir.mkdir(parents=True)
    (retro_dir / "context.json").write_text('{"signal_count": 2}\n', encoding="utf-8")
    (retro_dir / "proposal-candidates.json").write_text("{}\n", encoding="utf-8")
    (retro_dir / "retro-summary.md").write_text("# Summary\n", encoding="utf-8")

    missing_receipt = _run_helper(
        tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false'
    )
    (retro_dir / "accept-status.json").write_text("{}\n", encoding="utf-8")
    complete = _run_helper(
        tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false'
    )

    assert missing_receipt.returncode == 1
    assert complete.returncode == 0, complete.stderr
