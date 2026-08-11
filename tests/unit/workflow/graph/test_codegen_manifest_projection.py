"""Runtime completion of codegen generated-file evidence before freeze."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from assurance_agent.workflow.graph import codegen_manifest as codegen_manifest_module
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
)
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.finalize import finalize_task_result
from assurance_agent.workflow.graph.handlers.agent import AgentHandler
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.schema_v2 import (
    RetryPolicyDef,
    TimeoutPolicyDef,
    parse_workflow_v2,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend

_LAYERS = {
    "api": "tests/api/test_subject_api.py",
    "e2e": "tests/e2e/test_subject_e2e.py",
    "fuzz": "tests/fuzz/test_subject_fuzz.py",
    "performance": "tests/perf/locustfile_subject.py",
}


def _workspace(tmp_path: Path, *, layer: str) -> tuple[Path, TreeStore, TaskWorkspace]:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    base_tree = store.capture(project)
    workspace = WorkspaceBackend(change).create(
        task_id=f"{layer}-codegen",
        base_tree_id=base_tree,
        store=store,
    )
    return project, store, workspace


def _task(layer: str, repo_paths: tuple[str, ...]) -> ExecutableTask:
    manifest = f"change:codegen/{layer}-generated-files.json"
    del repo_paths
    claims = (ResourcePath.parse(manifest), ResourcePath.parse("repo:**"))
    return ExecutableTask(
        task_id=f"{layer}-codegen",
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        graph_id="main",
        node_id="codegen",
        structural_path="main",
        input={"outputs": [manifest]},
        input_sha256="input",
        contract_digest="contract",
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=30, heartbeat_seconds=5),
        target=f"skill:aa-{layer}-codegen",
        resources=ResourceClaims(writes=claims, authorization_writes=claims),
    )


def _write_manifest(workspace: TaskWorkspace, layer: str, payload: dict[str, object]) -> Path:
    path = workspace.change_dir / "codegen" / f"{layer}-generated-files.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _finalize(
    store: TreeStore,
    workspace: TaskWorkspace,
    *,
    layer: str,
    repo_paths: tuple[str, ...],
    task: ExecutableTask | None = None,
) -> TaskResult:
    return finalize_task_result(
        compiled=cast(CompiledWorkflow, SimpleNamespace(graphs={})),
        store=store,
        task=task or _task(layer, repo_paths),
        result=TaskResult(status="succeeded"),
        workspace=workspace,
        context=RuntimeContext(
            project_root=workspace.project_root,
            repo_root=workspace.repo_root,
            change_dir=workspace.change_dir,
            change_id="CH-1",
        ),
    )


class _SuccessfulInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        del request
        return AgentResult(ok=True)


class _StaticClaimsCatalog:
    def __init__(self, claims: ResourceClaims) -> None:
        self._claims = claims
        # AgentHandler looks up ``contracts[task.target]`` for read_isolation
        # before calling claims_for; keep a permissive stub entry.
        self.contracts: dict[str, object] = {}

    def claims_for(self, node: object) -> ResourceClaims:
        del node
        return self._claims


def test_agent_handler_completes_manifest_before_its_own_freeze(tmp_path: Path) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    project, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_bytes("你好\n".encode())
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )
    task = _task(layer, (repo_path,)).model_copy(update={"resources": ResourceClaims()})
    manifest_claim = ResourcePath.parse("change:codegen/api-generated-files.json")
    repo_claim = ResourcePath.parse("repo:tests/api/**")
    effective_claims = ResourceClaims(
        writes=(manifest_claim, repo_claim),
        authorization_writes=(manifest_claim, repo_claim),
    )
    compiled = compile_workflow(
        parse_workflow_v2(
            f"""schema_version: "2"
name: codegen-test
entrypoints:
  full: {{graph: main}}
graphs:
  main:
    max_supersteps: 2
    nodes:
      codegen:
        uses: {task.target}
        outputs:
          - change:codegen/{layer}-generated-files.json
    edges:
      - {{from: START, to: codegen}}
      - {{from: codegen, to: END}}
gates: {{}}
"""
        )
    )
    handler = AgentHandler(
        _SuccessfulInvoker(),
        store,
        contracts=cast(ExecutionContractCatalog, _StaticClaimsCatalog(effective_claims)),
        compiled=compiled,
    )

    result = handler.execute(
        task,
        workspace,
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=project / "qa" / "changes" / "CH-1",
            change_id="CH-1",
        ),
    )

    assert result.status == "succeeded"
    assert result.write_set_id is not None
    manifest_sha = store.load_write_set(result.write_set_id).outputs_sha256[
        "change:codegen/api-generated-files.json"
    ]
    frozen = json.loads(store.read_object(manifest_sha))
    assert frozen["files"][0]["content_sha256"] == (
        "sha256:4e0826721642ed8e3a27e7147538ac7b7013a08fe5ae343a8ef09749b7e5790f"
    )


def test_agent_handler_rejects_repo_file_replaced_between_completion_and_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen repo blob must be the exact blob recorded by runtime completion."""
    layer = "api"
    repo_path = _LAYERS[layer]
    project, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"completed bytes\n")
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )
    task = _task(layer, (repo_path,)).model_copy(update={"resources": ResourceClaims()})
    manifest_claim = ResourcePath.parse("change:codegen/api-generated-files.json")
    repo_claim = ResourcePath.parse("repo:tests/api/**")
    effective_claims = ResourceClaims(
        writes=(manifest_claim, repo_claim),
        authorization_writes=(manifest_claim, repo_claim),
    )
    compiled = compile_workflow(
        parse_workflow_v2(
            f"""schema_version: "2"
name: codegen-test
entrypoints:
  full: {{graph: main}}
graphs:
  main:
    max_supersteps: 2
    nodes:
      codegen:
        uses: {task.target}
        outputs:
          - change:codegen/{layer}-generated-files.json
    edges:
      - {{from: START, to: codegen}}
      - {{from: codegen, to: END}}
gates: {{}}
"""
        )
    )
    handler = AgentHandler(
        _SuccessfulInvoker(),
        store,
        contracts=cast(ExecutionContractCatalog, _StaticClaimsCatalog(effective_claims)),
        compiled=compiled,
    )
    original_freeze = store.freeze_write_set

    def replace_then_freeze(
        task_workspace: TaskWorkspace,
        *,
        claims: ResourceClaims,
        outputs: tuple[str, ...] = (),
    ):
        generated.rename(generated.with_suffix(".completed"))
        generated.write_bytes(b"replacement bytes\n")
        return original_freeze(task_workspace, claims=claims, outputs=outputs)

    monkeypatch.setattr(store, "freeze_write_set", replace_then_freeze)

    result = handler.execute(
        task,
        workspace,
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=project / "qa" / "changes" / "CH-1",
            change_id="CH-1",
        ),
    )

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "changed after codegen manifest completion" in (result.error or "")
    assert result.write_set_id is None
    assert not (project / repo_path).exists()


def test_finalize_rejects_repo_file_replaced_between_completion_and_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    project, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"completed bytes\n")
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )
    original_freeze = store.freeze_write_set

    def replace_then_freeze(
        task_workspace: TaskWorkspace,
        *,
        claims: ResourceClaims,
        outputs: tuple[str, ...] = (),
    ):
        generated.rename(generated.with_suffix(".completed"))
        generated.write_bytes(b"replacement bytes\n")
        return original_freeze(task_workspace, claims=claims, outputs=outputs)

    monkeypatch.setattr(store, "freeze_write_set", replace_then_freeze)

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "changed after codegen manifest completion" in (result.error or "")
    assert result.write_set_id is None
    assert not (project / repo_path).exists()


def test_finalize_accepts_same_bytes_replaced_between_completion_and_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    content = b"content-addressed bytes\n"
    generated.write_bytes(content)
    original_inode = generated.stat().st_ino
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )
    original_freeze = store.freeze_write_set

    def replace_then_freeze(
        task_workspace: TaskWorkspace,
        *,
        claims: ResourceClaims,
        outputs: tuple[str, ...] = (),
    ):
        generated.rename(generated.with_suffix(".completed"))
        generated.write_bytes(content)
        assert generated.stat().st_ino != original_inode
        return original_freeze(task_workspace, claims=claims, outputs=outputs)

    monkeypatch.setattr(store, "freeze_write_set", replace_then_freeze)

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "succeeded"
    assert result.write_set_id is not None


@pytest.mark.parametrize(("layer", "repo_path"), tuple(_LAYERS.items()))
def test_codegen_manifest_is_completed_with_exact_bytes_before_freeze(
    tmp_path: Path,
    layer: str,
    repo_path: str,
) -> None:
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_bytes("你好\n".encode())
    manifest = _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "succeeded"
    completed = json.loads(manifest.read_text(encoding="utf-8"))
    assert completed["files"][0]["content_sha256"] == (
        "sha256:4e0826721642ed8e3a27e7147538ac7b7013a08fe5ae343a8ef09749b7e5790f"
    )
    assert result.write_set_id is not None
    write_set = store.load_write_set(result.write_set_id)
    logical_manifest = f"change:codegen/{layer}-generated-files.json"
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert write_set.outputs_sha256[logical_manifest] == manifest_sha
    assert store.read_object(manifest_sha) == manifest.read_bytes()


def test_codegen_manifest_overwrites_agent_supplied_digest_and_sorts_files(tmp_path: Path) -> None:
    layer = "api"
    _, store, workspace = _workspace(tmp_path, layer=layer)
    first = "tests/api/a.py"
    second = "tests/api/z.py"
    for repo_path, content in ((first, "你好\n".encode()), (second, b"second\n")):
        path = workspace.project_root / repo_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": second,
                    "disposition": "reused",
                    "role": "support",
                    "case_ids": [],
                    "content_sha256": "sha256:" + "0" * 64,
                },
                {
                    "repo_path": first,
                    "disposition": "updated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                    "content_sha256": "sha256:" + "f" * 64,
                },
            ],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(first, second))

    assert result.status == "succeeded"
    completed = json.loads(manifest.read_text(encoding="utf-8"))
    assert [item["repo_path"] for item in completed["files"]] == [first, second]
    assert [item["content_sha256"] for item in completed["files"]] == [
        "sha256:4e0826721642ed8e3a27e7147538ac7b7013a08fe5ae343a8ef09749b7e5790f",
        "sha256:480c2336b410f1ad5f8bf1b28944490255804b65350c527787e74ebdd511e3a4",
    ]


def test_codegen_manifest_rejects_malformed_legacy_digest(tmp_path: Path) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_text("test\n", encoding="utf-8")
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                    "content_sha256": "not-a-legacy-sha256",
                }
            ],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "content_sha256" in (result.error or "")


@pytest.mark.parametrize(
    ("change_id", "manifest_layer", "error_fragment"),
    (
        ("CH-WRONG", "api", "change_id"),
        ("CH-1", "e2e", "layer"),
    ),
)
def test_codegen_manifest_rejects_wrong_change_or_layer(
    tmp_path: Path,
    change_id: str,
    manifest_layer: str,
    error_fragment: str,
) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_text("test\n", encoding="utf-8")
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": change_id,
            "layer": manifest_layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert error_fragment in (result.error or "")
    assert result.write_set_id is None


def test_codegen_manifest_rejects_duplicate_repo_paths(tmp_path: Path) -> None:
    layer = "fuzz"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_text("test\n", encoding="utf-8")
    entry = {
        "repo_path": repo_path,
        "disposition": "generated",
        "role": "test_entry",
        "case_ids": ["CASE-1"],
    }
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [entry, entry],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "unique" in (result.error or "")
    assert result.write_set_id is None


@pytest.mark.parametrize(
    ("repo_path", "prepare", "error_fragment"),
    (
        ("/tmp/absolute.py", "none", "project-relative"),
        ("../escape.py", "none", "project-relative"),
        ("tests/api/*.py", "none", "project-relative"),
        ("tests/api/missing.py", "none", "missing"),
        ("tests/api/directory.py", "directory", "regular file"),
        ("tests/api/link.py", "symlink", "symlink"),
    ),
)
def test_codegen_manifest_rejects_unsafe_or_unreadable_repo_file(
    tmp_path: Path,
    repo_path: str,
    prepare: str,
    error_fragment: str,
) -> None:
    layer = "api"
    _, store, workspace = _workspace(tmp_path, layer=layer)
    candidate = workspace.project_root / repo_path
    if prepare == "directory":
        candidate.mkdir(parents=True)
    elif prepare == "symlink":
        target = workspace.project_root / "tests" / "api" / "real.py"
        target.parent.mkdir(parents=True)
        target.write_text("real\n", encoding="utf-8")
        os.symlink(target.name, candidate)
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert error_fragment in (result.error or "")
    assert result.write_set_id is None


def test_codegen_manifest_cannot_list_itself_as_a_repo_file(tmp_path: Path) -> None:
    layer = "performance"
    repo_path = "qa/changes/CH-1/codegen/performance-generated-files.json"
    _, store, workspace = _workspace(tmp_path, layer=layer)
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "support",
                    "case_ids": [],
                }
            ],
        },
    )

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "must not list itself" in (result.error or "")
    assert result.write_set_id is None


def test_codegen_manifest_rejects_symlinked_parent_before_external_write(tmp_path: Path) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_text("test\n", encoding="utf-8")

    external_codegen = tmp_path / "outside" / "codegen"
    external_codegen.mkdir(parents=True)
    manifest = external_codegen / "api-generated-files.json"
    original = json.dumps(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        }
    ).encode()
    manifest.write_bytes(original)
    os.symlink(external_codegen, workspace.change_dir / "codegen", target_is_directory=True)

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert manifest.read_bytes() == original
    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "symlink" in (result.error or "")


def test_codegen_manifest_rejects_fifo_without_waiting_for_writer(tmp_path: Path) -> None:
    layer = "api"
    _, store, workspace = _workspace(tmp_path, layer=layer)
    manifest = workspace.change_dir / "codegen" / "api-generated-files.json"
    manifest.parent.mkdir(parents=True)
    os.mkfifo(manifest)
    results: list[TaskResult] = []
    errors: list[BaseException] = []

    def finalize() -> None:
        try:
            results.append(_finalize(store, workspace, layer=layer, repo_paths=()))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=finalize, daemon=True)
    worker.start()
    worker.join(timeout=1.0)
    blocked = worker.is_alive()
    if blocked:
        unblock_fd = os.open(manifest, os.O_RDWR | getattr(os, "O_NONBLOCK", 0))
        os.close(unblock_fd)
        worker.join(timeout=2.0)

    assert not blocked, "manifest FIFO open waited for a writer before validating file type"
    assert not errors
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error_kind == "invalid_output"
    assert "regular file" in (results[0].error or "")


def test_codegen_manifest_rejects_hardlink_alias_to_itself(tmp_path: Path) -> None:
    layer = "api"
    repo_path = "tests/api/manifest-alias.json"
    _, store, workspace = _workspace(tmp_path, layer=layer)
    manifest = _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "support",
                    "case_ids": [],
                }
            ],
        },
    )
    alias = workspace.project_root / repo_path
    alias.parent.mkdir(parents=True)
    os.link(manifest, alias)

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "must not list itself" in (result.error or "")
    assert result.write_set_id is None


def test_cleanup_close_failure_preserves_primary_error_and_closes_remaining_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = "api"
    _, store, workspace = _workspace(tmp_path, layer=layer)
    manifest = workspace.change_dir / "codegen" / "api-generated-files.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{", encoding="utf-8")
    original_open = os.open
    original_close = os.close
    opened_fds: list[int] = []
    injected = False

    def tracking_open(*args: object, **kwargs: object) -> int:
        fd = original_open(*args, **kwargs)  # type: ignore[arg-type]
        opened_fds.append(fd)
        return fd

    def close_once_with_error(fd: int) -> None:
        nonlocal injected
        original_close(fd)
        if not injected:
            injected = True
            raise OSError("injected cleanup close failure")

    monkeypatch.setattr(codegen_manifest_module.os, "open", tracking_open)
    monkeypatch.setattr(codegen_manifest_module.os, "close", close_once_with_error)
    try:
        result = _finalize(store, workspace, layer=layer, repo_paths=())

        assert result.status == "failed"
        assert result.error_kind == "invalid_output"
        assert "not readable JSON" in (result.error or "")
        assert "cleanup close" not in (result.error or "")
        for fd in opened_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for fd in opened_fds:
            try:
                original_close(fd)
            except OSError:
                pass


def test_cleanup_unlink_failure_does_not_mask_manifest_replace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"generated bytes\n")
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )
    original_unlink = os.unlink

    def replace_fails(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected manifest replace failure")

    def cleanup_unlink_fails(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("injected cleanup unlink failure")

    monkeypatch.setattr(codegen_manifest_module.os, "replace", replace_fails)
    monkeypatch.setattr(codegen_manifest_module.os, "unlink", cleanup_unlink_fails)
    try:
        result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

        assert result.status == "failed"
        assert result.error_kind == "invalid_output"
        assert "manifest replace failure" in (result.error or "")
        assert "cleanup unlink" not in (result.error or "")
    finally:
        for temp_path in (workspace.change_dir / "codegen").glob("*.runtime.tmp"):
            try:
                original_unlink(temp_path)
            except FileNotFoundError:
                pass


def test_temp_fd_close_error_is_not_retried_after_the_fd_may_have_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = "api"
    repo_path = _LAYERS[layer]
    _, store, workspace = _workspace(tmp_path, layer=layer)
    generated = workspace.project_root / repo_path
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"generated bytes\n")
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["CASE-1"],
                }
            ],
        },
    )
    original_open = os.open
    original_close = os.close
    temp_fd: int | None = None
    temp_close_attempts = 0

    def tracking_open(*args: object, **kwargs: object) -> int:
        nonlocal temp_fd
        fd = original_open(*args, **kwargs)  # type: ignore[arg-type]
        if isinstance(args[0], str) and args[0].endswith(".runtime.tmp"):
            temp_fd = fd
        return fd

    def close_temp_once_with_error(fd: int) -> None:
        nonlocal temp_close_attempts
        if fd == temp_fd:
            temp_close_attempts += 1
            original_close(fd)
            if temp_close_attempts == 1:
                raise OSError("injected temp close failure")
            return
        original_close(fd)

    monkeypatch.setattr(codegen_manifest_module.os, "open", tracking_open)
    monkeypatch.setattr(codegen_manifest_module.os, "close", close_temp_once_with_error)

    result = _finalize(store, workspace, layer=layer, repo_paths=(repo_path,))

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "temp close failure" in (result.error or "")
    assert temp_close_attempts == 1


def test_codegen_manifest_cannot_make_runtime_hash_an_unauthorized_repo_file(
    tmp_path: Path,
) -> None:
    layer = "api"
    repo_path = "app/private.py"
    project = tmp_path / "project"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    private = project / repo_path
    private.parent.mkdir(parents=True)
    private.write_text("secret = True\n", encoding="utf-8")
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id=f"{layer}-codegen",
        base_tree_id=store.capture(project),
        store=store,
    )
    _write_manifest(
        workspace,
        layer,
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "reused",
                    "role": "support",
                    "case_ids": [],
                }
            ],
        },
    )
    manifest_claim = ResourcePath.parse("change:codegen/api-generated-files.json")
    tests_claim = ResourcePath.parse("repo:tests/api/**")
    task = _task(layer, (repo_path,)).model_copy(
        update={
            "resources": ResourceClaims(
                writes=(manifest_claim, tests_claim),
                authorization_writes=(manifest_claim, tests_claim),
            )
        }
    )

    result = _finalize(
        store,
        workspace,
        layer=layer,
        repo_paths=(repo_path,),
        task=task,
    )

    assert result.status == "failed"
    assert result.error_kind == "invalid_output"
    assert "outside authorization_writes" in (result.error or "")
    assert result.write_set_id is None
