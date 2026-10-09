from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState
from langgraph.graph import END, START, StateGraph

from assurance_healing.graphs.factory import build_healing_graphs as _build_healing_graphs
from graph_engine.attempts.models.resolutions import AttemptResolution, PermanentTaskFailure
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

from test_healing_graph_factory import (  # type: ignore[import-not-found]
    application_output,
    failure_agent_output,
    failure_graph_input,
    healing_contracts,
)

from graph_engine.testing.feature_bundle import compile_bundle


def build_healing_graphs(*args, **kwargs):
    return compile_bundle(_build_healing_graphs(*args, **kwargs))


class _RepairChannels(CheckpointBridgeState, total=False):
    change_id: str
    plan_digest: str
    plan_ref: dict[str, str]
    owner_id: str
    capability_leafs: list[str]
    allowed_paths: list[str]
    allowed_roots: list[str]
    allowed_artifact_paths: list[str]
    baseline_digest: str
    candidate_digest: str
    policy_digest: str
    mapping_paths: list[str]
    execution_evidence_digest: str
    issue_analysis_ref: dict[str, str]
    coverage_epoch: int
    repair_round: int
    product_policy: dict[str, str]
    generation_ref: dict[str, str]
    execution_ref: dict[str, str]
    execution_receipt: dict[str, str]
    mapping_ref: dict[str, str]
    source_refs: list[dict[str, str]]
    allowed_test_paths: list[str]
    proposal_ref: dict[str, str]
    status: str
    repair_result: dict[str, object]
    attempt_failure: dict[str, object]


_SHA = "a" * 64


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "repair-coverage",
        }
    }


async def test_published_receipt_refs_come_from_kernel_receipt_not_output_extras() -> None:
    from graph_engine.attempts.models.resolutions import ReceiptRef

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    receipt = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)
    failure_output = failure_agent_output()
    failure_output["receipt_refs"] = [{"receipt_id": "forged-receipt", "receipt_digest": _SHA}]
    failure = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={
            "healing.fix-proposal": [
                committed(
                    failure_output,
                    receipt,
                    artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
                )
            ],
            "healing.apply-test-repair": [committed(application_output(), receipt)],
        },
    )
    published = failure.published_update
    assert published is None or "forged-receipt" not in str(published)
    assert failure.interrupt_envelope is None


async def _run_repair(apply: AttemptResolution):
    from graph_engine.attempts.models.resolutions import ReceiptRef

    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_healing_graphs(
        harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    )
    receipt = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)
    harness._kernel.load_script(
        {
            "healing.fix-proposal": [
                committed(
                    failure_agent_output(),
                    receipt,
                    artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
                )
            ],
            "healing.apply-test-repair": [apply],
        }
    )
    wrapper: StateGraph[_RepairChannels] = StateGraph(_RepairChannels)
    wrapper.add_node("repair", cast(Any, bundle.repair_failure))
    wrapper.add_edge(START, "repair")
    wrapper.add_edge("repair", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    result = await graph.ainvoke(cast(Any, failure_graph_input()), config=config)
    assert _interrupt_value(result) is None
    snapshot = await graph.aget_state(config)
    assert not snapshot.next
    return result


@pytest.mark.parametrize(
    ("apply", "expected"),
    [("committed", "applied"), ("invalid_output", "needs_review"), ("invalid_input", "failed")],
)
async def test_repair_runs_without_approval_and_routes_apply_result(apply, expected) -> None:
    from graph_engine.attempts.models.resolutions import ReceiptRef

    if apply == "committed":
        resolution = committed(application_output(), ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA))
    else:
        resolution = PermanentTaskFailure(kind=apply, message=apply)
    result = await _run_repair(resolution)
    assert result["status"] == expected


def _interrupt_value(result: object) -> object | None:
    if isinstance(result, GraphInterrupt):
        interrupts = getattr(result, "args", ())
        if interrupts:
            first = interrupts[0]
            if isinstance(first, tuple) and first:
                return getattr(first[0], "value", first[0])
            return getattr(first, "value", first)
        return None
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)
