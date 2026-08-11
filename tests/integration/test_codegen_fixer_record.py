"""Dark-ship integration: drive healing ops through registered operation handlers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.healing_codegen import (
    ApiCodegenFixApplyIntentV1,
    FixerAuthorityPathV1,
    FixerAuthorityTargetV1,
    FixerAuthorityV1,
)
from assurance_agent.workflow.graph.contracts import (
    ExecutionContract,
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
)
from assurance_agent.workflow.graph.durable_effects import (
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
    production_effect_registry,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.precommit import (
    CODEGEN_FIX_CANDIDATE_V1,
    PrecommitValidationContext,
    validate_candidate,
)
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef
from assurance_agent.workflow.graph.task_inputs import (
    TaskInputSnapshotEntryV1,
    TaskInputSnapshotV1,
    _entries_input_sha256,
    load_task_input_snapshot,
    store_task_input_snapshot,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

_CONTRACT_DIGEST = "sha256:" + "c" * 64
_POLICY_DIGEST = "sha256:" + "d" * 64


def _store(change_dir: Path) -> TreeStore:
    return TreeStore(change_dir)


def _workspace(project: Path, task_id: str = "task-1") -> tuple[TaskWorkspace, TreeStore]:
    change = project / "qa" / "changes" / "CH-1"
    store = _store(change)
    tree_id = store.capture(project)
    workspace = WorkspaceBackend(change).create(task_id=task_id, base_tree_id=tree_id, store=store)
    return workspace, store


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params={},
    )


def _task(target: str, *, with_params: dict | None = None) -> ExecutableTask:
    return ExecutableTask(
        task_id=f"task-{target.split(':')[-1]}",
        invocation_id="inv-heal-1",
        checkpoint_ns="inv-heal-1",
        graph_id="healing",
        node_id=target.split(":")[-1],
        structural_path=f"healing/{target}",
        input={"with": with_params or {}, "context": {"change_id": "CH-1"}},
        input_sha256="in-1",
        contract_digest="cd-1",
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=60.0, heartbeat_seconds=10.0),
        target=target,
        resources=ResourceClaims(),
    )


def _store_bytes(store: TreeStore, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    store._write_object(digest, data)  # noqa: SLF001
    return digest


def _build_fixer_snapshot(
    store: TreeStore,
    *,
    attempt_id: str,
    base_tree_id: str,
    artifacts: dict[str, bytes],
) -> str:
    entries: list[TaskInputSnapshotEntryV1] = []
    for logical, raw in sorted(artifacts.items()):
        digest = _store_bytes(store, raw)
        rel = logical.removeprefix("change:")
        entries.append(
            TaskInputSnapshotEntryV1(
                physical_relpath=f"qa/changes/CH-1/{rel}",
                repo_relpath=f"qa/changes/CH-1/{rel}",
                logical_aliases=[logical],
                matched_claims=["change:healing/**"],
                origins=["contract_read"],
                kind="file",
                mode=0o644,
                sha256=f"sha256:{digest}",
                symlink_target=None,
            )
        )
    entries = sorted(entries, key=lambda item: item.physical_relpath)
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id="inv-heal-1",
        task_id="fixer-api",
        attempt_id=attempt_id,
        base_tree_id=base_tree_id,
        materialized_tree_id=base_tree_id,
        input_sha256=_entries_input_sha256(entries),
        runtime_context_sha256=None,
        contract_digest=_CONTRACT_DIGEST,
        claims_digest="sha256:" + "e" * 64,
        entries=list(entries),
    )
    raw = canonical_json_bytes(snapshot)
    snapshot_id = hashlib.sha256(raw).hexdigest()
    store_task_input_snapshot(store, snapshot_id, raw)
    return snapshot_id


def _seed_noop_candidate_receipt(
    workspace: TaskWorkspace,
    store: TreeStore,
    *,
    authority: FixerAuthorityV1,
    intent: ApiCodegenFixApplyIntentV1,
) -> tuple[str, str, PrecommitValidationContext, dict[str, object]]:
    """Freeze a no_op fixer candidate; return receipt_id, write_set_id, context, verify bundle."""
    change = workspace.change_dir
    healing = change / "healing"
    healing.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": "1.0",
        "summary": {"eligible_count": 1},
        "proposals": [
            {
                "proposal_id": "FIX-1",
                "target": "api",
                "eligible": True,
                "files_to_modify": ["tests/api/test_login.py"],
                "risk_level": "low",
            }
        ],
    }
    baseline = {
        "schema_version": "1",
        "entry_batch_id": "batch-1",
        "episode_id": "ep-1",
    }
    (healing / "fix-proposal.json").write_bytes(canonical_json_bytes(proposal) + b"\n")
    (healing / "fixer-authority.json").write_bytes(canonical_json_bytes(authority) + b"\n")
    (healing / "entry-baseline.json").write_bytes(canonical_json_bytes(baseline) + b"\n")
    (healing / "api-apply-intent.json").write_bytes(canonical_json_bytes(intent) + b"\n")

    write_set = store.freeze_write_set(
        workspace,
        claims=ResourceClaims(
            writes=(ResourcePath.parse("change:healing/**"),),
            authorization_writes=(ResourcePath.parse("change:healing/**"),),
        ),
        outputs=("change:healing/api-apply-intent.json",),
    )
    attempt_id = "fix-api-1"
    snapshot_id = _build_fixer_snapshot(
        store,
        attempt_id=attempt_id,
        base_tree_id=workspace.base_tree_id,
        artifacts={
            "change:healing/fix-proposal.json": canonical_json_bytes(proposal),
            "change:healing/fixer-authority.json": canonical_json_bytes(authority),
            "change:healing/entry-baseline.json": canonical_json_bytes(baseline),
        },
    )
    context = PrecommitValidationContext.model_validate(
        {
            "root_invocation_id": "inv-heal-1",
            "invocation_id": "inv-heal-1",
            "task_id": "fixer-api",
            "attempt_id": attempt_id,
            "target": "skill:aa-api-codegen-fixer",
            "base_tree_id": workspace.base_tree_id,
            "current_tree_id": workspace.base_tree_id,
            "input_snapshot_id": snapshot_id,
            "contract_digest": _CONTRACT_DIGEST,
            "policy_object_id": "d" * 64,
            "policy_digest": _POLICY_DIGEST,
            "gate_attempt_id": None,
            "interrupt_id": None,
            "output_digests": dict(sorted(write_set.outputs_sha256.items())),
            "write_set_id": write_set.write_set_id,
            "definition_semantics": {
                "assurance_profile_digest": "unbound",
                "contract_digest": _CONTRACT_DIGEST,
                "gate_semantics_digest": "unbound",
                "graph_digest": "g" * 64,
            },
        }
    )
    receipt_id, _receipt = validate_candidate(
        CODEGEN_FIX_CANDIDATE_V1,
        context,
        store=store,
        write_set=write_set,
        input_snapshot=load_task_input_snapshot(store, snapshot_id),
        plan_text="",
        cases=[],
        change_id="CH-1",
        layer="api",
        current_change_repo_path="qa/changes/CH-1",
        project_root=workspace.project_root,
    )
    verify_bundle = {
        "context": context.model_dump(mode="json"),
        "plan_text": "",
        "cases": [],
        "current_change_repo_path": "qa/changes/CH-1",
    }
    return receipt_id, write_set.write_set_id, context, verify_bundle


def test_api_only_record_and_combine_via_operation_handlers(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    project = tmp_path
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "api" / "test_login.py").write_text("def test_login():\n    assert True\n")
    change = project / "qa" / "changes" / "CH-1"
    (change / "execution").mkdir(parents=True, exist_ok=True)
    (change / "execution" / "execution-manifest.yaml").write_text(
        "batch_id: batch-1\n",
        encoding="utf-8",
    )
    workspace, store = _workspace(project)
    context = _context(project)
    ops = default_operations()
    authority = FixerAuthorityV1(
        schema_version="1",
        change_id="CH-1",
        targets=[
            FixerAuthorityTargetV1(
                target="api",
                status="ready",
                codegen_attempt_id="cg-1",
                generated_files_sha256="sha256:" + "a" * 64,
                summary_sha256="sha256:" + "b" * 64,
                write_set_id="ws-1",
                execution_batch_id="batch-1",
                paths=[
                    FixerAuthorityPathV1(
                        repo_path="tests/api/test_login.py",
                        disposition="generated",
                        content_sha256="sha256:"
                        + hashlib.sha256(
                            (project / "tests" / "api" / "test_login.py").read_bytes()
                        ).hexdigest(),
                    )
                ],
            )
        ],
    )
    ready = ops["operation:fixer-authority-ready"](
        _task("operation:fixer-authority-ready"), workspace, context
    )
    # Authority file not written yet — ready gate stops.
    assert ready.status == "succeeded"
    assert ready.value == {"route": "stop", "reason": "missing_fixer_authority"}

    intent = ApiCodegenFixApplyIntentV1(
        schema_version="1",
        target="api",
        outcome="no_op",
        proposal_ids=[],
        reason="nothing to change",
        claimed_modified_paths=[],
    )
    receipt_id, write_set_id, _precommit, verify_bundle = _seed_noop_candidate_receipt(
        workspace, store, authority=authority, intent=intent
    )
    ready = ops["operation:fixer-authority-ready"](
        _task("operation:fixer-authority-ready"), workspace, context
    )
    assert ready.status == "succeeded"
    assert ready.value == {"route": "pass"}

    record = ops["operation:record-codegen-fix-apply"](
        _task(
            "operation:record-codegen-fix-apply",
            with_params={
                "target": "api",
                "write_set_id": write_set_id,
                "fixer_attempt_id": "fix-api-1",
                "fixer_task_id": "fixer-api",
                "fixer_invocation_id": "inv-heal-1",
                "input_snapshot_id": cast(dict[str, object], verify_bundle["context"])["input_snapshot_id"],
                "candidate_validation_receipt_id": receipt_id,
                "candidate_receipt_verify": verify_bundle,
                "attempt_id": "att-record-1",
                "passed": True,
                "needs_review": False,
            },
        ),
        workspace,
        context,
    )
    assert record.status == "succeeded", record
    assert (workspace.change_dir / "healing" / "api-apply-summary.json").is_file()
    assert (workspace.change_dir / "healing" / "api-fixer-safety-check.json").is_file()
    assert len(record.durable_effects) == 1
    assert record.durable_effects[0]["kind"] == HEAL_RECORD_APPLY_V2

    # Missing receipt fails closed.
    missing = ops["operation:record-codegen-fix-apply"](
        _task(
            "operation:record-codegen-fix-apply",
            with_params={
                "target": "api",
                "write_set_id": write_set_id,
                "fixer_attempt_id": "fix-api-1",
            },
        ),
        workspace,
        context,
    )
    assert missing.status == "failed"

    combine = ops["operation:combine-fixer-safety"](
        _task("operation:combine-fixer-safety", with_params={"active_targets": ["api"]}),
        workspace,
        context,
    )
    assert combine.status == "succeeded"
    assert (workspace.change_dir / "healing" / "fixer-safety-check.json").is_file()
    assert not (workspace.change_dir / "healing" / "e2e-fixer-safety-check.json").exists()


def test_production_registry_and_dark_ship_contracts() -> None:
    kinds = production_effect_registry().kinds()
    assert HEALING_ALLOCATION_V2 in kinds
    assert HEAL_RECORD_APPLY_V2 in kinds
    contract = ExecutionContract(target="operation:allocate-healing-attempt", handler="operation")
    assert contract.durable_effects == ()
    catalog = ExecutionContractCatalog(contracts={contract.target: contract})
    assert catalog.contracts[contract.target].durable_effects == ()


def test_authority_digest_stable() -> None:
    authority = FixerAuthorityV1(
        schema_version="1",
        change_id="CH-1",
        targets=[FixerAuthorityTargetV1(target="api", status="unverified", paths=[])],
    )
    digest = sha256_bytes(canonical_json_bytes(authority))
    assert digest.startswith("sha256:")
