from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from pydantic import ValidationError

from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_generation.graphs.factory import GenerationGraphs, build_generation_graphs
from assurance_generation.graphs.nodes import activation_codegen, terminal_done
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef
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


def _reviewed_case() -> dict[str, object]:
    plan_ref = {
        "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
        "digest": _SHA,
    }
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [
            {"path": "qa/requirement.md", "digest": _SHA},
            plan_ref,
        ],
        "case_refs": [
            {
                "path": "qa/cases/menus/case.yaml",
                "digest": _SHA,
            }
        ],
        "review_ref": {
            "path": "qa/results/review/case-review.json",
            "digest": _SHA,
        },
    }


def generation_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    return contracts


def generation_graph_input(
    *, selected: tuple[str, ...] = ("api", "e2e", "fuzz", "performance")
) -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": _SHA,
        "plan_ref": {
            "path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json",
            "digest": _SHA,
        },
        "selected_test_families": list(selected),
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
        "rounds_used": 0,
        "rounds_budget": 2,
        "coverage_epoch": 0,
        "reviewed_case": _reviewed_case(),
    }


def family_graph_input(family: str, *, selected: bool = True) -> dict[str, object]:
    return {
        **generation_graph_input(selected=(family,) if selected else ("api",)),
        "family": family,
        "lane_selected": selected,
        "review_stage": "codegen",
    }


def _receipt() -> ReceiptRef:
    return ReceiptRef(receipt_id=_RECEIPT_ID, receipt_digest=_SHA)


def _plan_output() -> dict[str, object]:
    return {"artifacts": [{"path": "qa/results", "digest": _SHA}]}


def _review_output(
    route: str = "codegen",
    *,
    used: int | None = 0,
    budget: int | None = 2,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "route": route,
        "finding_ids": [],
        "artifacts": [{"path": "qa/results", "digest": _SHA}],
    }
    if used is not None:
        payload["rounds_used"] = used
    if budget is not None:
        payload["rounds_budget"] = budget
    return payload


def _codegen_output() -> dict[str, object]:
    return {"schema_version": "1"}


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
        for attr in ("runnable", "bound"):
            child = getattr(node, attr, None)
            if child is not None and child is not graph:
                names.update(_node_names(child))
        subgraphs = getattr(node, "subgraphs", None)
        if isinstance(subgraphs, list):
            for subgraph in subgraphs:
                names.update(_node_names(subgraph))
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
        "init_runtime",
        "resolve_inputs",
    )
    assert isinstance(bundle, GenerationGraphs)
    unique = tuple(dict.fromkeys(recording_context.bound_contract_ids))
    assert len(unique) == 11
    assert set(recording_context.bound_contract_ids) == set(unique)
    assert set(unique) == {
        *(contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()),
        *(contract.contract_id for contract in TASK_ATTEMPT_CONTRACTS.values()),
    }
    assert all(item not in unique for item in _PURE_IDS)
    assert all(item is None for item in recording_context.compiled_subgraph_checkpointers)


@pytest.mark.parametrize("failed", (False, True))
async def test_resolve_inputs_export_publishes_review_or_stops(failed: bool) -> None:
    harness = GraphHarness()
    bundle = build_generation_graphs(
        harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    )
    payload = generation_graph_input()
    payload["reviewed_case"] = None
    payload["source_artifacts"] = [{"path": "qa/cases/reviewed-case.json", "digest": _SHA}]
    resolution = (
        PermanentTaskFailure(kind="invalid_input", message="review digest changed")
        if failed
        else committed(_reviewed_case(), ReceiptRef(receipt_id="review", receipt_digest=_SHA))
    )
    result = await harness.run(
        bundle.resolve_inputs, input=payload, script={"generation.resolve-inputs": [resolution]}
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["generation.resolve-inputs"]
    assert isinstance(result.terminal, dict)
    assert result.terminal["status"] == ("failed" if failed else "completed")
    assert result.terminal.get("reviewed_case") == (None if failed else _reviewed_case())


def test_factory_binds_no_task_contract_for_pure_completion_or_round_advance(recording_context) -> None:
    build_generation_graphs(recording_context)
    assert "assurance.generation.complete" not in recording_context.bound_contract_ids
    assert "assurance.generation.review-round.advance" not in recording_context.bound_contract_ids
    assert AGENT_JOB_CONTRACTS.keys().isdisjoint({"complete", "review-round.advance"})


def test_root_factory_does_not_ainvoke_family_graphs(recording_context) -> None:
    factory = _GRAPHS_ROOT / "factory.py"
    source = factory.read_text(encoding="utf-8")
    assert "ainvoke" not in source
    assert "_family_result_only" not in source
    bundle = build_generation_graphs(recording_context)
    assert "api" in _node_names(bundle.generation)
    assert "e2e" in _node_names(bundle.generation)
    assert "fuzz" in _node_names(bundle.generation)
    assert "performance" in _node_names(bundle.generation)
    assert "codegen-human-review" in _node_names(bundle.generation)
    assert "codegen-human-review-retry" in _node_names(bundle.generation)


def test_terminal_done_without_family_does_not_write_api_lane() -> None:
    skipped = {
        "family": "api",
        "receipt_id": "receipt-api",
        "selected": False,
        "status": "skipped",
    }
    update = terminal_done({"family_results": [skipped], "decision": "pass"})
    assert "family_results" not in update
    assert update.get("status") == "passed"


def test_codegen_activation_changes_across_coverage_epochs() -> None:
    state = {
        "coverage_epoch": 0,
        "family": "api",
        "rounds_used": 0,
        "reviewed_case": _reviewed_case(),
    }
    first = activation_codegen(state)
    second = activation_codegen({**state, "coverage_epoch": 1})
    assert first != second
    assert activation_codegen(dict(state)) == first


def test_codegen_activation_changes_when_same_case_path_has_new_bytes() -> None:
    reviewed = _reviewed_case()
    state = {
        "coverage_epoch": 0,
        "family": "api",
        "rounds_used": 0,
        "reviewed_case": reviewed,
    }
    changed = {
        **reviewed,
        "case_refs": [{**reviewed["case_refs"][0], "digest": "b" * 64}],  # type: ignore[index]
    }
    assert activation_codegen(state) != activation_codegen({**state, "reviewed_case": changed})


def test_target_graphs_contain_no_phase_nodes(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    names = set()
    for graph in (bundle.generation, bundle.api, bundle.e2e, bundle.fuzz, bundle.performance):
        names.update(_node_names(graph))
    assert names.isdisjoint(_PHASE_NODES)
    for path in _walk_graph_python():
        source = path.read_text(encoding="utf-8")
        assert "capability_slot" not in source


def test_pure_nodes_match_existing_handlers_for_valid_and_invalid_inputs() -> None:

    from assurance_generation.contracts.decisions import advance_review_round, complete_generation
    from assurance_generation.graphs.nodes import advance_review_round_node, complete_generation_node

    complete_state = {
        "family_results": [
            {
                "family": family,
                "receipt_id": f"r-{family}",
                "selected": family in {"api", "fuzz"},
                "status": "passed" if family in {"api", "fuzz"} else "skipped",
            }
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
        {"family": "api", "review_stage": "codegen", "rounds_used": 0, "rounds_budget": 2}
    )
    expected_advance = advance_review_round(
        {"family": "api", "stage": "codegen", "rounds_used": 0, "rounds_budget": 2}
    )
    assert advanced["rounds_used"] == expected_advance.rounds_used == 1
    with pytest.raises((ValidationError, ValueError)):
        advance_review_round_node(
            {"family": "api", "review_stage": "codegen", "rounds_used": 2, "rounds_budget": 2}
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
            _semantic("api", "codegen"): [committed(_codegen_output(), receipt)],
            _semantic("api", "codegen-review"): [committed(_review_output(), receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        _semantic("api", "codegen"),
        _semantic("api", "codegen-review"),
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        "assurance.generation.agent.api.codegen.v1",
        "assurance.generation.agent.api.codegen-review.v1",
    ]
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal.get("status") in {"passed", "done"} or terminal.get("decision") == "pass"


async def test_selected_family_plan_failure_stops_before_plan_review() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    receipt = _receipt()
    result = await harness.run(
        bundle.e2e,
        input=family_graph_input("e2e"),
        script={
            _semantic("e2e", "codegen"): [PermanentTaskFailure(kind="transient", message="provider TLS failed")],
            _semantic("e2e", "codegen-review"): [committed(_review_output(), receipt)],
        },
    )

    assert [call.semantic_node_id for call in result.semantic_calls] == [_semantic("e2e", "codegen")]
    terminal = result.terminal
    assert isinstance(terminal, Mapping)
    assert terminal["status"] == "failed"
    family_results = terminal["family_results"]
    assert isinstance(family_results, list)
    assert family_results[0]["family"] == "e2e"
    assert family_results[0]["status"] == "failed"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_authenticated_codegen_finishes_without_a_repair_verdict(family: Any, tmp_path: Path) -> None:
    from test_generation_cycle import cycle_fixture  # pyright: ignore[reportMissingImports]

    payload, script = await cycle_fixture(tmp_path, (family,))
    generated = payload.families[0]
    output = {
        "schema_version": "1",
        "change_id": payload.change_id,
        "layer": family,
        "files": [item.model_dump(mode="json") for item in generated.files],
        "mapping": generated.mapping.model_dump(mode="json"),
        "required_capabilities": list(payload.capability_leafs),
    }
    script[_semantic(family, "codegen")] = [committed(output, generated.receipt)]
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    graph = getattr(build_generation_graphs(context), family)
    result = await harness.run(graph, input=family_graph_input(family), script=script)

    assert isinstance(result.terminal, Mapping)
    assert result.terminal["status"] == "passed"
    lanes = result.terminal["family_results"]
    assert isinstance(lanes, list)
    lane = lanes[0]
    assert isinstance(lane, Mapping)
    generated_output = lane["generated"]
    assert isinstance(generated_output, Mapping)
    assert generated_output["files"] == output["files"]
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        _semantic(family, "codegen"),
        _semantic(family, "codegen-review"),
    ]


async def test_root_done_does_not_overwrite_skipped_api_result(tmp_path: Path) -> None:
    from test_generation_cycle import cycle_fixture  # pyright: ignore[reportMissingImports]
    from assurance_generation.operations.cycle import complete_generation_cycle

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    receipt = _receipt()
    payload, script = await cycle_fixture(tmp_path, ("e2e",))
    script["generation.publish-cycle"] = [
        committed(
            complete_generation_cycle(payload, tmp_path, tmp_path / ".stage").model_dump(mode="json"),
            receipt,
        )
    ]
    graph_input = generation_graph_input(selected=("e2e",))
    graph_input.update(
        {
            "plan_digest": payload.plan_digest,
            "plan_ref": payload.plan_ref.model_dump(mode="json"),
            "reviewed_case": payload.reviewed_case.model_dump(mode="json"),
        }
    )
    result = await harness.run(
        bundle.generation,
        input=graph_input,
        script=script,
    )
    terminal = result.terminal
    assert isinstance(terminal, dict)
    results = terminal["family_results"]
    by_family = {item["family"]: item for item in results}
    assert by_family["api"]["status"] == "skipped"
    assert by_family["api"]["selected"] is False
    assert by_family["e2e"]["status"] == "passed"
    assert by_family["fuzz"]["status"] == "skipped"
    assert by_family["performance"]["status"] == "skipped"


@pytest.mark.parametrize("family", _FAMILIES)
async def test_root_fails_closed_without_publishing_when_selected_family_attempt_fails(
    family: Any,
    tmp_path: Path,
) -> None:
    from test_generation_cycle import cycle_fixture  # pyright: ignore[reportMissingImports]

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    payload, script = await cycle_fixture(tmp_path, (family,))
    script[_semantic(family, "codegen")] = [
        PermanentTaskFailure(kind="invalid_output", message="generated target is missing")
    ]

    result = await harness.run(
        bundle.generation,
        input={
            **generation_graph_input(selected=(family,)),
            "plan_digest": payload.plan_digest,
            "plan_ref": payload.plan_ref.model_dump(mode="json"),
            "reviewed_case": payload.reviewed_case.model_dump(mode="json"),
        },
        script=script,
    )

    terminal = result.terminal
    assert isinstance(terminal, Mapping)
    assert terminal["status"] == "failed"
    assert "generation.publish-cycle" not in [call.semantic_node_id for call in result.semantic_calls]
    families = terminal["family_results"]
    assert isinstance(families, list)
    failed = next(item for item in families if isinstance(item, Mapping) and item.get("family") == family)
    assert failed["selected"] is True
    assert failed["status"] == "failed"
    assert "generated" not in failed
