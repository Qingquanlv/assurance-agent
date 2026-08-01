"""Dark-ship integration: drive healing ops through registered operation handlers."""

from __future__ import annotations

import json
from pathlib import Path

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
)
from assurance_agent.workflow.graph.durable_effects import (
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
    production_effect_registry,
)
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config


def _store(project: Path) -> TreeStore:
    return TreeStore(project / "qa" / "changes" / "CH-1" / ".objects")


def _workspace(project: Path, task_id: str = "task-1") -> TaskWorkspace:
    store = _store(project)
    tree_id = store.capture(project)
    return WorkspaceBackend(project / "qa" / "changes" / "CH-1").create(
        task_id=task_id, base_tree_id=tree_id, store=store
    )


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


def test_api_only_record_and_combine_via_operation_handlers(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    project = tmp_path
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    workspace = _workspace(project)
    context = _context(project)
    ops = default_operations()

    (workspace.change_dir / "execution").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "healing").mkdir(parents=True, exist_ok=True)
    (workspace.change_dir / "execution" / "execution-manifest.yaml").write_text(
        "batch_id: batch-1\n",
        encoding="utf-8",
    )
    (workspace.change_dir / "healing" / "fix-proposal.json").write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )
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
                        content_sha256="sha256:" + "c" * 64,
                    )
                ],
            )
        ],
    )
    (workspace.change_dir / "healing" / "fixer-authority.json").write_text(
        canonical_json_bytes(authority).decode("utf-8") + "\n",
        encoding="utf-8",
    )

    ready = ops["operation:fixer-authority-ready"](
        _task("operation:fixer-authority-ready"), workspace, context
    )
    assert ready.status == "succeeded"
    assert ready.value == {"route": "pass"}

    intent = ApiCodegenFixApplyIntentV1(
        schema_version="1",
        target="api",
        outcome="no_op",
        proposal_ids=[],
        reason="nothing to change",
        claimed_modified_paths=[],
    )
    (workspace.change_dir / "healing" / "api-apply-intent.json").write_bytes(
        canonical_json_bytes(intent) + b"\n"
    )
    record = ops["operation:record-codegen-fix-apply"](
        _task(
            "operation:record-codegen-fix-apply",
            with_params={
                "target": "api",
                "write_set_id": "ws-fixer-1",
                "fixer_attempt_id": "fix-api-1",
                "attempt_id": "att-record-1",
                "passed": True,
                "needs_review": False,
            },
        ),
        workspace,
        context,
    )
    assert record.status == "succeeded"
    assert (workspace.change_dir / "healing" / "api-apply-summary.json").is_file()
    assert (workspace.change_dir / "healing" / "api-fixer-safety-check.json").is_file()
    assert len(record.durable_effects) == 1
    assert record.durable_effects[0]["kind"] == HEAL_RECORD_APPLY_V2

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
