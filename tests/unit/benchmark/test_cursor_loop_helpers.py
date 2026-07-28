from __future__ import annotations

import json
import shlex
import stat
import subprocess
from pathlib import Path


_ROOT = Path(__file__).parents[3]
_HELPERS = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "cursor-loop-helpers.sh"
_CURSOR_LOOP = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "run-workflow-loop-cursor.sh"


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
if [ "${1:-}" = "retro" ]; then
  printf '{"retro_id":"retro-batch","status":"completed_with_gaps"}\\n'
  exit 0
fi
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


def _manifest_command(tmp_path: Path, batch_id: str, change_ids: tuple[str, ...]) -> str:
    manifest = tmp_path / "batch-manifest.json"
    quoted_ids = " ".join(shlex.quote(change_id) for change_id in change_ids)
    return (
        f"initialize_retro_batch_manifest {shlex.quote(str(manifest))} {shlex.quote(batch_id)} {quoted_ids}"
    )


def test_batch_manifest_initializes_canonical_members_and_resumes_identically(tmp_path: Path) -> None:
    command = _manifest_command(tmp_path, "batch-1", ("CH-B", "CH-A"))

    first = _run_helper(tmp_path, command)
    resumed = _run_helper(tmp_path, command)

    assert first.returncode == 0, first.stderr
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads((tmp_path / "batch-manifest.json").read_text(encoding="utf-8")) == {
        "batch_id": "batch-1",
        "members": [
            {
                "change_id": "CH-A",
                "evidence_availability": "absent",
                "execution_status": "not_started",
            },
            {
                "change_id": "CH-B",
                "evidence_availability": "absent",
                "execution_status": "not_started",
            },
        ],
        "schema_version": "1",
        "status": "incomplete",
    }


def test_batch_manifest_resume_rejects_member_drift(tmp_path: Path) -> None:
    assert _run_helper(tmp_path, _manifest_command(tmp_path, "batch-1", ("CH-A",))).returncode == 0

    drifted = _run_helper(tmp_path, _manifest_command(tmp_path, "batch-1", ("CH-A", "CH-B")))

    assert drifted.returncode != 0
    assert "membership_mismatch" in drifted.stderr


def test_batch_manifest_preserves_exact_member_counts(tmp_path: Path) -> None:
    for count in (0, 2, 11):
        case_dir = tmp_path / str(count)
        case_dir.mkdir()
        change_ids = tuple(f"CH-{index:02d}" for index in range(count))

        result = _run_helper(case_dir, _manifest_command(case_dir, f"batch-{count}", change_ids))

        assert result.returncode == 0, result.stderr
        manifest = json.loads((case_dir / "batch-manifest.json").read_text(encoding="utf-8"))
        assert [member["change_id"] for member in manifest["members"]] == list(change_ids)


def test_batch_manifest_updates_each_member_atomically_and_recomputes_status(tmp_path: Path) -> None:
    manifest = tmp_path / "batch-manifest.json"
    assert _run_helper(tmp_path, _manifest_command(tmp_path, "batch-1", ("CH-A", "CH-B"))).returncode == 0

    first = _run_helper(
        tmp_path,
        f"update_retro_batch_member {shlex.quote(str(manifest))} CH-A failed partial",
    )
    second = _run_helper(
        tmp_path,
        f"update_retro_batch_member {shlex.quote(str(manifest))} CH-B completed complete",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "incomplete"
    assert payload["members"] == [
        {
            "change_id": "CH-A",
            "evidence_availability": "partial",
            "execution_status": "failed",
        },
        {
            "change_id": "CH-B",
            "evidence_availability": "complete",
            "execution_status": "completed",
        },
    ]
    assert not tuple(tmp_path.glob(".batch-manifest.json.*.tmp"))


def test_batch_member_outcomes_cover_terminal_and_abnormal_states(tmp_path: Path) -> None:
    expected = {
        "completed": "completed|complete",
        "failed": "failed|partial",
        "stopped": "stopped|partial",
        "hard_timeout": "hard_timeout|partial",
        "cancelled": "cancelled|partial",
        "not_started": "not_started|absent",
    }
    evidence = tmp_path / "events.jsonl"
    evidence.write_text("{}\n", encoding="utf-8")

    for status, wanted in expected.items():
        path = evidence if status != "not_started" else tmp_path / "missing.jsonl"
        result = _run_helper(
            tmp_path,
            f"retro_batch_member_outcome {status} {shlex.quote(str(path))}",
        )
        assert result.returncode == 0, (status, result.stderr)
        assert result.stdout == wanted


def test_batch_retro_helper_invokes_only_canonical_manifest_cli(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    call_log = tmp_path / "aa-calls.log"
    retro_log = tmp_path / "retro.log"
    manifest = tmp_path / "batch-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    command = (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(call_log))} "
        f"run_batch_retro {shlex.quote(str(fake))} {shlex.quote(str(manifest))} "
        f"retro-1 'cursor-agent --print' false {shlex.quote(str(retro_log))}"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        f"retro --batch-manifest {manifest} --retro-id retro-1 --json"
    ]


def test_cursor_loop_runs_only_explicit_batch_retro_after_all_items_settle() -> None:
    source = _CURSOR_LOOP.read_text(encoding="utf-8")
    loop_start = source.index('for item in "${BENCHMARK_ITEMS[@]}"; do', source.index("# Main loop"))
    loop_end = source.index("\ndone\n\nretro_id=", loop_start)
    retro_call = source.index("\nrun_retro_collect\n", loop_end)

    assert retro_call > loop_end
    assert '"$AA_BIN" "$BATCH_MANIFEST" "$RETRO_ID"' in source
    for obsolete in (
        "retro_last",
        "RETRO_LAST",
        "select_latest_retro_dir",
        "retro_shell_change_id",
        "USE_WORKFLOW_RETRO",
        "RETRO_ENTRYPOINT",
    ):
        assert obsolete not in source


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
        "benchmark_result_exit_code true 'RET-current|failed|assurance failed|archived=no'",
    )

    assert result.returncode == 1


def test_archive_runs_only_for_completed_green_execution(tmp_path: Path) -> None:
    for final_status in ("PASS", "PASS_WITH_WARNINGS"):
        eligible = _run_helper(
            tmp_path,
            f"benchmark_should_run_archive true completed {final_status}",
        )
        assert eligible.returncode == 0, final_status

    for command in (
        "benchmark_should_run_archive true completed FAIL",
        "benchmark_should_run_archive true failed PASS",
        "benchmark_should_run_archive false completed PASS",
    ):
        ineligible = _run_helper(tmp_path, command)
        assert ineligible.returncode == 1, command


def test_benchmark_gate_fails_completed_execution_with_failed_test_status(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code false 'RET-current|completed|final_status=FAIL|archive=disabled'",
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
        "benchmark_result_exit_code true "
        "'RET-current|completed|final_status=PASS|archived=yes'",
    )

    assert result.returncode == 0, result.stderr


def test_persisted_workflow_terminals_stop_outer_attempts(tmp_path: Path) -> None:
    for terminal in ("completed", "stopped", "failed"):
        result = _run_helper(tmp_path, f"workflow_attempts_should_stop {terminal}")
        assert result.returncode == 0, terminal

    running = _run_helper(tmp_path, "workflow_attempts_should_stop running")
    assert running.returncode == 1


def test_benchmark_gate_passes_completed_archived_workflow(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code true 'RET-current|completed|final_status=PASS|archived=yes'",
    )

    assert result.returncode == 0, result.stderr


def test_benchmark_gate_fails_when_required_archive_was_skipped(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "benchmark_result_exit_code true 'RET-current|completed|final_status=PASS|archived=no'",
    )

    assert result.returncode == 1


def test_retro_artifact_contract_rejects_a_fresh_but_empty_directory(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-current"
    retro_dir.mkdir(parents=True)

    result = _run_helper(tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false')

    assert result.returncode == 1


def test_retro_artifact_contract_rejects_context_without_final_status(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-current"
    retro_dir.mkdir(parents=True)
    (retro_dir / "context.json").write_text('{"signal_count": 0}\n', encoding="utf-8")

    result = _run_helper(tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false')

    assert result.returncode == 1


def test_retro_artifact_contract_accepts_typed_final_status(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-current"
    retro_dir.mkdir(parents=True)
    (retro_dir / "retro-status.json").write_text(
        '{"schema_version":"1","retro_id":"retro-current","batch_id":"batch-1",'
        '"result":"completed_with_gaps","improvement_ids":[],"outbox_id":null,'
        '"failure_ids":["FAIL-1"]}\n',
        encoding="utf-8",
    )

    result = _run_helper(tmp_path, 'retro_artifacts_complete "$PWD/qa/retro/retro-current" false')

    assert result.returncode == 0, result.stderr
