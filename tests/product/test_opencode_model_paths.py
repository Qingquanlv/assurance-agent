"""Model-facing paths must be reusable without weakening physical write confinement."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from assurance_product.opencode_agents import install_opencode_agents, workspace_binding_title

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

_WRITE_ROOT = "qa/.staging/task-1/attempt-1"
_LOGICAL = "qa/proposal.md"
_PRELUDE = """
import fs from "node:fs";
import { pathToFileURL } from "node:url";
const payload = JSON.parse(process.argv[2]);
const { default: createPlugin } = await import(pathToFileURL(process.argv[1]));
const hooks = await createPlugin({
  client: { session: { get: async ({path}) => ({data: payload.sessions[path.id]}) } },
});
const emit = (value) => process.stdout.write(JSON.stringify(value));
"""


@pytest.fixture
def model_workspace(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    project = tmp_path / "project"
    project.mkdir()
    baseline = project / _LOGICAL
    baseline.parent.mkdir()
    baseline.write_text("baseline\n", encoding="utf-8")
    staged = project / _WRITE_ROOT / _LOGICAL
    staged.parent.mkdir(parents=True)
    staged.write_text("staged\n", encoding="utf-8")
    _, plugin = install_opencode_agents(project)
    session: dict[str, object] = {
        "id": "ses-paths",
        "directory": str(project.resolve()),
        "agent": "assurance-v1-doc-author",
        "title": workspace_binding_title(
            session_id="ses-paths",
            agent_profile="assurance-v1-doc-author",
            project_root=project,
            write_root=_WRITE_ROOT,
            allowed_outputs=(_LOGICAL,),
            task_id="task-1",
            attempt=1,
            attempt_id="attempt-1",
        ),
    }
    return project, plugin, session


def _run(workspace: tuple[Path, Path, dict[str, object]], script: str, **data: Any) -> Any:
    _, plugin, session = workspace
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "--input-type=module",
            "-e",
            _PRELUDE + script,
            str(plugin),
            json.dumps({"sessions": {session["id"]: session}, **data}),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _after(workspace: tuple[Path, Path, dict[str, object]], tool: str, result: dict[str, Any]) -> Any:
    return _run(
        workspace,
        """
await hooks["tool.execute.after"]?.(
  {tool: payload.tool, sessionID: "ses-paths", callID: "call-1", args: {}}, payload.result,
);
emit(payload.result);
""",
        tool=tool,
        result=result,
    )


def test_read_path_can_be_reused_for_repeated_edits_without_touching_baseline(model_workspace) -> None:
    result = _run(
        model_workspace,
        """
const input = {tool: "read", sessionID: "ses-paths", callID: "read-1"};
const read = {args: {filePath: "qa/proposal.md"}};
await hooks["tool.execute.before"](input, read);
const result = {
  title: read.args.filePath,
  output: `<path>${read.args.filePath}</path>\n<content>${fs.readFileSync(read.args.filePath, "utf8")}</content>`,
  metadata: {display: {path: read.args.filePath}},
};
await hooks["tool.execute.after"]?.({...input, args: read.args}, result);
const modelPath = result.output.match(/^<path>(.*?)<\\/path>/)[1];
const attempts = [];
for (let index = 0; index < 2; index++) {
  const edit = {args: {filePath: modelPath, oldString: "staged", newString: "edited"}};
  try {
    await hooks["tool.execute.before"]({...input, tool: "edit", callID: `edit-${index}`}, edit);
    fs.writeFileSync(edit.args.filePath, `edited-${index}\n`);
    attempts.push({status: "allowed", path: edit.args.filePath});
  } catch (error) { attempts.push({status: "denied", error: error.message}); }
}
emit({modelPath, readPath: read.args.filePath, attempts});
""",
    )
    project, _, _ = model_workspace
    staged = project / _WRITE_ROOT / _LOGICAL
    assert result["modelPath"] == _LOGICAL
    assert result["readPath"] == str(staged)
    assert result["attempts"] == [{"status": "allowed", "path": str(staged)}] * 2
    assert staged.read_text() == "edited-1\n"
    assert (project / _LOGICAL).read_text() == "baseline\n"


def test_read_normalizes_only_path_fields_not_file_contents(model_workspace) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    contents = f"1: literal {physical}\n2: <path>{physical}</path>"
    result = _after(
        model_workspace,
        "read",
        {
            "title": f"{_WRITE_ROOT}/{_LOGICAL}",
            "output": f"<path>{physical}</path>\n<type>file</type>\n<content>\n{contents}\n</content>",
            "metadata": {"display": {"path": physical, "text": contents}, "preview": contents},
        },
    )
    assert result["title"] == _LOGICAL
    assert (
        result["output"] == f"<path>{_LOGICAL}</path>\n<type>file</type>\n<content>\n{contents}\n</content>"
    )
    assert result["metadata"] == {"display": {"path": _LOGICAL, "text": contents}, "preview": contents}


def test_read_keeps_loaded_instruction_paths_for_opencode_deduplication(model_workspace) -> None:
    project, _, _ = model_workspace
    instruction = str(project / "qa" / "AGENTS.md")
    result = _run(
        model_workspace,
        """
const result = {title: "qa/proposal.md", output: "read", metadata: {loaded: [payload.instruction]}};
await hooks["tool.execute.after"]?.(
  {tool: "read", sessionID: "ses-paths", callID: "read-1", args: {}}, result,
);
const persisted = structuredClone(result);
const messages = [{info: {sessionID: "ses-paths"}, parts: [{
  type: "tool", tool: "read", sessionID: "ses-paths",
  state: {status: "completed", input: {filePath: "qa/proposal.md"}, ...result},
}]}];
await hooks["experimental.chat.messages.transform"]?.({}, {messages});
// OpenCode tools retain this same messages array for Instruction.resolve().
const loaded = messages[0].parts[0].state.metadata.loaded;
emit({persisted, alreadyLoaded: new Set(loaded).has(payload.instruction)});
""",
        instruction=instruction,
    )
    assert result["persisted"]["metadata"]["loaded"] == [instruction]
    assert result["alreadyLoaded"] is True


@pytest.mark.parametrize("tool", ["glob", "grep"])
def test_search_results_expose_reusable_paths_without_changing_matches(model_workspace, tool: str) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    source = str(project / "app.py")
    output = (
        f"{physical}\n{source}" if tool == "glob" else f"Found 1 matches\n{physical}:\n  Line 3: {physical}"
    )
    result = _after(model_workspace, tool, {"title": "pattern", "output": output, "metadata": {}})
    expected = (
        f"{_LOGICAL}\napp.py" if tool == "glob" else f"Found 1 matches\n{_LOGICAL}:\n  Line 3: {physical}"
    )
    assert result["output"] == expected


def test_write_results_normalize_titles_and_metadata_without_changing_diff_hunks(model_workspace) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    patch = f"--- {physical}\n+++ {physical}\n@@ -1 +1 @@\n-{physical}\n+literal\n"
    result = _after(
        model_workspace,
        "edit",
        {
            "title": f"{_WRITE_ROOT}/{_LOGICAL}",
            "output": f'Edit applied successfully.\n<diagnostics file="{physical}">error</diagnostics>',
            "metadata": {"filediff": {"file": physical, "patch": patch}},
        },
    )
    assert result["title"] == _LOGICAL
    assert (
        result["output"]
        == 'Edit applied successfully.\n<diagnostics file="qa/proposal.md">error</diagnostics>'
    )
    assert result["metadata"]["filediff"]["file"] == _LOGICAL
    assert result["metadata"]["filediff"]["patch"] == patch.replace(physical, _LOGICAL, 2)


@pytest.mark.parametrize("status", ["completed", "error"])
def test_model_history_uses_logical_paths_without_mutating_borrowed_audit_parts(
    model_workspace, status
) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    state = {
        "status": status,
        "input": {"filePath": physical, "oldString": physical, "newString": "fixed"},
        "time": {"start": 1, "end": 2},
        **(
            {"output": "Edit applied successfully.", "title": physical, "metadata": {}}
            if status == "completed"
            else {"error": "Assurance write boundary: path is not allowed"}
        ),
    }
    part = {
        "type": "tool",
        "tool": "edit",
        "sessionID": "ses-paths",
        "id": "part-1",
        "callID": "call-1",
        "state": state,
    }
    result = _run(
        model_workspace,
        """
const messages = [{info: {sessionID: "ses-paths", role: "assistant"}, parts: [payload.part]}];
const borrowed = messages[0].parts[0];
await hooks["experimental.chat.messages.transform"]?.({}, {messages});
emit({model: messages[0].parts[0], borrowed});
""",
        part=part,
    )
    assert result["model"]["state"]["input"]["filePath"] == _LOGICAL
    assert result["model"]["state"]["input"]["oldString"] == physical
    assert result["borrowed"] == part


def test_apply_patch_history_changes_headers_only(model_workspace) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    patch = f"*** Begin Patch\n*** Update File: {physical}\n@@\n-{physical}\n+literal\n*** End Patch"
    result = _run(
        model_workspace,
        """
const messages = [{info: {sessionID: "ses-paths"}, parts: [{
  type: "tool", tool: "apply_patch", sessionID: "ses-paths",
  state: {status: "completed", input: {patchText: payload.patch},
    output: `Success. Updated the following files:\nM ${payload.physical}`, metadata: {}, title: "patch"},
}]}];
await hooks["experimental.chat.messages.transform"]?.({}, {messages});
emit(messages[0].parts[0].state);
""",
        patch=patch,
        physical=physical,
    )
    assert result["input"]["patchText"] == patch.replace(
        f"Update File: {physical}", f"Update File: {_LOGICAL}"
    )
    assert result["output"] == f"Success. Updated the following files:\nM {_LOGICAL}"


@pytest.mark.parametrize(
    ("tool", "error"),
    [
        ("read", "File not found: {path}"),
        ("read", "Error: File not found: {path}\n\nDid you mean one of these?\n{path}.bak"),
        ("read", "Cannot read binary file: {path}"),
        ("edit", "File {path} not found"),
        ("edit", "Path is a directory, not a file: {path}"),
        ("apply_patch", "apply_patch verification failed: Failed to read file to update: {path}"),
    ],
)
def test_failed_tool_history_does_not_echo_physical_path(model_workspace, tool, error) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    result = _run(
        model_workspace,
        """
const messages = [{info: {sessionID: "ses-paths"}, parts: [{
  type: "tool", tool: payload.tool, sessionID: "ses-paths",
  state: {status: "error", input: {filePath: payload.physical},
    error: payload.error},
}]}];
await hooks["experimental.chat.messages.transform"]?.({}, {messages});
emit(messages[0].parts[0].state);
""",
        physical=physical,
        tool=tool,
        error=error.format(path=physical),
    )
    assert result["input"]["filePath"] == _LOGICAL
    assert result["error"] == error.format(path=_LOGICAL)


@pytest.mark.parametrize(
    "error",
    [
        "apply_patch verification failed: Error: Failed to find expected lines in {path}:\n{literal}",
        "apply_patch verification failed: Error: Failed to find context '{literal}' in {path}",
    ],
)
def test_patch_error_normalizes_path_without_changing_expected_file_text(model_workspace, error) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    result = _run(
        model_workspace,
        """
const messages = [{info: {sessionID: "ses-paths"}, parts: [{
  type: "tool", tool: "apply_patch", sessionID: "ses-paths",
  state: {status: "error", input: {patchText: "patch"}, error: payload.error},
}]}];
await hooks["experimental.chat.messages.transform"]?.({}, {messages});
emit(messages[0].parts[0].state.error);
""",
        error=error.format(path=physical, literal=physical),
    )
    assert result == error.format(path=_LOGICAL, literal=physical)


@pytest.mark.parametrize("tool", ["write", "artifact_write"])
def test_history_keeps_artifact_bytes_and_non_tool_messages_unchanged(model_workspace, tool) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    result = _run(
        model_workspace,
        """
const user = {info: {sessionID: "ses-paths", role: "user"}, parts: [{type: "text", text: payload.physical}]};
const messages = [user, {info: {sessionID: "ses-paths"}, parts: [{
  type: "tool", tool: payload.tool, sessionID: "ses-paths",
  state: {status: "completed", input: {filePath: payload.physical, content: payload.physical},
    output: "Wrote file successfully.", metadata: {filepath: payload.physical}},
}]}];
await hooks["experimental.chat.messages.transform"]?.({}, {messages});
emit({user: messages[0], state: messages[1].parts[0].state});
""",
        physical=physical,
        tool=tool,
    )
    assert result["user"]["parts"][0]["text"] == physical
    assert result["state"]["input"] == {"filePath": _LOGICAL, "content": physical}
    assert result["state"]["metadata"]["filepath"] == _LOGICAL


@pytest.mark.parametrize(
    "candidate",
    [
        "qa/.staging/task-1/attempt-2/qa/proposal.md",
        "qa/.staging/task-1/attempt-10/qa/proposal.md",
        "../outside.md",
    ],
)
def test_display_normalization_does_not_launder_foreign_attempt_or_traversal_paths(
    model_workspace, candidate
) -> None:
    result = _after(
        model_workspace, "read", {"title": candidate, "output": f"<path>{candidate}</path>", "metadata": {}}
    )
    assert result["output"] == f"<path>{candidate}</path>"
    refused = _run(
        model_workspace,
        """
try {
  await hooks["tool.execute.before"](
    {tool: "write", sessionID: "ses-paths", callID: "denied"},
    {args: {filePath: payload.path, content: "not written"}},
  );
  emit({denied: false});
} catch (error) { emit({denied: true, error: error.message}); }
""",
        path=candidate,
    )
    assert refused["denied"] is True


def test_physical_current_attempt_path_is_still_not_write_authority(model_workspace) -> None:
    project, _, _ = model_workspace
    physical = str(project / _WRITE_ROOT / _LOGICAL)
    result = _run(
        model_workspace,
        """
try {
  await hooks["tool.execute.before"](
    {tool: "edit", sessionID: "ses-paths", callID: "denied"},
    {args: {filePath: payload.physical, oldString: "staged", newString: "changed"}},
  );
  emit({denied: false});
} catch (error) { emit({denied: true, error: error.message}); }
""",
        physical=physical,
    )
    assert result["denied"] is True
    assert (project / _WRITE_ROOT / _LOGICAL).read_text() == "staged\n"
