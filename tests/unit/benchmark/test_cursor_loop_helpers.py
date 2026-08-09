from __future__ import annotations

import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path


_ROOT = Path(__file__).parents[3]
_HELPERS = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "cursor-loop-helpers.sh"
_CURSOR_LOOP = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "run-workflow-loop-cursor.sh"
_OPENCODE_LOOP = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "run-workflow-loop.sh"


def _run_helper(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(_HELPERS))}; {command}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_opencode_batch_boundary(tmp_path: Path, promotion_exit: int) -> subprocess.CompletedProcess[str]:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")
    function_start = source.find("run_batch_knowledge_promotion_boundary() {")
    function_end = source.find("# END batch knowledge promotion boundary", function_start)
    function_source = source[function_start:function_end] if function_start >= 0 else ""
    call_log = tmp_path / "boundary-calls.log"
    command = f"""
set -e
{function_source}
CALL_LOG={shlex.quote(str(call_log))}
PROMOTION_EXIT={promotion_exit}
promote_batch_knowledge_proposals() {{ printf '%s\\n' promote >>"$CALL_LOG"; return "$PROMOTION_EXIT"; }}
run_retro_collect() {{ printf '%s\\n' retro >>"$CALL_LOG"; return 0; }}
capture_retro_artifacts() {{ printf '%s\\n' capture >>"$CALL_LOG"; return 1; }}
run_benchmark_eval() {{ printf '%s\\n' eval >>"$CALL_LOG"; return 0; }}
log() {{ :; }}
AA_BIN=aa
BATCH_MANIFEST=batch-manifest.json
BATCH_CHANGE_IDS=(CH-A)
RETRO_ID=retro-1
DO_BENCHMARK_EVAL=true
knowledge_promotion_status=not_run
benchmark_eval_status=not_run
retro_result=technical_failure
retro_collect_exit=""
run_batch_knowledge_promotion_boundary
printf '%s|%s|%s|%s\\n' \
  "$knowledge_promotion_status" "$retro_result" "$retro_collect_exit" "$benchmark_eval_status"
"""
    return subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_opencode_eval_summary(tmp_path: Path, eval_status: str) -> subprocess.CompletedProcess[str]:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")
    function_start = source.find("render_benchmark_eval_summary() {")
    function_end = source.find("# END benchmark eval summary", function_start)
    function_source = source[function_start:function_end] if function_start >= 0 else ""
    command = f"""
set -e
{function_source}
DO_BENCHMARK_EVAL=true
benchmark_eval_status={shlex.quote(eval_status)}
RUNSTAMP=run-1
BENCHMARK_EVAL_ROWS=("suite-a|pass|eval-run-1")
render_benchmark_eval_summary
"""
    return subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
if [ "${1:-}" = "knowledge" ] && [ "${2:-}" = "promote" ]; then
  exit 0
fi
if [ "${1:-}" = "workflow" ] && [ "${2:-}" = "resume" ]; then
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


def test_uncommitted_success_replay_is_not_a_duplicate_commit(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "task_attempt_succeeded", "task_id": "task-1"},
                {"type": "task_attempt_succeeded", "task_id": "task-1"},
                {"type": "superstep_committed", "committed_task_ids": ["task-1"]},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_helper(
        tmp_path,
        f"assert_no_duplicate_committed_task_ids {shlex.quote(str(events))}",
    )

    assert result.returncode == 0, result.stderr


def test_duplicate_committed_task_id_is_rejected(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "superstep_committed", "committed_task_ids": ["task-1"]},
                {"type": "superstep_committed", "committed_task_ids": ["task-1"]},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_helper(
        tmp_path,
        f"assert_no_duplicate_committed_task_ids {shlex.quote(str(events))}",
    )

    assert result.returncode != 0
    assert "duplicate committed task ids across restarts" in result.stderr


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
        "needs_human_review": "running|partial",
        "interrupted": "running|partial",
        "missing": "running|partial",
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
    assert _run_helper(tmp_path, _manifest_command(tmp_path, "batch-1", ("CH-A",))).returncode == 0
    assert (
        _run_helper(
            tmp_path,
            f"update_retro_batch_member {shlex.quote(str(manifest))} CH-A completed complete",
        ).returncode
        == 0
    )
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


def test_batch_retro_refuses_nonterminal_or_timeout_members(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    call_log = tmp_path / "aa-calls.log"
    retro_log = tmp_path / "retro.log"
    manifest = tmp_path / "batch-manifest.json"
    assert _run_helper(tmp_path, _manifest_command(tmp_path, "batch-1", ("CH-A",))).returncode == 0

    for status in ("not_started", "running", "hard_timeout"):
        assert (
            _run_helper(
                tmp_path,
                f"update_retro_batch_member {shlex.quote(str(manifest))} CH-A {status} absent",
            ).returncode
            == 0
        )
        command = (
            f"AA_FAKE_CALL_LOG={shlex.quote(str(call_log))} "
            f"run_batch_retro {shlex.quote(str(fake))} {shlex.quote(str(manifest))} "
            f"retro-1 'cursor-agent --print' false {shlex.quote(str(retro_log))}"
        )

        result = _run_helper(tmp_path, command)

        assert result.returncode != 0, status
        assert not call_log.exists(), status


def test_knowledge_promotion_runs_only_after_batch_members_settle(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    call_log = tmp_path / "aa-calls.log"
    manifest = tmp_path / "batch-manifest.json"
    proposal = tmp_path / "qa" / "changes" / "CH-A" / "plans" / "data-knowledge.proposal.api.yaml"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("version: 1\n", encoding="utf-8")
    assert _run_helper(tmp_path, _manifest_command(tmp_path, "batch-1", ("CH-A",))).returncode == 0
    command = (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(call_log))} "
        f"promote_batch_knowledge_proposals {shlex.quote(str(fake))} "
        f"{shlex.quote(str(manifest))} CH-A"
    )

    active = _run_helper(tmp_path, command)

    assert active.returncode != 0
    assert not call_log.exists()

    assert (
        _run_helper(
            tmp_path,
            f"update_retro_batch_member {shlex.quote(str(manifest))} CH-A stopped partial",
        ).returncode
        == 0
    )
    settled = _run_helper(tmp_path, command)

    assert settled.returncode == 0, settled.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == ["knowledge promote --change CH-A --yes"]


def test_opencode_batch_boundary_runs_retro_and_eval_after_successful_promotion(
    tmp_path: Path,
) -> None:
    result = _run_opencode_batch_boundary(tmp_path, promotion_exit=0)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "boundary-calls.log").read_text(encoding="utf-8").splitlines() == [
        "promote",
        "retro",
        "capture",
        "eval",
    ]
    assert result.stdout == "completed|technical_failure|0|completed\n"


def test_opencode_batch_boundary_skips_retro_and_eval_after_failed_promotion(
    tmp_path: Path,
) -> None:
    result = _run_opencode_batch_boundary(tmp_path, promotion_exit=1)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "boundary-calls.log").read_text(encoding="utf-8").splitlines() == ["promote"]
    assert result.stdout == (
        "failed|skipped_knowledge_promotion_failed|skipped|skipped_knowledge_promotion_failed\n"
    )


def test_opencode_eval_summary_advertises_artifacts_only_after_eval_runs(tmp_path: Path) -> None:
    result = _run_opencode_eval_summary(tmp_path, eval_status="completed")

    assert result.returncode == 0, result.stderr
    assert "- status: `completed`" in result.stdout
    assert "| `suite-a` | pass | `eval-run-1` |" in result.stdout
    assert "- metrics: `eval/out/runs/<run_id>/metrics.json`" in result.stdout
    assert "- log: `benchmark/runs/run-1-opencode/benchmark-eval.log`" in result.stdout


def test_opencode_eval_summary_reports_skip_without_advertising_artifacts(tmp_path: Path) -> None:
    result = _run_opencode_eval_summary(
        tmp_path,
        eval_status="skipped_knowledge_promotion_failed",
    )

    assert result.returncode == 0, result.stderr
    assert "- status: `skipped_knowledge_promotion_failed`" in result.stdout
    assert "suite-a" not in result.stdout
    assert "eval/out/runs" not in result.stdout
    assert "benchmark-eval.log" not in result.stdout


def test_opencode_loop_reports_and_gates_knowledge_promotion() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    assert 'knowledge_promotion_status="not_run"' in source
    assert 'knowledge_promotion_status="completed"' in source
    assert 'knowledge_promotion_status="failed"' in source
    assert 'retro_result="skipped_knowledge_promotion_failed"' in source
    assert 'benchmark_knowledge_promotion_exit_code "$knowledge_promotion_status"' in source
    assert 'benchmark_eval_status="skipped_nonterminal_batch"' in source


def test_opencode_loop_preserves_opencode_agents_and_five_item_defaults() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")
    driver = source[source.index("run_driver() {") : source.index("\n}", source.index("run_driver() {"))]
    retro = source[
        source.index("run_retro_collect() {") : source.index("\n}", source.index("run_retro_collect() {"))
    ]
    archive_agent = source[
        source.index("run_opencode_agent() {") : source.index("\n}", source.index("run_opencode_agent() {"))
    ]
    workflow_entrypoint = source[
        source.index("run_workflow_entrypoint() {") : source.index(
            "\n}", source.index("run_workflow_entrypoint() {")
        )
    ]
    items_block = source[
        source.index("  BENCHMARK_ITEMS=(") : source.index(")\nfi", source.index("  BENCHMARK_ITEMS=("))
    ]
    default_items = [
        line.strip().strip('"') for line in items_block.splitlines() if line.strip().startswith('"RET-')
    ]

    assert '"$AA_BIN" skill refresh --sync-agents' in source
    assert 'DRIVER_ADAPTER="${DRIVER_ADAPTER:-opencode}"' in source
    assert '--adapter "$DRIVER_ADAPTER"' in driver
    assert '--server "$OPENCODE_SERVER"' in driver
    assert '--directory "$PROJECT_ROOT"' in driver
    assert 'adapter_args+=(--model "$OPENCODE_MODEL")' in driver
    assert 'retro_command+=(--adapter "$DRIVER_ADAPTER")' in retro
    assert 'retro_command+=(--server "$OPENCODE_SERVER")' in retro
    assert 'retro_command+=(--directory "$PROJECT_ROOT")' in retro
    assert 'retro_command+=(--model "$OPENCODE_MODEL")' in retro
    assert "--agent aa-archiver" in archive_agent
    assert 'cmd+=(--model "$OPENCODE_MODEL")' in archive_agent
    assert '--adapter "$DRIVER_ADAPTER"' in workflow_entrypoint
    assert '--server "$OPENCODE_SERVER"' in workflow_entrypoint
    assert '--directory "$PROJECT_ROOT"' in workflow_entrypoint
    assert 'adapter_args+=(--model "$OPENCODE_MODEL")' in workflow_entrypoint
    assert source.index('"$AA_BIN" skill refresh --sync-agents') < source.index(
        'if ! curl -sf -o /dev/null "$OPENCODE_SERVER"'
    )
    assert default_items == [
        "RET-dept-management:requirements/dept-management.md",
        "RET-user-management:requirements/user-management.md",
        "RET-api-management:requirements/api-management.md",
        "RET-role-management:requirements/role-management.md",
        "RET-menu-management:requirements/menu-management.md",
    ]


def test_cursor_loop_runs_only_explicit_batch_retro_after_all_items_settle() -> None:
    source = _CURSOR_LOOP.read_text(encoding="utf-8")
    loop_start = source.index('for item in "${BENCHMARK_ITEMS[@]}"; do', source.index("# Main loop"))
    loop_end = source.index("\ndone\n\nretro_id=", loop_start)
    settled_guard = source.index('if batch_members_settled "$BATCH_MANIFEST"; then', loop_end)
    retro_call = source.index("run_retro_collect", settled_guard)

    assert loop_end < settled_guard < retro_call
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


def test_cursor_loop_manages_backend_and_frontend_lifecycles() -> None:
    source = _CURSOR_LOOP.read_text(encoding="utf-8")

    assert 'FRONTEND_READY_URL="${FRONTEND_READY_URL:-${E2E_FRONTEND_URL%/}/}"' in source
    assert 'FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"' in source
    assert 'FRONTEND_LOG="$RUN_DIR/frontend.log"' in source
    assert 'stop_benchmark_sut "$FRONTEND_PID_FILE"' in source
    assert 'ensure_benchmark_sut \\\n    "$FRONTEND_READY_URL" "$FRONTEND_LOG" "$FRONTEND_PID_FILE"' in source
    assert '"$PNPM_BIN" --dir "$PROJECT_ROOT/web" run dev' in source
    assert '"$PNPM_BIN" --dir "$PROJECT_ROOT/web" run dev --' not in source
    assert "ensure_loop_sut || exit 1\nensure_loop_frontend || exit 1" in source


def test_cursor_loop_enforces_task_workspace_sandbox_for_every_cursor_invocation() -> None:
    source = _CURSOR_LOOP.read_text(encoding="utf-8")

    assert (
        'local cmd="$CURSOR_AGENT_BIN --print --output-format '
        '$CURSOR_OUTPUT_FORMAT --sandbox enabled --trust"' in source
    )
    assert '    --sandbox\n    enabled\n    --workspace "$PROJECT_ROOT"' in source
    assert "--force" not in source
    assert "CURSOR_AGENT_FORCE" not in source

    benchmark_dir = _CURSOR_LOOP.parent
    for env_path in benchmark_dir.glob("benchmark*.env"):
        assert "CURSOR_AGENT_FORCE" not in env_path.read_text(encoding="utf-8"), env_path


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


def test_managed_sut_starts_waits_for_readiness_and_stops(tmp_path: Path) -> None:
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    (serve_dir / "openapi.json").write_text('{"openapi":"3.1.0"}\n', encoding="utf-8")
    port = _unused_local_port()
    ready_url = f"http://127.0.0.1:{port}/openapi.json"
    pid_file = tmp_path / "sut.pid"
    log_file = tmp_path / "sut.log"
    server_command = shlex.join(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(serve_dir),
        ]
    )
    command = (
        f"ensure_benchmark_sut {shlex.quote(ready_url)} "
        f"{shlex.quote(str(log_file))} {shlex.quote(str(pid_file))} 50 0.05 "
        f"-- {server_command}"
    )

    started = _run_helper(tmp_path, command)

    try:
        assert started.returncode == 0, started.stderr
        pid = int(pid_file.read_text(encoding="utf-8"))
        os.kill(pid, 0)
        with urllib.request.urlopen(ready_url, timeout=1.0) as response:
            assert response.status == 200

        stopped = _run_helper(
            tmp_path,
            f"stop_benchmark_sut {shlex.quote(str(pid_file))}",
        )

        assert stopped.returncode == 0, stopped.stderr
        assert not pid_file.exists()
        assert not Path(f"{pid_file}.identity").exists()
        identity = _run_helper(tmp_path, f"benchmark_process_identity {pid}")
        assert identity.stdout == ""
    finally:
        if pid_file.exists():
            os.kill(int(pid_file.read_text(encoding="utf-8")), 9)


def test_stale_pid_identity_never_kills_an_unrelated_process(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = tmp_path / "stale.pid"
    pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
    Path(f"{pid_file}.identity").write_text("different-process\n", encoding="utf-8")

    try:
        result = _run_helper(
            tmp_path,
            f"stop_benchmark_sut {shlex.quote(str(pid_file))}",
        )

        assert result.returncode == 1
        assert process.poll() is None
        assert not pid_file.exists()
        assert not Path(f"{pid_file}.identity").exists()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_pid_identity_is_rechecked_before_force_kill(tmp_path: Path) -> None:
    pid_file = tmp_path / "owned.pid"
    identity_file = Path(f"{pid_file}.identity")
    call_count = tmp_path / "identity-calls"
    kill_log = tmp_path / "kill.log"
    pid_file.write_text("12345\n", encoding="utf-8")
    identity_file.write_text("owned-process\n", encoding="utf-8")
    command = (
        "benchmark_process_identity() { "
        f"n=$(cat {shlex.quote(str(call_count))} 2>/dev/null || echo 0); "
        "n=$((n + 1)); "
        f'printf "%s\\n" "$n" >{shlex.quote(str(call_count))}; '
        'if [ "$n" -eq 1 ]; then echo owned-process; else echo reused-process; fi; '
        "}; "
        f'kill() {{ printf "%s\\n" "$*" >>{shlex.quote(str(kill_log))}; return 0; }}; '
        "sleep() { :; }; "
        f"stop_benchmark_sut {shlex.quote(str(pid_file))}"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert kill_log.read_text(encoding="utf-8").splitlines() == ["-TERM 12345"]
    assert not pid_file.exists()
    assert not identity_file.exists()


def test_failed_force_kill_preserves_process_identity_for_retry(tmp_path: Path) -> None:
    pid_file = tmp_path / "owned.pid"
    identity_file = Path(f"{pid_file}.identity")
    pid_file.write_text("12345\n", encoding="utf-8")
    identity_file.write_text("owned-process\n", encoding="utf-8")
    command = (
        "benchmark_process_identity() { echo owned-process; }; "
        'kill() { [ "$1" = "-KILL" ] && return 1; return 0; }; '
        "sleep() { :; }; "
        f"stop_benchmark_sut {shlex.quote(str(pid_file))}"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 1
    assert pid_file.read_text(encoding="utf-8") == "12345\n"
    assert identity_file.read_text(encoding="utf-8") == "owned-process\n"


def test_http_readiness_probe_does_not_use_host_proxy_settings() -> None:
    source = _HELPERS.read_text(encoding="utf-8")

    assert "urllib.request.ProxyHandler({})" in source
    assert 'headers={"Accept": "*/*"}' in source


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


def test_knowledge_proposal_interrupt_continues_without_mid_batch_promotion(tmp_path: Path) -> None:
    proposal = _run_helper(tmp_path, "benchmark_interrupt_action proposal_pending")
    ordinary = _run_helper(tmp_path, "benchmark_interrupt_action unchanged")

    assert proposal.returncode == 0
    assert proposal.stdout == "accept_risk"
    assert ordinary.returncode == 0
    assert ordinary.stdout == "accept_risk"


def test_interrupt_resume_preserves_headless_agent_arguments(tmp_path: Path) -> None:
    fake = _install_fake_aa(tmp_path)
    call_log = tmp_path / "aa-calls.log"
    command = (
        f"AA_FAKE_CALL_LOG={shlex.quote(str(call_log))} "
        f"resume_benchmark_interrupt {shlex.quote(str(fake))} CH-1 INT-1 accept_risk "
        "'benchmark decision' headless "
        "'cursor-agent --print --output-format stream-json --sandbox enabled --trust "
        "--model cursor-grok-4.5-high-fast'"
    )

    result = _run_helper(tmp_path, command)

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "workflow resume --change CH-1 --interrupt INT-1 --action accept_risk "
        "--reason benchmark decision --adapter headless --agent-cmd "
        "cursor-agent --print --output-format stream-json --sandbox enabled --trust "
        "--model cursor-grok-4.5-high-fast"
    ]


def test_knowledge_promotion_failure_is_a_benchmark_failure(tmp_path: Path) -> None:
    completed = _run_helper(tmp_path, "benchmark_knowledge_promotion_exit_code completed")
    not_run = _run_helper(tmp_path, "benchmark_knowledge_promotion_exit_code not_run")
    failed = _run_helper(tmp_path, "benchmark_knowledge_promotion_exit_code failed")

    assert completed.returncode == 0
    assert not_run.returncode == 0
    assert failed.returncode == 1


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


def test_workflow_entrypoint_dispatch_preserves_metrics_nightly_arguments(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        "capture_runner() { printf '<%s>\\n' \"$@\"; }; "
        "dispatch_benchmark_workflow_entrypoint capture_runner metrics.log CH-METRICS "
        "aa metrics-nightly '{}' headless 'cursor-agent --print --trust'",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "<metrics.log>",
        "<CH-METRICS>",
        "<aa>",
        "<workflow>",
        "<run>",
        "<--change>",
        "<CH-METRICS>",
        "<--entrypoint>",
        "<metrics-nightly>",
        "<--adapter>",
        "<headless>",
        "<--params>",
        "<{}>",
        "<--agent-cmd>",
        "<cursor-agent --print --trust>",
    ]


def _write_verification_metric_artifacts(change_dir: Path, change_id: str) -> None:
    inspect = change_dir / "inspect"
    inspect.mkdir(parents=True)
    (inspect / "metrics-nightly.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "change_id": change_id,
                "cadence": "nightly",
                "floor_ratio": 0.8,
            }
        ),
        encoding="utf-8",
    )
    (inspect / "metrics-nightly-shortboards.json").write_text(
        json.dumps(
            {
                "change_id": change_id,
                "source_rel": "inspect/metrics-nightly.json",
                "sufficiency_verdict": "needs_human",
            }
        ),
        encoding="utf-8",
    )
    (inspect / "metrics-c-layer.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "cadence": "report",
                "escape_rate": {"status": "evaluated"},
                "counterexample_promotion_rate": {"status": "not_evaluated"},
                "coverage_gap_closure_rate": {"status": "evaluated"},
                "seed_replay_stability": {"status": "not_evaluated"},
            }
        ),
        encoding="utf-8",
    )
    (inspect / "quarantine-projection.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "entries": [
                    {"subject_kind": "property", "subject_key": "p1", "status": "active"},
                    {"subject_kind": "journey", "subject_key": "j1", "status": "released"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_verification_metrics_summary_validates_identity_and_reports_vectors(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-METRICS"
    _write_verification_metric_artifacts(change_dir, "CH-METRICS")

    valid = _run_helper(
        tmp_path,
        f"summarize_verification_metrics {shlex.quote(str(change_dir))} CH-METRICS",
    )

    assert valid.returncode == 0, valid.stderr
    assert valid.stdout == "0.8|needs_human|2/4|1"

    c_layer = change_dir / "inspect" / "metrics-c-layer.json"
    payload = json.loads(c_layer.read_text(encoding="utf-8"))
    payload["change_id"] = "CH-FOREIGN"
    c_layer.write_text(json.dumps(payload), encoding="utf-8")

    foreign = _run_helper(
        tmp_path,
        f"summarize_verification_metrics {shlex.quote(str(change_dir))} CH-METRICS",
    )

    assert foreign.returncode != 0
    assert "identity_mismatch" in foreign.stderr


def test_verification_metrics_accept_the_reject_verdict(tmp_path: Path) -> None:
    """``reject`` (collection gap / no number to judge) is an honest nightly outcome.

    The summarizer and the loop exit-code gate validate the artifact contract,
    not the verdict's desirability — a reject row must not read as
    ``invalid_artifacts``.
    """
    change_dir = tmp_path / "qa" / "changes" / "CH-METRICS"
    _write_verification_metric_artifacts(change_dir, "CH-METRICS")
    shortboards = change_dir / "inspect" / "metrics-nightly-shortboards.json"
    payload = json.loads(shortboards.read_text(encoding="utf-8"))
    payload["sufficiency_verdict"] = "reject"
    shortboards.write_text(json.dumps(payload), encoding="utf-8")

    summary = _run_helper(
        tmp_path,
        f"summarize_verification_metrics {shlex.quote(str(change_dir))} CH-METRICS",
    )
    gate = _run_helper(
        tmp_path,
        "benchmark_verification_metrics_exit_code true 'CH-A|completed|0.8|reject|2/4|1'",
    )

    assert summary.returncode == 0, summary.stderr
    assert summary.stdout == "0.8|reject|2/4|1"
    assert gate.returncode == 0, gate.stderr


def test_verification_metrics_gate_requires_complete_rows_when_enabled(tmp_path: Path) -> None:
    complete = _run_helper(
        tmp_path,
        "benchmark_verification_metrics_exit_code true "
        "'CH-A|completed|0.8|pass|4/4|0' 'CH-B|completed|n/a|not_evaluated|1/4|2'",
    )
    failed = _run_helper(
        tmp_path,
        "benchmark_verification_metrics_exit_code true 'CH-A|failed|n/a|n/a|n/a|n/a'",
    )
    malformed = _run_helper(
        tmp_path,
        "benchmark_verification_metrics_exit_code true 'CH-A|completed||||'",
    )
    missing = _run_helper(tmp_path, "benchmark_verification_metrics_exit_code true")
    disabled = _run_helper(tmp_path, "benchmark_verification_metrics_exit_code false")

    assert complete.returncode == 0, complete.stderr
    assert failed.returncode == 1
    assert malformed.returncode == 1
    assert missing.returncode == 1
    assert disabled.returncode == 0, disabled.stderr


def test_verification_metrics_stage_reports_command_and_artifact_failures(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-METRICS"
    _write_verification_metric_artifacts(change_dir, "CH-METRICS")
    command = (
        "ok_runner() { return 0; }; "
        f"execute_verification_metrics_stage ok_runner metrics.log CH-METRICS "
        f"{shlex.quote(str(change_dir))} metrics-nightly '{{}}'"
    )

    complete = _run_helper(tmp_path, command)
    command_failed = _run_helper(
        tmp_path,
        "failed_runner() { return 40; }; "
        f"execute_verification_metrics_stage failed_runner metrics.log CH-METRICS "
        f"{shlex.quote(str(change_dir))} metrics-nightly '{{}}'",
    )
    (change_dir / "inspect" / "metrics-nightly.json").unlink()
    artifact_failed = _run_helper(tmp_path, command)

    assert complete.returncode == 0, complete.stderr
    assert complete.stdout == "CH-METRICS|completed|0.8|needs_human|2/4|1"
    assert command_failed.returncode == 1
    assert command_failed.stdout == "CH-METRICS|failed|n/a|n/a|n/a|n/a"
    assert artifact_failed.returncode == 1
    assert artifact_failed.stdout == "CH-METRICS|invalid_artifacts|n/a|n/a|n/a|n/a"
    assert "artifact_invalid:metrics-nightly.json" in artifact_failed.stderr


def test_verification_metrics_snapshot_is_complete_or_writes_nothing(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-METRICS"
    _write_verification_metric_artifacts(change_dir, "CH-METRICS")
    run_dir = tmp_path / "run"

    complete = _run_helper(
        tmp_path,
        f"snapshot_verification_metrics {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(run_dir))} CH-METRICS",
    )

    assert complete.returncode == 0, complete.stderr
    snapshot = run_dir / "CH-METRICS.verification-metrics.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["change_id"] == "CH-METRICS"
    assert sorted(payload["artifacts"]) == [
        "inspect/metrics-c-layer.json",
        "inspect/metrics-nightly-shortboards.json",
        "inspect/metrics-nightly.json",
        "inspect/quarantine-projection.json",
    ]
    before = snapshot.read_bytes()

    (change_dir / "inspect" / "metrics-c-layer.json").unlink()
    incomplete = _run_helper(
        tmp_path,
        f"snapshot_verification_metrics {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(run_dir))} CH-METRICS",
    )

    assert incomplete.returncode == 1
    assert snapshot.read_bytes() == before
    assert not tuple(run_dir.glob(".*.tmp"))


def _write_coverage_repair_artifacts(
    change_dir: Path,
    change_id: str,
    *,
    status: str = "repaired",
    attempts_used: int = 1,
) -> None:
    repair = change_dir / "coverage-repair"
    inspect = change_dir / "inspect"
    repair.mkdir(parents=True)
    inspect.mkdir(parents=True)
    (repair / "brief.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "batch_id": "20260806-120000-000000001",
                "probe_verdict": "pass" if status == "repaired" else "needs_human",
                "eligible": False,
                "allowed_test_files": [],
                "shortboards": [],
                "repair_items": [],
                "deferred_to_intake": [],
            }
        ),
        encoding="utf-8",
    )
    (repair / "status.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "status": status,
                "attempts_used": attempts_used,
                "last_batch_id": "20260806-115959-999999999" if attempts_used else None,
                "deferred_to_intake": [],
            }
        ),
        encoding="utf-8",
    )
    if attempts_used:
        (repair / "safety-check.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "change_id": change_id,
                    "attempt": attempts_used,
                    "passed": True,
                    "needs_review": False,
                }
            ),
            encoding="utf-8",
        )
    (inspect / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "change_id": change_id,
                "cadence": "pr",
                "computed_at": "2026-08-06T12:00:01Z",
                "risk_tier": "low",
                "risk_tier_lower_bound": "low",
                "risk_tier_declared": None,
                "risk_declaration_lowered": False,
                "risk_lowered_declarations": [],
                "metrics": {},
                "collection_gaps": [],
                "shortboards": [],
                "floor_ratio": None,
                "policy_digest": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    (inspect / "metrics-source-batch.json").write_text(
        json.dumps(
            {
                "change_id": change_id,
                "batch_id": "20260806-120000-000000001",
            }
        ),
        encoding="utf-8",
    )


def test_coverage_repair_snapshot_reports_attempt_and_post_repair_batch(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-REPAIR"
    _write_coverage_repair_artifacts(change_dir, "CH-REPAIR")
    run_dir = tmp_path / "run"

    result = _run_helper(
        tmp_path,
        f"snapshot_coverage_repair {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(run_dir))} CH-REPAIR {shlex.quote(sys.executable)}",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ("CH-REPAIR|repaired|1|20260806-115959-999999999|20260806-120000-000000001|pass")
    snapshot = json.loads((run_dir / "CH-REPAIR.coverage-repair.json").read_text(encoding="utf-8"))
    assert snapshot["change_id"] == "CH-REPAIR"
    assert sorted(snapshot["artifacts"]) == [
        "coverage-repair/brief.json",
        "coverage-repair/safety-check.json",
        "coverage-repair/status.json",
        "inspect/metrics-source-batch.json",
        "inspect/metrics.json",
    ]


def test_coverage_repair_snapshot_rejects_identity_drift_and_writes_nothing(
    tmp_path: Path,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-REPAIR"
    _write_coverage_repair_artifacts(change_dir, "CH-FOREIGN")
    run_dir = tmp_path / "run"

    result = _run_helper(
        tmp_path,
        f"snapshot_coverage_repair {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(run_dir))} CH-REPAIR {shlex.quote(sys.executable)}",
    )

    assert result.returncode != 0
    assert "identity_mismatch" in result.stderr
    assert not (run_dir / "CH-REPAIR.coverage-repair.json").exists()


def test_coverage_repair_snapshot_rejects_foreign_source_receipt(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-REPAIR"
    _write_coverage_repair_artifacts(change_dir, "CH-REPAIR")
    receipt = change_dir / "inspect" / "metrics-source-batch.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["change_id"] = "CH-FOREIGN"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_helper(
        tmp_path,
        f"snapshot_coverage_repair {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(tmp_path / 'run'))} CH-REPAIR {shlex.quote(sys.executable)}",
    )

    assert result.returncode == 1
    assert "identity_mismatch:inspect/metrics-source-batch.json" in result.stderr


def test_coverage_repair_snapshot_validates_canonical_artifact_contracts(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-REPAIR"
    _write_coverage_repair_artifacts(change_dir, "CH-REPAIR")
    metrics = change_dir / "inspect" / "metrics.json"
    payload = json.loads(metrics.read_text(encoding="utf-8"))
    del payload["computed_at"]
    metrics.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_helper(
        tmp_path,
        f"snapshot_coverage_repair {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(tmp_path / 'run'))} CH-REPAIR {shlex.quote(sys.executable)}",
    )

    assert result.returncode == 1
    assert "artifact_invalid:coverage-repair:contract" in result.stderr


def test_coverage_repair_gate_requires_terminal_well_formed_rows(tmp_path: Path) -> None:
    valid_rows = (
        "CH-A|repaired|1|old|new|pass",
        "CH-A|exhausted|1|old|new|review",
        "CH-A|exhausted|0|new|new|not_run",
        "CH-A|not_eligible|0|new|new|not_run",
        "CH-A|failed|0|new|new|not_run",
        "CH-A|failed|1|old|new|fail",
    )
    for row in valid_rows:
        result = _run_helper(
            tmp_path,
            f"benchmark_coverage_repair_exit_code {shlex.quote(row)}",
        )
        assert result.returncode == 0, row

    for row in (
        "CH-A|in_progress|1|old|new|pass",
        "CH-A|repaired|x|old|new|pass",
        "CH-A|repaired|0|new|new|not_run",
        "CH-A|not_eligible|1|old|new|pass",
        "CH-A|not_eligible|0|new|new|pass",
        "CH-A|repaired|1|old||pass",
        "CH-A|repaired|1|old|new|unknown",
    ):
        result = _run_helper(
            tmp_path,
            f"benchmark_coverage_repair_exit_code {shlex.quote(row)}",
        )
        assert result.returncode == 1, row


def test_coverage_repair_snapshot_rejects_non_increasing_or_malformed_batch(
    tmp_path: Path,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-REPAIR"
    _write_coverage_repair_artifacts(change_dir, "CH-REPAIR")
    run_dir = tmp_path / "run"
    receipt = change_dir / "inspect" / "metrics-source-batch.json"

    for bad_batch in ("20260806-115959-999999999", "not-a-batch"):
        receipt.write_text(
            json.dumps({"change_id": "CH-REPAIR", "batch_id": bad_batch}),
            encoding="utf-8",
        )
        result = _run_helper(
            tmp_path,
            f"snapshot_coverage_repair {shlex.quote(str(change_dir))} "
            f"{shlex.quote(str(run_dir))} CH-REPAIR {shlex.quote(sys.executable)}",
        )

        assert result.returncode == 1, bad_batch
        assert not (run_dir / "CH-REPAIR.coverage-repair.json").exists()


def test_coverage_repair_snapshot_reports_mechanical_failure_before_review(
    tmp_path: Path,
) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-REPAIR"
    _write_coverage_repair_artifacts(change_dir, "CH-REPAIR")
    safety = change_dir / "coverage-repair" / "safety-check.json"
    payload = json.loads(safety.read_text(encoding="utf-8"))
    payload.update({"passed": False, "needs_review": True})
    safety.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_helper(
        tmp_path,
        f"snapshot_coverage_repair {shlex.quote(str(change_dir))} "
        f"{shlex.quote(str(tmp_path / 'run'))} CH-REPAIR {shlex.quote(sys.executable)}",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("|fail")


def test_coverage_repair_summary_rows_allow_empty_input_under_nounset(tmp_path: Path) -> None:
    result = _run_helper(tmp_path, "set -u; render_coverage_repair_rows")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_cursor_loop_passes_and_reports_coverage_repair_budget() -> None:
    source = _CURSOR_LOOP.read_text(encoding="utf-8")

    assert 'MAX_COVERAGE_REPAIR_ATTEMPTS="${MAX_COVERAGE_REPAIR_ATTEMPTS:-1}"' in source
    assert '"max_coverage_repair_attempts": int(os.environ["MAX_COVERAGE_REPAIR"])' in source
    assert "## Coverage Repair Fast Loop" in source
    assert "<change_id>.coverage-repair.json" in source


def test_opencode_loop_passes_coverage_repair_budget() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    assert 'AA_PYTHON="${AA_PYTHON:-$AA_REPO_ROOT/.venv/bin/python}"' in source
    assert 'MAX_COVERAGE_REPAIR_ATTEMPTS="${MAX_COVERAGE_REPAIR_ATTEMPTS:-1}"' in source
    assert 'MAX_COVERAGE_REPAIR="$MAX_COVERAGE_REPAIR_ATTEMPTS"' in source
    assert '"max_coverage_repair_attempts": int(os.environ["MAX_COVERAGE_REPAIR"])' in source


def test_opencode_loop_runs_and_gates_verified_metrics_lifecycle() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    required = (
        'DO_VERIFICATION_METRICS="${DO_VERIFICATION_METRICS:-true}"',
        'VERIFICATION_METRICS_ENTRYPOINT="${VERIFICATION_METRICS_ENTRYPOINT:-metrics-nightly}"',
        "run_verification_metrics_stage()",
        "record_coverage_repair_result()",
        "declare -a VERIFICATION_METRICS_ROWS=()",
        "declare -a COVERAGE_REPAIR_ROWS=()",
        'record_coverage_repair_result "$change_id" || true',
        'run_verification_metrics_stage "$change_id" || true',
        "## Verification Metrics (M2–M4)",
        "## Coverage Repair Fast Loop",
        "benchmark_verification_metrics_exit_code",
        "benchmark_coverage_repair_exit_code",
    )
    for token in required:
        assert token in source

    resume_start = source.index('if [ "$workflow_kind" = "completed" ]; then')
    resume_coverage = source.index('record_coverage_repair_result "$change_id" || true', resume_start)
    resume_metrics = source.index('run_verification_metrics_stage "$change_id" || true', resume_coverage)
    resume_archive = source.index(
        'if benchmark_should_run_archive "$DO_ARCHIVE"',
        resume_metrics,
    )
    fresh_start = source.index('if [ "$workflow_kind" != "completed" ]; then', resume_archive)
    fresh_coverage = source.index('record_coverage_repair_result "$change_id" || true', fresh_start)
    fresh_metrics = source.index('run_verification_metrics_stage "$change_id" || true', fresh_coverage)
    fresh_archive = source.index(
        'if benchmark_should_run_archive "$DO_ARCHIVE"',
        fresh_metrics,
    )

    assert resume_coverage < resume_metrics < resume_archive
    assert fresh_coverage < fresh_metrics < fresh_archive
