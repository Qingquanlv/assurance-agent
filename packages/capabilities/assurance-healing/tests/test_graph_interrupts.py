from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from assurance_healing.graphs.factory import build_healing_graphs as _build_healing_graphs
from graph_engine.attempts.resolutions import AttemptResolution, PermanentTaskFailure
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

from test_healing_graph_factory import (  # type: ignore[import-not-found]
    EFFECT_IDS,
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
    approval_ref: dict[str, str]
    human_action: str
    status: str
    effect_refs: list[dict[str, str]]
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


def test_kernel_protocol_receipts_use_exactly_the_three_healing_effect_ids() -> None:
    from graph_engine import RegistryPorts

    from assurance_healing.contracts.attempts import HEALING_EFFECT_IDS
    from assurance_healing.plugin import HealingPlugin

    contribution = HealingPlugin.contribute(RegistryPorts(engine_api="2.0"))
    protocol_kinds = tuple(sorted(item.kind for item in contribution.effects))
    receipt_schemas = tuple(sorted(item.receipt_schema_id for item in contribution.effects))
    expected = tuple(sorted(HEALING_EFFECT_IDS))
    assert expected == (
        "assurance.healing.effect.allocation.v2",
        "assurance.healing.effect.heal-apply.v2",
        "assurance.healing.effect.proposal-approved.v1",
    )
    assert protocol_kinds == expected
    assert receipt_schemas == (
        "assurance.healing.schema.allocation-receipt.v2",
        "assurance.healing.schema.heal-apply-receipt.v2",
        "assurance.healing.schema.proposal-approved-receipt.v1",
    )
    assert set(EFFECT_IDS) == set(expected)


async def test_published_effect_refs_come_from_kernel_receipt_not_output_extras() -> None:
    from graph_engine.attempts.resolutions import ReceiptRef

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    receipt = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)
    failure_output = failure_agent_output()
    failure_output["effect_refs"] = [{"kind": "forged.failure.effect", "digest": _SHA}]
    failure = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={
            "healing.fix-proposal": [committed(failure_output, receipt)],
            "healing.apply-test-repair": [committed(application_output(), receipt)],
        },
    )
    published = failure.published_update
    assert published is None or "forged.failure.effect" not in str(published)
    assert failure.interrupt_envelope is not None


async def test_fix_proposal_waits_for_approval_ref_before_application() -> None:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_healing_graphs(
        harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    )
    from graph_engine.attempts.resolutions import ReceiptRef

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
            "healing.apply-test-repair": [committed(application_output(), receipt)],
        }
    )

    wrapper: StateGraph[_RepairChannels] = StateGraph(_RepairChannels)
    wrapper.add_node("repair", cast(Any, bundle.repair_failure))
    wrapper.add_edge(START, "repair")
    wrapper.add_edge("repair", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _config()
    initial = failure_graph_input()

    interrupted = await graph.ainvoke(cast(Any, initial), config=config)
    value = _interrupt_value(interrupted)
    assert isinstance(value, dict)
    assert value["interrupt_id"] == "healing.approval"
    assert "healing.proposal" in value["show"]

    resumed = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "approval_ref": {
                    "path": "qa/results/healing/approval.json",
                    "digest": _SHA,
                },
            }
        ),
        config=config,
    )
    assert resumed["status"] == "applied"
    assert _interrupt_value(resumed) is None


async def _resume_repair(apply: AttemptResolution, resume: dict[str, object]):
    from graph_engine.attempts.resolutions import ReceiptRef

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
    interrupted = await graph.ainvoke(cast(Any, failure_graph_input()), config=config)
    assert _interrupt_value(interrupted) is not None
    return await graph.ainvoke(Command(resume=resume), config=config)


@pytest.mark.parametrize(
    ("resume", "apply", "expected"),
    [
        (
            {
                "action": "approve",
                "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
            },
            "committed",
            "applied",
        ),
        ({"action": "reject"}, "committed", "needs_review"),
        (
            {
                "action": "approve",
                "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
            },
            "invalid_output",
            "needs_review",
        ),
        (
            {
                "action": "approve",
                "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
            },
            "invalid_input",
            "failed",
        ),
    ],
)
async def test_repair_failure_routes_approval_and_apply(resume, apply, expected) -> None:
    from graph_engine.attempts.resolutions import ReceiptRef

    if apply == "committed":
        resolution = committed(application_output(), ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA))
    else:
        resolution = PermanentTaskFailure(kind=apply, message=apply)
    resumed = await _resume_repair(resolution, resume)
    assert resumed["status"] == expected


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
