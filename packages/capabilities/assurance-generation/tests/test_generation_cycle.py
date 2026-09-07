from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from assurance_generation.contracts.families import LayerName
from assurance_generation.operations.codegen import CodegenFinalizeHandler
from assurance_generation.contracts.workflow import CompleteGenerationInputV1
from assurance_generation.operations.cycle import complete_generation_cycle
from assurance_product.graphs.execute import _route_generation, adapt_execution
from assurance_product.graphs.state import ProductState
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.attempts.contracts import resolve_contract
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.testing import committed
from graph_engine.testing.graph_harness import ScriptedAttempt
from tests.product.test_change_local_output_routing import execute_task
from tests.product.test_product_input import valid_product_input
from tests.verified_generation_fixture import accepted_verified_execution_input
from codegen_fixtures import codegen_result, family_symbol, family_test_file, fake_agent_result  # pyright: ignore[reportMissingImports]
from planning_fixtures import reviewed_cases  # pyright: ignore[reportMissingImports]
from test_resolve_inputs import _fixture, _write  # pyright: ignore[reportMissingImports]


async def cycle_fixture(
    root: Path, families: tuple[LayerName, ...] = ("api", "e2e"), *, coverage_epoch: int = 0
):
    reviewed, _ = _fixture(root, coverage_epoch=coverage_epoch)
    cases = {"schema_version": "1.0", "added": [], "modified": [], "removed": []}
    for family in families:
        cases["added"].extend(reviewed_cases(family)["added"])  # type: ignore[union-attr]
    case_ref = _write(root, reviewed.case_refs[0].path, yaml.safe_dump(cases).encode())
    reviewed = reviewed.model_copy(update={"case_refs": (case_ref,)})
    script = {
        "generation.resolve-inputs": [
            committed(
                reviewed.model_dump(mode="json"), ReceiptRef(receipt_id="review", receipt_digest="a" * 64)
            )
        ]
    }
    family_inputs = []
    for family in families:
        target = family_test_file(family)
        source = f"qa/changes/CH-DEMO-001/generated/{family}/files/{target}"
        _write(root, source, f"def {family_symbol(family)}():\n    assert True\n".encode())
        plan = f"qa/changes/CH-DEMO-001/plans/{family}-plan.md"
        _write(root, plan, b"reviewed test plan\n")
        _write(
            root,
            f"qa/changes/CH-DEMO-001/codegen/{family}-generated-files.json",
            json.dumps(codegen_result([target], family=family)).encode(),
        )
        finalized = await execute_task(
            CodegenFinalizeHandler(family),
            fake_agent_result(codegen_result([target], family=family)),
            root,
            write_root=root,
        )
        assert finalized.status == "succeeded", finalized.failure
        output = cast(dict[str, Any], finalized.output)
        receipt = ReceiptRef(receipt_id=f"codegen-{family}", receipt_digest="a" * 64)
        script[f"generation.{family}.plan"] = [committed({"output_files": [plan]}, receipt)]
        script[f"generation.{family}.plan-review"] = [
            committed({"decision": "pass", "codegen_readiness": "ready"}, receipt)
        ]
        script[f"generation.{family}.codegen"] = [committed(output, receipt)]
        family_inputs.append(
            {
                "family": family,
                "coverage_epoch": coverage_epoch,
                "plan_files": [plan],
                "files": output["files"],
                "mapping": output["mapping"],
                "receipt": receipt.model_dump(mode="json"),
            }
        )
    payload = CompleteGenerationInputV1.model_validate(
        {
            "change_id": reviewed.change_id,
            "coverage_epoch": coverage_epoch,
            "plan_digest": reviewed.plan_digest,
            "plan_ref": reviewed.plan_ref.model_dump(mode="json"),
            "reviewed_case": reviewed.model_dump(mode="json"),
            "selected_test_families": families,
            "capability_leafs": ["entities.item.create"],
            "families": family_inputs,
        }
    )
    return payload, script


async def run_generation_boundary(root: Path, coverage_epoch: int = 0):
    # Composition tests may evict capability modules while loading isolated wheels.
    from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
    from assurance_generation.graphs.factory import build_generation_graphs
    from assurance_generation.operations.cycle import PublishGenerationCycleHandler
    from assurance_product.runtime_bindings import DeterministicTaskExecutor

    project = root / "project"
    payload, script = await cycle_fixture(project, coverage_epoch=coverage_epoch)
    task = TASK_ATTEMPT_CONTRACTS["publish-cycle"]
    executor = DeterministicTaskExecutor(task.handler_id, PublishGenerationCycleHandler(), task.output_model)
    resolved = resolve_contract(task, executor=executor)
    journal = MemoryAttemptJournal()
    kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(TaskWorkspaceStore(project, root / "attempts", root / "receipts")),
        graph_revision="b" * 64,
    )
    leaves = ScriptedAttempt()
    leaves.load_script(script)

    class BoundaryKernel:
        async def execute_or_recover(self, attempt_key, contract, validated_input, context):
            if context.semantic_node_id == "generation.publish-cycle":
                return await kernel.execute_or_recover(attempt_key, resolved, validated_input, context)
            return await leaves.execute_or_recover(attempt_key, contract, validated_input, context)

    contracts = {item.contract_id: item.to_task_contract() for item in AGENT_JOB_CONTRACTS.values()}
    contracts.update({item.contract_id: item for item in TASK_ATTEMPT_CONTRACTS.values()})
    context = EngineGraphBuildContext(
        contracts=contracts,
        resolved_contracts={task.contract_id: resolved},
        checkpointer=None,
        approved_source_roots=(),
        attempt_factory=AttemptNodeFactory(journal=journal, kernel=BoundaryKernel()),
    )
    graph = build_generation_graphs(context.for_capability("assurance.generation")).generation
    state = await graph.ainvoke(
        {
            "change_id": payload.change_id,
            "coverage_epoch": coverage_epoch,
            "plan_digest": payload.plan_digest,
            "plan_ref": payload.plan_ref.model_dump(mode="json"),
            "reviewed_case": payload.reviewed_case.model_dump(mode="json"),
            "selected_test_families": ["api", "e2e"],
            "capability_leafs": ["entities.item.create"],
            "allowed_artifact_paths": [],
            "rounds_used": 0,
            "rounds_budget": 2,
        },
        config={
            "configurable": {
                "thread_id": "cycle",
                "assurance_revision_id": "b" * 64,
                "assurance_fencing_token": 1,
                "assurance_entrypoint": "full",
            }
        },
    )
    return state, project, executor


def _verified_cycle_payload(root: Path) -> CompleteGenerationInputV1:
    execution = accepted_verified_execution_input(root)
    generation = execution.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    manifest_path = root / f"qa/changes/{execution.change_id}/codegen/api-generated-files.json"
    manifest = json.loads(manifest_path.read_bytes())
    source = root / "qa/changes/CH-USER-001/generated/api/files/tests/api/test_user_create.py"
    family = {
        "family": "api",
        "coverage_epoch": execution.coverage_epoch,
        "plan_files": [ref.path for ref in generation.plan_refs],
        "files": [
            {
                **manifest["files"][0],
                "content_sha256": f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}",
            }
        ],
        "mapping": manifest["mapping"],
        "receipt": {"receipt_id": "codegen-api", "receipt_digest": "a" * 64},
        "case_execution_plan_ref": generation.case_execution_plan_ref.model_dump(mode="json"),
        "case_execution_plan_digest": generation.case_execution_plan_digest,
    }
    return CompleteGenerationInputV1.model_validate(
        {
            "change_id": execution.change_id,
            "coverage_epoch": execution.coverage_epoch,
            "plan_digest": execution.plan_digest,
            "plan_ref": execution.plan_ref.model_dump(mode="json"),
            "reviewed_case": generation.reviewed_case.model_dump(mode="json"),
            "selected_test_families": execution.selected_test_families,
            "capability_leafs": execution.capability_leafs,
            "families": [family],
        }
    )


def test_verified_generation_cycle_rejects_synchronized_bridge_forgery(tmp_path: Path) -> None:
    payload = _verified_cycle_payload(tmp_path)
    source = tmp_path / "qa/changes/CH-USER-001/generated/api/files/tests/api/test_user_create.py"
    source.write_text(
        "from assurance_execution.bridge import execute_case\n\n"
        "def test_tc_user_create_001__create():\n"
        "    pass\n",
        encoding="utf-8",
    )
    changed_file = (
        payload.families[0]
        .files[0]
        .model_copy(update={"content_sha256": f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"})
    )
    payload = payload.model_copy(
        update={"families": (payload.families[0].model_copy(update={"files": (changed_file,)}),)}
    )

    with pytest.raises(ValueError, match="bridge"):
        complete_generation_cycle(payload, tmp_path, tmp_path / ".stage")


@pytest.mark.parametrize("coverage_epoch", [0, 1])
async def test_generation_cycle_is_committed_and_passed_to_execution(
    tmp_path: Path, coverage_epoch: int
) -> None:
    state, project, executor = await run_generation_boundary(tmp_path, coverage_epoch)
    assert state["status"] == "passed", state.get("attempt_failure")
    assert _route_generation(state) == "execution"
    result = state["generation_result"]
    assert result["coverage_epoch"] == coverage_epoch
    mapping = json.loads((project / result["mapping_ref"]["path"]).read_bytes())
    assert {entry["layer"] for entry in mapping["mappings"]} == {"api", "e2e"}
    assert len(result["source_refs"]) == 4
    assert {
        ref["path"] for ref in result["source_refs"] if ref["path"].endswith("-generated-files.json")
    } == {
        "qa/changes/CH-DEMO-001/codegen/api-generated-files.json",
        "qa/changes/CH-DEMO-001/codegen/e2e-generated-files.json",
    }
    assert len(result["plan_refs"]) == 2
    assert state["generation_receipt"]["receipt_digest"] != "a" * 64
    assert executor.dispatch_count == 1
    adapted = adapt_execution(cast(ProductState, {**valid_product_input(), **state}))
    feature_input = cast(dict[str, object], adapted["feature_input"])
    assert feature_input["generation_result"] == result


@pytest.mark.parametrize("fault", ["changed_source", "stale_epoch", "missing_family", "foreign_case"])
async def test_generation_cycle_rejects_invalid_family_evidence(tmp_path: Path, fault: str) -> None:
    payload, _ = await cycle_fixture(tmp_path)
    if fault == "changed_source":
        target = family_test_file("api")
        (tmp_path / f"qa/changes/CH-DEMO-001/generated/api/files/{target}").write_bytes(b"changed")
    elif fault == "stale_epoch":
        payload = payload.model_copy(
            update={
                "families": (
                    payload.families[0].model_copy(update={"coverage_epoch": 1}),
                    payload.families[1],
                )
            }
        )
    elif fault == "missing_family":
        payload = payload.model_copy(update={"families": payload.families[:1]})
    else:
        entry = payload.families[0].mapping.entries[0].model_copy(update={"case_id": "TC_API_OTHER_001"})
        mapping = payload.families[0].mapping.model_copy(update={"entries": (entry,)})
        payload = payload.model_copy(
            update={
                "families": (payload.families[0].model_copy(update={"mapping": mapping}), payload.families[1])
            }
        )
    with pytest.raises(ValueError):
        complete_generation_cycle(payload, tmp_path, tmp_path / ".stage")
