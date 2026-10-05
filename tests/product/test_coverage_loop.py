"""Coverage re-entry on the real full flow."""

from __future__ import annotations

import asyncio

from graph_engine.testing.graph_harness import committed

from assurance_product.graphs.factory import build_product_graphs
from tests.product.test_full_flow import (
    _SHA,
    _RECEIPT,
    _case,
    _front,
    _invoke,
    _reported,
    _tail_until_inspect,
)
from tests.product.test_product_stategraph_flow import _build_context, _public_input
from tests.product.test_stategraph_entrypoints import _real_features


def _names(captured: list[tuple[str, object]]) -> list[str]:
    return [name for name, _item in captured]


def test_coverage_insufficient_reenters_the_shared_case_flow() -> None:
    asyncio.run(_reentry())


async def _reentry() -> None:
    script: dict[str, list[object]] = {}
    _front(script)
    _case(script, "pass", "pass", epochs=(0, 1))
    _tail_until_inspect(script, ("coverage_insufficient", 0), ("satisfied", 1))
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    _reported(script)
    done = await _invoke(
        script,
        budgets={
            "review_rounds": 2,
            "coverage_rounds": 1,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    )
    assert done.outcome == "achieved"
    assert _names(done.captured).count("intake.intake") == 1
    assert _names(done.captured).count("generation.init-test-runtime") == 1
    design = [item for name, item in done.captured if name == "intake.case-design"]
    reviews = [item for name, item in done.captured if name == "intake.case-review"]
    rework = [item for name, item in done.captured if name == "intake.coverage-rework"]
    assert len(design) == 2
    assert len(reviews) == 2
    assert len(rework) == 1
    assert getattr(design[0], "coverage_epoch") == 0
    assert getattr(design[0], "rework_ref") is None
    assert getattr(design[1], "coverage_epoch") == 1
    assert getattr(design[1], "rework_ref").path == "qa/results/cases/case-rework-context.json"
    assert getattr(rework[0], "reviewed_case_ref").path == "qa/cases/reviewed-case.json"
    assert getattr(rework[0], "handoff_ref").path == "qa/results/inspect/coverage-rework-handoff.json"
    assert getattr(rework[0], "inspect_receipt") == _RECEIPT
    assert getattr(reviews[0], "coverage_epoch") == 0
    assert getattr(reviews[1], "coverage_epoch") == 1
    generated = [item for name, item in done.captured if name == "generation.resolve-inputs"]
    assert getattr(generated[1], "coverage_epoch") == 1
    assert getattr(generated[1], "reviewed_case_ref").path == "qa/cases/reviewed-case.json"
    assert done.result["flow_control"]["loops"]["coverage"] == 1  # type: ignore[index]


def test_exhausted_coverage_budget_stops_without_report_or_retro() -> None:
    asyncio.run(_exhausted())


async def _exhausted() -> None:
    script: dict[str, list[object]] = {}
    _front(script)
    _case(script)
    _tail_until_inspect(script, ("coverage_insufficient", 0))
    stopped = await _invoke(
        script,
        budgets={
            "review_rounds": 2,
            "coverage_rounds": 0,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    )
    names = _names(stopped.captured)
    assert stopped.outcome == "not_achieved"
    assert names.count("intake.case-design") == 1
    assert "quality.report" not in names
    assert "improvement.retro" not in names
    assert stopped.result["terminal"] == {"status": "failed", "reason": "not_achieved"}  # type: ignore[index]


def test_compiled_product_graph_has_no_coverage_repair_route() -> None:
    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    nodes = set(graphs.entrypoints["full"].nodes)
    assert "tail" in nodes
    assert nodes.isdisjoint(
        {
            "coverage-repair",
            "coverage-repair-brief",
            "quality-recheck",
            "coverage-needed",
            "advance-coverage",
            "execute-tail",
        }
    )


def test_same_case_graph_object_handles_initial_and_rework_inputs() -> None:
    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert "case" in graphs.entrypoints["full"].nodes
    assert set(graphs.entrypoints["full"].nodes).isdisjoint({"case-rework", "coverage-case"})
    assert _public_input("full")["case_delta_paths"]
