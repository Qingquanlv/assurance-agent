"""Allocate-time fixer-authority binding from write sets / imported markers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.generated_files import ApiGeneratedFilesV1
from assurance_agent.artifacts.models.healing_codegen import FixerAuthorityTargetV1, FixerAuthorityV1
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.precommit import GENERATED_FILES_CANDIDATE_V1
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef
from assurance_agent.workflow.graph.task_inputs import (
    TaskInputSnapshotEntryV1,
    TaskInputSnapshotV1,
    _entries_input_sha256,
    store_task_input_snapshot,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from assurance_agent.workflow.healing.operations import (
    AUTHORITY_REL,
    allocate_authority_bindings_from_artifacts,
    enhance_allocate_result_with_authority,
    operation_fixer_authority_ready,
)
from tests.helpers_aa import write_aa_config


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params={},
    )


def _task(with_params: dict) -> ExecutableTask:
    return ExecutableTask(
        task_id="allocate-1",
        invocation_id="inv-1",
        checkpoint_ns="inv-1",
        graph_id="healing",
        node_id="allocate",
        structural_path="healing/allocate",
        input={"with": with_params},
        input_sha256="in",
        contract_digest="cd",
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=30.0, heartbeat_seconds=10.0),
        target="operation:allocate-healing-attempt",
        resources=ResourceClaims(),
    )


def test_imported_codegen_yields_unverified_authority(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id="t1",
        base_tree_id=store.capture(tmp_path),
        store=store,
    )
    bindings = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(tmp_path),
        active_targets=["api"],
        params={"imported": True},
    )
    assert bindings["api"]["status"] == "unverified"
    assert bindings["api"]["paths"] == []


def test_authority_bindings_passthrough_only_when_artifacts_absent(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    tree_id = store.capture(tmp_path)
    workspace = WorkspaceBackend(change).create(task_id="t1", base_tree_id=tree_id, store=store)
    seam = {
        "api": {
            "status": "ready",
            "codegen_attempt_id": "cg-1",
            "generated_files_sha256": "sha256:" + "a" * 64,
            "summary_sha256": "sha256:" + "b" * 64,
            "write_set_id": "ws-seam",
            "execution_batch_id": "batch-1",
            "paths": [
                {
                    "repo_path": "tests/api/test_login.py",
                    "disposition": "generated",
                    "content_sha256": "sha256:" + "c" * 64,
                }
            ],
        }
    }
    bindings = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(tmp_path),
        active_targets=["api"],
        params={"authority_bindings": seam},
    )
    assert bindings["api"]["status"] == "ready"
    assert bindings["api"]["write_set_id"] == "ws-seam"


def test_allocate_binds_generated_after_digest_from_write_set(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    project = tmp_path
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    store = TreeStore(change)
    tree_id = store.capture(project)
    workspace = WorkspaceBackend(change).create(task_id="t1", base_tree_id=tree_id, store=store)

    body = b"def test_login():\n    assert True\n"
    after = hashlib.sha256(body).hexdigest()
    out = workspace.project_root / "tests" / "api" / "test_login.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)

    manifest = ApiGeneratedFilesV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": "api",
            "files": [
                {
                    "repo_path": "tests/api/test_login.py",
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["API_001"],
                    "content_sha256": f"sha256:{after}",
                }
            ],
        }
    )
    (workspace.change_dir / "codegen").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "codegen" / "api-generated-files.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    (workspace.change_dir / "codegen" / "api-codegen-summary.md").write_bytes(b"# summary\n")

    write_set = store.freeze_write_set(
        workspace,
        claims=ResourceClaims(
            writes=(
                ResourcePath.parse("repo:tests/api/**"),
                ResourcePath.parse("change:codegen/**"),
            ),
            authorization_writes=(
                ResourcePath.parse("repo:tests/api/**"),
                ResourcePath.parse("change:codegen/**"),
            ),
        ),
        outputs=(
            "change:codegen/api-codegen-summary.md",
            "change:codegen/api-generated-files.json",
        ),
    )

    bindings = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(project),
        active_targets=["api"],
        params={
            "codegen_write_set_ids": {"api": write_set.write_set_id},
            "codegen_attempt_ids": {"api": "cg-api-1"},
            "execution_batch_id": "batch-1",
        },
    )
    assert bindings["api"]["status"] == "ready"
    assert bindings["api"]["write_set_id"] == write_set.write_set_id
    paths = bindings["api"]["paths"]
    assert len(paths) == 1
    assert paths[0].repo_path == "tests/api/test_login.py"
    assert paths[0].content_sha256 == f"sha256:{after}"

    result = enhance_allocate_result_with_authority(
        task=_task(
            {
                "active_targets": ["api"],
                "codegen_write_set_ids": {"api": write_set.write_set_id},
                "codegen_attempt_ids": {"api": "cg-api-1"},
                "execution_batch_id": "batch-1",
            }
        ),
        workspace=workspace,
        context=_context(project),
        allocation={
            "episode_id": "ep",
            "attempt_id": "ha-1",
            "attempt_number": 1,
            "operation_id": "op",
            "source_batch_id": "batch-1",
            "entry_batch_id": "batch-1",
            "baseline_sha256": "sha256:" + "f" * 64,
        },
        attempt_id="ha-1",
        emit_durable_effect=False,
    )
    assert result.status == "succeeded"
    authority_path = workspace.change_dir / "healing" / "fixer-authority.json"
    assert authority_path.is_file()
    dumped = json.loads(authority_path.read_text(encoding="utf-8"))
    assert dumped["targets"][0]["status"] == "ready"
    assert dumped["targets"][0]["paths"][0]["content_sha256"] == f"sha256:{after}"
    assert sha256_bytes(canonical_json_bytes(dumped)).startswith("sha256:")


def _seed_reused_only_workspace(
    tmp_path: Path,
) -> tuple[Path, TaskWorkspace, TreeStore, str, str]:
    """Return project, workspace, store, write_set_id, content digest for a reused private-root path."""
    write_aa_config(tmp_path)
    project = tmp_path
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    body = b"def test_existing():\n    assert True\n"
    digest = hashlib.sha256(body).hexdigest()
    (project / "tests" / "api" / "existing.py").write_bytes(body)
    store = TreeStore(change)
    tree_id = store.capture(project)
    workspace = WorkspaceBackend(change).create(task_id="t-reuse", base_tree_id=tree_id, store=store)

    manifest = ApiGeneratedFilesV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": "api",
            "files": [
                {
                    "repo_path": "tests/api/existing.py",
                    "disposition": "reused",
                    "role": "test_entry",
                    "case_ids": ["API_REUSE"],
                    "content_sha256": f"sha256:{digest}",
                }
            ],
        }
    )
    (workspace.change_dir / "codegen").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "codegen" / "api-generated-files.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    (workspace.change_dir / "codegen" / "api-codegen-summary.md").write_bytes(b"# summary\n")
    write_set = store.freeze_write_set(
        workspace,
        claims=ResourceClaims(
            writes=(ResourcePath.parse("change:codegen/**"),),
            authorization_writes=(ResourcePath.parse("change:codegen/**"),),
        ),
        outputs=(
            "change:codegen/api-codegen-summary.md",
            "change:codegen/api-generated-files.json",
        ),
    )
    return project, workspace, store, write_set.write_set_id, digest


def test_reused_authority_fail_closed_without_or_mismatched_snapshot(tmp_path: Path) -> None:
    project, workspace, _store, write_set_id, digest = _seed_reused_only_workspace(tmp_path)
    base_params = {
        "codegen_write_set_ids": {"api": write_set_id},
        "codegen_attempt_ids": {"api": "cg-api-1"},
        "execution_batch_id": "batch-1",
    }

    # No snapshot entries → cannot prove reuse → unverified.
    missing = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(project),
        active_targets=["api"],
        params=base_params,
    )
    assert missing["api"]["status"] == "unverified"
    assert missing["api"]["paths"] == []

    empty = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(project),
        active_targets=["api"],
        params={**base_params, "input_snapshot_entries": []},
    )
    assert empty["api"]["status"] == "unverified"

    # Snapshot present but digest mismatch → unverified.
    mismatched = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(project),
        active_targets=["api"],
        params={
            **base_params,
            "input_snapshot_entries": [
                {
                    "kind": "file",
                    "repo_relpath": "tests/api/existing.py",
                    "logical_aliases": ["repo:tests/api/existing.py"],
                    "sha256": "sha256:" + "0" * 64,
                }
            ],
        },
    )
    assert mismatched["api"]["status"] == "unverified"
    assert mismatched["api"]["paths"] == []

    # Matching snapshot digest → ready with reused path authority.
    matched = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(project),
        active_targets=["api"],
        params={
            **base_params,
            "input_snapshot_entries": [
                {
                    "kind": "file",
                    "repo_relpath": "tests/api/existing.py",
                    "logical_aliases": ["repo:tests/api/existing.py", "project:tests/api/existing.py"],
                    "sha256": f"sha256:{digest}",
                }
            ],
        },
    )
    assert matched["api"]["status"] == "ready"
    paths = matched["api"]["paths"]
    assert len(paths) == 1
    assert paths[0].disposition == "reused"
    assert paths[0].content_sha256 == f"sha256:{digest}"


def _seed_committed_api_codegen_events(
    change: Path,
    *,
    write_set_id: str,
    snapshot_id: str | None = None,
    task_id: str = "api-branch:codegen",
    inv: str = "inv-root",
) -> None:
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": inv,
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "gd-1",
            "contract_digests": {},
            "params": {},
            "params_sha256": "p" * 64,
            "root_tree_id": "tree-root",
            "max_parallel_tasks": 4,
            "checkpoint_ns": inv,
            "structural_path": "main",
        },
    )
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "task_attempt_started",
            "invocation_id": inv,
            "checkpoint_ns": inv,
            "superstep_id": "ss-codegen",
            "task_id": task_id,
            "attempt_id": f"{task_id}-a1",
            "node_id": "codegen",
            "input_sha256": "in-1",
            "graph_digest": "gd-1",
            "contract_digest": "cd-1",
            "attempt_number": 1,
            "lease_expires_at": "2026-08-01T00:00:00+00:00",
            "started_at": "2026-08-01T00:00:00+00:00",
            "input_snapshot_id": snapshot_id,
            "precommit_validator": GENERATED_FILES_CANDIDATE_V1,
            "target": "skill:aa-api-codegen",
        },
    )
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": inv,
            "checkpoint_ns": inv,
            "superstep_id": "ss-codegen",
            "task_id": task_id,
            "attempt_id": f"{task_id}-a1",
            "write_set_id": write_set_id,
            "outputs_sha256": {
                "change:codegen/api-codegen-summary.md": "s" * 64,
                "change:codegen/api-generated-files.json": "m" * 64,
            },
            "input_snapshot_id": snapshot_id,
            "candidate_validation_receipt_id": "receipt-1",
        },
    )
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "superstep_committed",
            "invocation_id": inv,
            "checkpoint_ns": inv,
            "superstep_id": "ss-codegen",
            "checkpoint_id": "cp-codegen",
            "parent_checkpoint_id": None,
            "write_set_ids": [write_set_id],
            "target_tree_id": "tree-after-codegen",
            "state_values": {},
            "committed_task_ids": [task_id],
        },
    )


def test_allocate_binds_ready_from_production_ledger_without_with_params(tmp_path: Path) -> None:
    """Packaged allocate discovers committed codegen write-set IDs from the ledger."""
    write_aa_config(tmp_path)
    project = tmp_path
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    store = TreeStore(change)
    tree_id = store.capture(project)
    workspace = WorkspaceBackend(change).create(task_id="allocate-1", base_tree_id=tree_id, store=store)

    body = b"def test_login():\n    assert True\n"
    after = hashlib.sha256(body).hexdigest()
    out = workspace.project_root / "tests" / "api" / "test_login.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    manifest = ApiGeneratedFilesV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": "api",
            "files": [
                {
                    "repo_path": "tests/api/test_login.py",
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["API_001"],
                    "content_sha256": f"sha256:{after}",
                }
            ],
        }
    )
    (workspace.change_dir / "codegen").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "codegen" / "api-generated-files.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    (workspace.change_dir / "codegen" / "api-codegen-summary.md").write_bytes(b"# summary\n")
    write_set = store.freeze_write_set(
        workspace,
        claims=ResourceClaims(
            writes=(
                ResourcePath.parse("repo:tests/api/**"),
                ResourcePath.parse("change:codegen/**"),
            ),
            authorization_writes=(
                ResourcePath.parse("repo:tests/api/**"),
                ResourcePath.parse("change:codegen/**"),
            ),
        ),
        outputs=(
            "change:codegen/api-codegen-summary.md",
            "change:codegen/api-generated-files.json",
        ),
    )
    (workspace.change_dir / "healing").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "healing" / "fix-proposal.json").write_text(
        json.dumps(
            {
                "proposals": [
                    {
                        "proposal_id": "FIX_001",
                        "target": "api",
                        "eligible": True,
                        "risk_level": "low",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _seed_committed_api_codegen_events(change, write_set_id=write_set.write_set_id)

    # No codegen_write_set_ids in with — production packaged allocate path.
    result = enhance_allocate_result_with_authority(
        task=_task({"active_targets": ["api"], "execution_batch_id": "batch-1"}),
        workspace=workspace,
        context=_context(project),
        allocation={
            "episode_id": "ep",
            "attempt_id": "ha-1",
            "attempt_number": 1,
            "operation_id": "op",
            "source_batch_id": "batch-1",
            "entry_batch_id": "batch-1",
            "baseline_sha256": "sha256:" + "f" * 64,
        },
        attempt_id="ha-1",
        emit_durable_effect=False,
    )
    assert result.status == "succeeded"
    dumped = json.loads((workspace.change_dir / AUTHORITY_REL).read_text(encoding="utf-8"))
    assert dumped["targets"][0]["status"] == "ready"
    assert dumped["targets"][0]["write_set_id"] == write_set.write_set_id
    ready = operation_fixer_authority_ready(
        _task({}),
        workspace,
        _context(project),
    )
    assert ready.value == {"route": "pass"}


def test_allocate_binds_reused_from_ledger_snapshot_entries(tmp_path: Path) -> None:
    project, workspace, store, write_set_id, digest = _seed_reused_only_workspace(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    entries = [
        TaskInputSnapshotEntryV1(
            physical_relpath="tests/api/existing.py",
            repo_relpath="tests/api/existing.py",
            logical_aliases=["project:tests/api/existing.py", "repo:tests/api/existing.py"],
            matched_claims=["repo:tests/api/**"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=f"sha256:{digest}",
            symlink_target=None,
        )
    ]
    entries = sorted(entries, key=lambda item: item.physical_relpath)
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id="inv-root",
        task_id="api-branch:codegen",
        attempt_id="api-branch:codegen-a1",
        base_tree_id="tree-root",
        materialized_tree_id="tree-root",
        input_sha256=_entries_input_sha256(entries),
        runtime_context_sha256=None,
        contract_digest="sha256:" + "c" * 64,
        claims_digest="sha256:" + "d" * 64,
        entries=list(entries),
    )
    raw = canonical_json_bytes(snapshot)
    snapshot_id = hashlib.sha256(raw).hexdigest()
    store_task_input_snapshot(store, snapshot_id, raw)
    _seed_committed_api_codegen_events(change, write_set_id=write_set_id, snapshot_id=snapshot_id)

    result = enhance_allocate_result_with_authority(
        task=_task({"active_targets": ["api"], "execution_batch_id": "batch-1"}),
        workspace=workspace,
        context=_context(project),
        allocation={
            "episode_id": "ep",
            "attempt_id": "ha-1",
            "attempt_number": 1,
            "operation_id": "op",
            "source_batch_id": "batch-1",
            "entry_batch_id": "batch-1",
            "baseline_sha256": "sha256:" + "f" * 64,
        },
        attempt_id="ha-1",
        emit_durable_effect=False,
    )
    assert result.status == "succeeded"
    dumped = json.loads((workspace.change_dir / AUTHORITY_REL).read_text(encoding="utf-8"))
    assert dumped["targets"][0]["status"] == "ready"
    assert dumped["targets"][0]["paths"][0]["disposition"] == "reused"


def test_missing_write_set_binding_reason_is_not_imported(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id="t1",
        base_tree_id=store.capture(tmp_path),
        store=store,
    )
    (workspace.change_dir / "codegen").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "codegen" / "api-generated-files.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": "CH-1",
                "layer": "api",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    (workspace.change_dir / "codegen" / "api-codegen-summary.md").write_text("# s\n", encoding="utf-8")
    # Manifests present but no committed write-set binding → unverified, not imported.
    bindings = allocate_authority_bindings_from_artifacts(
        workspace=workspace,
        context=_context(tmp_path),
        active_targets=["api"],
        params={},
    )
    assert bindings["api"]["status"] == "unverified"
    authority = FixerAuthorityV1(
        schema_version="1",
        change_id="CH-1",
        targets=[FixerAuthorityTargetV1(target="api", status="unverified", paths=[])],
    )
    authority_path = workspace.change_dir / AUTHORITY_REL
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_bytes(canonical_json_bytes(authority) + b"\n")
    ready = operation_fixer_authority_ready(_task({}), workspace, _context(tmp_path))
    assert isinstance(ready.value, dict)
    assert ready.value.get("route") == "stop"
    assert ready.value.get("reason") == "missing_codegen_write_set_binding"


def test_imported_codegen_stop_reason_remains_distinct(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id="t1",
        base_tree_id=store.capture(tmp_path),
        store=store,
    )
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "inv-root",
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "gd-1",
            "contract_digests": {},
            "params": {},
            "params_sha256": "p" * 64,
            "root_tree_id": "tree-root",
            "max_parallel_tasks": 4,
            "checkpoint_ns": "inv-root",
            "structural_path": "main",
        },
    )
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "task_imported",
            "invocation_id": "inv-root",
            "checkpoint_ns": "inv-root",
            "graph_id": "api-branch",
            "node_id": "codegen",
            "structural_path": "main/api/api-branch",
            "outputs_sha256": {
                "change:codegen/api-generated-files.json": "m" * 64,
                "change:codegen/api-codegen-summary.md": "s" * 64,
            },
        },
    )
    authority = FixerAuthorityV1(
        schema_version="1",
        change_id="CH-1",
        targets=[FixerAuthorityTargetV1(target="api", status="unverified", paths=[])],
    )
    (workspace.change_dir / AUTHORITY_REL).parent.mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / AUTHORITY_REL).write_bytes(canonical_json_bytes(authority) + b"\n")
    ready = operation_fixer_authority_ready(_task({}), workspace, _context(tmp_path))
    assert ready.value == {"route": "stop", "reason": "unverified_imported_codegen"}
