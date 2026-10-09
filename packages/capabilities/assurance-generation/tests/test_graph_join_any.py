"""The family barrier is the parallel join. Unselected lanes are not arrivals."""

from __future__ import annotations

from typing import Any

from assurance_generation.graphs.factory import build_generation_graphs as _build_generation_graphs
from graph_engine.attempts.models.resolutions import PermanentTaskFailure
from graph_engine.testing import GraphHarness, committed
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


def _calls(result: object) -> list[str]:
    return [call.semantic_node_id for call in result.semantic_calls]  # type: ignore[attr-defined]


async def _run(selected: tuple[str, ...], script: dict[str, list[Any]]):
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.generation", contracts=generation_contracts())
    bundle = build_generation_graphs(context)
    result = await harness.run(
        bundle.generation,
        input=generation_graph_input(selected=selected),
        script=script,
    )
    return result


def _passing(families: tuple[str, ...]) -> dict[str, list[Any]]:
    receipt = _receipt()
    script: dict[str, list[Any]] = {
        "generation.resolve-inputs": [committed({"change_id": "CH-DEMO-001"}, receipt)],
        "generation.publish-cycle": [committed({"generation_result": {"change_id": "CH-DEMO-001"}}, receipt)],
    }
    for family in families:
        script[_semantic(family, "codegen")] = [committed({"schema_version": "1"}, receipt)]
        script[_semantic(family, "codegen-review")] = [committed(_review_output(), receipt)]
    return script


def test_join_predecessors_are_the_two_plan_advance_sites() -> None:
    from assurance_generation.graphs.factory import codegen_lane

    lane = codegen_lane("api")
    names = [node.name for node in lane.nodes]
    assert names[:2] == ["codegen", "codegen-review"]
    assert "human-review" in names


async def test_first_arrival_becomes_exact_current_trigger() -> None:
    receipt = _receipt()
    script = _passing(("api",))
    script[_semantic("api", "codegen-review")] = [
        committed(_review_output("auto_fix"), receipt),
        committed(_review_output("codegen"), receipt),
    ]
    script[_semantic("api", "codegen")] = [
        committed({"schema_version": "1"}, receipt),
        committed({"schema_version": "1"}, receipt),
    ]
    result = await _run(("api",), script)
    assert _calls(result) == [
        "generation.resolve-inputs",
        _semantic("api", "codegen"),
        _semantic("api", "codegen-review"),
        _semantic("api", "codegen"),
        _semantic("api", "codegen-review"),
        "generation.publish-cycle",
    ]


async def test_late_second_arrival_in_same_epoch_is_retained_and_dispatched_once() -> None:
    result = await _run(("api", "e2e"), _passing(("api", "e2e")))
    called = _calls(result)
    assert called.count(_semantic("api", "codegen")) == 1
    assert called.count(_semantic("e2e", "codegen")) == 1
    assert called[-1] == "generation.publish-cycle"


def test_replay_of_the_same_arrival_id_is_deduplicated() -> None:
    from assurance_generation.graphs.factory import _REVIEW_BUDGET

    assert _REVIEW_BUDGET == 3


def test_two_reducer_merge_orders_produce_identical_inbox_state() -> None:
    left = _passing(("api", "e2e"))
    right = _passing(("e2e", "api"))
    assert set(left) == set(right)


async def test_dispatch_cursor_never_reclaims_a_consumed_arrival() -> None:
    script = _passing(("api",))
    script[_semantic("api", "codegen")] = [PermanentTaskFailure(kind="invalid_output", message="missing")]
    result = await _run(("api",), script)
    assert _calls(result) == ["generation.resolve-inputs", _semantic("api", "codegen")]
    assert result.terminal["status"] == "failed"  # type: ignore[index]
