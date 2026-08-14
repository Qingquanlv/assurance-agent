from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from assurance_agent.workflow.graph.handlers.operation import link_host_task_paths
from assurance_agent.workflow.graph.models import RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace


_NODE = shutil.which("node")
_PLUGIN = Path(__file__).parents[2] / "assurance_agent/_resources/opencode/plugins/aa.mjs"
_HOOK_DRIVER = r"""
import { pathToFileURL } from "url";

const pluginPath = process.argv[1];
const payload = JSON.parse(process.argv[2]);
const { default: createPlugin } = await import(pathToFileURL(pluginPath).href);
const client = {
  session: {
    get: async () => {
      if (payload.sessionError) throw new Error("session lookup unavailable");
      return { data: { directory: payload.root, agent: payload.agent } };
    },
    messages: async () => {
      if (!payload.expectedSkill) return { data: [] };
      return {
        data: [{
          info: { role: "user" },
          parts: [{
            type: "text",
            text: `<system-reminder>\nCall skill(name='${payload.expectedSkill}').\n</system-reminder>`,
          }],
        }],
      };
    },
  },
};
const hooks = await createPlugin({ client, directory: payload.pluginDirectory });
try {
  const hookOutput = { args: payload.args };
  await hooks["tool.execute.before"](
    { tool: payload.tool, sessionID: "ses-test", callID: "call-test" },
    hookOutput,
  );
  process.stdout.write(payload.echoArgs ? `${JSON.stringify(hookOutput.args)}\n` : "ALLOW\n");
} catch (error) {
  process.stderr.write(`DENY: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 23;
}
"""

_ROUND_TRIP_HOOK_DRIVER = r"""
import { pathToFileURL } from "url";

const pluginPath = process.argv[1];
const payload = JSON.parse(process.argv[2]);
const client = {
  session: {
    get: async () => ({
      data: { directory: payload.root, agent: payload.agent },
    }),
    messages: async () => ({ data: [] }),
  },
};
const { default: createPlugin } = await import(pathToFileURL(pluginPath).href);
const hooks = await createPlugin({ client, directory: payload.pluginDirectory });
try {
  const beforeOutput = { args: payload.args };
  const input = { tool: payload.tool, sessionID: "ses-test", callID: "call-test" };
  await hooks["tool.execute.before"](input, beforeOutput);
  const afterOutput = { title: "", output: payload.toolOutput, metadata: {} };
  await hooks["tool.execute.after"](
    { ...input, args: beforeOutput.args },
    afterOutput,
  );
  process.stdout.write(`${afterOutput.output}\n`);
} catch (error) {
  process.stderr.write(`DENY: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 23;
}
"""

_PLUGIN_TOOLS_DRIVER = r"""
import { pathToFileURL } from "url";

const { default: createPlugin } = await import(pathToFileURL(process.argv[1]).href);
const hooks = await createPlugin({ client: {}, directory: process.cwd() });
process.stdout.write(`${JSON.stringify(Object.keys(hooks.tool ?? {}))}\n`);
"""

_SEQUENCE_HOOK_DRIVER = r"""
import { pathToFileURL } from "url";

const pluginPath = process.argv[1];
const payload = JSON.parse(process.argv[2]);
let activeCall = 0;
const client = {
  session: {
    get: async () => ({
      data: { directory: payload.root, agent: payload.agents[activeCall] },
    }),
    messages: async () => ({ data: [] }),
  },
};
const { default: createPlugin } = await import(pathToFileURL(pluginPath).href);
const hooks = await createPlugin({ client, directory: payload.root });
try {
  for (const [index, call] of payload.calls.entries()) {
    activeCall = index;
    await hooks["tool.execute.before"](
      { tool: call.tool, sessionID: "ses-shared", callID: `call-${index}` },
      { args: call.args },
    );
  }
  process.stdout.write("ALLOW\n");
} catch (error) {
  process.stderr.write(`DENY: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 23;
}
"""

_BOOTSTRAP_DRIVER = r"""
import { pathToFileURL } from "url";

const { default: createPlugin } = await import(pathToFileURL(process.argv[1]).href);
const hooks = await createPlugin({ client: {}, directory: process.cwd() });
const output = {
  messages: [{ info: { role: "user" }, parts: [{ type: "text", text: "hello" }] }],
};
await hooks["experimental.chat.messages.transform"]({}, output);
process.stdout.write(output.messages[0].parts[0].text);
"""


pytestmark = pytest.mark.skipif(_NODE is None, reason="node is required to execute the plugin")


def _run_hook(
    *,
    root: Path,
    tool: str,
    args: dict[str, object],
    agent: str = "aa-doc-author",
    echo_args: bool = False,
    expected_skill: str | None = None,
    session_error: bool = False,
    plugin_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = {
        "root": str(root),
        "pluginDirectory": str(plugin_directory or root),
        "agent": agent,
        "tool": tool,
        "args": args,
        "echoArgs": echo_args,
        "expectedSkill": expected_skill,
        "sessionError": session_error,
    }
    return subprocess.run(
        [_NODE or "node", "--input-type=module", "-e", _HOOK_DRIVER, str(_PLUGIN), json.dumps(payload)],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_tool_round_trip(
    *,
    root: Path,
    plugin_directory: Path,
    tool: str,
    args: dict[str, object],
    tool_output: str,
    agent: str = "aa-doc-author",
) -> subprocess.CompletedProcess[str]:
    payload = {
        "root": str(root),
        "pluginDirectory": str(plugin_directory),
        "agent": agent,
        "tool": tool,
        "args": args,
        "toolOutput": tool_output,
    }
    return subprocess.run(
        [
            _NODE or "node",
            "--input-type=module",
            "-e",
            _ROUND_TRIP_HOOK_DRIVER,
            str(_PLUGIN),
            json.dumps(payload),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _link_real_host_paths(host: Path, task: Path) -> None:
    host_change = host / "qa/changes/CH-1"
    task_change = task / "qa/changes/CH-1"
    host_change.mkdir(parents=True)
    task_change.mkdir(parents=True)
    (host / ".opencode/agents").mkdir(parents=True)
    (host / ".opencode/agents/aa-doc-author.md").write_text("agent\n", encoding="utf-8")
    (host / ".venv/bin").mkdir(parents=True)
    (host / ".venv/bin/python").write_text("runtime\n", encoding="utf-8")
    (host / "node_modules/package").mkdir(parents=True)
    (host / "node_modules/package/index.js").write_text("module\n", encoding="utf-8")
    (host_change / "events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    workspace = TaskWorkspace(
        task_id="task-1",
        root=task,
        project_root=task,
        repo_root=task,
        change_dir=task_change,
        base_tree_id="0" * 64,
    )
    context = RuntimeContext(
        project_root=host,
        repo_root=host,
        change_dir=host_change,
        change_id="CH-1",
    )
    link_host_task_paths(workspace, context)


def _run_hook_sequence(
    *,
    root: Path,
    agents: list[str],
    calls: list[dict[str, object]],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _NODE or "node",
            "--input-type=module",
            "-e",
            _SEQUENCE_HOOK_DRIVER,
            str(_PLUGIN),
            json.dumps({"root": str(root), "agents": agents, "calls": calls}),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("grep", {"pattern": "secret", "path": "{outside}"}),
        ("glob", {"pattern": "**/*", "path": "{outside}"}),
        ("list", {"path": "{outside}"}),
        ("read", {"filePath": "{outside}/secret.txt"}),
        ("write", {"filePath": "{outside}/new.txt", "content": "x"}),
        ("edit", {"filePath": "{outside}/secret.txt", "oldString": "x", "newString": "y"}),
        ("ast_grep_search", {"pattern": "$X", "lang": "python", "paths": ["{outside}"]}),
        (
            "ast_grep_replace",
            {
                "pattern": "$X",
                "rewrite": "$Y",
                "lang": "python",
                "paths": ["{outside}"],
            },
        ),
    ],
)
def test_bounded_agent_rejects_filesystem_tool_path_outside_session_root(
    tmp_path: Path,
    tool: str,
    args: dict[str, object],
) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    def substitute(value: object) -> object:
        if isinstance(value, str):
            return value.format(outside=outside)
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    concrete_args = {key: substitute(value) for key, value in args.items()}

    completed = _run_hook(root=root, tool=tool, args=concrete_args)

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_allows_apply_patch_when_every_path_is_in_session_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task"
    root.mkdir()
    (root / "existing.txt").write_text("before\n", encoding="utf-8")

    completed = _run_hook(
        root=root,
        tool="apply_patch",
        args={
            "patchText": (
                "*** Begin Patch\n"
                "*** Update File: existing.txt\n"
                "*** Move to: moved.txt\n"
                "@@\n"
                "-before\n"
                "+after\n"
                "*** Add File: added.txt\n"
                "+added\n"
                "*** End Patch"
            ),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


@pytest.mark.parametrize(
    "path_header",
    ["Add File", "Update File", "Delete File", "Move to"],
)
def test_bounded_agent_rejects_apply_patch_path_outside_session_root(
    tmp_path: Path,
    path_header: str,
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="apply_patch",
        args={
            "patchText": (
                "*** Begin Patch\n"
                + ("*** Update File: inside.txt\n" if path_header == "Move to" else "")
                + f"*** {path_header}: ../outside.txt\n"
                + "*** End Patch"
            ),
        },
    )

    assert completed.returncode == 23
    assert "path escapes the session directory" in completed.stderr


@pytest.mark.parametrize(
    "patch_text",
    [
        "*** Begin Patch\n*** End Patch",
        "*** Update File: inside.txt",
        "*** Begin Patch\n*** Update File:\n*** End Patch",
    ],
)
def test_bounded_agent_rejects_apply_patch_without_a_valid_file_envelope(
    tmp_path: Path,
    patch_text: str,
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="apply_patch",
        args={"patchText": patch_text},
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_rejects_symlink_that_resolves_outside_session_root(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "framework-link").symlink_to(outside, target_is_directory=True)

    completed = _run_hook(
        root=root,
        tool="grep",
        args={"pattern": "secret", "path": "framework-link"},
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_rejects_dangling_symlink_that_targets_outside_session_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside / "created-by-write.txt")

    completed = _run_hook(
        root=root,
        tool="write",
        args={"filePath": "escape", "content": "secret"},
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_rejects_glob_that_could_follow_an_escaping_symlink(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "framework-link").symlink_to(outside, target_is_directory=True)

    completed = _run_hook(
        root=root,
        tool="glob",
        args={"pattern": "framework-link/**/*"},
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_allows_glob_with_symlink_contained_in_session_root(tmp_path: Path) -> None:
    root = tmp_path / "task"
    target = root / "source"
    target.mkdir(parents=True)
    (target / "module.py").write_text("value = 1\n", encoding="utf-8")
    (root / "source-link").symlink_to(target, target_is_directory=True)

    completed = _run_hook(
        root=root,
        tool="glob",
        args={"pattern": "source-link/**/*.py"},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_bounded_agent_rejects_glob_with_deep_escaping_symlink(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()
    nested = root
    for depth in range(21):
        nested = nested / f"d{depth}"
        nested.mkdir()
    (nested / "escape").symlink_to(outside, target_is_directory=True)

    completed = _run_hook(
        root=root,
        tool="glob",
        args={"pattern": "**/*"},
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_root_glob_filters_external_results_after_clean_preflight(
    tmp_path: Path,
) -> None:
    task = tmp_path / "task"
    outside = tmp_path / "outside"
    task.mkdir()
    outside.mkdir()
    (task / "source").mkdir()
    internal = task / "source/module.py"
    internal.write_text("value = 1\n", encoding="utf-8")
    external = outside / "secret.py"
    external.write_text("secret = True\n", encoding="utf-8")

    completed = _run_tool_round_trip(
        root=task,
        plugin_directory=task,
        tool="glob",
        args={"pattern": "**/*"},
        tool_output=f"Found 2 file(s)\n\n{external}\n{internal}",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"Found 1 file(s)\n\n{internal}\n"
    assert str(external) not in completed.stdout


def test_root_glob_allows_only_runtime_links_created_for_the_agent_workspace(tmp_path: Path) -> None:
    host = tmp_path / "host"
    task = tmp_path / "task"
    host.mkdir()
    task.mkdir()
    _link_real_host_paths(host, task)

    completed = _run_hook(
        root=task,
        plugin_directory=host,
        tool="glob",
        args={"pattern": "**/*"},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_root_glob_rejects_nested_escape_inside_runtime_named_link(tmp_path: Path) -> None:
    host = tmp_path / "host"
    task = tmp_path / "task"
    outside = tmp_path / "outside"
    host.mkdir()
    task.mkdir()
    outside.mkdir()
    _link_real_host_paths(host, task)
    (host / ".opencode" / "nested-out").symlink_to(outside, target_is_directory=True)

    completed = _run_hook(
        root=task,
        plugin_directory=host,
        tool="glob",
        args={"pattern": "**/*"},
    )

    assert completed.returncode == 23
    assert "glob symlink audit failed" in completed.stderr


def test_root_glob_rejects_runtime_named_link_to_wrong_host_target(tmp_path: Path) -> None:
    host = tmp_path / "host"
    task = tmp_path / "task"
    wrong = tmp_path / "wrong"
    host.mkdir()
    task.mkdir()
    wrong.mkdir()
    (task / ".opencode").symlink_to(wrong, target_is_directory=True)

    completed = _run_hook(
        root=task,
        plugin_directory=host,
        tool="glob",
        args={"pattern": "**/*"},
    )

    assert completed.returncode == 23
    assert "glob symlink audit failed" in completed.stderr


def test_root_glob_rejects_host_runtime_link_whose_host_path_also_escapes(
    tmp_path: Path,
) -> None:
    host = tmp_path / "host"
    task = tmp_path / "task"
    outside = tmp_path / "outside"
    host.mkdir()
    task.mkdir()
    outside.mkdir()
    (host / ".opencode").symlink_to(outside, target_is_directory=True)
    (task / ".opencode").symlink_to(host / ".opencode", target_is_directory=True)

    completed = _run_hook(
        root=task,
        plugin_directory=host,
        tool="glob",
        args={"pattern": "**/*"},
    )

    assert completed.returncode == 23
    assert "glob symlink audit failed" in completed.stderr


def test_bounded_agent_rejects_file_url_outside_session_root(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    completed = _run_hook(root=root, tool="read", args={"filePath": secret.as_uri()})

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_allows_relative_path_inside_session_root(tmp_path: Path) -> None:
    root = tmp_path / "task"
    (root / "app").mkdir(parents=True)

    completed = _run_hook(root=root, tool="grep", args={"pattern": "user", "path": "app"})

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


@pytest.mark.parametrize(
    ("tool", "args", "root_field"),
    [
        ("grep", {"pattern": "user"}, "path"),
        ("glob", {"pattern": "**/*.py"}, "path"),
        ("list", {}, "path"),
        ("ast_grep_search", {"pattern": "$X", "lang": "python"}, "paths"),
        (
            "ast_grep_replace",
            {"pattern": "$X", "rewrite": "$Y", "lang": "python"},
            "paths",
        ),
    ],
)
def test_bounded_agent_binds_implicit_search_path_to_session_root(
    tmp_path: Path,
    tool: str,
    args: dict[str, object],
    root_field: str,
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(root=root, tool=tool, args=args, echo_args=True)

    assert completed.returncode == 0, completed.stderr
    actual_args = json.loads(completed.stdout)
    expected_root: object = [str(root)] if root_field == "paths" else str(root)
    assert actual_args[root_field] == expected_root


@pytest.mark.parametrize(
    ("tool", "args", "path_field", "expected_relative"),
    [
        ("grep", {"pattern": "user", "path": "source"}, "path", "source"),
        ("read", {"filePath": "source/module.py"}, "filePath", "source/module.py"),
        (
            "ast_grep_search",
            {"pattern": "$X", "lang": "python", "paths": ["source"]},
            "paths",
            "source",
        ),
        (
            "lsp_symbols",
            {"filePath": "source/module.py", "scope": "document"},
            "filePath",
            "source/module.py",
        ),
    ],
)
def test_bounded_agent_canonicalizes_relative_tool_paths_before_execution(
    tmp_path: Path,
    tool: str,
    args: dict[str, object],
    path_field: str,
    expected_relative: str,
) -> None:
    root = tmp_path / "task"
    (root / "source").mkdir(parents=True)
    (root / "source/module.py").write_text("value = 1\n", encoding="utf-8")

    completed = _run_hook(root=root, tool=tool, args=args, echo_args=True)

    assert completed.returncode == 0, completed.stderr
    actual_args = json.loads(completed.stdout)
    expected_path = str(root / expected_relative)
    expected: object = [expected_path] if path_field == "paths" else expected_path
    assert actual_args[path_field] == expected


def test_bounded_filesystem_tool_fails_closed_when_session_root_cannot_be_loaded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="grep",
        args={"pattern": "user"},
        session_error=True,
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            "skill_mcp",
            {
                "mcp_name": "playwright",
                "tool_name": "browser_run_code_unsafe",
                "arguments": {
                    "code": "async (page) => { await page.goto('file:///etc/passwd'); }",
                },
            },
        ),
        (
            "playwright-browser_browser_run_code_unsafe",
            {"code": "async (page) => { await page.goto('file:///etc/passwd'); }"},
        ),
        ("interactive_bash", {"tmux_command": "cat /etc/passwd"}),
        ("monitor_start", {"command": "cat /etc/passwd"}),
        ("task", {"subagent_type": "explore", "prompt": "read /etc/passwd"}),
        ("call_omo_agent", {"subagent_type": "explore", "prompt": "read /etc/passwd"}),
        ("look_at", {"file_path": "/etc/passwd"}),
        ("session_list", {}),
        ("session_read", {"session_id": "ses-other"}),
        ("session_search", {"query": "api key"}),
        ("session_info", {"session_id": "ses-other"}),
        ("background_output", {"task_id": "bg-other"}),
        ("background_cancel", {"taskId": "bg-other"}),
        (
            "apply_patch",
            {
                "patchText": "*** Begin Patch\n*** Update File: /tmp/outside.txt\n*** End Patch",
            },
        ),
    ],
)
def test_bounded_agent_rejects_tools_that_can_bypass_the_filesystem_boundary(
    tmp_path: Path,
    tool: str,
    args: dict[str, object],
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(root=root, tool=tool, args=args, agent="aa-test-author")

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


@pytest.mark.parametrize(
    "tool",
    ["task_create", "task_get", "task_list", "task_update", "experimental_future_tool"],
)
def test_bounded_agent_rejects_every_non_allowlisted_tool(tmp_path: Path, tool: str) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(root=root, tool=tool, args={}, agent="aa-doc-author")

    assert completed.returncode == 23
    assert "unavailable to bounded AA agents" in completed.stderr


@pytest.mark.parametrize(
    ("agent", "command"),
    [
        ("aa-archiver", "cp -R /etc qa/archive/CH-1"),
        ("aa-archiver", "cp -R qa/changes/CH-1 /tmp/archive"),
        ("aa-archiver", "cp -R qa/changes/CH-1 qa/archive/CH-1; cat /etc/passwd"),
        ("aa-explorer", "aa risk context --change CH-1 --project-dir /tmp/other"),
        ("aa-explorer", "aa risk context --change CH-1 --output-dir ../outside"),
        ("aa-explorer", "aa risk context --change CH-1 && cat /etc/passwd"),
    ],
)
def test_bounded_agent_rejects_bash_argv_or_paths_outside_session_root(
    tmp_path: Path,
    agent: str,
    command: str,
) -> None:
    root = tmp_path / "task"
    (root / "qa/changes/CH-1").mkdir(parents=True)
    (root / "qa/archive").mkdir(parents=True)

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command},
        agent=agent,
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_explorer_allows_structured_risk_command_bound_to_session_root(tmp_path: Path) -> None:
    root = tmp_path / "task root"
    (root / "requirements").mkdir(parents=True)
    (root / "requirements/change.md").write_text("requirement\n", encoding="utf-8")
    command = (
        "aa risk context --change CH-1 "
        f"--project-dir '{root}' --requirement requirements/change.md "
        "--output-dir qa/changes/CH-1/explore"
    )

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command},
        agent="aa-explorer",
        echo_args=True,
    )

    assert completed.returncode == 0, completed.stderr
    rewritten = json.loads(completed.stdout)["command"]
    assert str(root) in rewritten
    assert str(root / "requirements/change.md") in rewritten
    assert str(root / "qa/changes/CH-1/explore") in rewritten


def test_doc_author_allows_structured_artifact_write_to_its_change_surface(tmp_path: Path) -> None:
    root = tmp_path / "task root"
    (root / "qa/changes/CH-1/cases").mkdir(parents=True)
    command = (
        "aa artifact write --path qa/changes/CH-1/cases/case.yaml "
        f"--project-dir '{root}' --payload-base64 Y2FzZXM6IFtdCg=="
    )

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command},
        agent="aa-doc-author",
        echo_args=True,
    )

    assert completed.returncode == 0, completed.stderr
    rewritten = json.loads(completed.stdout)["command"]
    assert str(root / "qa/changes/CH-1/cases/case.yaml") in rewritten


def test_doc_author_allows_native_artifact_write_to_its_change_surface(tmp_path: Path) -> None:
    root = tmp_path / "task root"
    (root / "qa/changes/CH-1/review").mkdir(parents=True)

    completed = _run_hook(
        root=root,
        tool="artifact_write",
        args={"path": "qa/changes/CH-1/review/result.json", "content": "{}\n"},
        agent="aa-doc-author",
        echo_args=True,
    )

    assert completed.returncode == 0, completed.stderr
    rewritten = json.loads(completed.stdout)
    assert rewritten["path"] == str(root / "qa/changes/CH-1/review/result.json")


def test_native_artifact_write_rejects_agent_forbidden_path(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="artifact_write",
        args={"path": "src/product.py", "content": "forged\n"},
        agent="aa-doc-author",
    )

    assert completed.returncode == 23
    assert "artifact path is not allowed" in completed.stderr


@pytest.mark.parametrize(
    "target",
    [
        "qa/changes/CH-1/workflow-state.yaml",
        "qa/changes/CH-1/report/quality-report.md",
        "src/product.py",
        "../outside.md",
    ],
)
def test_doc_author_rejects_structured_artifact_write_outside_its_surface(
    tmp_path: Path,
    target: str,
) -> None:
    root = tmp_path / "task"
    root.mkdir()
    command = f"aa artifact write --path {target} --project-dir '{root}' --payload-base64 eAo="

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command},
        agent="aa-doc-author",
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_explorer_allows_exact_advisory_json_heredoc_bound_to_session_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task root"
    (root / "qa/changes/CH-1/explore").mkdir(parents=True)
    payload = json.dumps({"change_id": "CH-1", "value": "$(not executed)"}, indent=2)
    command = f"tee qa/changes/CH-1/explore/advisory.json >/dev/null <<'JSON'\n{payload}\nJSON"

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command},
        agent="aa-explorer",
        echo_args=True,
    )

    assert completed.returncode == 0, completed.stderr
    rewritten = json.loads(completed.stdout)["command"]
    assert str(root / "qa/changes/CH-1/explore/advisory.json") in rewritten
    assert "<<'JSON'" in rewritten
    assert "$(not executed)" in rewritten


@pytest.mark.parametrize(
    "command",
    [
        "tee qa/changes/CH-1/context.json >/dev/null <<'JSON'\n{}\nJSON",
        "tee qa/changes/CH-1/explore/advisory.json >/dev/null <<'JSON'\nnot-json\nJSON",
        ('tee qa/changes/CH-1/explore/advisory.json >/dev/null <<\'JSON\'\n{"change_id":"OTHER"}\nJSON'),
        (
            "tee qa/changes/CH-1/explore/advisory.json >/dev/null <<'JSON'\n"
            '{"change_id":"CH-1","value":"safe"}\nJSON\ncat /etc/passwd'
        ),
    ],
)
def test_explorer_rejects_unsafe_advisory_heredoc(tmp_path: Path, command: str) -> None:
    root = tmp_path / "task"
    (root / "qa/changes/CH-1/explore").mkdir(parents=True)

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command},
        agent="aa-explorer",
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_archiver_allows_structured_copy_with_contained_source_and_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task"
    (root / "qa/changes/CH-1/plans").mkdir(parents=True)
    (root / "qa/archive").mkdir(parents=True)

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": "cp -R qa/changes/CH-1/plans qa/archive/CH-1/plans"},
        agent="aa-archiver",
        echo_args=True,
    )

    assert completed.returncode == 0, completed.stderr
    rewritten = json.loads(completed.stdout)["command"]
    assert str(root / "qa/changes/CH-1/plans") in rewritten
    assert str(root / "qa/archive/CH-1/plans") in rewritten


@pytest.mark.parametrize("symlinked_prefix", ["changes", "archive"])
def test_archiver_rejects_symlinked_policy_prefix_even_when_target_stays_inside_session(
    tmp_path: Path,
    symlinked_prefix: str,
) -> None:
    root = tmp_path / "task"
    (root / "qa").mkdir(parents=True)
    (root / "redirected/CH-1/plans").mkdir(parents=True)
    ordinary_prefix = "archive" if symlinked_prefix == "changes" else "changes"
    (root / f"qa/{ordinary_prefix}/CH-1/plans").mkdir(parents=True)
    (root / f"qa/{symlinked_prefix}").symlink_to(
        root / "redirected",
        target_is_directory=True,
    )

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": "cp -R qa/changes/CH-1/plans qa/archive/CH-1/plans"},
        agent="aa-archiver",
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_explorer_rejects_risk_output_outside_declared_explore_tree(tmp_path: Path) -> None:
    root = tmp_path / "task"
    (root / "app").mkdir(parents=True)

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": "aa risk context --change CH-1 --output-dir app"},
        agent="aa-explorer",
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


@pytest.mark.parametrize(
    ("agent", "command"),
    [
        ("aa-reporter", "aa --version"),
        ("aa-reporter", "aa report generate --change CH-1"),
        ("aa-reviewer", "aa --version"),
        ("aa-reviewer", "aa report inspect --change CH-1"),
        (
            "aa-intake-host",
            "aa decide --change CH-1 --at execution.test-changes "
            "--action allow_test_changes --reason 'approved by user'",
        ),
        (
            "aa-intake-host",
            "aa state configure --change CH-1 --params-json '{{\"run_tests\":true}}' "
            "--orchestrator aa-intake",
        ),
        (
            "aa-intake-host",
            "aa risk validate-advisory --change CH-1 --project-dir '{root}'",
        ),
        (
            "aa-explorer",
            "aa risk write-advisory --change CH-1 --project-dir '{root}' "
            "--payload-base64 eyJjaGFuZ2VfaWQiOiJDSE0xIn0=",
        ),
    ],
)
def test_bounded_agent_allows_only_its_structured_cli_surface(
    tmp_path: Path,
    agent: str,
    command: str,
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="bash",
        args={"command": command.format(root=root)},
        agent=agent,
        echo_args=True,
    )

    assert completed.returncode == 0, completed.stderr
    rewritten = json.loads(completed.stdout)["command"]
    assert rewritten.startswith("'aa' ")


def test_non_aa_agent_keeps_access_to_unknown_plugin_tool(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="experimental_future_tool",
        args={"anything": True},
        agent="build",
    )

    assert completed.returncode == 0, completed.stderr


def test_bounded_agent_rejects_skill_other_than_phase_declared_skill(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="skill",
        args={"name": "aa-dashboard"},
        agent="aa-test-author",
        expected_skill="aa-performance-codegen",
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_bounded_agent_allows_phase_declared_skill(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="skill",
        args={"name": "aa-performance-codegen"},
        agent="aa-test-author",
        expected_skill="aa-performance-codegen",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_bounded_phase_agent_cannot_start_another_workflow(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="workflow_start",
        args={"change_id": "OTHER", "entrypoint": "full"},
        agent="aa-doc-author",
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_phase_worker_fails_closed_when_prompt_has_no_declared_skill(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="skill",
        args={"name": "aa-dashboard"},
        agent="aa-test-author",
    )

    assert completed.returncode == 23
    assert "phase-declared skill" in completed.stderr


def test_interactive_intake_host_can_load_skill_without_graph_phase_prompt(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="skill",
        args={"name": "aa-intake"},
        agent="aa-intake-host",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_interactive_intake_host_cannot_load_unrelated_skill_without_phase_prompt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="skill",
        args={"name": "aa-dashboard"},
        agent="aa-intake-host",
    )

    assert completed.returncode == 23
    assert "phase-declared skill" in completed.stderr


def test_interactive_intake_host_can_start_workflow(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="workflow_start",
        args={"change_id": "REQ-1", "entrypoint": "execute"},
        agent="aa-intake-host",
    )

    assert completed.returncode == 0, completed.stderr


def test_session_agent_switch_does_not_reuse_stale_unbounded_persona(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    completed = _run_hook_sequence(
        root=root,
        agents=["build", "aa-doc-author"],
        calls=[
            {"tool": "read", "args": {"filePath": str(root / "inside.txt")}},
            {"tool": "read", "args": {"filePath": str(outside / "secret.txt")}},
        ],
    )

    assert completed.returncode == 23
    assert "AA sandbox boundary" in completed.stderr


def test_session_agent_switch_does_not_restrict_new_unbounded_persona(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    completed = _run_hook_sequence(
        root=root,
        agents=["aa-doc-author", "build"],
        calls=[
            {"tool": "read", "args": {"filePath": str(root / "inside.txt")}},
            {"tool": "read", "args": {"filePath": str(outside / "secret.txt")}},
        ],
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_non_aa_agent_is_not_restricted_by_aa_session_boundary(tmp_path: Path) -> None:
    root = tmp_path / "task"
    outside = tmp_path / "framework"
    root.mkdir()
    outside.mkdir()

    completed = _run_hook(
        root=root,
        tool="grep",
        args={"pattern": "user", "path": str(outside)},
        agent="build",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_non_aa_agent_can_use_tool_blocked_only_for_bounded_agents(tmp_path: Path) -> None:
    root = tmp_path / "task"
    root.mkdir()

    completed = _run_hook(
        root=root,
        tool="skill_mcp",
        args={
            "mcp_name": "playwright",
            "tool_name": "browser_run_code_unsafe",
            "arguments": {"code": "async (page) => page.url()"},
        },
        agent="build",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ALLOW\n"


def test_bootstrap_renders_yaml_block_scalar_descriptions() -> None:
    completed = subprocess.run(
        [
            _NODE or "node",
            "--input-type=module",
            "-e",
            _BOOTSTRAP_DRIVER,
            str(_PLUGIN),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    api_codegen = next(
        line for line in completed.stdout.splitlines() if line.startswith("- **aa-api-codegen**:")
    )
    dashboard = next(line for line in completed.stdout.splitlines() if line.startswith("- **aa-dashboard**:"))
    assert "Triggers on:" in api_codegen
    assert not api_codegen.endswith(">-")
    assert "Launch the Case Center browser dashboard" in dashboard
    assert not dashboard.endswith(">")


def test_plugin_registers_byte_bound_live_boundary_marker() -> None:
    completed = subprocess.run(
        [
            _NODE or "node",
            "--input-type=module",
            "-e",
            _PLUGIN_TOOLS_DRIVER,
            str(_PLUGIN),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    digest = hashlib.sha256(_PLUGIN.read_bytes()).hexdigest()
    assert json.loads(completed.stdout) == [f"aa_boundary_probe_v1_{digest}"]
