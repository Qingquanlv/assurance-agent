"""Attempt-bound declared-only input snapshots and plan-fixer runtime context (D13)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import StrictStr, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.contracts import (
    ExecutionContract,
    ResourceClaims,
    ResourcePath,
    path_covers,
)
from assurance_agent.workflow.graph.models import ExecutableTask
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError

_SHA256_PREFIXED_LENGTH = len("sha256:") + 64
_RUNTIME_CONTEXT_INJECTION = "platform:plan-fixer-runtime-context/v1"
_RUNTIME_CONTEXT_HEADING = "## Runtime Context"


class TaskInputError(AaError):
    """Input snapshot / runtime-context authority failure."""


def _validate_prefixed_sha256(value: str) -> str:
    if (
        len(value) != _SHA256_PREFIXED_LENGTH
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value.removeprefix("sha256:"))
    ):
        raise ValueError("must be a lowercase sha256:<64-hex> digest")
    return value


def _validate_canonical_strings(values: Sequence[str], *, label: str) -> None:
    if any(not value for value in values):
        raise ValueError(f"{label} must not contain empty values")
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    if list(values) != sorted(values):
        raise ValueError(f"{label} must be canonically sorted")


def _validate_physical_relpath(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("must be a normalized task-project-relative POSIX path")
    return value


class TaskInputSnapshotEntryV1(StrictWireModel):
    physical_relpath: StrictStr
    repo_relpath: str | None
    logical_aliases: list[StrictStr]
    matched_claims: list[StrictStr]
    origins: list[Literal["contract_read", "skill_bundle"]]
    kind: Literal["file", "symlink"]
    mode: int
    sha256: str | None
    symlink_target: str | None

    @model_validator(mode="after")
    def validate_entry(self) -> TaskInputSnapshotEntryV1:
        _validate_physical_relpath(self.physical_relpath)
        if self.repo_relpath is not None:
            _validate_physical_relpath(self.repo_relpath)
        _validate_canonical_strings(self.logical_aliases, label="logical_aliases")
        _validate_canonical_strings(self.matched_claims, label="matched_claims")
        _validate_canonical_strings(self.origins, label="origins")
        if not self.logical_aliases:
            raise ValueError("logical_aliases must be non-empty")
        if not self.origins:
            raise ValueError("origins must be non-empty")
        if self.kind == "file":
            if self.sha256 is None:
                raise ValueError("file entries require sha256")
            _validate_prefixed_sha256(self.sha256)
            if self.symlink_target is not None:
                raise ValueError("file entries must not set symlink_target")
        else:
            if self.symlink_target is None:
                raise ValueError("symlink entries require symlink_target")
            if self.sha256 is not None:
                raise ValueError("symlink entries must not set sha256")
        if not isinstance(self.mode, int) or isinstance(self.mode, bool) or self.mode < 0:
            raise ValueError("mode must be a non-negative int")
        return self


class TaskInputSnapshotV1(StrictWireModel):
    schema_version: Literal["1"]
    invocation_id: StrictStr
    task_id: StrictStr
    attempt_id: StrictStr
    base_tree_id: StrictStr
    materialized_tree_id: StrictStr
    input_sha256: str
    runtime_context_sha256: str | None
    contract_digest: StrictStr
    claims_digest: StrictStr
    entries: list[TaskInputSnapshotEntryV1]

    @model_validator(mode="after")
    def validate_snapshot(self) -> TaskInputSnapshotV1:
        _validate_prefixed_sha256(self.input_sha256)
        if self.runtime_context_sha256 is not None:
            _validate_prefixed_sha256(self.runtime_context_sha256)
        _validate_prefixed_sha256(self.contract_digest)
        _validate_prefixed_sha256(self.claims_digest)
        paths = [entry.physical_relpath for entry in self.entries]
        _validate_canonical_strings(paths, label="entries.physical_relpath")
        recomputed = _entries_input_sha256(self.entries)
        if recomputed != self.input_sha256:
            raise ValueError("input_sha256 does not match canonical entry digest")
        return self


class PlanFixerRuntimeContextV1(StrictWireModel):
    schema_version: Literal["1"]
    mode: Literal["automatic_healing", "human_approved"]
    target: Literal["api", "e2e"]
    change_id: StrictStr
    root_invocation_id: StrictStr
    invocation_id: StrictStr
    task_id: StrictStr
    attempt_id: StrictStr
    base_tree_id: StrictStr
    source_review_path: StrictStr
    source_review_sha256: str
    source_interrupt_task_id: str | None
    resume_action: Literal["fix_and_proceed"] | None
    human_reason: str | None
    human_decision_sha256: str | None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> PlanFixerRuntimeContextV1:
        _validate_prefixed_sha256(self.source_review_sha256)
        human_fields = (
            self.source_interrupt_task_id,
            self.resume_action,
            self.human_reason,
            self.human_decision_sha256,
        )
        if self.mode == "automatic_healing":
            if any(field is not None for field in human_fields):
                raise ValueError("automatic_healing requires all four human-decision fields to be null")
            return self
        if self.source_interrupt_task_id is None or not self.source_interrupt_task_id.strip():
            raise ValueError("human_approved requires source_interrupt_task_id")
        if self.resume_action != "fix_and_proceed":
            raise ValueError("human_approved requires resume_action fix_and_proceed")
        if self.human_reason is None or not self.human_reason.strip():
            raise ValueError("human_approved requires non-empty human_reason")
        if self.human_decision_sha256 is None:
            raise ValueError("human_approved requires human_decision_sha256")
        _validate_prefixed_sha256(self.human_decision_sha256)
        return self


def claims_digest_for(claims: ResourceClaims) -> str:
    payload = {
        "authorization_writes": [f"{p.root}:{p.pattern}" for p in claims.authorization_writes],
        "exclusive": list(claims.exclusive),
        "reads": [f"{p.root}:{p.pattern}" for p in claims.reads],
        "synchronized": [f"{p.root}:{p.pattern}" for p in claims.synchronized],
        "writes": [f"{p.root}:{p.pattern}" for p in claims.writes],
    }
    return sha256_bytes(canonical_json_bytes(payload))


def runtime_context_digest(context: PlanFixerRuntimeContextV1) -> str:
    return sha256_bytes(canonical_json_bytes(context))


def prepare_plan_fixer_runtime_context(
    context: PlanFixerRuntimeContextV1,
) -> tuple[PlanFixerRuntimeContextV1, str]:
    """Validate and return ``(context, runtime_context_sha256)`` for attempt prep."""
    runtime_context_sha256 = runtime_context_digest(context)
    return context, runtime_context_sha256


def resume_plan_fixer_runtime_context(
    context: PlanFixerRuntimeContextV1,
    *,
    expected_runtime_context_sha256: str,
) -> PlanFixerRuntimeContextV1:
    """Resume-time binding: reject stale/mismatched runtime_context_sha256."""
    runtime_context_sha256 = runtime_context_digest(context)
    if runtime_context_sha256 != expected_runtime_context_sha256:
        raise TaskInputError("resume runtime_context_sha256 mismatch")
    return context


def render_plan_fixer_runtime_context_block(context: PlanFixerRuntimeContextV1) -> str:
    """Render the structured Runtime Context prompt section bound to AgentRequest."""
    body = json.dumps(
        context.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return (
        f"{_RUNTIME_CONTEXT_HEADING}\n"
        f"injection: {_RUNTIME_CONTEXT_INJECTION}\n"
        f"digest: {runtime_context_digest(context)}\n"
        f"```json\n{body}\n```\n"
    )


def bind_runtime_context_to_prompt(prompt: str, context: PlanFixerRuntimeContextV1) -> str:
    block = render_plan_fixer_runtime_context_block(context)
    if block.strip() in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{block}"


def assert_prompt_runtime_context_match(prompt: str, context: PlanFixerRuntimeContextV1 | None) -> None:
    """Reject prompt/context digest mismatch before adapter dispatch."""
    if context is None:
        if _RUNTIME_CONTEXT_INJECTION in prompt or f"{_RUNTIME_CONTEXT_HEADING}\n" in prompt:
            raise TaskInputError("prompt contains Runtime Context without bound runtime context")
        return
    expected = render_plan_fixer_runtime_context_block(context).strip()
    if expected not in prompt:
        raise TaskInputError("prompt/runtime-context digest mismatch")


def capture_task_input_snapshot(
    *,
    invocation_id: str,
    task: ExecutableTask,
    attempt_id: str,
    workspace: TaskWorkspace,
    contract: ExecutionContract,
    runtime_context: PlanFixerRuntimeContextV1 | None,
) -> tuple[str, bytes]:
    """Return the CAS identity and canonical snapshot bytes."""
    if workspace.materialized_tree_id is None:
        raise TaskInputError("declared-only snapshot requires materialized_tree_id")
    if runtime_context is not None:
        if runtime_context.invocation_id != invocation_id:
            raise TaskInputError("runtime context invocation_id mismatch")
        if runtime_context.task_id != task.task_id:
            raise TaskInputError("runtime context task_id mismatch")
        if runtime_context.attempt_id != attempt_id:
            raise TaskInputError("runtime context attempt_id mismatch")
        if runtime_context.base_tree_id != workspace.base_tree_id:
            raise TaskInputError("runtime context base_tree_id mismatch")
        if runtime_context.target not in ("api", "e2e"):
            raise TaskInputError("runtime context target rejected")
    roots = _load_workspace_roots(workspace)
    skill_name = task.target.partition(":")[2] if task.target.startswith("skill:") else None
    entries = _collect_entries(
        workspace=workspace,
        roots=roots,
        claims=task.resources,
        skill_name=skill_name,
    )
    runtime_digest = runtime_context_digest(runtime_context) if runtime_context is not None else None
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id=invocation_id,
        task_id=task.task_id,
        attempt_id=attempt_id,
        base_tree_id=workspace.base_tree_id,
        materialized_tree_id=workspace.materialized_tree_id,
        input_sha256=_entries_input_sha256(entries),
        runtime_context_sha256=runtime_digest,
        contract_digest=_normalize_digest(task.contract_digest),
        claims_digest=claims_digest_for(task.resources),
        entries=entries,
    )
    _ = contract  # contract identity is bound via task.contract_digest
    raw = canonical_json_bytes(snapshot)
    snapshot_id = hashlib.sha256(raw).hexdigest()
    return snapshot_id, raw


def load_task_input_snapshot(store: TreeStore, snapshot_id: str) -> TaskInputSnapshotV1:
    """Load, digest-check, and strictly validate one snapshot object."""
    try:
        raw = store._read_object(snapshot_id)  # noqa: SLF001
    except WorkspaceError as exc:
        raise TaskInputError(f"missing input snapshot object: {snapshot_id}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != snapshot_id:
        raise TaskInputError(f"input snapshot digest mismatch for {snapshot_id}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskInputError(f"invalid input snapshot object: {snapshot_id}") from exc
    try:
        return TaskInputSnapshotV1.model_validate(payload)
    except Exception as exc:
        raise TaskInputError(f"invalid input snapshot object: {snapshot_id}: {exc}") from exc


def _normalize_digest(value: str) -> str:
    if value.startswith("sha256:"):
        return _validate_prefixed_sha256(value)
    if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
        return f"sha256:{value}"
    return sha256_bytes(value.encode("utf-8"))


def store_task_input_snapshot(store: TreeStore, snapshot_id: str, raw: bytes) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    if digest != snapshot_id:
        raise TaskInputError("snapshot_id does not match canonical bytes")
    store._write_object(snapshot_id, raw)  # noqa: SLF001


def _entries_input_sha256(entries: Sequence[TaskInputSnapshotEntryV1]) -> str:
    payload = [entry.model_dump(mode="json") for entry in entries]
    return sha256_bytes(canonical_json_bytes(payload))


def _load_workspace_roots(workspace: TaskWorkspace) -> dict[str, str]:
    manifest_path = workspace.tree_manifest_path or (workspace.root / ".graph-runtime" / "tree.json")
    try:
        payload = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskInputError(f"workspace lacks tree manifest: {manifest_path}") from exc
    raw_roots = payload.get("roots") if isinstance(payload, dict) else None
    if not isinstance(raw_roots, dict):
        raise TaskInputError(f"tree manifest lacks roots: {manifest_path}")
    roots: dict[str, str] = {}
    for name, prefix in raw_roots.items():
        if not isinstance(name, str) or not isinstance(prefix, str):
            raise TaskInputError(f"invalid tree root mapping: {manifest_path}")
        roots[name] = prefix
    return roots


def _collect_entries(
    *,
    workspace: TaskWorkspace,
    roots: Mapping[str, str],
    claims: ResourceClaims,
    skill_name: str | None,
) -> list[TaskInputSnapshotEntryV1]:
    root = workspace.root.resolve()
    collected: dict[str, TaskInputSnapshotEntryV1] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        # Never descend into control metadata if it still exists under root.
        dirnames[:] = [name for name in dirnames if name not in {".git", ".graph-runtime"}]
        rel_dir = directory.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        for name in sorted(filenames):
            path = directory / name
            rel = name if not rel_dir else f"{rel_dir}/{name}"
            if rel.startswith(".graph-runtime/") or rel == ".graph-runtime":
                continue
            entry = _entry_for_path(
                root=root,
                rel=rel,
                path=path,
                roots=roots,
                claims=claims,
                skill_name=skill_name,
            )
            if entry is None:
                continue
            if rel in collected:
                raise TaskInputError(f"duplicate physical path in snapshot: {rel}")
            collected[rel] = entry
        for name in sorted(dirnames):
            path = directory / name
            if path.is_symlink():
                rel = name if not rel_dir else f"{rel_dir}/{name}"
                entry = _entry_for_path(
                    root=root,
                    rel=rel,
                    path=path,
                    roots=roots,
                    claims=claims,
                    skill_name=skill_name,
                )
                if entry is None:
                    continue
                if rel in collected:
                    raise TaskInputError(f"duplicate physical path in snapshot: {rel}")
                collected[rel] = entry
    return [collected[key] for key in sorted(collected)]


def _entry_for_path(
    *,
    root: Path,
    rel: str,
    path: Path,
    roots: Mapping[str, str],
    claims: ResourceClaims,
    skill_name: str | None,
) -> TaskInputSnapshotEntryV1 | None:
    resolutions = _resolutions(roots, rel)
    if not resolutions:
        return None
    aliases = sorted({f"{name}:{logical}" for name, logical in resolutions})
    matched_claims: list[str] = []
    origins: set[Literal["contract_read", "skill_bundle"]] = set()
    for name, logical in resolutions:
        claim_path = ResourcePath.parse(f"{name}:{logical}")
        for read in claims.reads:
            if path_covers(read, claim_path):
                matched_claims.append(f"{read.root}:{read.pattern}")
                origins.add("contract_read")
        if name == "project" and skill_name and logical.startswith(f"skills/{skill_name}/"):
            origins.add("skill_bundle")
            matched_claims.append(f"project:skills/{skill_name}/**")
    if not origins:
        return None
    matched = sorted(set(matched_claims))
    repo_relpath = next((logical for name, logical in resolutions if name == "repo"), None)
    st = path.lstat()
    mode = int(st.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        return TaskInputSnapshotEntryV1(
            physical_relpath=rel,
            repo_relpath=repo_relpath,
            logical_aliases=aliases,
            matched_claims=matched,
            origins=sorted(origins),
            kind="symlink",
            mode=mode,
            sha256=None,
            symlink_target=target,
        )
    if not path.is_file():
        return None
    digest = sha256_bytes(path.read_bytes())
    return TaskInputSnapshotEntryV1(
        physical_relpath=rel,
        repo_relpath=repo_relpath,
        logical_aliases=aliases,
        matched_claims=matched,
        origins=sorted(origins),
        kind="file",
        mode=mode,
        sha256=digest,
        symlink_target=None,
    )


def _resolutions(roots: Mapping[str, str], rel: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name, prefix in roots.items():
        if prefix == ".":
            found.append((name, rel))
        elif rel.startswith(f"{prefix}/"):
            found.append((name, rel[len(prefix) + 1 :]))
    return found


__all__ = [
    "PlanFixerRuntimeContextV1",
    "TaskInputError",
    "TaskInputSnapshotEntryV1",
    "TaskInputSnapshotV1",
    "assert_prompt_runtime_context_match",
    "bind_runtime_context_to_prompt",
    "capture_task_input_snapshot",
    "claims_digest_for",
    "load_task_input_snapshot",
    "prepare_plan_fixer_runtime_context",
    "render_plan_fixer_runtime_context_block",
    "resume_plan_fixer_runtime_context",
    "runtime_context_digest",
    "store_task_input_snapshot",
]
