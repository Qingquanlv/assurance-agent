from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.contracts.plan import plan_artifact_ref, plan_bytes, seal_plan
from assurance_intake.graphs.factory import build_intake_graphs
from assurance_intake.graphs.nodes import select_load_plan
from assurance_intake.operations.plan_artifacts import load_plan_artifact
from assurance_product.graphs.entrypoints import adapt_load_plan, build_case_root
from assurance_product.graphs.execute import build_execute_root
from assurance_product.graphs.state import ProductState
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef, RejectedTaskResult
from graph_engine.canonical import canonical_digest
from graph_engine.testing import GraphHarness, committed
from tests.acg_plan_fixture import install_plan
from tests.product.test_product_input import valid_product_input


def _public_input(root: Path) -> dict[str, object]:
    plan, _ = install_plan(root, "CH-DEMO-001")
    plan = seal_plan(
        {
            **plan.model_dump(mode="json", exclude={"plan_digest"}),
            "requirement_digest": canonical_digest({"requirement": "Add login"}),
        }
    )
    ref = plan_artifact_ref(plan)
    path = root / ref.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plan_bytes(plan))
    sources = dict(plan.quality_goal.source_resource_digests)
    return valid_product_input(
        resolved_plan_ref=ref.model_dump(mode="json"),
        capability_leafs=("entities.item.constraints.name",),
        budgets=plan.resolved_budgets.model_dump(mode="json"),
        product_policy={"resource_id": plan.policy_resource_id, "sha256": plan.policy_digest},
        **{
            name: {"resource_id": resource_id, "sha256": sources[resource_id]}
            for name, resource_id in (
                ("capability_catalog", "assurance.product.configuration.capability-catalog"),
                ("data_knowledge", "assurance.product.configuration.data-knowledge"),
            )
        },
    )


@pytest.mark.parametrize("entrypoint", ("case", "execute"))
@pytest.mark.parametrize("load_result", ("committed", "invalid_input", "rejected"))
def test_standalone_entrypoint_routes_the_real_load_graph(
    tmp_path: Path, entrypoint: str, load_result: str
) -> None:
    payload = _public_input(tmp_path)
    if entrypoint == "case":
        payload["case_delta_paths"] = ("qa/cases/item/case.yaml",)
    loaded = load_plan_artifact(
        select_load_plan(adapt_load_plan(cast(ProductState, payload))), project_root=tmp_path
    )
    harness = GraphHarness()
    contracts = {job.contract_id: job.to_task_contract() for job in AGENT_JOB_CONTRACTS.values()}
    contracts.update({task.contract_id: task for task in TASK_ATTEMPT_CONTRACTS.values()})
    intake = build_intake_graphs(harness.recording_context(owner_id="assurance.intake", contracts=contracts))
    downstream_bindings: list[tuple[object, object, object]] = []

    def consume_plan(state: ProductState) -> dict[str, object]:
        downstream_bindings.append(
            (state.get("plan_digest"), state.get("plan_ref"), state.get("selected_test_families"))
        )
        return {"status": "completed"}

    child: StateGraph[ProductState] = StateGraph(ProductState)
    child.add_node("consume-plan", consume_plan)
    child.add_edge(START, "consume-plan")
    child.add_edge("consume-plan", END)
    context = cast(Any, SimpleNamespace(compile_root=lambda builder: builder.compile()))
    graph = (
        build_case_root(context, intake.load_plan, child.compile())
        if entrypoint == "case"
        else build_execute_root(context, None, intake.load_plan, child.compile())
    )
    resolution = {
        "committed": committed(loaded, ReceiptRef(receipt_id="load-receipt", receipt_digest="a" * 64)),
        "invalid_input": PermanentTaskFailure(kind="invalid_input", message="plan digest mismatch"),
        "rejected": RejectedTaskResult(reason="load rejected"),
    }[load_result]

    result = asyncio.run(harness.run(graph, input=payload, script={"intake.load-plan": [resolution]}))

    assert isinstance(result.terminal, dict)
    assert result.terminal["output"]["status"] == ("completed" if load_result == "committed" else "failed")
    assert downstream_bindings == (
        [(loaded.plan.plan_digest, loaded.plan_ref.model_dump(mode="json"), ["api"])]
        if load_result == "committed"
        else []
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["intake.load-plan"]
