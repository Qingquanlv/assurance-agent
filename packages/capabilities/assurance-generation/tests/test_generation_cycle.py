from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from assurance_generation.contracts.families import LayerName
from assurance_generation.operations.codegen import CodegenFinalizeHandler
from assurance_generation.contracts.workflow import CompleteGenerationInputV1, GENERATION_CYCLE_PATH
from assurance_generation.operations.cycle import complete_generation_cycle
from assurance_intake.contracts.case_selection import CaseSelectionV1, SelectedCaseV1
from graph_engine.attempts.resources.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.attempts.models.contracts import resolve_contract
from graph_engine.attempts.orchestration.kernel import AssuranceAttemptKernel
from graph_engine.attempts.orchestration.node_factory import AttemptNodeFactory
from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.attempts.resources.resource_arbiter import ResourceArbiter
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.flow import BoundFlow
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.testing import committed
from graph_engine.testing.graph_harness import ScriptedAttempt
from tests.product.test_change_local_output_routing import execute_task
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    codegen_result,
    durable_oracle_path,
    family_symbol,
    fake_agent_result,
    locked_oracle_paths,
)
from planning_fixtures import reviewed_cases  # pyright: ignore[reportMissingImports]
from test_resolve_inputs import _fixture, _write  # pyright: ignore[reportMissingImports]


def _artifact(root: Path, path: str) -> dict[str, str]:
    data = (root / path).read_bytes()
    return {"path": path, "digest": hashlib.sha256(data).hexdigest()}


async def cycle_fixture(
    root: Path, families: tuple[LayerName, ...] = ("api", "e2e"), *, coverage_epoch: int = 0
):
    reviewed, _ = _fixture(root, coverage_epoch=coverage_epoch)
    placeholder = root / "qa/cases/menus/case.yaml"
    if placeholder.is_file():
        placeholder.unlink()
    cases = {"schema_version": "1.0", "added": [], "modified": [], "removed": []}
    for family in families:
        cases["added"].extend(reviewed_cases(family)["added"])  # type: ignore[union-attr]
    case_ref = _write(root, "qa/cases/items/case.yaml", yaml.safe_dump(cases).encode())
    selection = CaseSelectionV1(
        schema_version="1",
        change_id=reviewed.change_id,
        coverage_epoch=coverage_epoch,
        plan_digest=reviewed.plan_digest,
        inventory_ref=reviewed.plan_ref.model_dump(mode="json"),
        cases=tuple(
            SelectedCaseV1(
                case_id=entry["case_id"],
                origin="added",
                source_ref=case_ref.model_dump(mode="json"),
                source_locator=f"added[{index}]",
                mrc_ids=(),
            )
            for index, entry in enumerate(cases["added"])
        ),
    )
    selection_ref = _write(
        root,
        f"qa/results/cases/epochs/{coverage_epoch}/selection.json",
        selection.model_dump_json().encode(),
    )
    reviewed = reviewed.model_copy(update={"case_refs": (case_ref,), "selection_ref": selection_ref})
    script = {
        "generation.resolve-inputs": [
            committed(
                reviewed.model_dump(mode="json"), ReceiptRef(receipt_id="review", receipt_digest="a" * 64)
            )
        ]
    }
    family_inputs = []
    for family in families:
        target, data_path = locked_oracle_paths(family)
        _write(root, target, f"def {family_symbol(family)}():\n    assert True\n".encode())
        _write(root, data_path, b"helper\n")
        plan = f"qa/results/plans/{family}-plan.md"
        _write(root, plan, b"reviewed test plan\n")
        _write(root, f"qa/results/codegen/{family}-codegen-summary.md", b"reviewed codegen summary\n")
        authored = codegen_result([target, data_path], family=family)
        _write(
            root,
            f"qa/results/codegen/{family}-generated-files.json",
            json.dumps(authored).encode(),
        )
        finalized = await execute_task(
            CodegenFinalizeHandler(family),
            fake_agent_result(authored),
            root,
            write_root=root,
        )
        assert finalized.status == "succeeded", finalized.failure
        output = cast(dict[str, Any], finalized.output)
        receipt = ReceiptRef(receipt_id=f"codegen-{family}", receipt_digest="a" * 64)
        script[f"generation.{family}.codegen"] = [
            committed(
                output,
                receipt,
                artifacts=[
                    _artifact(root, f"qa/results/codegen/{family}-codegen-summary.md"),
                    _artifact(root, f"qa/results/codegen/{family}-generated-files.json"),
                ],
            )
        ]
        script[f"generation.{family}.codegen-review"] = [committed({"route": "codegen"}, receipt)]
        family_inputs.append(
            {
                "family": family,
                "coverage_epoch": coverage_epoch,
                "plan_files": [plan],
                "files": output["files"],
                "mapping": output["mapping"],
                "receipt": receipt.model_dump(mode="json"),
                "method_plans": [],
                "semantic_reviews": [],
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
    journal = MemoryAttemptCheckpointStore()
    kernel = AssuranceAttemptKernel(
        checkpoints=journal,
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
        attempt_factory=AttemptNodeFactory(checkpoints=journal, kernel=BoundaryKernel()),
    )
    generation = build_generation_graphs(context.for_capability("assurance.generation")).generation
    assert isinstance(generation, BoundFlow)
    graph = generation.compile(outcome_field="status")
    graph_input = {
        "change_id": payload.change_id,
        "coverage_epoch": coverage_epoch,
        "plan_digest": payload.plan_digest,
        "plan_ref": payload.plan_ref.model_dump(mode="json"),
        "reviewed_case_ref": _write(
            project,
            "qa/cases/reviewed-case.json",
            payload.reviewed_case.model_dump_json().encode(),
        ).model_dump(mode="json"),
        "selected_test_families": ["api", "e2e"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
    }
    # A mounted root does not echo its inputs. The parent keeps them.
    state = {
        **graph_input,
        **await graph.ainvoke(
            graph_input,
            config={
                "configurable": {
                    "thread_id": "cycle",
                    "assurance_revision_id": "b" * 64,
                    "assurance_fencing_token": 1,
                    "assurance_entrypoint": "full",
                }
            },
        ),
    }
    return state, project, executor


@pytest.mark.parametrize("coverage_epoch", [0, 1])
async def test_generation_cycle_is_committed_and_passed_to_execution(
    tmp_path: Path, coverage_epoch: int
) -> None:
    state, project, executor = await run_generation_boundary(tmp_path, coverage_epoch)
    assert state["status"] == "passed", state.get("attempt_failure")
    result = json.loads((project / GENERATION_CYCLE_PATH).read_bytes())
    assert result["coverage_epoch"] == coverage_epoch
    mapping = json.loads((project / result["mapping_ref"]["path"]).read_bytes())
    assert {entry["layer"] for entry in mapping["mappings"]} == {"api", "e2e"}
    assert {ref["path"] for ref in result["source_refs"]} == {
        "qa/tests/api/items/test_items.py",
        "qa/tests/testdata/api/items.py",
        "qa/tests/e2e/items/test_items.py",
        "qa/tests/testdata/e2e/items.py",
    }
    assert len(result["plan_refs"]) == 4
    method_document = json.loads((project / result["method_plan_ref"]["path"]).read_bytes())
    assert method_document["plan_digest"] == result["plan_digest"]
    assert method_document["method_plans"] == []
    assert {ref["path"] for ref in result["plan_refs"]} == {
        "qa/results/codegen/api-codegen-summary.md",
        "qa/results/codegen/api-generated-files.json",
        "qa/results/codegen/e2e-codegen-summary.md",
        "qa/results/codegen/e2e-generated-files.json",
    }
    assert executor.dispatch_count == 1
    assert result["coverage_epoch"] == coverage_epoch


async def test_generation_cycle_requires_results_plan_prefix(tmp_path: Path) -> None:
    payload, _ = await cycle_fixture(tmp_path)
    result = complete_generation_cycle(payload, tmp_path, tmp_path / ".stage")
    assert all(
        ref.path.startswith("qa/results/plans/") or ref.path.startswith("qa/results/codegen/")
        for ref in result.plan_refs
    )


async def test_generation_cycle_rejects_change_scoped_plan_prefix(tmp_path: Path) -> None:
    payload, _ = await cycle_fixture(tmp_path, families=("api",))
    old_plan = "/".join(("generated", "plans", "api-plan.md"))
    (tmp_path / old_plan).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / old_plan).write_bytes(b"legacy plan\n")
    family = payload.families[0].model_copy(update={"plan_files": (old_plan,)})
    with pytest.raises(ValueError, match="plan artifacts"):
        complete_generation_cycle(
            payload.model_copy(update={"families": (family,)}),
            tmp_path,
            tmp_path / ".stage",
        )


@pytest.mark.parametrize("fault", ["changed_source", "stale_epoch", "missing_family", "foreign_case"])
async def test_generation_cycle_rejects_invalid_family_evidence(tmp_path: Path, fault: str) -> None:
    payload, _ = await cycle_fixture(tmp_path)
    if fault == "changed_source":
        target = durable_oracle_path()
        (tmp_path / target).write_bytes(b"changed")
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
