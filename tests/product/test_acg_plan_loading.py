from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.contracts.plan import plan_artifact_ref, plan_bytes
from assurance_intake.operations.plan_codec import seal_plan
from assurance_intake.graphs.factory import build_intake_graphs
from assurance_intake.graphs.nodes import select_load_plan
from assurance_intake.operations.plan_artifacts import load_plan_artifact
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_generation.operations.resolve_inputs import resolve_generation_input
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_JOBS
from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS as GENERATION_TASKS
from assurance_generation.graphs.factory import build_generation_graphs
from assurance_product.graphs.execute import adapt_fact_baseline
from assurance_product.graphs.entrypoints import adapt_load_plan, build_case_root
from assurance_product.graphs.execute import build_execute_root
from assurance_product.graphs.state import ProductState
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef, RejectedTaskResult
from graph_engine.canonical import canonical_digest
from graph_engine.testing import GraphHarness, committed
from tests.acg_plan_fixture import install_plan
from tests.product.test_product_input import valid_product_input


def _seed_review(root: Path, loaded: Any) -> dict[str, str]:
    def write(relative: str, data: bytes) -> EvidenceArtifactRefV1:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())

    case = write("qa/cases/item/case.yaml", b"case")
    review = write(
        "qa/results/review/case-review.json",
        json.dumps(
            {
                "schema_version": "1.0",
                "review_type": "case",
                "change_id": "CH-DEMO-001",
                "decision": "pass",
                "findings": [],
                "auto_fix_plan": [],
                "next_action": "continue",
                "auto_fix_allowed": False,
                "human_review_required": False,
                "risk_level": "low",
                "minimum_coverage": {"total_required": 0, "covered": 0, "skipped_by_scope": 0, "missing": []},
                "source_verification": {
                    "independent": True,
                    "reviewed_source_files": ["src/app.py"],
                    "verified_claims": [{"claim": "item persists", "evidence_files": ["src/app.py"]}],
                },
            }
        ).encode(),
    )
    reviewed = ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        plan_digest=loaded.plan.plan_digest,
        plan_ref=loaded.plan_ref,
        preparation_refs=(loaded.plan_ref,),
        case_refs=(case,),
        review_ref=review,
        selection_ref=write("qa/results/cases/epochs/0/selection.json", b'{"schema_version":"1"}'),
    )
    return write("qa/cases/reviewed-case.json", reviewed.model_dump_json().encode()).model_dump(mode="json")


@pytest.mark.parametrize("tampered", (False, True))
def test_execute_restores_authenticated_review_after_real_plan_load_before_baseline(
    tmp_path: Path, tampered: bool
) -> None:
    payload = _public_input(tmp_path)
    loaded = load_plan_artifact(
        select_load_plan(adapt_load_plan(cast(ProductState, payload))), project_root=tmp_path
    )
    assert "reviewed_case" not in loaded.model_dump(mode="json")
    payload["artifacts"] = [_seed_review(tmp_path, loaded)]
    if tampered:
        (tmp_path / "qa/cases/item/case.yaml").write_bytes(b"tampered")
    harness = GraphHarness()
    contracts = {task.contract_id: task for task in TASK_ATTEMPT_CONTRACTS.values()}
    contracts.update({job.contract_id: job.to_task_contract() for job in AGENT_JOB_CONTRACTS.values()})
    intake = build_intake_graphs(harness.recording_context(owner_id="assurance.intake", contracts=contracts))
    generation_contracts = {task.contract_id: task for task in GENERATION_TASKS.values()}
    generation_contracts.update({job.contract_id: job.to_task_contract() for job in GENERATION_JOBS.values()})
    generation = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts)
    )
    expected_input = {
        "change_id": payload["change_id"],
        "coverage_epoch": 0,
        "plan_digest": loaded.plan.plan_digest,
        "plan_ref": loaded.plan_ref.model_dump(mode="json"),
        "reviewed_case": None,
        "source_artifacts": payload["artifacts"],
    }
    if tampered:
        with pytest.raises(ValueError, match="digest changed"):
            resolve_generation_input(expected_input, tmp_path)
        resolution = PermanentTaskFailure(kind="invalid_input", message="review digest changed")
    else:
        reviewed = resolve_generation_input(expected_input, tmp_path)
        resolution = committed(reviewed, ReceiptRef(receipt_id="review", receipt_digest="b" * 64))
    baseline_inputs: list[dict[str, object]] = []

    def baseline(state: ProductState) -> dict[str, object]:
        baseline_inputs.append(adapt_fact_baseline(state))
        return {"status": "completed"}

    tail: StateGraph[ProductState] = StateGraph(ProductState)
    tail.add_node("baseline", baseline)
    tail.add_edge(START, "baseline")
    tail.add_edge("baseline", END)
    bundles = SimpleNamespace(generation=generation)
    context = cast(Any, SimpleNamespace(compile_root=lambda builder: builder.compile()))
    graph = build_execute_root(context, bundles, intake.load_plan, tail.compile())

    async def run():
        return await harness.run(
            graph,
            input=payload,
            script={
                "intake.load-plan": [
                    committed(loaded, ReceiptRef(receipt_id="load", receipt_digest="a" * 64))
                ],
                "generation.resolve-inputs": [resolution],
            },
        )

    result = asyncio.run(run())
    assert isinstance(result.terminal, dict)
    assert result.select_values[1] == expected_input
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "intake.load-plan",
        "generation.resolve-inputs",
    ]
    if tampered:
        assert result.terminal["output"]["status"] == "failed"
        assert baseline_inputs == []
        return
    assert result.terminal["output"]["status"] == "completed"
    restored = ReviewedCaseV1.model_validate(baseline_inputs[0]["reviewed_case"])
    assert restored.coverage_epoch == 0
    assert restored.case_refs[0].path == "qa/cases/item/case.yaml"


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
    # This test isolates plan-loading routes; the authenticated review boundary
    # is exercised separately above with real review files and the real resolver.
    resolver: StateGraph[ProductState] = StateGraph(ProductState)
    resolver.add_node("noop", lambda state: {})
    resolver.add_edge(START, "noop")
    resolver.add_edge("noop", END)
    context = cast(Any, SimpleNamespace(compile_root=lambda builder: builder.compile()))
    graph = (
        build_case_root(context, intake.load_plan, child.compile())
        if entrypoint == "case"
        else build_execute_root(
            context,
            SimpleNamespace(generation=SimpleNamespace(resolve_inputs=resolver.compile())),
            intake.load_plan,
            child.compile(),
        )
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
