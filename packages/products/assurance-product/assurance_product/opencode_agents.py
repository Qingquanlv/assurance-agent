from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path

from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

_BINDING_TITLE_PREFIX = "aa-workspace-binding-v1:"


_BOUNDARY_PLUGIN_ENTRY = "./.opencode/plugins/assurance-boundary.mjs"

_DISABLED_TOOLS = (
    "ast_grep_replace",
    "ast_grep_search",
    "assurance_boundary_v1",
    "background_cancel",
    "background_output",
    "call_omo_agent",
    "interactive_bash",
    "invalid",
    "lsp_diagnostics",
    "lsp_find_references",
    "lsp_goto_definition",
    "lsp_prepare_rename",
    "lsp_rename",
    "lsp_symbols",
    "look_at",
    "monitor_start",
    "question",
    "session_info",
    "session_list",
    "session_read",
    "session_search",
    "skill",
    "skill_mcp",
    "task",
    "task_create",
    "task_get",
    "task_list",
    "task_update",
    "todowrite",
    "webfetch",
    "websearch",
    "websearch_web_search_exa",
    "workflow_start",
)

_EDIT_RULES: Mapping[str, tuple[str, ...]] = {
    "assurance-v1-archiver": (),
    "assurance-v1-doc-author": (
        "qa/.qa.yaml",
        "qa/cases/**",
        "qa/results/facts/**",
        "qa/results/healing/**",
        "qa/results/plans/**",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results/retro/**",
        "qa/results/review/**",
        "qa/results/trace/**",
    ),
    "assurance-v1-executor": ("qa/results/execution/**",),
    "assurance-v1-explorer": ("qa/results/explore/**",),
    "assurance-v1-reporter": (
        "qa/results/inspect/**",
        "qa/results/issue-review/**",
        "qa/results/report/**",
    ),
    "assurance-v1-reviewer": (
        "qa/results/inspect/**",
        "qa/results/review/**",
    ),
    "assurance-v1-test-author": (
        "qa/results/codegen/**",
        "qa/results/coverage-repair/**",
        "qa/results/healing/**",
        "qa/tests/**",
        "qa/fixtures/**",
    ),
}

_EXECUTION_VIEW = "**/qa/.staging/execution/*"
_EXECUTOR_COMMANDS = (
    f"npm run test --prefix {_EXECUTION_VIEW} *",
    f"npm test --prefix {_EXECUTION_VIEW} *",
    f"npx playwright test --config={_EXECUTION_VIEW} *",
    f"pnpm --dir {_EXECUTION_VIEW} run test *",
    f"pnpm --dir {_EXECUTION_VIEW} test *",
    (
        "PYTHONDONTWRITEBYTECODE=1 "
        "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-* "
        f"uv run --isolated pytest -p no:cacheprovider --tb=line --rootdir {_EXECUTION_VIEW} *"
    ),
    f"PYTHONDONTWRITEBYTECODE=1 uv run --isolated locust --locustfile {_EXECUTION_VIEW} *",
)

_BASH_RULES: Mapping[str, tuple[str, ...]] = {
    "assurance-v1-archiver": (),
    "assurance-v1-doc-author": (),
    "assurance-v1-executor": _EXECUTOR_COMMANDS,
    "assurance-v1-explorer": (),
    "assurance-v1-reporter": (
        "aa --version",
        "aa artifact write *",
        "aa report generate *",
    ),
    "assurance-v1-reviewer": (),
    "assurance-v1-test-author": (),
}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def workspace_binding_document(
    *,
    session_id: str,
    agent_profile: str,
    project_root: Path,
    write_root: str,
    allowed_outputs: Sequence[str],
    task_id: str,
    attempt: int,
    attempt_id: str,
    read_roots: Sequence[str] = (),
) -> dict[str, object]:
    if not session_id or session_id.strip() != session_id or any(ch.isspace() for ch in session_id):
        raise ValueError("workspace binding requires a provider session id")
    if not agent_profile:
        raise ValueError("workspace binding requires an agent profile")
    if not task_id or attempt < 1 or not attempt_id:
        raise ValueError("workspace binding requires task and attempt identity")
    if not write_root or write_root.startswith("/") or "\\" in write_root or ".." in write_root.split("/"):
        raise ValueError("write_root must be a canonical project-relative path")
    outputs = tuple(allowed_outputs)
    if outputs != tuple(sorted(outputs)) or len(set(outputs)) != len(outputs):
        raise ValueError("allowed outputs must be unique sorted exact logical paths")
    for item in outputs:
        if not item or item.startswith("/") or "\\" in item or ".." in item.split("/"):
            raise ValueError("allowed outputs must be canonical project-relative paths")
    roots = tuple(read_roots)
    if roots != tuple(sorted(roots)) or len(set(roots)) != len(roots):
        raise ValueError("read roots must be unique sorted exact project-relative paths")
    for item in roots:
        if not item or item.startswith("/") or "\\" in item or ".." in item.split("/"):
            raise ValueError("read roots must be canonical project-relative paths")
    payload: dict[str, object] = {
        "schema_version": "1",
        "session_id": session_id,
        "agent_profile": agent_profile,
        "project_root_digest": _canonical_digest(str(project_root.resolve())),
        "write_root": write_root,
        "allowed_outputs": list(outputs),
        "read_roots": list(roots),
        "task_id": task_id,
        "attempt": attempt,
        "attempt_id": attempt_id,
    }
    return {**payload, "digest": _canonical_digest(payload)}


def workspace_binding_title(
    *,
    session_id: str,
    agent_profile: str,
    project_root: Path,
    write_root: str,
    allowed_outputs: Sequence[str],
    task_id: str,
    attempt: int,
    attempt_id: str,
    read_roots: Sequence[str] = (),
) -> str:
    document = workspace_binding_document(
        session_id=session_id,
        agent_profile=agent_profile,
        project_root=project_root,
        write_root=write_root,
        allowed_outputs=allowed_outputs,
        task_id=task_id,
        attempt=attempt,
        attempt_id=attempt_id,
        read_roots=read_roots,
    )
    return _BINDING_TITLE_PREFIX + _canonical_json_bytes(document).decode("utf-8")


def bounded_agent_profiles() -> tuple[str, ...]:
    referenced = {contract.agent_profile for contract in AGENT_EXECUTION_CONTRACTS.values()}
    declared = set(_EDIT_RULES)
    if referenced != declared:
        raise ValueError(
            "OpenCode bounded agent catalog drifted; "
            f"missing={sorted(referenced - declared)}, extra={sorted(declared - referenced)}"
        )
    if set(_BASH_RULES) != declared:
        raise ValueError("OpenCode bounded agent command catalog drifted")
    return tuple(sorted(declared))


_EDIT_DENY_FILES = (
    "qa/results/explore/context.json",
    "qa/results/workflow-state.json",
    "qa/results/workflow-state.yaml",
)


def _edit_match_patterns(logical: str) -> tuple[str, ...]:
    if logical.startswith("**/"):
        return (logical,)
    # OpenCode serializes permission keys with sort_keys=True and last-match wins.
    # `**/<logical>` covers repo-prefixed remapped paths. `qa/.staging/**/<logical>`
    # sorts after both `qa/.staging/**` and directory globs such as
    # `qa/.staging/**/qa/results/explore/**`.
    return (logical, f"**/{logical}", f"qa/.staging/**/{logical}")


def _agent_definition(agent_profile: str) -> dict[str, object]:
    edit: dict[str, str] = {
        "**": "deny",
        "qa/.runtime/**": "deny",
        "qa/.staging/**": "deny",
    }
    for logical in _EDIT_RULES[agent_profile]:
        for pattern in _edit_match_patterns(logical):
            edit[pattern] = "allow"
    for logical in _EDIT_DENY_FILES:
        for pattern in _edit_match_patterns(logical):
            edit[pattern] = "deny"
    bash = {"*": "deny"}
    bash.update({command: "allow" for command in _BASH_RULES[agent_profile]})
    tools = {tool: False for tool in _DISABLED_TOOLS}
    mutation = agent_profile != "assurance-v1-archiver"
    tools.update(
        {
            "bash": bool(_BASH_RULES[agent_profile]),
            "write": mutation,
            "artifact_write": mutation,
            "apply_patch": mutation,
        }
    )
    if _BASH_RULES[agent_profile]:
        write_guidance = (
            "Use an explicitly allowed aa artifact write command only when native write is unavailable."
        )
    else:
        write_guidance = (
            "Shell commands and legacy aa commands are disabled. A tool literally named `write` "
            "is available for new files, and `edit` is available for allowed existing files. "
            "If this model cannot invoke `write`, use the guarded `apply_patch` fallback. Do not "
            "infer that file writing is unavailable merely because `bash` is disabled."
        )
    prompt = (
        "Execute exactly the supplied frozen skill, persona, and business instructions.\n\n"
        "Do not ask questions, delegate, load a global skill, choose a model or adapter, "
        "or drive the workflow. Produce only the declared files and structured result. "
        "After completing every required side effect, make the final assistant text exactly the "
        "single JSON object required by the runtime result contract. Never answer with `Complete` "
        "or a prose completion summary. If execution continues after you have returned the final "
        "JSON, repeat that exact JSON object verbatim without commentary. "
        "The current session directory is the complete project root. Use project-relative "
        "paths for read, glob, and grep calls; never inspect a parent directory or any path "
        "outside the current session directory. "
        "Never write workflow-state files. Create allowed artifacts with the native write tool "
        "and update allowed artifacts with the edit tool. "
        "Never invoke write, edit, artifact_write, or apply_patch in parallel: complete exactly "
        "one file mutation, wait for that tool call to finish, then start the next file. "
        "`apply_patch` is allowed only within the installed Assurance per-agent write boundary. "
        + write_guidance
    )
    return {
        "mode": "all",
        "description": f"Execute one bounded Assurance skill as {agent_profile}.",
        "prompt": prompt,
        "tools": tools,
        "permission": {
            "edit": edit,
            "bash": bash,
            "external_directory": "deny",
        },
    }


def _opencode_config() -> str:
    document = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [_BOUNDARY_PLUGIN_ENTRY],
        "agent": {profile: _agent_definition(profile) for profile in bounded_agent_profiles()},
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _assert_install_target(path: Path, content: str) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"OpenCode agent target is not a regular file: {path}")
    if path.read_text(encoding="utf-8") != content:
        raise ValueError(f"OpenCode agent target already has different content: {path}")


def install_opencode_agents(project_dir: Path) -> tuple[Path, ...]:
    root = project_dir.resolve()
    if not root.is_dir():
        raise ValueError("OpenCode agent installation requires an existing project directory")
    config_path = root / "opencode.json"
    config_content = _opencode_config()
    plugin_path = root / ".opencode" / "plugins" / "assurance-boundary.mjs"
    plugin_content = (
        files("assurance_product")
        .joinpath("resources", "opencode", "assurance-boundary.mjs")
        .read_text(encoding="utf-8")
    )
    for path, content in (
        (config_path, config_content),
        (plugin_path, plugin_content),
    ):
        _assert_install_target(path, content)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return config_path, plugin_path


__all__ = [
    "bounded_agent_profiles",
    "install_opencode_agents",
    "workspace_binding_document",
    "workspace_binding_title",
]
