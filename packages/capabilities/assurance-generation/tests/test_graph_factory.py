from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_generation.graphs.factory import GenerationGraphs, build_generation_graphs
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed

_SHA = "a" * 64
_RECEIPT_ID = "receipt-1"
_FAMILIES = ("api", "e2e", "fuzz", "performance")
_PHASE_NODES = frozenset(
    {
        "prepare",
        "execute",
        "finalize",
        "finalize-inputs",
        "repair-prepare",
        "repair-execute",
        "repair-finalize-inputs",
        "repair-finalize",
    }
)
_PURE_IDS = (
    "assurance.generation.complete",
    "assurance.generation.review-round.advance",
)
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_generation" / "graphs"


def generation_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


def generation_graph_input(
    *, selected: tuple[str, ...] = ("api", "e2e", "fuzz", "performance")
) -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "selected_test_families": list(selected),
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def family_graph_input(family: str, *, selected: bool = True) -> dict[str, object]:
    return {
        **generation_graph_input(selected=(family,) if selected else ("api",)),
        "family": family,
        "lane_selected": selected,
        "review_stage": "plan",
    }


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def _plan_output() -> dict[str, object]:
    return {"artifacts": [{"path": "qa/changes", "digest": _SHA}]}


def _review_output(
    decision: str = "pass",
    *,
    auto_fix: bool = False,
    human: bool = False,
    readiness: str = "ready",
    used: int | None = 0,
    budget: int | None = 2,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": decision,
        "auto_fix_allowed": auto_fix,
        "human_review_required": human,
        "codegen_readiness": readiness,
        "artifacts": [{"path": "qa/changes", "digest": _SHA}],
    }
    if used is not None:
        payload["rounds_used"] = used
    if budget is not None:
        payload["rounds_budget"] = budget
    return payload


def _codegen_output(*, verdict: str = "accepted") -> dict[str, object]:
    if verdict == "needs_fix":
        return {
            "schema_version": "2",
            "verdict": "needs_fix",
            "repair": {"allowed_paths": ["tests/api/test_users.py"], "summary": "repair"},
        }
    return {"schema_version": "2", "verdict": verdict}


def _fuzz_codegen_output() -> dict[str, object]:
    return {"schema_version": "1", "needs_fix": False}


def _node_names(graph: object) -> set[str]:
    names: set[str] = set()
    nodes = getattr(graph, "nodes", {})
    if not isinstance(nodes, dict):
        return names
    for name, node in nodes.items():
        if name in {"__start__", "__end__"}:
            continue
        names.add(str(name))
        nested = getattr(node, "nodes", None)
        if nested is not None:
            names.update(_node_names(node))
        runnable = getattr(node, "runnable", None)
        if runnable is not None:
            names.update(_node_names(runnable))
    return names


def _walk_graph_python() -> Iterator[Path]:
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def _semantic(family: str, stage: str) -> str:
    return f"generation.{family}.{stage}"


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.generation",
        contracts=generation_contracts(),
    )


def test_generation_factory_exports_root_and_four_families(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    assert tuple(item.name for item in fields(bundle)) == (
        "generation",
        "api",
        "e2e",
        "fuzz",
        "performance",
    )
    assert isinstance(bundle, GenerationGraphs)
    unique = tuple(dict.fromkeys(recording_context.bound_contract_ids))
    assert len(recording_context.bound_contract_ids) == 14
    assert len(unique) == 14
    assert set(unique) == {contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()}
    assert all(item not in unique for item in _PURE_IDS)
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)


def test_factory_binds_no_task_contract_for_pure_completion_or_round_advance(recording_context) -> None:
    build_generation_graphs(recording_context)
    assert "assurance.generation.complete" not in recording_context.bound_contract_ids
    assert "assurance.generation.review-round.advance" not in recording_context.bound_contract_ids
    assert AGENT_JOB_CONTRACTS.keys().isdisjoint({"complete", "review-round.advance"})


def test_target_graphs_contain_no_phase_nodes(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    names = set()
    for graph in (bundle.generation, bundle.api, bundle.e2e, bundle.fuzz, bundle.performance):
        names.update(_node_names(graph))
    assert names.isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source


def test_api_e2e_have_codegen_fix_and_fuzz_performance_do_not(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    assert "codegen-fix" in _node_names(bundle.api)
    assert "codegen-round-advance" in _node_names(bundle.api)
    assert "codegen-fix" in _node_names(bundle.e2e)
    assert "codegen-round-advance" in _node_names(bundle.e2e)
    assert "codegen-fix" not in _node_names(bundle.fuzz)
    assert "codegen-round-advance" not in _node_names(bundle.fuzz)
    assert "codegen-fix" not in _node_names(bundle.performance)
    assert "codegen-round-advance" not in _node_names(bundle.performance)


def test_pure_nodes_match_existing_handlers_for_valid_and_invalid_inputs() -> None:
    from pydantic import ValidationError

    from assurance_generation.contracts.decisions import advance_review_round, complete_generation
    from assurance_generation.graphs.nodes import advance_review_round_node, complete_generation_node

    complete_state = {
        "family_results": [
            {"family": family, "receipt_id": f"r-{family}", "selected": family in {"api", "fuzz"}}
            for family in _FAMILIES
        ],
        "selected_test_families": ["api", "fuzz"],
    }
    output = complete_generation_node(complete_state)
    expected = complete_generation({"completed": [{"value": True}] * 4, "selected_families": ["api", "fuzz"]})
    assert output == expected.model_dump(mode="json")
    with pytest.raises((ValidationError, ValueError)):
        complete_generation_node(
            {
                "family_results": [{"family": "api", "receipt_id": "r", "selected": True}],
                "selected_test_families": ["api"],
            }
        )

    advanced = advance_review_round_node(
        {"family": "api", "review_stage": "plan", "rounds_used": 0, "rounds_budget": 2}
    )
    expected_advance = advance_review_round(
        {"family": "api", "stage": "plan", "rounds_used": 0, "rounds_budget": 2}
    )
    assert advanced["rounds_used"] == expected_advance.rounds_used == 1
    with pytest.raises((ValidationError, ValueError)):
        advance_review_round_node(
            {"family": "api", "review_stage": "plan", "rounds_used": 2, "rounds_budget": 2}
        )


async def test_selected_family_runs_plan_review_codegen_without_phase_nodes() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.api,
        input=family_graph_input("api"),
        script={
            _semantic("api", "plan"): [committed(_plan_output(), receipt)],
            _semantic("api", "plan-review"): [committed(_review_output(), receipt)],
            _semantic("api", "codegen"): [committed(_codegen_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        _semantic("api", "plan"),
        _semantic("api", "plan-review"),
        _semantic("api", "codegen"),
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        "assurance.generation.agent.api.plan.v1",
        "assurance.generation.agent.api.plan-review.v1",
        "assurance.generation.agent.api.codegen.v1",
    ]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal.get("status") in {"passed", "done"} or terminal.get("decision") in {"pass", "approved"}


@pytest.mark.parametrize("family", ("fuzz", "performance"))
async def test_no_fix_family_completes_codegen_without_fixer(family: str) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    receipt = _receipt()
    graph = bundle.fuzz if family == "fuzz" else bundle.performance
    result = await harness.run(
        graph,
        input=family_graph_input(family),
        script={
            _semantic(family, "plan"): [committed(_plan_output(), receipt)],
            _semantic(family, "plan-review"): [committed(_review_output(), receipt)],
            _semantic(family, "codegen"): [committed(_fuzz_codegen_output(), receipt)],
        },
    )
    assert _semantic(family, "codegen-fix") not in [call.semantic_node_id for call in result.semantic_calls]
    assert result.terminal is not None


@pytest.mark.parametrize("family", ("api", "e2e"))
async def test_codegen_fix_loop_reaches_fixer(family: str) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    receipt = _receipt()
    graph = bundle.api if family == "api" else bundle.e2e
    result = await harness.run(
        graph,
        input=family_graph_input(family),
        script={
            _semantic(family, "plan"): [committed(_plan_output(), receipt)],
            _semantic(family, "plan-review"): [committed(_review_output(), receipt)],
            _semantic(family, "codegen"): [committed(_codegen_output(verdict="needs_fix"), receipt)],
            _semantic(family, "codegen-fix"): [committed(_codegen_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls][-2:] == [
        _semantic(family, "codegen"),
        _semantic(family, "codegen-fix"),
    ]
    assert result.terminal is not None
