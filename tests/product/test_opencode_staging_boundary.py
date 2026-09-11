from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from agent_runtime_contracts import AgentWorkspaceV1
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import InvocationMetadata, TaskContext, TaskWorkspaceIdentity

from assurance_product.opencode_agents import install_opencode_agents, workspace_binding_title


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

_SESSION_ID = "ses-bound"
_AGENT = "assurance-v1-doc-author"
_WRITE_ROOT = "qa/.staging/task-1/attempt-1"
_ALLOWED = "qa/proposal.md"
_OTHER_NODE = "qa/results/review/review.md"
_EXECUTION_VIEW = (
    f"{_WRITE_ROOT}/qa/.staging/execution/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
_DRIVER = r"""
import { pathToFileURL } from "node:url";
const pluginPath = process.argv[1];
const payload = JSON.parse(process.argv[2]);
const { default: createPlugin } = await import(pathToFileURL(pluginPath).href);
const hooks = await createPlugin({
  client: { session: { get: async () => ({ data: payload.session }) } },
});
const output = { args: payload.args };
try {
  await hooks["tool.execute.before"](
    { tool: payload.tool, sessionID: payload.session.id, callID: "call-test" },
    output,
  );
  process.stdout.write(JSON.stringify({ status: "ALLOW", args: output.args }) + "\n");
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 23;
}
"""


def _binding_title(
    project: Path,
    *,
    session_id: str = _SESSION_ID,
    agent_profile: str = _AGENT,
    write_root: str = _WRITE_ROOT,
    allowed_outputs: tuple[str, ...] = (_ALLOWED,),
    read_roots: tuple[str, ...] = (),
    task_id: str = "task-1",
    attempt: int = 1,
    attempt_id: str = "attempt-1",
) -> str:
    return workspace_binding_title(
        session_id=session_id,
        agent_profile=agent_profile,
        project_root=project,
        write_root=write_root,
        allowed_outputs=allowed_outputs,
        task_id=task_id,
        attempt=attempt,
        attempt_id=attempt_id,
        read_roots=read_roots,
    )


def _session(
    project: Path, title: str, *, agent: str = _AGENT, session_id: str = _SESSION_ID
) -> dict[str, object]:
    return {
        "id": session_id,
        "directory": str(project.resolve()),
        "agent": agent,
        "title": title,
    }


def _run(
    plugin: Path,
    *,
    session: Mapping[str, object],
    tool: str,
    args: Mapping[str, object],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            shutil.which("node") or "node",
            "--input-type=module",
            "-e",
            _DRIVER,
            str(plugin),
            json.dumps({"session": session, "tool": tool, "args": args}),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _allowed(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ALLOW"
    assert isinstance(payload["args"], dict)
    return payload["args"]


def _denied(result: subprocess.CompletedProcess[str], fragment: str) -> None:
    assert result.returncode == 23
    assert fragment in result.stderr


def _staged(project: Path, logical: str, write_root: str = _WRITE_ROOT) -> Path:
    return (project / write_root / logical).resolve()


def _install(project: Path) -> Path:
    _config, plugin = install_opencode_agents(project)
    return plugin


@pytest.mark.parametrize(
    "name", ["retro", "retro-eval-analysis", "retro-issue-analysis", "retro-workflow-analysis"]
)
def test_retro_contract_output_passes_both_profile_and_session_boundaries(tmp_path: Path, name: str) -> None:
    from fnmatch import fnmatchcase
    from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS
    from assurance_product.opencode_agents import _opencode_config

    contract = AGENT_JOB_CONTRACTS[name]
    logical = contract.resources.writes[0].format(change_id="CH-1")
    profile = json.loads(_opencode_config())["agent"][contract.agent_profile]
    action = "deny"
    for pattern, rule in profile["permission"]["edit"].items():
        if fnmatchcase(logical, pattern):
            action = rule
    assert action == "allow"
    plugin = _install(tmp_path)
    title = _binding_title(tmp_path, allowed_outputs=(logical,))
    allowed = _allowed(
        _run(
            plugin,
            session=_session(tmp_path, title),
            tool="write",
            args={"filePath": logical, "content": "{}"},
        )
    )
    assert allowed["filePath"] == str(_staged(tmp_path, logical))


def _task_context(project: Path) -> TaskContext:
    write_root = project / _WRITE_ROOT
    write_root.mkdir(parents=True, exist_ok=True)
    identity_payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [_ALLOWED],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    return TaskContext(
        project_root=project,
        write_root=write_root,
        workspace_identity=TaskWorkspaceIdentity(
            **identity_payload,
            identity_digest=canonical_digest(identity_payload),
        ),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest="a" * 64,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
    )


def _workspace(*, agent_profile: str = _AGENT) -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "agent_profile": agent_profile,
        "scope_id": "CH-1",
        "write_root": _WRITE_ROOT,
        "allowed_outputs": [_ALLOWED],
        "read_roots": [],
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def test_product_and_adapter_builders_produce_the_same_plugin_acceptable_title(tmp_path: Path) -> None:
    from agent_runtime_opencode.workspace_binding import workspace_binding_title as adapter_title

    project = tmp_path / "project"
    project.mkdir()
    workspace = _workspace()
    product = _binding_title(project, agent_profile=workspace.agent_profile)
    adapter = adapter_title(
        _task_context(project),
        workspace,
        _SESSION_ID,
    )
    assert adapter == product
    assert adapter.startswith("aa-workspace-binding-v1:")
    plugin = _install(project)
    session = _session(project, adapter, agent=workspace.agent_profile)
    document = json.loads(adapter.split(":", 1)[1])
    assert document["agent_profile"] == workspace.agent_profile
    assert session["agent"] == document["agent_profile"]
    args = _allowed(_run(plugin, session=session, tool="write", args={"filePath": _ALLOWED}))
    assert args["filePath"] == str(_staged(project, _ALLOWED))


def test_stamped_production_title_is_plugin_acceptable_and_session_agent_matches(tmp_path: Path) -> None:
    from agent_runtime_opencode.workspace_binding import workspace_binding_title as adapter_title

    project = tmp_path / "project"
    project.mkdir()
    workspace = _workspace(agent_profile="assurance-v1-executor")
    title = adapter_title(_task_context(project), workspace, _SESSION_ID)
    document = json.loads(title.split(":", 1)[1])
    assert document["agent_profile"] == "assurance-v1-executor"
    session = _session(project, title, agent=workspace.agent_profile)
    assert session["agent"] == document["agent_profile"]
    plugin = _install(project)
    args = _allowed(_run(plugin, session=session, tool="write", args={"filePath": _ALLOWED}))
    assert args["filePath"] == str(_staged(project, _ALLOWED))


def test_workspace_binding_is_digest_bound_and_requires_session_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    title = _binding_title(project)
    assert title.startswith("aa-workspace-binding-v1:")
    document = json.loads(title.split(":", 1)[1])
    digest = document.pop("digest")
    assert digest == canonical_digest(document)
    assert document["session_id"] == _SESSION_ID
    assert document["agent_profile"] == _AGENT
    assert document["project_root_digest"] == canonical_digest(str(project.resolve()))
    assert document["write_root"] == _WRITE_ROOT
    assert document["allowed_outputs"] == [_ALLOWED]
    assert document["read_roots"] == []
    assert document["task_id"] == "task-1"
    assert document["attempt"] == 1
    assert document["attempt_id"] == "attempt-1"
    with pytest.raises(ValueError, match="session"):
        workspace_binding_title(
            session_id="",
            agent_profile=_AGENT,
            project_root=project,
            write_root=_WRITE_ROOT,
            allowed_outputs=(_ALLOWED,),
            task_id="task-1",
            attempt=1,
            attempt_id="attempt-1",
        )


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("key", ["filePath", "file_path", "path"])
def test_native_write_and_edit_redirect_allowed_logical_path_to_write_root(
    tmp_path: Path, tool: str, key: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    args = _allowed(_run(plugin, session=session, tool=tool, args={key: _ALLOWED}))
    assert args[key] == str(_staged(project, _ALLOWED))
    assert (project / _ALLOWED).exists() is False


@pytest.mark.parametrize("tool", ["edit", "apply_patch"])
def test_repair_mutation_copies_the_existing_output_to_staging_before_redirect(
    tmp_path: Path, tool: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    baseline = project / _ALLOWED
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(b"baseline bytes\n")
    baseline.chmod(0o640)
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    if tool == "edit":
        original_args: Mapping[str, object] = {
            "filePath": _ALLOWED,
            "oldString": "baseline",
            "newString": "repaired",
        }
    else:
        original_args = {
            "patchText": (
                f"*** Begin Patch\n*** Update File: {_ALLOWED}\n"
                "@@\n-baseline bytes\n+repaired bytes\n*** End Patch"
            )
        }

    args = _allowed(_run(plugin, session=session, tool=tool, args=original_args))

    staged = _staged(project, _ALLOWED)
    assert staged.read_bytes() == b"baseline bytes\n"
    assert staged.stat().st_mode & 0o777 == 0o640
    assert baseline.read_bytes() == b"baseline bytes\n"
    redirected = args.get("filePath") or args.get("patchText")
    assert str(staged) in str(redirected)


@pytest.mark.parametrize("baseline_kind", ["symlink", "hardlink", "parent_symlink"])
def test_repair_copy_on_write_rejects_non_private_project_baselines(
    tmp_path: Path, baseline_kind: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    baseline = project / _ALLOWED
    if baseline_kind == "symlink":
        baseline.parent.mkdir(parents=True)
        baseline.symlink_to(outside)
    elif baseline_kind == "hardlink":
        baseline.parent.mkdir(parents=True)
        os.link(outside, baseline)
    else:
        outside_qa = tmp_path / "outside-qa"
        outside_baseline = outside_qa / "proposal.md"
        outside_baseline.parent.mkdir(parents=True)
        outside_baseline.write_text("outside\n", encoding="utf-8")
        (project / "qa").symlink_to(outside_qa, target_is_directory=True)
        outside = outside_baseline
    plugin = _install(project)
    session = _session(project, _binding_title(project))

    denied = _run(
        plugin,
        session=session,
        tool="edit",
        args={"filePath": _ALLOWED, "oldString": "outside", "newString": "changed"},
    )

    _denied(denied, "not allowed")
    assert not _staged(project, _ALLOWED).exists()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_repair_copy_on_write_never_overwrites_an_existing_staged_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    baseline = project / _ALLOWED
    baseline.parent.mkdir(parents=True)
    baseline.write_text("baseline\n", encoding="utf-8")
    staged = _staged(project, _ALLOWED)
    staged.parent.mkdir(parents=True)
    staged.write_text("prior staged repair\n", encoding="utf-8")
    plugin = _install(project)
    session = _session(project, _binding_title(project))

    args = _allowed(
        _run(
            plugin,
            session=session,
            tool="edit",
            args={"filePath": _ALLOWED, "oldString": "prior", "newString": "next"},
        )
    )

    assert args["filePath"] == str(staged)
    assert staged.read_text(encoding="utf-8") == "prior staged repair\n"


def test_new_file_write_does_not_copy_a_project_baseline(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    baseline = project / _ALLOWED
    baseline.parent.mkdir(parents=True)
    baseline.write_text("baseline\n", encoding="utf-8")
    plugin = _install(project)
    session = _session(project, _binding_title(project))

    args = _allowed(_run(plugin, session=session, tool="write", args={"filePath": _ALLOWED}))

    staged = _staged(project, _ALLOWED)
    assert args["filePath"] == str(staged)
    assert not staged.exists()


@pytest.mark.parametrize(
    "header",
    ["Add File", "Update File", "Delete File", "Move to"],
)
def test_every_apply_patch_header_is_redirected_to_write_root(tmp_path: Path, header: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    if header == "Move to":
        patch = (
            "*** Begin Patch\n"
            f"*** Update File: {_ALLOWED}\n"
            f"*** Move to: {_ALLOWED}\n"
            "@@\n-old\n+new\n"
            "*** End Patch"
        )
    elif header == "Add File":
        patch = f"*** Begin Patch\n*** Add File: {_ALLOWED}\n+new\n*** End Patch"
    elif header == "Delete File":
        patch = f"*** Begin Patch\n*** Delete File: {_ALLOWED}\n*** End Patch"
    else:
        patch = f"*** Begin Patch\n*** Update File: {_ALLOWED}\n@@\n-old\n+new\n*** End Patch"
    args = _allowed(_run(plugin, session=session, tool="apply_patch", args={"patchText": patch}))
    patch_text = args["patchText"]
    assert isinstance(patch_text, str)
    assert str(_staged(project, _ALLOWED)) in patch_text
    assert f"{header}: {_ALLOWED}\n" not in patch_text
    assert (project / _ALLOWED).exists() is False


def test_reads_prefer_staged_overlay(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    canonical = project / _ALLOWED
    canonical.parent.mkdir(parents=True)
    canonical.write_text("canonical\n", encoding="utf-8")
    staged = _staged(project, _ALLOWED)
    staged.parent.mkdir(parents=True)
    staged.write_text("staged\n", encoding="utf-8")
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    args = _allowed(_run(plugin, session=session, tool="read", args={"filePath": _ALLOWED}))
    assert args["filePath"] == str(staged)


def test_reads_fall_back_to_project_when_overlay_is_absent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    canonical = project / _ALLOWED
    canonical.parent.mkdir(parents=True)
    canonical.write_text("canonical\n", encoding="utf-8")
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    args = _allowed(_run(plugin, session=session, tool="read", args={"filePath": _ALLOWED}))
    assert Path(str(args["filePath"])).resolve() == canonical.resolve()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("write", {"filePath": _OTHER_NODE}),
        ("edit", {"path": "qa/requirement.md"}),
        (
            "apply_patch",
            {
                "patchText": (
                    "*** Begin Patch\n*** Update File: qa/results/requirement.md\n"
                    "@@\n-old\n+new\n*** End Patch"
                )
            },
        ),
        ("write", {"filePath": "tests/e2e/test_dept.py"}),
        ("write", {"filePath": "qa/.staging/task-1/attempt-2/qa/proposal.md"}),
        ("write", {"filePath": "qa/results/workflow-state.json"}),
    ],
)
def test_writes_outside_exact_allowed_outputs_are_denied(
    tmp_path: Path, tool: str, args: dict[str, object]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    _denied(_run(plugin, session=session, tool=tool, args=args), "not allowed")


def test_ambient_glob_permission_is_not_authority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(project, allowed_outputs=(_ALLOWED,)),
        agent="assurance-v1-doc-author",
    )
    _denied(
        _run(
            plugin,
            session=session,
            tool="write",
            args={"filePath": "qa/requirement.md"},
        ),
        "not allowed",
    )


def test_different_attempt_binding_cannot_write_this_attempt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(
            project,
            write_root="qa/.staging/task-1/attempt-2",
            attempt=2,
            attempt_id="attempt-2",
        ),
    )
    args = _allowed(_run(plugin, session=session, tool="write", args={"filePath": _ALLOWED}))
    staged = args["filePath"]
    assert isinstance(staged, str)
    assert "attempt-2" in staged
    assert "attempt-1" not in staged
    _denied(
        _run(
            plugin,
            session=_session(project, _binding_title(project)),
            tool="write",
            args={"filePath": "qa/.staging/task-1/attempt-2/qa/proposal.md"},
        ),
        "not allowed",
    )


def test_symlink_write_targets_are_denied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    link = project / _ALLOWED
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    _denied(_run(plugin, session=session, tool="write", args={"filePath": _ALLOWED}), "symlink")


def test_shell_escapes_and_root_swaps_are_denied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    parent = project.parent
    for command in (
        f"mv {project} {parent / 'swapped'}",
        f"rm -rf {project}",
        f"ln -s {parent / 'other'} {project}",
        f"echo hijack > {project / _ALLOWED}",
        f"python -c \"__import__('os').replace({str(project)!r}, {str(parent / 'other')!r})\"",
    ):
        _denied(
            _run(plugin, session=session, tool="bash", args={"command": command}),
            "shell",
        )


@pytest.mark.parametrize(
    "title",
    ["", "aa:activity-1", "aa-workspace-binding-v1:{", "aa-workspace-binding-v1:{}"],
)
def test_missing_or_invalid_bindings_are_denied(tmp_path: Path, title: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, title)
    _denied(_run(plugin, session=session, tool="write", args={"filePath": _ALLOWED}), "binding")


def test_binding_session_and_digest_must_match(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    forged = json.loads(_binding_title(project).split(":", 1)[1])
    forged["session_id"] = "ses-other"
    forged["digest"] = canonical_digest({key: value for key, value in forged.items() if key != "digest"})
    _denied(
        _run(
            plugin,
            session=_session(
                project, "aa-workspace-binding-v1:" + canonical_json_bytes(forged).decode("utf-8")
            ),
            tool="write",
            args={"filePath": _ALLOWED},
        ),
        "binding",
    )
    tampered = json.loads(_binding_title(project).split(":", 1)[1])
    tampered["digest"] = "0" * 64
    _denied(
        _run(
            plugin,
            session=_session(
                project, "aa-workspace-binding-v1:" + json.dumps(tampered, separators=(",", ":"))
            ),
            tool="write",
            args={"filePath": _ALLOWED},
        ),
        "binding",
    )


_EXECUTOR_VIEW_COMMAND = (
    "PYTHONDONTWRITEBYTECODE=1 "
    "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-1 "
    "uv run --isolated pytest -p no:cacheprovider --tb=line --rootdir "
    f"{_EXECUTION_VIEW} {_EXECUTION_VIEW}/tests/api/test_generated.py::test_ok"
)
_PLAYWRIGHT_VIEW_COMMAND = f"npx playwright test --config={_EXECUTION_VIEW}"
_NPM_VIEW_COMMAND = f"npm test --prefix {_EXECUTION_VIEW}"
_CWD_RENAME = (
    "python -c \"__import__('os').rename(__import__('os').getcwd(), __import__('os').getcwd()+'.bak')\""
)


def test_executor_execution_view_shell_is_allowed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ),
        agent="assurance-v1-executor",
    )
    args = _allowed(_run(plugin, session=session, tool="bash", args={"command": _EXECUTOR_VIEW_COMMAND}))
    assert args["command"] == _EXECUTOR_VIEW_COMMAND


def test_executor_playwright_config_view_shell_is_allowed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ),
        agent="assurance-v1-executor",
    )
    args = _allowed(_run(plugin, session=session, tool="bash", args={"command": _PLAYWRIGHT_VIEW_COMMAND}))
    assert args["command"] == _PLAYWRIGHT_VIEW_COMMAND


def test_executor_shell_rejects_a_sibling_attempt_execution_view(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ),
        agent="assurance-v1-executor",
    )
    sibling = _EXECUTION_VIEW.replace("attempt-1", "attempt-2")
    command = _EXECUTOR_VIEW_COMMAND.replace(_EXECUTION_VIEW, sibling)

    _denied(_run(plugin, session=session, tool="bash", args={"command": command}), "shell")


def test_executor_cannot_overwrite_the_authenticated_execution_view(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ),
        agent="assurance-v1-executor",
    )

    _denied(
        _run(plugin, session=session, tool="write", args={"filePath": _EXECUTION_VIEW}),
        "path",
    )


def test_executor_read_is_confined_to_the_authenticated_execution_view(tmp_path: Path) -> None:
    project = tmp_path / "project"
    selected = project / _EXECUTION_VIEW / "tests/api/test_generated.py"
    selected.parent.mkdir(parents=True)
    selected.write_text("def test_ok(): pass\n", encoding="utf-8")
    sibling = Path(str(selected).replace("attempt-1", "attempt-2"))
    sibling.parent.mkdir(parents=True)
    sibling.write_text("def test_sibling(): pass\n", encoding="utf-8")
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ),
        agent="assurance-v1-executor",
    )

    args = _allowed(
        _run(
            plugin,
            session=session,
            tool="read",
            args={"filePath": selected.relative_to(project).as_posix()},
        )
    )
    assert Path(str(args["filePath"])).resolve() == selected.resolve()
    _denied(
        _run(
            plugin,
            session=session,
            tool="read",
            args={"filePath": sibling.relative_to(project).as_posix()},
        ),
        "path",
    )


def test_tampered_execution_read_root_is_rejected_by_binding_digest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    document = json.loads(
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ).split(":", 1)[1]
    )
    document["read_roots"] = [_EXECUTION_VIEW.replace("attempt-1", "attempt-2")]
    title = "aa-workspace-binding-v1:" + canonical_json_bytes(document).decode("utf-8")

    _denied(
        _run(
            plugin,
            session=_session(project, title, agent="assurance-v1-executor"),
            tool="bash",
            args={"command": _EXECUTOR_VIEW_COMMAND},
        ),
        "binding",
    )


def test_signed_noncanonical_execution_read_roots_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    document = json.loads(
        _binding_title(
            project,
            agent_profile="assurance-v1-executor",
            read_roots=(_EXECUTION_VIEW,),
        ).split(":", 1)[1]
    )
    document["read_roots"] = [_EXECUTION_VIEW, _EXECUTION_VIEW]
    document["digest"] = canonical_digest({key: value for key, value in document.items() if key != "digest"})
    title = "aa-workspace-binding-v1:" + canonical_json_bytes(document).decode("utf-8")

    _denied(
        _run(
            plugin,
            session=_session(project, title, agent="assurance-v1-executor"),
            tool="bash",
            args={"command": _EXECUTOR_VIEW_COMMAND},
        ),
        "binding",
    )


def test_executor_project_root_mv_is_denied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(project, agent_profile="assurance-v1-executor"),
        agent="assurance-v1-executor",
    )
    _denied(
        _run(
            plugin,
            session=session,
            tool="bash",
            args={"command": f"mv {project} {project.parent / 'project.bak'}"},
        ),
        "shell",
    )


def test_executor_chained_python_rename_after_view_substring_is_denied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(project, agent_profile="assurance-v1-executor"),
        agent="assurance-v1-executor",
    )
    command = (
        f"{_EXECUTOR_VIEW_COMMAND}; python -c "
        f'"import os; os.rename({str(project)!r}, {str(project) + ".bak"!r})"'
    )
    _denied(_run(plugin, session=session, tool="bash", args={"command": command}), "shell")


def test_executor_echo_view_then_rm_outside_view_is_denied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(project, agent_profile="assurance-v1-executor"),
        agent="assurance-v1-executor",
    )
    _denied(
        _run(
            plugin,
            session=session,
            tool="bash",
            args={"command": "echo qa/.staging/execution/x; rm -rf /Users"},
        ),
        "shell",
    )


@pytest.mark.parametrize(
    "command",
    [
        f"{_CWD_RENAME} qa/.staging/execution/api",
        f"PYTHONDONTWRITEBYTECODE=1 uv run --isolated {_CWD_RENAME} qa/.staging/execution/api",
        f"{_EXECUTOR_VIEW_COMMAND} <({_CWD_RENAME})",
        f"{_EXECUTOR_VIEW_COMMAND} >hijack",
    ],
)
def test_executor_cwd_root_mutation_bypasses_are_denied_by_installed_plugin(
    tmp_path: Path, command: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(project, agent_profile="assurance-v1-executor"),
        agent="assurance-v1-executor",
    )
    _denied(_run(plugin, session=session, tool="bash", args={"command": command}), "shell")


@pytest.mark.parametrize(
    "command",
    [
        f"{_EXECUTOR_VIEW_COMMAND} --html hijack",
        f"{_EXECUTOR_VIEW_COMMAND} --basetemp hijack",
        f"{_EXECUTOR_VIEW_COMMAND} --override-ini=cache_dir=.",
        f"{_EXECUTOR_VIEW_COMMAND} --override-ini=cache_dir=hijack",
        f"{_EXECUTOR_VIEW_COMMAND} --override-ini=cache_dir=.cache",
        f"{_EXECUTOR_VIEW_COMMAND} --override-ini=cache_dir=tmp",
        f"{_NPM_VIEW_COMMAND} --cache hijack",
    ],
)
def test_executor_cwd_output_flags_are_denied_by_installed_plugin(tmp_path: Path, command: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(
        project,
        _binding_title(project, agent_profile="assurance-v1-executor"),
        agent="assurance-v1-executor",
    )
    _denied(_run(plugin, session=session, tool="bash", args={"command": command}), "shell")


def test_artifact_write_redirects_allowed_logical_path_to_write_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    args = _allowed(_run(plugin, session=session, tool="artifact_write", args={"filePath": _ALLOWED}))
    assert args["filePath"] == str(_staged(project, _ALLOWED))


def test_author_profile_cannot_run_execution_view_shell(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    _denied(
        _run(plugin, session=session, tool="bash", args={"command": _EXECUTOR_VIEW_COMMAND}),
        "shell",
    )


def test_adversarial_rename_replace_and_swap_of_project_or_output_parent_are_denied(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "qa").mkdir(parents=True)
    plugin = _install(project)
    session = _session(project, _binding_title(project))
    output_parent = "qa"
    for tool, args in (
        ("write", {"filePath": str(project)}),
        ("write", {"filePath": output_parent}),
        ("write", {"rename": str(project)}),
        (
            "apply_patch",
            {
                "patchText": (
                    f"*** Begin Patch\n*** Update File: {output_parent}\n@@\n-old\n+new\n*** End Patch"
                )
            },
        ),
        ("bash", {"command": f"mv {project / output_parent} {project / 'qa.bak'}"}),
        ("bash", {"command": f"mv {project} {project.parent / 'project.bak'}"}),
        (
            "bash",
            {"command": (f"mv {project} {project.parent / 'tmp'} && mv {project.parent / 'tmp'} {project}")},
        ),
    ):
        denied = _run(plugin, session=session, tool=tool, args=args)
        assert denied.returncode == 23, denied.stdout + denied.stderr
        assert (project / "qa").is_dir()
        assert project.is_dir()
