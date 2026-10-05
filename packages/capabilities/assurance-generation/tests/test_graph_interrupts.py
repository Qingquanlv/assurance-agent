"""Codegen lanes pause on human-review and resume with Command."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.types import Command

from assurance_generation.graphs.factory import build_generation_graphs as _build_generation_graphs
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import (
    _INPUT,
    _LOCK,
    _REVISION,
    _prepare_anchored_backend,
)
from test_generation_graph_factory import (  # pyright: ignore[reportMissingImports]
    _receipt,
    _review_output,
    _semantic,
    generation_contracts,
    generation_graph_input,
)

from graph_engine.testing.feature_bundle import compile_bundle


def build_generation_graphs(*args, **kwargs):
    return compile_bundle(_build_generation_graphs(*args, **kwargs))


_FAMILIES = ("api", "e2e", "fuzz", "performance")


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": _REVISION,
            "assurance_product_lock_digest": _LOCK,
            "assurance_root_input_digest": _INPUT,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "execute",
        }
    }


async def _open(family: str, reviews: list[dict[str, object]], *, codegen_count: int = 1):
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    receipt = _receipt()
    script: dict[str, list[Any]] = {
        "generation.resolve-inputs": [committed({"change_id": "CH-DEMO-001"}, receipt)],
        "generation.publish-cycle": [committed({"generation_result": {"change_id": "CH-DEMO-001"}}, receipt)],
        _semantic(family, "codegen"): [
            committed({"schema_version": "1"}, receipt) for _ in range(codegen_count)
        ],
        _semantic(family, "codegen-review"): [committed(item, receipt) for item in reviews],
    }
    harness._kernel.load_script(script)
    graph = bundle.generation
    graph.checkpointer = backend  # type: ignore[attr-defined]
    return harness, graph


def _interrupt_id(result: object) -> str:
    assert isinstance(result, dict)
    interrupts = result.get("__interrupt__")
    assert interrupts
    value = getattr(interrupts[0], "value", None)
    assert isinstance(value, dict)
    return str(value["interrupt_id"])


@pytest.mark.parametrize("family", _FAMILIES)
async def test_pass_completes_without_another_codegen(family: str) -> None:
    harness, graph = await _open(family, [_review_output("codegen")])
    finished = await graph.ainvoke(generation_graph_input(selected=(family,)), config=_config())
    assert finished["status"] == "passed"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls].count(
        _semantic(family, "codegen")
    ) == 1


@pytest.mark.parametrize("family", _FAMILIES)
async def test_reject_fails_the_lane(family: str) -> None:
    harness, graph = await _open(family, [_review_output("reject")])
    finished = await graph.ainvoke(generation_graph_input(selected=(family,)), config=_config())
    assert finished["status"] == "failed"
    assert "generation.publish-cycle" not in [
        call.semantic_node_id for call in harness._kernel.semantic_calls
    ]


@pytest.mark.parametrize("family", _FAMILIES)
async def test_human_approve_resumes_to_publish(family: str) -> None:
    _harness, graph = await _open(family, [_review_output("human")])
    paused = await graph.ainvoke(generation_graph_input(selected=(family,)), config=_config())
    assert _interrupt_id(paused) == f"generation.{family}.human-review"
    finished = await graph.ainvoke(Command(resume={"action": "approve"}), config=_config())
    assert finished["status"] == "passed"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_human_reject_fails_without_publish(family: str) -> None:
    harness, graph = await _open(family, [_review_output("human")])
    paused = await graph.ainvoke(generation_graph_input(selected=(family,)), config=_config())
    assert _interrupt_id(paused) == f"generation.{family}.human-review"
    finished = await graph.ainvoke(Command(resume={"action": "reject"}), config=_config())
    assert finished["status"] == "failed"
    assert "generation.publish-cycle" not in [
        call.semantic_node_id for call in harness._kernel.semantic_calls
    ]


async def test_request_rework_runs_codegen_again() -> None:
    harness, graph = await _open(
        "api",
        [_review_output("human"), _review_output("codegen")],
        codegen_count=2,
    )
    paused = await graph.ainvoke(generation_graph_input(selected=("api",)), config=_config())
    assert _interrupt_id(paused) == "generation.api.human-review"
    finished = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=_config())
    assert finished["status"] == "passed"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls].count(
        "generation.api.codegen"
    ) == 2


async def test_auto_fix_exhausts_after_the_budget() -> None:
    harness, graph = await _open(
        "api",
        [_review_output("auto_fix") for _ in range(4)],
        codegen_count=4,
    )
    finished = await graph.ainvoke(generation_graph_input(selected=("api",)), config=_config())
    assert finished["status"] == "failed"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls].count(
        "generation.api.codegen"
    ) == 4
    assert "generation.publish-cycle" not in [
        call.semantic_node_id for call in harness._kernel.semantic_calls
    ]
