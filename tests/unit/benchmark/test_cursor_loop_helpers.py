from __future__ import annotations

import os
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


def _install_fake_aa(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-aa"
    fake.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >>"$AA_FAKE_CALL_LOG"
if [ "${1:-}" != "eval" ] || [ "${2:-}" != "run" ]; then
  exit 99
fi
shift 2
suite=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--suite" ]; then
    suite="$2"
    shift 2
    continue
  fi
  shift
done
case "$suite" in
  suite-a) verdict="fail" ;;
  suite-b) verdict="pass" ;;
  *) verdict="inconclusive" ;;
esac
printf '{"run_id":"run-%s","verdict":"%s"}\\n' "$suite" "$verdict"
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _collect_eval_command(tmp_path: Path, suites: str) -> str:
    fake = _install_fake_aa(tmp_path)
    engine = tmp_path / "engine"
    sut = tmp_path / "sut"
    engine.mkdir()
    sut.mkdir()
    call_log = tmp_path / "aa-calls.log"
    eval_log = tmp_path / "eval.log"
    return (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(call_log))} "
        f"collect_benchmark_eval_rows {shlex.quote(str(fake))} "
        f"{shlex.quote(str(engine))} {shlex.quote(str(sut))} "
        f"{shlex.quote(suites)} {shlex.quote(str(eval_log))}"
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


def test_benchmark_eval_collects_absolute_verdicts_without_regression_commands(
    tmp_path: Path,
) -> None:
    result = _run_helper(tmp_path, _collect_eval_command(tmp_path, "suite-a,suite-b"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "suite-a|fail|run-suite-a",
        "suite-b|pass|run-suite-b",
    ]
    assert (tmp_path / "aa-calls.log").read_text(encoding="utf-8").splitlines() == [
        f"eval run --suite suite-a --sut-dir {tmp_path / 'sut'} --json",
        f"eval run --suite suite-b --sut-dir {tmp_path / 'sut'} --json",
    ]


def test_benchmark_eval_setting_prefers_new_name_and_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    preferred = _run_helper(tmp_path, "benchmark_eval_setting true false default")
    legacy = _run_helper(tmp_path, "benchmark_eval_setting '' false true")
    defaulted = _run_helper(tmp_path, "benchmark_eval_setting '' '' true")

    assert preferred.stdout == "true"
    assert legacy.stdout == "false"
    assert defaulted.stdout == "true"


def test_benchmark_gate_ignores_failed_eval_metric_row(tmp_path: Path) -> None:
    collect = _collect_eval_command(tmp_path, "suite-a")
    result = _run_helper(
        tmp_path,
        f'row="$({collect})" && '
        'test "$row" = "suite-a|fail|run-suite-a" && '
        "benchmark_result_exit_code true true 0 true "
        "'RET-current|completed|final_status=PASS|archived=yes'",
    )

    assert result.returncode == 0, result.stderr


def test_persisted_workflow_terminals_stop_outer_attempts(tmp_path: Path) -> None:
    for terminal in ("completed", "stopped", "failed"):
        result = _run_helper(tmp_path, f"workflow_attempts_should_stop {terminal}")
        assert result.returncode == 0, terminal

    running = _run_helper(tmp_path, "workflow_attempts_should_stop running")
    assert running.returncode == 1


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


def test_latest_retro_selection_uses_current_run_mtime_not_lexicographic_id(
    tmp_path: Path,
) -> None:
    retro_root = tmp_path / "qa" / "retro"
    # Historical Cursor IDs may encode local time and sort after a newer UTC ID.
    stale_but_lexically_later = retro_root / "retro-20260726-223933-cursor"
    current_but_lexically_earlier = retro_root / "retro-20260726-170349"
    stale_but_lexically_later.mkdir(parents=True)
    current_but_lexically_earlier.mkdir()
    os.utime(stale_but_lexically_later, (100.0, 100.0))
    os.utime(current_but_lexically_earlier, (200.0, 200.0))

    result = _run_helper(
        tmp_path,
        'select_latest_retro_dir "$PWD/qa/retro" 150 ""',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(current_but_lexically_earlier)
