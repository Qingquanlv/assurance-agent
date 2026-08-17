from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from assurance_agent import resources


_ROOT = Path(__file__).parents[3]
_OPENCODE_LOOP = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "run-workflow-loop.sh"
_BENCHMARK_ROOT = _ROOT / "benchmark" / "vue-fastapi-admin"


def test_opencode_loop_uses_canonical_batch_retro_and_eval_pipeline() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    loop_start = source.index(
        'for item in "${BENCHMARK_ITEMS[@]}"; do',
        source.index("total_items=${#BENCHMARK_ITEMS[@]}"),
    )
    loop_end = source.index("\ndone\n", loop_start)
    boundary_start = source.index("run_batch_knowledge_promotion_boundary() {", loop_end)
    boundary_end = source.index("# END batch knowledge promotion boundary", boundary_start)
    promotion = source.index("promote_batch_knowledge_proposals", boundary_start, boundary_end)
    retro = source.index("run_retro_collect", promotion, boundary_end)
    evaluation = source.index("run_benchmark_eval", retro, boundary_end)
    settled_guard = source.index('if batch_members_settled "$BATCH_MANIFEST"; then', boundary_end)
    boundary_call = source.index("run_batch_knowledge_promotion_boundary", settled_guard)

    assert boundary_start < promotion < retro < evaluation < boundary_end
    assert loop_end < settled_guard < boundary_call
    assert "initialize_retro_batch_manifest" in source
    assert 'retro_command+=(--adapter "$DRIVER_ADAPTER")' in source
    assert 'retro_command+=(--server "$OPENCODE_SERVER")' in source
    assert 'retro_command+=(--directory "$PROJECT_ROOT")' in source
    assert "collect_benchmark_eval_rows" in source
    assert "proposal-candidates.json" in source
    assert "auto-review-summary.json" in source
    for obsolete in (
        "DO_NIGHTLY_COLLECT",
        "DO_RETRO_PROPOSALS",
        "retro_proposals_prompt",
        "retro nightly",
        "proposals.json",
    ):
        assert obsolete not in source


def test_opencode_loop_uses_hard_timeout_managed_services_and_graph_archive() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    assert 'HARD_TIMEOUT_PY="$SCRIPT_DIR/run_with_hard_timeout.py"' in source
    assert "run_hard_timeout" in source
    assert "ensure_loop_sut || exit 1\nensure_loop_frontend || exit 1" in source
    assert 'FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"' in source
    assert 'stop_benchmark_sut "$FRONTEND_PID_FILE"' in source
    assert "run_archive_stage" in source
    assert 'ARCHIVE_ENTRYPOINT="${ARCHIVE_ENTRYPOINT:-archive}"' in source
    assert "benchmark_should_run_archive" in source
    assert "benchmark_result_exit_code" in source


def test_opencode_loop_keeps_explicit_denies_with_native_adapter() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    assert "--dangerously-skip-permissions" not in source
    assert '--adapter "$DRIVER_ADAPTER"' in source
    assert '--server "$OPENCODE_SERVER"' in source
    assert 'DRIVER_ADAPTER="${DRIVER_ADAPTER:-opencode}"' in source


def test_opencode_loop_validates_live_agent_policy_before_creating_changes() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    sync = source.index(
        '"$AA_BIN" skill refresh --sync-agents --sync-opencode-user-skills --sync-opencode-user-agents'
    )
    skill_hash_postcondition = source.index("verify_packaged_skills", sync)
    agent_hash_postcondition = source.index("verify_packaged_agents", skill_hash_postcondition)
    agent_preflight = source.index("validate_bounded_agent_server", sync)
    isolated_preflight = source.index("TemporaryDirectory", agent_preflight)
    isolated_agent_validation = source.index("validate_bounded_agent_server", isolated_preflight)
    skill_preflight = source.index("validate_packaged_skill_server", isolated_agent_validation)
    run_dir_creation = source.index('mkdir -p "$RUN_DIR" "$RESUME_LOG_DIR"', skill_preflight)
    tracking = source.index("setup_run_tracking", run_dir_creation)
    seed_change = source.index('    seed_change "$change_id"', tracking)

    assert "opencode_user_skills_root" in source[sync:agent_preflight]
    assert "opencode_user_agents_root" in source[sync:agent_preflight]
    assert "git" in source[isolated_preflight:isolated_agent_validation]
    assert "init" in source[isolated_preflight:isolated_agent_validation]
    assert (
        sync
        < skill_hash_postcondition
        < agent_hash_postcondition
        < agent_preflight
        < isolated_preflight
        < isolated_agent_validation
        < skill_preflight
        < run_dir_creation
        < tracking
        < seed_change
    )
    assert "restart OpenCode" in source[skill_preflight:run_dir_creation]
    assert "server working directory" in source[skill_preflight:run_dir_creation]
    assert "live boundary plugin" in source[skill_preflight:run_dir_creation]


def _isolated_opencode_loop(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    project = tmp_path / "sut"
    benchmark = project / "benchmark"
    benchmark.mkdir(parents=True)
    script = benchmark / "run-workflow-loop.sh"
    shutil.copy2(_OPENCODE_LOOP, script)
    shutil.copy2(_OPENCODE_LOOP.with_name("cursor-loop-helpers.sh"), benchmark)
    shutil.copy2(_OPENCODE_LOOP.with_name("run_with_hard_timeout.py"), benchmark)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name in ("aa", "curl", "opencode"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *validate_packaged_skill_server*)\n"
        '    [ -z "${FAKE_PREFLIGHT_MARKER:-}" ] || : >"$FAKE_PREFLIGHT_MARKER"\n'
        '    exit "${FAKE_SKILL_PREFLIGHT_EXIT:-0}"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    marker = tmp_path / "skill-preflight-ran"
    env = {
        **os.environ,
        "AA_BIN": str(fake_bin / "aa"),
        "AA_PYTHON": str(fake_python),
        "AA_REPO_ROOT": str(_ROOT),
        "BENCHMARK_ENV": str(tmp_path / "missing.env"),
        "DAEMON": "0",
        "DRIVER_ADAPTER": "opencode",
        "FAKE_PREFLIGHT_MARKER": str(marker),
        "OPENCODE_BIN": str(fake_bin / "opencode"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RESUME_RUNSTAMP": "preflight-order-test",
    }
    return script, env, marker


def test_live_preflight_failure_creates_no_run_tracking_or_change(tmp_path: Path) -> None:
    script, env, marker = _isolated_opencode_loop(tmp_path)
    env["FAKE_SKILL_PREFLIGHT_EXIT"] = "23"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=script.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert marker.is_file(), result.stdout + result.stderr
    assert not (script.parent / "runs/preflight-order-test-opencode").exists()
    assert not (script.parent.parent / "qa/changes").exists()


def test_opencode_daemon_mode_is_rejected_after_live_preflight_without_cursor_spawn(
    tmp_path: Path,
) -> None:
    script, env, marker = _isolated_opencode_loop(tmp_path)
    env["DAEMON"] = "1"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=script.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert marker.is_file(), result.stdout + result.stderr
    assert "DAEMON=1 is not supported by the OpenCode benchmark" in result.stderr
    assert "daemonize-loop.py" not in _OPENCODE_LOOP.read_text(encoding="utf-8")
    assert not (script.parent / "runs/preflight-order-test-opencode").exists()


def test_hybrid_model_routes_reasoning_to_glm_and_bulk_work_to_deepseek() -> None:
    config = yaml.safe_load((_BENCHMARK_ROOT / ".aa/config.yaml").read_text(encoding="utf-8"))
    routing = config["execution"]["model_routing"]
    routes = routing["routes"]

    assert routing["strict_routes"] is True
    for skill in (
        "aa-case-design",
        "aa-api-plan",
        "aa-e2e-plan",
        "aa-fuzz-plan",
        "aa-performance-plan",
        "aa-coverage-repair",
    ):
        assert routes[skill] == "anthropic/glm-5.2", skill
    for skill in (
        "aa-explore",
        "aa-fact-baseline",
        "aa-case-reviewer",
        "aa-api-plan-reviewer",
        "aa-e2e-plan-reviewer",
        "aa-fuzz-plan-reviewer",
        "aa-performance-plan-reviewer",
        "aa-api-codegen",
        "aa-api-codegen-fixer",
        "aa-e2e-codegen",
        "aa-e2e-codegen-fixer",
        "aa-fuzz-codegen",
        "aa-performance-codegen",
        "aa-fix-proposal",
        "aa-issue-analyzer",
        "aa-issue-triage-advisor",
        "aa-archive",
        "aa-retro-issue-analysis",
        "aa-retro-workflow-analysis",
        "aa-retro-eval-analysis",
        "aa-retro",
    ):
        assert routes[skill] == "anthropic/deepseek-v4-flash", skill
    assert routes["aa-improvement-reviewer"] == "anthropic/glm-5.2"
    assert routing["default"] == "anthropic/deepseek-v4-flash"
    assert routing["escalation"] == {
        "model": "anthropic/glm-5.2",
        "on_error_kinds": ["invalid_output", "forbidden_write"],
    }

    schema = yaml.safe_load(resources.read_text("schemas", "workflow-schema.yaml"))
    compiled_skills = {
        node["uses"].removeprefix("skill:")
        for graph in schema["graphs"].values()
        for node in (graph.get("nodes") or {}).values()
        if isinstance(node, dict) and str(node.get("uses", "")).startswith("skill:")
    }
    assert set(routes) == compiled_skills


def test_hybrid_benchmark_envs_do_not_pin_a_global_model() -> None:
    for name in ("benchmark.opencode-dept.env", "benchmark.opencode-user.env"):
        source = (_BENCHMARK_ROOT / "benchmark" / name).read_text(encoding="utf-8")
        assert 'OPENCODE_MODEL=""' in source, name
        assert 'OPENCODE_MODEL="anthropic/' not in source, name


def _agent_frontmatter(name: str) -> dict[str, object]:
    text = resources.read_text("opencode", "agents", f"{name}.md")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match is not None, name
    loaded = yaml.safe_load(match.group(1))
    assert isinstance(loaded, dict), name
    return loaded


def _sample_output_path(output: str) -> str:
    if output.startswith("change:"):
        suffix = output.removeprefix("change:")
        path = f"/workspace/qa/changes/CH-1/{suffix}"
    elif output.startswith("project:"):
        path = f"/workspace/{output.removeprefix('project:')}"
    else:
        raise AssertionError(f"unsupported agent output token: {output}")
    path = re.sub(r"\$\{[^}]+\}", "BOUND", path)
    return path.replace("**", "sample").rstrip("/") + ("/file" if output.endswith("/") else "")


def _edit_action(agent: str, path: str) -> str | None:
    permission = _agent_frontmatter(agent).get("permission")
    assert isinstance(permission, dict), agent
    edit = permission.get("edit")
    if isinstance(edit, str):
        return edit
    assert isinstance(edit, dict), agent
    action: str | None = None
    for pattern, candidate in edit.items():
        if fnmatch.fnmatchcase(path, str(pattern)):
            action = str(candidate)
    return action


def test_every_workflow_agent_write_is_allowed_by_its_runtime_agent() -> None:
    schema = yaml.safe_load(resources.read_text("schemas", "workflow-schema.yaml"))
    uncovered: list[str] = []

    for graph_name, graph in schema["graphs"].items():
        for node_name, node in (graph.get("nodes") or {}).items():
            if not isinstance(node, dict) or not node.get("agent"):
                continue
            agent = node["agent"]
            node_resources = node.get("resources") or {}
            declared_writes = set(node.get("outputs") or []) | set(node_resources.get("writes") or [])
            for output in declared_writes:
                path = _sample_output_path(output)
                if _edit_action(agent, path) != "allow":
                    uncovered.append(f"{agent}:{graph_name}.{node_name}:{output}")

    assert uncovered == []
