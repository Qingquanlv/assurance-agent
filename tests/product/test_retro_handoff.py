"""Retro entrypoint binds an explicit window, and a failed retro stays failed."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

from graph_engine.application.application import _status_from_snapshot
from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.testing.graph_harness import GraphHarness

from assurance_improvement.contracts.retro import (
    RetroBuildSlicesInputV1,
    RetroSelectionSnapshot,
    RetroWindow,
)
from assurance_improvement.contracts.retro_identity import prepare_retro_identity
from assurance_improvement.graphs.retro import RetroFlowInput
from assurance_intake.contracts import EvidenceArtifactRefV1
from graph_engine.attempts.keys import BusinessActivation, derive_attempt_key
from assurance_product.graphs.entrypoints import _RETRO_INPUTS, thin_root_flows
from assurance_product.graphs.factory import ProductFeatureBundles
from tests.product.test_execute_tail_flow import _features
from tests.product.test_product_stategraph_flow import _public_input
from tests.product.test_stategraph_entrypoints import _stub_features


def test_retro_entrypoint_passes_the_caller_window() -> None:
    assert _RETRO_INPUTS["window"] == "retro_window"
    assert _RETRO_INPUTS["source_refs"] == "artifacts"
    window = RetroWindow(
        selection=RetroSelectionSnapshot(
            mode="change_ids", requested_change_ids=("CH-DEMO-001", "CH-DEMO-002")
        ),
        change_ids=("CH-DEMO-001", "CH-DEMO-002"),
    )
    ref = EvidenceArtifactRefV1(path="qa/results/report/report.md", digest="a" * 64)
    entered = RetroFlowInput.model_validate(
        {
            "change_id": "CH-DEMO-001",
            "window": window.model_dump(mode="json"),
            "source_refs": [ref.model_dump(mode="json")],
            "report_receipt": {"receipt_id": "report", "receipt_digest": "b" * 64},
        }
    )
    assert entered.window == window
    identity = prepare_retro_identity(
        change_id="CH-DEMO-001",
        window=window,
        source_refs=(ref,),
        report_receipt_digest="b" * 64,
    )
    assert entered.retro_id == identity.retro_id
    omitted = RetroFlowInput.model_validate(
        {"change_id": "CH-DEMO-001", "source_refs": [ref.model_dump(mode="json")]}
    )
    assert omitted.window is not None
    assert omitted.window.change_ids == ("CH-DEMO-001",)


def test_retro_id_and_build_slices_attempt_key_match_the_pre_control_output_values() -> None:
    prep = EvidenceArtifactRefV1(path="qa/results/intake/prepare.json", digest="a" * 64)
    plan = EvidenceArtifactRefV1(
        path="qa/results/plan/" + "b" * 64 + "/resolved-assurance-plan.json",
        digest="c" * 64,
    )
    review = EvidenceArtifactRefV1(path="qa/results/review/case-review.json", digest="d" * 64)
    report = EvidenceArtifactRefV1(path="qa/results/report/report.md", digest="e" * 64)
    window = RetroWindow(
        selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=("CH-DEMO-001",)),
        change_ids=("CH-DEMO-001",),
    )
    entered = RetroFlowInput.model_validate(
        {
            "change_id": "CH-DEMO-001",
            "window": window.model_dump(mode="json"),
            "source_refs": [report.model_dump(mode="json")],
            "preparation_refs": [prep.model_dump(mode="json"), plan.model_dump(mode="json")],
            "reviewed_refs": [plan.model_dump(mode="json"), review.model_dump(mode="json")],
            "report_receipt": {"receipt_id": "report", "receipt_digest": "f" * 64},
        }
    )
    assert entered.retro_id == ("retro-b26132a8bdb1d48d9b7932098ae497c071938996648d6b3e90aa135f4c61de91")
    assert entered.window is not None
    assert entered.retro_id is not None
    slices = RetroBuildSlicesInputV1(
        retro_id=entered.retro_id,
        window=entered.window,
        source_refs=entered.source_refs,
    )
    key = derive_attempt_key(
        invocation_id="inv-1",
        graph_revision="b" * 64,
        public_entrypoint="full",
        semantic_node_id="improvement.retro-build-slices",
        business_activation=BusinessActivation.one_shot(),
        contract_id="assurance.improvement.retro-build-slices",
        validated_input=slices,
    )
    assert key.digest == "bbf1ed377be7c5e6d189bedb570f630f8b9101cd0b9c8d61a2db4a995cd9ff21"


def test_failed_retro_is_failed_at_application_status_boundary() -> None:
    asyncio.run(_failed_retro())


async def _failed_retro() -> None:
    harness = GraphHarness()
    child = cast(Any, _features(harness)["assurance.improvement"]).retro
    features = _stub_features()
    improvement = features["assurance.improvement"]
    features["assurance.improvement"] = replace(cast(Any, improvement), retro=child)
    graph = thin_root_flows(
        ProductFeatureBundles(
            **cast(Any, {key.removeprefix("assurance."): value for key, value in features.items()})
        )
    )["retro"].compile(
        EngineGraphBuildContext(contracts={}, checkpointer=None, approved_source_roots=()),
    )
    harness._kernel.load_script(
        cast(
            Any,
            {
                "improvement.retro-build-slices": [
                    PermanentTaskFailure(kind="invalid_output", message="retro failed")
                ]
            },
        )
    )
    result = await graph.ainvoke(_public_input("retro"))
    status = _status_from_snapshot(SimpleNamespace(values=result, next=(), interrupts=()))
    assert status.status == "failed"
