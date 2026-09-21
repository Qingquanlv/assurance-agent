from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.operations.runner import ConfinedExecutionProcessHost, RunTestsHandler
from graph_engine.canonical import canonical_digest
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    execute_task,
    fake_pytest_host,
    run_request,
    write_test,
    codegen_mapping,
    reviewed_cases,
)


@pytest.mark.parametrize("existing_evidence", [False, True])
async def test_handler_stages_evidence_without_changing_project(
    tmp_path: Path, existing_evidence: bool
) -> None:
    write_test(tmp_path / "qa/tests/api/test_sample.py")
    relative = "qa/results/execution/execute-result.json"
    previous = tmp_path / relative
    if existing_evidence:
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"previous committed evidence")
    result = await execute_task(
        RunTestsHandler(process_host=fake_pytest_host()),
        run_request(["qa/tests/api/test_sample.py"]),
        tmp_path,
    )
    assert result.outcome.status == "succeeded"
    if existing_evidence:
        assert previous.read_bytes() == b"previous committed evidence"
    else:
        assert not previous.exists()
    staged = tmp_path / "qa/.staging/attempt-1" / relative
    evidence = ExecutionEvidenceV1.model_validate(result.outcome.output)
    assert evidence.executed_at is not None
    assert evidence.receipt_digest == canonical_digest(evidence.receipt.model_dump(mode="json"))
    assert json.loads(staged.read_bytes()) == evidence.model_dump(mode="json")


def test_unsupported_family_stages_its_diagnostic(tmp_path: Path) -> None:
    from assurance_execution.contracts.agent import RunTestsInputV1
    from assurance_execution.operations.runner import run_observed_mapping

    project, stage = tmp_path / "project", tmp_path / "stage"
    project.mkdir()
    payload = run_request(["qa/tests/performance/test_sample.py"])
    payload["selected_targets"] = {"api": False, "e2e": False, "fuzz": False, "performance": True}
    payload["mapping"]["mappings"][0]["layer"] = "performance"
    evidence, _ = run_observed_mapping(
        RunTestsInputV1.model_validate(payload), project, fake_pytest_host(), write_root=stage
    )
    outcome = evidence.family_outcomes[0]
    assert outcome.state == "blocked"
    assert evidence.status == "failed"
    assert evidence.receipt.commands == ()
    assert evidence.receipt_digest == canonical_digest(evidence.receipt.model_dump(mode="json"))
    for ref in outcome.diagnostic_refs:
        assert not (project / ref.path).exists()
        assert hashlib.sha256((stage / ref.path).read_bytes()).hexdigest() == ref.digest


def test_publisher_preserves_typed_observation_reference() -> None:
    from assurance_execution.graphs.nodes import publish_execution
    from test_execution_graph_factory import (  # pyright: ignore[reportMissingImports]
        _RECEIPT,
        execution_evidence,
        generation_result,
    )

    reference = {
        "path": "qa/results/execution/epochs/2/batches/b/runtime-observations.json",
        "digest": "c" * 64,
    }
    evidence = ExecutionEvidenceV1.model_validate(
        {**execution_evidence().model_dump(mode="json"), "observations_ref": reference}
    )
    published = publish_execution(
        {"rounds_budget": 2, "rounds_used": 0, "generation_result": generation_result()},
        evidence,
        _RECEIPT,
    )
    cycle = published["execution_result"]
    assert isinstance(cycle, dict)
    assert cycle["observations_ref"] == reference
    encoded = (json.dumps(evidence.model_dump(mode="json"), indent=2) + "\n").encode()
    assert cycle["evidence_ref"]["digest"] == hashlib.sha256(encoded).hexdigest()


def _prepared_project(project: Path, *, passing: bool) -> dict[str, object]:
    from tests.acg_plan_fixture import install_plan
    from test_execution_graph_factory import generation_result  # pyright: ignore[reportMissingImports]

    plan, plan_ref = install_plan(project, "CH-DEMO-001", capability_leafs=("entities.item.create",))
    source = "qa/tests/api/test_sample.py"
    write_test(project / source, body=f"def test_tc_a_001__ok():\n    assert {passing}\n")

    def write(relative: str, document: object) -> dict[str, str]:
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(document).encode()
        path.write_bytes(data)
        return {"path": relative, "digest": hashlib.sha256(data).hexdigest()}

    write("qa/cases/items/case.yaml", reviewed_cases())
    mapping = codegen_mapping(target_file=source)
    write(
        "qa/results/codegen/api-generated-files.json",
        {
            "schema_version": "1",
            "change_id": "CH-DEMO-001",
            "layer": "api",
            "files": [
                {"repo_path": source, "disposition": "generated", "role": "test_entry", "case_ids": ["TC_A"]}
            ],
            "mapping": mapping,
        },
    )
    generation = generation_result()
    generation.update(plan_digest=plan.plan_digest, plan_ref=plan_ref, coverage_epoch=0)
    reviewed = cast(dict, generation["reviewed_case"])
    reviewed.update(plan_digest=plan.plan_digest, plan_ref=plan_ref, coverage_epoch=0)
    reviewed["selection_ref"] = write("qa/results/cases/epochs/0/selection.json", {})
    reviewed["preparation_refs"] = [plan_ref]
    generation["mapping_ref"] = write("qa/results/codegen/closed-mapping.json", mapping)
    generation["source_refs"] = [
        {"path": source, "digest": hashlib.sha256((project / source).read_bytes()).hexdigest()}
    ]
    generation["plan_refs"] = [
        write("qa/results/plans/api-plan.json", {"requirements": [], "method_plans": []})
    ]
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": plan.plan_digest,
        "plan_ref": plan_ref,
        "selected_test_families": ["api"],
        "capability_leafs": ["entities.item.create"],
        "coverage_epoch": 0,
        "rounds_budget": 2,
        "rounds_used": 0,
        "generation_result": generation,
    }


class _LocalInterpreterHost(ConfinedExecutionProcessHost):
    """Real pytest/collector; only interpreter provisioning is outside this test."""

    def spawn(self, argv: tuple[str, ...], cwd: Path):
        return super().spawn((sys.executable, "-m", *argv[argv.index("pytest") :]), cwd)


class _HandlerExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, validated_input: BaseModel, scope):
        from graph_engine.attempts.contracts import ExecutedAttemptResult
        from graph_engine.plugin_api import InvocationMetadata, TaskContext, TaskRequest

        self.calls += 1
        binding = scope.workspace
        invocation = InvocationMetadata(
            invocation_id="inv-1",
            lock_digest="b" * 64,
            composition_digest="c" * 64,
            entrypoint="execute",
        )
        outcome = await RunTestsHandler(process_host=_LocalInterpreterHost()).execute(
            TaskRequest(
                invocation_id="inv-1",
                task_id="execution",
                graph_instance_id="execution",
                node_id="execution.execute",
                capability_id="assurance.execution",
                binding_data=None,
                invocation=invocation,
                attempt=1,
                input=validated_input.model_dump(mode="json"),
            ),
            TaskContext(
                project_root=binding.project_root,
                write_root=binding.write_root,
                workspace_identity=binding.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=invocation,
            ),
        )
        assert outcome.status == "succeeded", outcome
        # Kernel has not sealed/promoted yet. The handler must leave the project untouched.
        assert not (binding.project_root / "qa/results/execution").exists()
        return ExecutedAttemptResult(output=ExecutionEvidenceV1.model_validate(outcome.output))


@pytest.mark.parametrize("passing", [True, False])
async def test_real_execution_commits_and_recovers_without_replaying_tests(
    tmp_path: Path, passing: bool
) -> None:
    from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_execution.graphs.factory import build_execution_graphs
    from assurance_execution.graphs.state import ExecutionState
    from assurance_product.graphs.routes import route_execute
    from graph_engine.attempts.contracts import resolve_contract
    from graph_engine.attempts.kernel import AssuranceAttemptKernel
    from graph_engine.attempts.node_factory import AttemptNodeFactory
    from graph_engine.attempts.resource_arbiter import ResourceArbiter
    from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
    from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
    from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
    from graph_engine.testing import RecordingCapabilityBuildContext
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from test_execution_graph_factory import _invoke_config, _revision  # pyright: ignore[reportMissingImports]

    project = tmp_path / "project"
    payload = _prepared_project(project, passing=passing)
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    journal = MemoryAttemptJournal()
    executor = _HandlerExecutor()
    cuts = ["after_promotion_before_receipt"]

    def cut(name: str) -> None:
        if cuts and name == cuts[0]:
            cuts.pop()
            raise RuntimeError("crash after promotion")

    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision=_revision(),
        transaction_cut=cut,
    )
    context = RecordingCapabilityBuildContext(
        owner_id="assurance.execution",
        contracts={
            c.contract_id: resolve_contract(c, executor=executor) for c in TASK_ATTEMPT_CONTRACTS.values()
        },
        attempt_factory=AttemptNodeFactory(journal=journal, kernel=kernel),
    )
    try:
        reached_quality: list[str] = []

        def quality_boundary(state):
            reached_quality.append(route_execute(state))
            return {}

        # Only the root owns a checkpointer, just as in the product composition.
        # The execution subgraph and its Task/Kernel are the production implementations.
        root = StateGraph(ExecutionState)
        root.add_node("execute", build_execution_graphs(context).execute)
        root.add_node("quality", quality_boundary)
        root.add_edge(START, "execute")
        root.add_conditional_edges("execute", route_execute, {"quality": "quality", "blocked": END})
        root.add_edge("quality", END)
        graph = root.compile(checkpointer=InMemorySaver())
        with pytest.raises(RuntimeError, match="crash after promotion"):
            await graph.ainvoke(cast(ExecutionState, payload), config=_invoke_config(entrypoint="execute"))
        assert reached_quality == []
        # Resume the saved root task, do not manufacture a second graph input.
        result = await graph.ainvoke(None, config=_invoke_config(entrypoint="execute"))
        assert executor.calls == 1
        assert reached_quality == ["quality"]
        assert result["status"] == ("passed" if passing else "failed")
        assert route_execute(result) == "quality"
        cycle = result["execution_result"]
        evidence_bytes = (project / cycle["evidence_ref"]["path"]).read_bytes()
        assert hashlib.sha256(evidence_bytes).hexdigest() == cycle["evidence_ref"]["digest"]
        evidence = ExecutionEvidenceV1.model_validate_json(evidence_bytes)
        assert evidence.executed_at == datetime.fromisoformat(cycle["executed_at"])
        assert evidence.receipt.commands
        assert evidence.receipt_digest == canonical_digest(evidence.receipt.model_dump(mode="json"))
        assert cycle["observations_ref"] is not None
        observations = (project / cycle["observations_ref"]["path"]).read_bytes()
        assert hashlib.sha256(observations).hexdigest() == cycle["observations_ref"]["digest"]
        lock = json.loads((project / "qa/.staging/execution/durable-execution-v1.json").read_bytes())
        assert lock["executed_at"] == cycle["executed_at"]
    finally:
        store.close()
