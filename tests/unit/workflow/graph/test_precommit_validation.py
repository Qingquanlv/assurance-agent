"""Candidate validation receipts and generated_files_candidate/v1 (Task 6 / B)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.generated_files import ApiGeneratedFilesV1
from assurance_agent.verification.generated_files import get_generated_files_contract
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
from assurance_agent.workflow.graph.contracts import (
    ContractError,
    ExecutionContract,
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
    catalog_from_pinned_contracts,
    parse_execution_contracts,
)
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    PlanResult,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.precommit import (
    CODEGEN_FIX_CANDIDATE_V1,
    GENERATED_FILES_CANDIDATE_V1,
    CandidateValidationError,
    CandidateValidationReceiptV1,
    PrecommitValidationContext,
    load_candidate_receipt,
    validate_candidate,
    validate_precommit_validator_id,
    validator_semantics_digest,
    verify_candidate_receipt,
)
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.task_inputs import (
    TaskInputSnapshotEntryV1,
    TaskInputSnapshotV1,
    store_task_input_snapshot,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef

_INV = "inv-precommit-1"
_DIGEST = "d" * 64
_CONTRACT_DIGEST = "sha256:" + "c" * 64
_CLAIMS_DIGEST = "sha256:" + "e" * 64


def _context_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "root_invocation_id": "root-1",
        "invocation_id": "inv-1",
        "task_id": "task-1",
        "attempt_id": "task-1-a1",
        "target": "skill:aa-api-codegen",
        "base_tree_id": "a" * 64,
        "current_tree_id": "b" * 64,
        "input_snapshot_id": "c" * 64,
        "contract_digest": _CONTRACT_DIGEST,
        "policy_object_id": "d" * 64,
        "policy_digest": "sha256:" + "d" * 64,
        "gate_attempt_id": None,
        "interrupt_id": None,
        "output_digests": {
            "change:codegen/api-codegen-summary.md": "1" * 64,
            "change:codegen/api-generated-files.json": "2" * 64,
        },
        "write_set_id": "f" * 64,
        "definition_semantics": {
            "assurance_profile_digest": "unbound",
            "contract_digest": _CONTRACT_DIGEST,
            "gate_semantics_digest": "unbound",
            "graph_digest": _DIGEST,
        },
    }
    base.update(overrides)
    return base


def test_precommit_context_and_receipt_wire_contract() -> None:
    context = PrecommitValidationContext.model_validate(_context_payload())
    assert list(context.output_digests) == sorted(context.output_digests)
    receipt = CandidateValidationReceiptV1(
        schema_version="1",
        validator_id=GENERATED_FILES_CANDIDATE_V1,
        validator_semantics_digest=validator_semantics_digest(GENERATED_FILES_CANDIDATE_V1),
        root_invocation_id=context.root_invocation_id,
        invocation_id=context.invocation_id,
        task_id=context.task_id,
        attempt_id=context.attempt_id,
        input_snapshot_id=context.input_snapshot_id,
        output_digests=dict(context.output_digests),
        write_set_id=context.write_set_id,
        decision_payload_sha256="sha256:" + "9" * 64,
    )
    assert canonical_json_bytes(receipt) == canonical_json_bytes(
        CandidateValidationReceiptV1.model_validate(json.loads(canonical_json_bytes(receipt)))
    )


def test_precommit_context_rejects_unsorted_output_keys_and_empty_ids() -> None:
    with pytest.raises(ValidationError):
        PrecommitValidationContext.model_validate(
            _context_payload(
                output_digests={
                    "change:codegen/api-generated-files.json": "2" * 64,
                    "change:codegen/api-codegen-summary.md": "1" * 64,
                }
            )
        )
    with pytest.raises(ValidationError):
        PrecommitValidationContext.model_validate(_context_payload(task_id=""))


def test_validator_registry_load_rules() -> None:
    validate_precommit_validator_id(None)
    validate_precommit_validator_id(GENERATED_FILES_CANDIDATE_V1)
    with pytest.raises(CandidateValidationError, match="not implemented until Task 8"):
        validate_precommit_validator_id(CODEGEN_FIX_CANDIDATE_V1)
    with pytest.raises(CandidateValidationError, match="unknown"):
        validate_precommit_validator_id("not_a_validator/v1")


def test_contract_load_rejects_unknown_and_deferred_validators() -> None:
    with pytest.raises(ContractError, match="unknown precommit validator"):
        parse_execution_contracts(
            """
schema_version: "1"
contracts:
  operation:x:
    handler: operation
    precommit_validator: mystery/v1
"""
        )
    with pytest.raises(ContractError, match="not implemented until Task 8"):
        catalog_from_pinned_contracts(
            (
                ExecutionContract(
                    target="operation:x",
                    handler="operation",
                    precommit_validator=CODEGEN_FIX_CANDIDATE_V1,
                ),
            )
        )
    ok = ExecutionContract(target="operation:y", handler="operation")
    assert ok.precommit_validator is None


# ---------------------------------------------------------------------------
# generated_files_candidate helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "testdata").mkdir(parents=True)
    (change / "plans").mkdir(parents=True)
    (change / "cases").mkdir(parents=True)
    (change / "codegen").mkdir(parents=True)
    (project / "tests" / "api" / "existing.py").write_text("existing\n", encoding="utf-8")
    return project


def _seed_invocation(change: Path, tree_id: str) -> None:
    append_event_strict(
        change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": _INV,
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": _DIGEST,
            "contract_digests": {},
            "params": {},
            "params_sha256": "p" * 64,
            "root_tree_id": tree_id,
            "max_parallel_tasks": 4,
            "checkpoint_ns": _INV,
            "structural_path": "main",
        },
    )


def _api_plan_text() -> str:
    return """# API Codegen Plan

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| API_001 | `test_api_001` | `tests/api/test_new.py` |
| API_REUSE | `test_existing` | `tests/api/existing.py` |
"""


def _api_cases() -> list[dict[str, object]]:
    return [
        {
            "added": [
                {"case_id": "API_001", "type": "API", "automation": {"required": True}},
                {"case_id": "API_REUSE", "type": "API", "automation": {"required": True}},
            ],
            "modified": [],
        }
    ]


def _store_bytes(store: TreeStore, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    store._write_object(digest, data)
    return digest


def _build_snapshot(
    store: TreeStore,
    *,
    project: Path,
    attempt_id: str,
    base_tree_id: str,
    plan_text: str,
) -> str:
    plan_digest = _store_bytes(store, plan_text.encode("utf-8"))
    existing = (project / "tests" / "api" / "existing.py").read_bytes()
    existing_digest = _store_bytes(store, existing)
    entries = [
        TaskInputSnapshotEntryV1(
            physical_relpath="qa/changes/CH-1/plans/api-codegen-plan.md",
            repo_relpath="qa/changes/CH-1/plans/api-codegen-plan.md",
            logical_aliases=["change:plans/api-codegen-plan.md"],
            matched_claims=["change:plans/api-*.md"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=f"sha256:{plan_digest}",
            symlink_target=None,
        ),
        TaskInputSnapshotEntryV1(
            physical_relpath="tests/api/existing.py",
            repo_relpath="tests/api/existing.py",
            logical_aliases=["project:tests/api/existing.py", "repo:tests/api/existing.py"],
            matched_claims=["repo:tests/api/**"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=f"sha256:{existing_digest}",
            symlink_target=None,
        ),
    ]
    entries = sorted(entries, key=lambda item: item.physical_relpath)
    input_sha256 = sha256_bytes(canonical_json_bytes([entry.model_dump(mode="json") for entry in entries]))
    # TaskInputSnapshotV1 recomputes input_sha256 from entries via its own helper.
    from assurance_agent.workflow.graph.task_inputs import _entries_input_sha256

    input_sha256 = _entries_input_sha256(entries)
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id=_INV,
        task_id="codegen-task",
        attempt_id=attempt_id,
        base_tree_id=base_tree_id,
        materialized_tree_id=base_tree_id,
        input_sha256=input_sha256,
        runtime_context_sha256=None,
        contract_digest=_CONTRACT_DIGEST,
        claims_digest=_CLAIMS_DIGEST,
        entries=list(entries),
    )
    raw = canonical_json_bytes(snapshot)
    snapshot_id = hashlib.sha256(raw).hexdigest()
    store_task_input_snapshot(store, snapshot_id, raw)
    return snapshot_id


def _freeze_valid_api_candidate(
    tmp_path: Path,
    *,
    omit_manifest_entry: bool = False,
    wrong_case_ids: bool = False,
) -> tuple[Path, TreeStore, str, str, PrecommitValidationContext, str]:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    (change / "plans" / "api-codegen-plan.md").write_text(_api_plan_text(), encoding="utf-8")
    (change / "cases" / "api.yaml").write_text(
        "added:\n"
        "  - case_id: API_001\n"
        "    type: API\n"
        "    automation: {required: true}\n"
        "  - case_id: API_REUSE\n"
        "    type: API\n"
        "    automation: {required: true}\n"
        "modified: []\n",
        encoding="utf-8",
    )
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    backend = WorkspaceBackend(change)
    claims = ResourceClaims(
        reads=(
            ResourcePath.parse("change:plans/api-*.md"),
            ResourcePath.parse("repo:tests/api/**"),
        ),
        writes=(
            ResourcePath.parse("change:codegen/**"),
            ResourcePath.parse("repo:tests/api/**"),
            ResourcePath.parse("repo:tests/testdata/**"),
        ),
        authorization_writes=(
            ResourcePath.parse("change:codegen/**"),
            ResourcePath.parse("repo:tests/api/**"),
            ResourcePath.parse("repo:tests/testdata/**"),
        ),
    )
    workspace = backend.create(
        task_id="codegen-task",
        base_tree_id=tree_id,
        store=store,
        sidecar_root=backend.sidecar_root_for("codegen-task"),
        claims=claims,
        declared_reads_only=False,
    )
    test_body = b"def test_api_001():\n    assert True\n"
    test_path = workspace.project_root / "tests" / "api" / "test_new.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_bytes(test_body)
    summary = b"# summary\n"
    summary_path = workspace.change_dir / "codegen" / "api-codegen-summary.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(summary)
    content_digest = hashlib.sha256(test_body).hexdigest()
    existing_digest = hashlib.sha256((project / "tests" / "api" / "existing.py").read_bytes()).hexdigest()
    files = [
        {
            "repo_path": "tests/api/existing.py",
            "disposition": "reused",
            "role": "test_entry",
            "case_ids": ["API_REUSE"],
            "content_sha256": f"sha256:{existing_digest}",
        },
        {
            "repo_path": "tests/api/test_new.py",
            "disposition": "generated",
            "role": "test_entry",
            "case_ids": ["API_002" if wrong_case_ids else "API_001"],
            "content_sha256": f"sha256:{content_digest}",
        },
    ]
    if omit_manifest_entry:
        files = [files[0]]  # drop generated write from manifest
    files = sorted(files, key=lambda item: item["repo_path"])
    manifest = ApiGeneratedFilesV1.model_validate(
        {
            "schema_version": "1",
            "change_id": "CH-1",
            "layer": "api",
            "files": files,
        }
    )
    (workspace.change_dir / "codegen" / "api-generated-files.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    outputs = (
        "change:codegen/api-codegen-summary.md",
        "change:codegen/api-generated-files.json",
    )
    write_set = store.freeze_write_set(workspace, claims=claims, outputs=outputs)
    snapshot_id = _build_snapshot(
        store,
        project=project,
        attempt_id="codegen-task-a1",
        base_tree_id=tree_id,
        plan_text=_api_plan_text(),
    )
    context = PrecommitValidationContext.model_validate(
        _context_payload(
            root_invocation_id=_INV,
            invocation_id=_INV,
            task_id="codegen-task",
            attempt_id="codegen-task-a1",
            base_tree_id=tree_id,
            current_tree_id=tree_id,
            input_snapshot_id=snapshot_id,
            output_digests=dict(sorted(write_set.outputs_sha256.items())),
            write_set_id=write_set.write_set_id,
        )
    )
    return project, store, snapshot_id, write_set.write_set_id, context, tree_id


def test_generated_files_candidate_accepts_mapped_add_and_reuse(tmp_path: Path) -> None:
    project, store, _snapshot_id, write_set_id, context, _tree = _freeze_valid_api_candidate(tmp_path)
    write_set = store.load_write_set(write_set_id)
    from assurance_agent.workflow.graph.task_inputs import load_task_input_snapshot

    snapshot = load_task_input_snapshot(store, context.input_snapshot_id)
    receipt_id, receipt = validate_candidate(
        GENERATED_FILES_CANDIDATE_V1,
        context,
        store=store,
        write_set=write_set,
        input_snapshot=snapshot,
        plan_text=_api_plan_text(),
        cases=_api_cases(),
        change_id="CH-1",
        layer="api",
        current_change_repo_path="qa/changes/CH-1",
    )
    assert receipt.validator_id == GENERATED_FILES_CANDIDATE_V1
    loaded = load_candidate_receipt(store, receipt_id)
    assert loaded == receipt
    verify_candidate_receipt(
        receipt,
        context,
        store=store,
        write_set=write_set,
        input_snapshot=snapshot,
        plan_text=_api_plan_text(),
        cases=_api_cases(),
        change_id="CH-1",
        layer="api",
        current_change_repo_path="qa/changes/CH-1",
    )
    assert get_generated_files_contract("api").manifest_path in context.output_digests
    assert project.exists()


def test_generated_files_candidate_rejects_omitted_write(tmp_path: Path) -> None:
    _project, store, _sid, write_set_id, context, _tree = _freeze_valid_api_candidate(
        tmp_path, omit_manifest_entry=True
    )
    write_set = store.load_write_set(write_set_id)
    from assurance_agent.workflow.graph.task_inputs import load_task_input_snapshot

    snapshot = load_task_input_snapshot(store, context.input_snapshot_id)
    with pytest.raises(CandidateValidationError, match="manifest/write-set path set mismatch"):
        validate_candidate(
            GENERATED_FILES_CANDIDATE_V1,
            context,
            store=store,
            write_set=write_set,
            input_snapshot=snapshot,
            plan_text=_api_plan_text(),
            cases=_api_cases(),
            change_id="CH-1",
            layer="api",
            current_change_repo_path="qa/changes/CH-1",
        )


def test_generated_files_candidate_rejects_wrong_case_ids(tmp_path: Path) -> None:
    _project, store, _sid, write_set_id, context, _tree = _freeze_valid_api_candidate(
        tmp_path, wrong_case_ids=True
    )
    write_set = store.load_write_set(write_set_id)
    from assurance_agent.workflow.graph.task_inputs import load_task_input_snapshot

    snapshot = load_task_input_snapshot(store, context.input_snapshot_id)
    with pytest.raises(CandidateValidationError, match="case_ids mismatch"):
        validate_candidate(
            GENERATED_FILES_CANDIDATE_V1,
            context,
            store=store,
            write_set=write_set,
            input_snapshot=snapshot,
            plan_text=_api_plan_text(),
            cases=_api_cases(),
            change_id="CH-1",
            layer="api",
            current_change_repo_path="qa/changes/CH-1",
        )


def test_codegen_fix_candidate_not_dispatchable(tmp_path: Path) -> None:
    _project, store, _sid, write_set_id, context, _tree = _freeze_valid_api_candidate(tmp_path)
    write_set = store.load_write_set(write_set_id)
    from assurance_agent.workflow.graph.task_inputs import load_task_input_snapshot

    snapshot = load_task_input_snapshot(store, context.input_snapshot_id)
    with pytest.raises(CandidateValidationError, match="not implemented until Task 8"):
        validate_candidate(
            CODEGEN_FIX_CANDIDATE_V1,
            context,
            store=store,
            write_set=write_set,
            input_snapshot=snapshot,
            plan_text=_api_plan_text(),
            cases=_api_cases(),
            change_id="CH-1",
            layer="api",
            current_change_repo_path="qa/changes/CH-1",
        )


# ---------------------------------------------------------------------------
# Scheduler no-commit
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    def __init__(self, handler) -> None:
        self._handler = handler

    def execute(self, task, workspace, context) -> TaskResult:
        return self._handler(task, workspace, context)


def _task(task_id: str, target: str, outputs: list[str]) -> ExecutableTask:
    return ExecutableTask(
        task_id=task_id,
        invocation_id=_INV,
        checkpoint_ns=_INV,
        graph_id="main",
        node_id="codegen",
        structural_path="main/codegen",
        input={"with": {"assurance_layer": "api"}, "outputs": outputs},
        input_sha256="i" * 64,
        contract_digest=_CONTRACT_DIGEST,
        retryable_errors=("invalid_output",),
        retry_policy=RetryPolicyDef(max_attempts=1, retry_on=[]),
        timeout_policy=TimeoutPolicyDef(run_seconds=30.0, heartbeat_seconds=30.0),
        target=target,
        resources=ResourceClaims(
            reads=(
                ResourcePath.parse("change:plans/api-*.md"),
                ResourcePath.parse("repo:tests/api/**"),
            ),
            writes=(
                ResourcePath.parse("change:codegen/**"),
                ResourcePath.parse("repo:tests/api/**"),
            ),
            authorization_writes=(
                ResourcePath.parse("change:codegen/**"),
                ResourcePath.parse("repo:tests/api/**"),
            ),
        ),
        declaration_index=0,
    )


def test_scheduler_invalid_generated_files_candidate_does_not_commit(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    (change / "plans" / "api-codegen-plan.md").write_text(_api_plan_text(), encoding="utf-8")
    (change / "cases" / "api.yaml").write_text(
        "added:\n"
        "  - case_id: API_001\n"
        "    type: API\n"
        "    automation: {required: true}\n"
        "  - case_id: API_REUSE\n"
        "    type: API\n"
        "    automation: {required: true}\n"
        "modified: []\n",
        encoding="utf-8",
    )
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    target = "operation:test-api-codegen"
    outputs = [
        "change:codegen/api-codegen-summary.md",
        "change:codegen/api-generated-files.json",
    ]

    def handler(task, workspace, context) -> TaskResult:
        body = b"def test_api_001():\n    assert True\n"
        out = workspace.project_root / "tests" / "api" / "test_new.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        (workspace.change_dir / "codegen").mkdir(parents=True, exist_ok=True)
        (workspace.change_dir / "codegen" / "api-codegen-summary.md").write_bytes(b"# s\n")
        # Shape-valid manifest that omits the generated test write.
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
                        "content_sha256": "sha256:"
                        + hashlib.sha256(
                            (project / "tests" / "api" / "existing.py").read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        )
        (workspace.change_dir / "codegen" / "api-generated-files.json").write_bytes(
            canonical_json_bytes(manifest)
        )
        return TaskResult(status="succeeded")

    task = _task("codegen-task", target, outputs)
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner(handler),
        contracts=ExecutionContractCatalog(
            contracts={
                target: ExecutionContract(
                    target=target,
                    handler="operation",
                    reads=("change:plans/api-*.md", "repo:tests/api/**"),
                    writes=("change:codegen/**", "repo:tests/api/**"),
                    authorization_writes=("change:codegen/**", "repo:tests/api/**"),
                    read_isolation="declared_only",
                    precommit_validator=GENERATED_FILES_CANDIDATE_V1,
                    retryable_errors=("invalid_output",),
                )
            }
        ),
    )
    before_tree = project_invocation(change, _INV).current_tree_id
    result = scheduler.execute(
        PlanResult(superstep_id="ss-1", checkpoint_id="bootstrap", tasks=(task,)),
        project_invocation(change, _INV).model_copy(
            update={"current_tree_id": tree_id, "root_tree_id": tree_id, "graph_digest": _DIGEST}
        ),
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id="CH-1",
        ),
    )
    assert result.succeeded == ()
    assert result.failed == (task.task_id,)
    events = read_events_strict(change)
    assert not any(event.get("type") == "task_attempt_succeeded" for event in events)
    assert not any(event.get("type") == "superstep_committed" for event in events)
    failed = [event for event in events if event.get("type") == "task_attempt_failed"]
    assert len(failed) == 1
    assert failed[0]["error_kind"] == "invalid_output"
    message = failed[0]["message"]
    assert isinstance(message, str)
    assert "manifest/write-set" in message
    assert project_invocation(change, _INV).current_tree_id == before_tree
    assert not (project / "tests" / "api" / "test_new.py").exists()


def test_scheduler_valid_generated_files_candidate_records_receipt(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    (change / "plans" / "api-codegen-plan.md").write_text(_api_plan_text(), encoding="utf-8")
    (change / "cases" / "api.yaml").write_text(
        "added:\n"
        "  - case_id: API_001\n"
        "    type: API\n"
        "    automation: {required: true}\n"
        "  - case_id: API_REUSE\n"
        "    type: API\n"
        "    automation: {required: true}\n"
        "modified: []\n",
        encoding="utf-8",
    )
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    target = "operation:test-api-codegen"
    outputs = [
        "change:codegen/api-codegen-summary.md",
        "change:codegen/api-generated-files.json",
    ]

    def handler(task, workspace, context) -> TaskResult:
        body = b"def test_api_001():\n    assert True\n"
        out = workspace.project_root / "tests" / "api" / "test_new.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        (workspace.change_dir / "codegen").mkdir(parents=True, exist_ok=True)
        (workspace.change_dir / "codegen" / "api-codegen-summary.md").write_bytes(b"# s\n")
        existing_digest = hashlib.sha256((project / "tests" / "api" / "existing.py").read_bytes()).hexdigest()
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
                        "content_sha256": f"sha256:{existing_digest}",
                    },
                    {
                        "repo_path": "tests/api/test_new.py",
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": ["API_001"],
                        "content_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                    },
                ],
            }
        )
        (workspace.change_dir / "codegen" / "api-generated-files.json").write_bytes(
            canonical_json_bytes(manifest)
        )
        return TaskResult(status="succeeded")

    task = _task("codegen-task", target, outputs)
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner(handler),
        contracts=ExecutionContractCatalog(
            contracts={
                target: ExecutionContract(
                    target=target,
                    handler="operation",
                    reads=("change:plans/api-*.md", "repo:tests/api/**"),
                    writes=("change:codegen/**", "repo:tests/api/**"),
                    authorization_writes=("change:codegen/**", "repo:tests/api/**"),
                    read_isolation="declared_only",
                    precommit_validator=GENERATED_FILES_CANDIDATE_V1,
                    retryable_errors=("invalid_output",),
                )
            }
        ),
    )
    result = scheduler.execute(
        PlanResult(superstep_id="ss-1", checkpoint_id="bootstrap", tasks=(task,)),
        project_invocation(change, _INV).model_copy(
            update={"current_tree_id": tree_id, "root_tree_id": tree_id, "graph_digest": _DIGEST}
        ),
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id="CH-1",
        ),
    )
    assert result.succeeded == (task.task_id,)
    success_events = [
        event for event in read_events_strict(change) if event.get("type") == "task_attempt_succeeded"
    ]
    assert len(success_events) == 1
    receipt_id = success_events[0].get("candidate_validation_receipt_id")
    assert isinstance(receipt_id, str) and receipt_id
    receipt = load_candidate_receipt(store, receipt_id)
    assert receipt.validator_id == GENERATED_FILES_CANDIDATE_V1
    assert (project / "tests" / "api" / "test_new.py").is_file()


def test_contract_without_validator_leaves_no_receipt(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    tree_id = store.capture(project)
    _seed_invocation(change, tree_id)
    target = "operation:plain"

    def handler(task, workspace, context) -> TaskResult:
        path = workspace.project_root / "tests" / "api" / "plain.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    task = ExecutableTask(
        task_id="plain-task",
        invocation_id=_INV,
        checkpoint_ns=_INV,
        graph_id="main",
        node_id="plain",
        structural_path="main/plain",
        input={"with": {}, "outputs": ["repo:tests/api/plain.py"]},
        input_sha256="i" * 64,
        contract_digest=_CONTRACT_DIGEST,
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1, retry_on=[]),
        timeout_policy=TimeoutPolicyDef(run_seconds=30.0, heartbeat_seconds=30.0),
        target=target,
        resources=ResourceClaims(
            writes=(ResourcePath.parse("repo:tests/api/**"),),
            authorization_writes=(ResourcePath.parse("repo:tests/api/**"),),
        ),
    )
    scheduler = Scheduler(
        checkpoints=CheckpointStore(change),
        object_store=store,
        workspace_backend=WorkspaceBackend(change),
        node_runner=_ScriptedRunner(handler),
        contracts=ExecutionContractCatalog(
            contracts={
                target: ExecutionContract(
                    target=target,
                    handler="operation",
                    writes=("repo:tests/api/**",),
                    authorization_writes=("repo:tests/api/**",),
                )
            }
        ),
    )
    result = scheduler.execute(
        PlanResult(superstep_id="ss-1", checkpoint_id="bootstrap", tasks=(task,)),
        project_invocation(change, _INV).model_copy(
            update={"current_tree_id": tree_id, "root_tree_id": tree_id, "graph_digest": _DIGEST}
        ),
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id="CH-1",
        ),
    )
    assert result.succeeded == (task.task_id,)
    success_events = [
        event for event in read_events_strict(change) if event.get("type") == "task_attempt_succeeded"
    ]
    assert len(success_events) == 1
    assert success_events[0].get("candidate_validation_receipt_id") is None
