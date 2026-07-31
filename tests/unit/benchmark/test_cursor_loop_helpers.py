from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).parents[3]
_HELPERS = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "cursor-loop-helpers.sh"
_CURSOR_LOOP = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "run-workflow-loop-cursor.sh"
_BENCHMARK_ENV = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "benchmark.env"


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
if [ "${1:-}" = "trace" ]; then
  [ "${AA_FAKE_TRACE_EMPTY:-false}" = "true" ] && exit "${AA_FAKE_TRACE_EXIT:-40}"
  printf '%s\\n' "${AA_FAKE_TRACE_JSON:-{\\"integrity\\":\\"complete\\",\\"gaps\\":[]}}"
  exit "${AA_FAKE_TRACE_EXIT:-0}"
fi
if [ "${1:-}" = "verify" ]; then
  verdict="${AA_FAKE_VERIFY_VERDICT:-pass}"
  if [ "${AA_FAKE_VERIFY_EMPTY:-false}" = "true" ]; then
    [ "$verdict" = "needs_human" ] && exit 30
    [ "$verdict" = "pass" ] && exit 0
    exit 40
  fi
  printf '{\\"verdict\\":\\"%s\\",\\"blocking_gaps\\":[],\\"insufficient\\":[]}\\n' "$verdict"
  case "$verdict" in
    pass) exit 0 ;;
    needs_human) exit 30 ;;
    *) exit 40 ;;
  esac
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


def test_trace_verify_collection_persists_json_and_reports_pass(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    call_log = tmp_path / "aa-calls.log"
    trace_path = tmp_path / "trace.json"
    verify_path = tmp_path / "verify.json"
    log_path = tmp_path / "evidence.log"
    command = (
        "python3() { return 127; }; "
        f"AA_FAKE_CALL_LOG={shlex.quote(str(call_log))} "
        f"collect_trace_verify_evidence {shlex.quote(str(fake))} CH-1 "
        f"{shlex.quote(str(trace_path))} {shlex.quote(str(verify_path))} "
        f"{shlex.quote(str(log_path))} {shlex.quote(sys.executable)}"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "CH-1|raw|none|0|complete|0|0|pass|0|0"
    assert json.loads(trace_path.read_text(encoding="utf-8"))["integrity"] == "complete"
    assert json.loads(verify_path.read_text(encoding="utf-8"))["verdict"] == "pass"
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "trace --change CH-1 --json",
        "verify --change CH-1 --json",
    ]


def test_trace_verify_collection_preserves_nonzero_verify_verdict(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    trace_path = tmp_path / "trace.json"
    verify_path = tmp_path / "verify.json"
    log_path = tmp_path / "evidence.log"
    command = (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(tmp_path / 'aa-calls.log'))} "
        "AA_FAKE_VERIFY_VERDICT=needs_human "
        f"collect_trace_verify_evidence {shlex.quote(str(fake))} CH-2 "
        f"{shlex.quote(str(trace_path))} {shlex.quote(str(verify_path))} "
        f"{shlex.quote(str(log_path))} {shlex.quote(sys.executable)}"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "CH-2|raw|none|0|complete|0|30|needs_human|0|0"


def test_trace_verify_collection_wraps_empty_error_outputs_as_json(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    trace_path = tmp_path / "trace.json"
    verify_path = tmp_path / "verify.json"
    log_path = tmp_path / "evidence.log"
    command = (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(tmp_path / 'aa-calls.log'))} "
        "AA_FAKE_TRACE_EMPTY=true AA_FAKE_TRACE_EXIT=40 "
        "AA_FAKE_VERIFY_EMPTY=true AA_FAKE_VERIFY_VERDICT=fail "
        f"collect_trace_verify_evidence {shlex.quote(str(fake))} CH-ERR "
        f"{shlex.quote(str(trace_path))} {shlex.quote(str(verify_path))} "
        f"{shlex.quote(str(log_path))} {shlex.quote(sys.executable)}"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert json.loads(trace_path.read_text(encoding="utf-8")) == {
        "change_id": "CH-ERR",
        "command": "trace",
        "error": "invalid_or_missing_json_output",
        "exit_code": 40,
        "schema_version": "1",
    }
    assert json.loads(verify_path.read_text(encoding="utf-8")) == {
        "change_id": "CH-ERR",
        "command": "verify",
        "error": "invalid_or_missing_json_output",
        "exit_code": 40,
        "schema_version": "1",
    }


def test_resolve_aa_python_uses_console_script_interpreter(tmp_path: Path) -> None:
    aa_console = tmp_path / "aa"
    aa_console.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    aa_console.chmod(aa_console.stat().st_mode | stat.S_IXUSR)

    result = _run_helper(
        tmp_path,
        f"resolve_aa_python_bin {shlex.quote(str(aa_console))} ''",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == sys.executable


def test_resolve_cursor_project_root_honors_external_sut_override(tmp_path: Path) -> None:
    script_dir = tmp_path / "tool" / "benchmark"
    sut_root = tmp_path / "sut"
    script_dir.mkdir(parents=True)
    sut_root.mkdir()

    result = _run_helper(
        tmp_path,
        f"resolve_cursor_project_root {shlex.quote(str(script_dir))} {shlex.quote(str(sut_root))}",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(sut_root.resolve())


def test_benchmark_env_preserves_caller_trace_verify_override(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'DO_TRACE_VERIFY=false; source {shlex.quote(str(_BENCHMARK_ENV))}; printf %s "$DO_TRACE_VERIFY"',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "false"


def test_benchmark_evidence_gate_requires_successful_trace_and_verify(tmp_path: Path) -> None:
    passing = _run_helper(
        tmp_path,
        "benchmark_evidence_exit_code true 'CH-1|complete|none|0|complete|0|0|pass|0|0'",
    )
    needs_human = _run_helper(
        tmp_path,
        "benchmark_evidence_exit_code true 'CH-1|complete|none|0|complete|0|30|needs_human|0|1'",
    )
    malformed = _run_helper(
        tmp_path,
        "benchmark_evidence_exit_code true 'CH-1|complete|none|0|complete|bogus|0|pass|0|0'",
    )
    disabled = _run_helper(tmp_path, "benchmark_evidence_exit_code false")

    assert passing.returncode == 0, passing.stderr
    assert needs_human.returncode == 1
    assert malformed.returncode == 1
    assert disabled.returncode == 0, disabled.stderr


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


def _write_workflow_result(
    path: Path,
    *,
    change_id: str = "CH-1",
    entrypoint: str = "full",
    root_invocation_id: str = "inv-root-1",
    started_new_root: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "entrypoint": entrypoint,
                "root_invocation_id": root_invocation_id,
                "started_new_root": started_new_root,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_pin_workflow_root_writes_stable_json_for_new_root(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result_file = tmp_path / "workflow-result.json"
    _write_workflow_result(result_file)
    command = (
        f"pin_workflow_root_from_result {shlex.quote(str(run_dir))} CH-1 {shlex.quote(str(result_file))} full"
    )

    first = _run_helper(tmp_path, command)
    second = _run_helper(tmp_path, command)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    state = json.loads((run_dir / "CH-1.workflow-root.json").read_text(encoding="utf-8"))
    assert state == {
        "schema_version": "1",
        "change_id": "CH-1",
        "entrypoint": "full",
        "root_invocation_id": "inv-root-1",
    }


def test_pin_workflow_root_skips_creation_when_started_new_root_false(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result_file = tmp_path / "workflow-result.json"
    _write_workflow_result(result_file, started_new_root=False)
    command = (
        f"pin_workflow_root_from_result {shlex.quote(str(run_dir))} CH-1 {shlex.quote(str(result_file))} full"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert not (run_dir / "CH-1.workflow-root.json").exists()


def test_pin_workflow_root_rejects_identity_drift(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result_file = tmp_path / "workflow-result.json"
    _write_workflow_result(result_file)
    assert (
        _run_helper(
            tmp_path,
            f"pin_workflow_root_from_result {shlex.quote(str(run_dir))} CH-1 "
            f"{shlex.quote(str(result_file))} full",
        ).returncode
        == 0
    )
    _write_workflow_result(result_file, root_invocation_id="inv-root-2")
    drift = _run_helper(
        tmp_path,
        f"pin_workflow_root_from_result {shlex.quote(str(run_dir))} CH-1 "
        f"{shlex.quote(str(result_file))} full",
    )
    assert drift.returncode != 0
    assert "workflow_root_invocation_mismatch" in drift.stderr


def test_validate_workflow_command_result_rejects_missing_invocation(tmp_path: Path) -> None:
    result_file = tmp_path / "workflow-result.json"
    result_file.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "entrypoint": "full",
                "root_invocation_id": "",
                "started_new_root": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run_helper(
        tmp_path,
        f"validate_workflow_command_result {shlex.quote(str(result_file))} CH-1 full",
    )
    assert result.returncode != 0
    assert "workflow_result_invocation_missing" in result.stderr


def test_read_workflow_root_state_never_calls_aa_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "CH-1.workflow-root.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "entrypoint": "full",
                "root_invocation_id": "inv-root-1",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    command = (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(tmp_path / 'aa-calls.log'))} "
        f"read_workflow_root_state {shlex.quote(str(run_dir))} CH-1"
    )
    result = _run_helper(tmp_path, command)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inv-root-1|full"
    assert not (tmp_path / "aa-calls.log").exists()


def test_specialty_collect_helper_omits_schema_root_escape_hatch() -> None:
    helpers = _HELPERS.read_text(encoding="utf-8")
    reporter = (
        _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "benchmark_specialty_report.py"
    ).read_text(encoding="utf-8")
    loop = _CURSOR_LOOP.read_text(encoding="utf-8")
    start = helpers.index("collect_benchmark_specialty_report() {")
    end = helpers.index("\nvalidate_workflow_command_result() {", start)
    collect_fn = helpers[start:end]
    assert "--schema-root" not in collect_fn
    assert "schema_root" not in collect_fn
    assert "--schema-root" not in reporter
    assert "schema_root" not in reporter
    assert "collect_benchmark_specialty_report" in loop
    # Caller no longer forwards AA_REPO_ROOT as a schema-root argument.
    stage_start = loop.index("run_specialty_report_stage() {")
    stage_end = loop.index("\nreuse_specialty_report_stage() {", stage_start)
    stage = loop[stage_start:stage_end]
    assert "collect_benchmark_specialty_report \\" in stage
    assert (
        '"$AA_REPO_ROOT"'
        not in stage.split("collect_benchmark_specialty_report", maxsplit=1)[1].split("||", maxsplit=1)[0]
    )


def test_raw_evidence_row_uses_ten_columns_and_unknown(tmp_path: Path) -> None:
    import os

    aa = _install_fake_aa(tmp_path)
    command = (
        f"collect_trace_verify_evidence {shlex.quote(str(aa))} CH-1 "
        f"{shlex.quote(str(tmp_path / 't.json'))} {shlex.quote(str(tmp_path / 'v.json'))} "
        f"{shlex.quote(str(tmp_path / 'log.txt'))} {shlex.quote(sys.executable)}"
    )
    result = subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(_HELPERS))}; {command}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "AA_FAKE_CALL_LOG": str(tmp_path / "calls.log"),
            "AA_FAKE_TRACE_EMPTY": "true",
            "AA_FAKE_VERIFY_EMPTY": "true",
            "AA_FAKE_TRACE_EXIT": "7",
            "AA_FAKE_VERIFY_VERDICT": "fail",
        },
    )
    assert result.returncode == 0, result.stderr
    parts = result.stdout.strip().split("|")
    assert len(parts) == 10
    assert parts[:3] == ["CH-1", "raw", "none"]
    assert parts[4] == "unknown"
    assert parts[5] == "unknown"


def test_parse_evidence_row_rejects_unknown_collection_status(tmp_path: Path) -> None:
    bad = _run_helper(tmp_path, "parse_evidence_row_fields 'CH-1|weird|none|0|complete|0|0|pass|0|0'")
    assert bad.returncode != 0
    good = _run_helper(
        tmp_path,
        "parse_evidence_row_fields 'CH-1|incomplete|execution_projection_missing|0|unknown|unknown|0|unknown|unknown|unknown'",
    )
    assert good.returncode == 0, good.stderr


def test_parse_evidence_row_rejects_unknown_reason_and_zero_substituted_incomplete(
    tmp_path: Path,
) -> None:
    from typing import get_args

    from assurance_agent.eval.specialty_models import TraceCollectionFailureReason

    for reason in get_args(TraceCollectionFailureReason):
        ok = _run_helper(
            tmp_path,
            "parse_evidence_row_fields "
            f"'CH-1|incomplete|{reason}|0|unknown|unknown|0|unknown|unknown|unknown'",
        )
        assert ok.returncode == 0, reason

    unknown_reason = _run_helper(
        tmp_path,
        "parse_evidence_row_fields "
        "'CH-1|incomplete|not_a_closed_reason|0|unknown|unknown|0|unknown|unknown|unknown'",
    )
    assert unknown_reason.returncode != 0

    zero_substituted = _run_helper(
        tmp_path,
        "parse_evidence_row_fields "
        "'CH-1|incomplete|execution_projection_missing|0|0|0|0|0|0|0'",
    )
    assert zero_substituted.returncode != 0

    complete_with_reason = _run_helper(
        tmp_path,
        "parse_evidence_row_fields "
        "'CH-1|complete|verify_result_missing|0|complete|0|0|pass|0|0'",
    )
    assert complete_with_reason.returncode != 0

    raw_with_reason = _run_helper(
        tmp_path,
        "parse_evidence_row_fields 'CH-1|raw|bogus|0|complete|0|0|pass|0|0'",
    )
    assert raw_with_reason.returncode != 0


def test_finalize_and_reuse_register_nothing_for_pending_or_mismatched_receipt(
    tmp_path: Path,
) -> None:
    import hashlib
    import json

    from assurance_agent.eval.specialty_models import SpecialtyReportV3

    reporter = (
        _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "benchmark_specialty_report.py"
    )
    # Minimal committed-shaped V3 incomplete report bytes for retention gates.
    report_payload = {
        "schema_version": "3",
        "change_id": "CH-1",
        "capability_contract_policy": {
            "semantics": "counterfactual_plan_check_actions/v2",
            "integrity": "incomplete",
            "definition_binding": None,
            "definition_failure": "root_invocation_unbound",
            "rows": [
                {
                    "layer": layer,
                    "case_type": case_type,
                    "status": "incomplete",
                    "reason_code": "root_invocation_unbound",
                }
                for layer, case_type in (
                    ("api", "API"),
                    ("e2e", "E2E"),
                    ("fuzz", "Fuzz"),
                    ("performance", "Performance"),
                )
            ],
        },
        "traceability_evidence": {
            "status": "incomplete",
            "reason_code": "execution_projection_missing",
            "detail": "",
            "command_status": {"trace_exit": 1, "verify_exit": 2},
        },
    }
    SpecialtyReportV3.model_validate(report_payload)
    report = tmp_path / "CH-1.specialty-report.json"
    report_bytes = (json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    report.write_bytes(report_bytes)
    receipt = Path(str(report) + ".receipt.json")

    receipt.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "state": "pending",
                "attempt_id": "attempt-1",
                "change_id": "CH-1",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    pending = _run_helper(
        tmp_path,
        f"finalize_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(reporter))} CH-1 {shlex.quote(str(report))} 1 attempt-1",
    )
    assert pending.stdout.strip().endswith("registered=false")
    reuse_pending = _run_helper(
        tmp_path,
        f"reuse_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(reporter))} CH-1 {shlex.quote(str(report))}",
    )
    assert reuse_pending.returncode != 0

    digest = hashlib.sha256(report_bytes).hexdigest()
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "state": "committed",
                "attempt_id": "attempt-old",
                "change_id": "CH-1",
                "report_sha256": digest,
                "trace_status": "incomplete",
                "capability_integrity": "incomplete",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    attempt_mismatch = _run_helper(
        tmp_path,
        f"finalize_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(reporter))} CH-1 {shlex.quote(str(report))} 1 attempt-new",
    )
    assert attempt_mismatch.stdout.strip().endswith("registered=false")

    receipt.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "state": "committed",
                "attempt_id": "attempt-1",
                "change_id": "CH-1",
                "report_sha256": "0" * 64,
                "trace_status": "incomplete",
                "capability_integrity": "incomplete",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    digest_mismatch = _run_helper(
        tmp_path,
        f"finalize_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(reporter))} CH-1 {shlex.quote(str(report))} 1 attempt-1",
    )
    assert digest_mismatch.stdout.strip().endswith("registered=false")

    receipt.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "state": "committed",
                "attempt_id": "attempt-1",
                "change_id": "CH-1",
                "report_sha256": digest,
                "trace_status": "incomplete",
                "capability_integrity": "incomplete",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    matching = _run_helper(
        tmp_path,
        f"finalize_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(reporter))} CH-1 {shlex.quote(str(report))} 1 attempt-1",
    )
    assert "registered=true" in matching.stdout
    reuse_ok = _run_helper(
        tmp_path,
        f"reuse_benchmark_specialty_report {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(reporter))} CH-1 {shlex.quote(str(report))}",
    )
    assert reuse_ok.returncode == 0, reuse_ok.stderr
    assert reuse_ok.stdout.startswith("CH-1|incomplete|execution_projection_missing|")
