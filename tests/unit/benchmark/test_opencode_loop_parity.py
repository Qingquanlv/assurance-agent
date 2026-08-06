from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

from assurance_agent import resources


_ROOT = Path(__file__).parents[3]
_OPENCODE_LOOP = _ROOT / "benchmark" / "vue-fastapi-admin" / "benchmark" / "run-workflow-loop.sh"


def test_opencode_loop_uses_canonical_batch_retro_and_eval_pipeline() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    loop_start = source.index('for item in "${BENCHMARK_ITEMS[@]}"; do', source.index("# Main loop"))
    loop_end = source.index("\ndone\n\nretro_id=", loop_start)
    settled_guard = source.index('if batch_members_settled "$BATCH_MANIFEST"; then', loop_end)
    promotion = source.index("promote_batch_knowledge_proposals", settled_guard)
    retro = source.index("run_retro_collect", promotion)
    evaluation = source.index("run_benchmark_eval", retro)

    assert loop_end < settled_guard < promotion < retro < evaluation
    assert "initialize_retro_batch_manifest" in source
    assert "run_batch_retro" in source
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


def test_opencode_loop_keeps_explicit_denies_under_auto_mode() -> None:
    source = _OPENCODE_LOOP.read_text(encoding="utf-8")

    assert "--dangerously-skip-permissions" not in source
    assert "--agent aa-doc-author --auto" in source
    assert "--attach $OPENCODE_SERVER" in source
    assert '--adapter "$DRIVER_ADAPTER"' in source
    assert '--server "$OPENCODE_SERVER"' in source
    assert 'DRIVER_ADAPTER="${DRIVER_ADAPTER:-opencode}"' in source


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
