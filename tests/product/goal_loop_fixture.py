from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from assurance_execution.graphs.factory import ExecutionGraphs
from assurance_generation.graphs.factory import GenerationGraphs
from assurance_healing.graphs.factory import HealingGraphs
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_intake.graphs.factory import IntakeGraphs
from assurance_product.graphs.factory import invoke_product_root
from assurance_quality.graphs.factory import QualityGraphs

from tests.product.test_product_stategraph_flow import (
    _applied,
    _case,
    _echo,
    _execution,
    _flow_features,
    _generation,
    _inspection,
    _product_graphs,
    _public_input,
    _report,
    _reviewed,
)


@dataclass(frozen=True, slots=True)
class GoalLoopScenario:
    coverage_values: tuple[float, ...] = (1.0,)
    coverage_rounds: int = 2
    execution_failures: int = 0
    healing_rounds: int = 2
    report_fails: bool = False
    proposal_only: bool = False
    unchanged_case_bytes: bool = False
    entrypoint: Literal["full", "execute"] = "full"


@dataclass(frozen=True, slots=True)
class GoalLoopRun:
    state: dict[str, object]
    node_visits: tuple[str, ...]
    task_dispatches: tuple[str, ...]
    coverage_epochs: tuple[int, ...]
    attempt_keys: dict[str, tuple[str, ...]]
    artifact_paths: tuple[Path, ...]

    def dispatch_count(self, semantic_node_id: str) -> int:
        return self.task_dispatches.count(semantic_node_id)


def _recording_graph(
    *,
    label: str,
    semantic_id: str | None,
    updates: tuple[Mapping[str, object], ...],
    visits: list[str],
    dispatches: list[str],
    attempt_keys: dict[str, list[str]],
):
    calls = {"count": 0}

    def update(state: object) -> dict[str, object]:
        del state
        index = min(calls["count"], len(updates) - 1)
        calls["count"] += 1
        visits.append(label)
        if semantic_id is not None:
            dispatches.append(semantic_id)
            attempt_keys.setdefault(semantic_id, []).append(f"{semantic_id}:{index}")
        return dict(updates[index])

    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(cast(Any, dict))
    builder.add_node("record", update)
    builder.add_edge(START, "record")
    builder.add_edge("record", END)
    return builder.compile()


def _scenario_features(
    scenario: GoalLoopScenario,
    visits: list[str],
    dispatches: list[str],
    attempt_keys: dict[str, list[str]],
) -> dict[str, object]:
    coverage = scenario.coverage_values or (1.0,)
    epochs = min(len(coverage), scenario.coverage_rounds + 1)
    cases = tuple(_case(epoch) for epoch in range(epochs))
    generations = tuple(_generation(epoch) for epoch in range(epochs))
    executions: list[Mapping[str, object]] = []
    inspections: list[Mapping[str, object]] = []
    if scenario.execution_failures:
        executions.append(_execution(0, status="FAIL"))
        inspections.append(_inspection(0, "repairable_execution_failure"))
        executions.append(_execution(0, repair_round=1))
        inspections.append(_inspection(0))
    else:
        for epoch, value in enumerate(coverage[:epochs]):
            executions.append(_execution(epoch))
            inspections.append(_inspection(epoch, "satisfied" if value >= 0.90 else "coverage_insufficient"))
    report_epoch = max(0, epochs - 1)
    features = _flow_features()
    features["assurance.intake"] = IntakeGraphs(
        prepare=_recording_graph(
            label="prepare",
            semantic_id="intake.prepare",
            updates=(
                {
                    "status": "prepared",
                    "preparation_refs": [ref.model_dump(mode="json") for ref in _reviewed().preparation_refs],
                },
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        case=_recording_graph(
            label="case",
            semantic_id="intake.case-design",
            updates=cases,
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
    )
    features["assurance.generation"] = GenerationGraphs(
        generation=_recording_graph(
            label="generation",
            semantic_id="generation.generate",
            updates=generations,
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        api=_echo({}),
        e2e=_echo({}),
        fuzz=_echo({}),
        performance=_echo({}),
    )
    features["assurance.execution"] = ExecutionGraphs(
        execute=_recording_graph(
            label="execution",
            semantic_id="execution.execute",
            updates=tuple(executions),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        rerun=_recording_graph(
            label="rerun",
            semantic_id="execution.run",
            updates=(executions[1] if scenario.execution_failures else executions[-1],),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
    )
    features["assurance.quality"] = QualityGraphs(
        assess=_recording_graph(
            label="inspect",
            semantic_id="quality.inspect",
            updates=tuple(inspections),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        issue_review=_echo({}),
        issue_analyze=_echo({}),
        issue_reconcile=_echo({}),
        report=_recording_graph(
            label="report",
            semantic_id="quality.report",
            updates=(
                {"status": "failed", "attempt_failure": {"kind": "runtime"}}
                if scenario.report_fails
                else _report(report_epoch),
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
    )
    repair = (
        {"proposal_result": {"change_id": "CH-DEMO-001"}, "status": "passed"}
        if scenario.proposal_only
        else (
            _applied()
            if scenario.healing_rounds > 0
            else {
                "repair_result": {
                    "change_id": "CH-DEMO-001",
                    "coverage_epoch": 0,
                    "repair_round": 1,
                    "status": "exhausted",
                },
                "status": "exhausted",
            }
        )
    )
    features["assurance.healing"] = HealingGraphs(
        repair_failure=_recording_graph(
            label="fix-proposal",
            semantic_id="healing.apply-test-repair",
            updates=(repair,),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        repair_coverage=_echo({}),
    )
    improvement = features["assurance.improvement"]
    assert isinstance(improvement, ImprovementGraphs)
    features["assurance.improvement"] = ImprovementGraphs(
        archive=improvement.archive,
        retro=_recording_graph(
            label="retro",
            semantic_id="improvement.retro",
            updates=({"status": "done"},),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        review=improvement.review,
        evaluate=improvement.evaluate,
        export=improvement.export,
        apply=_recording_graph(
            label="improvement",
            semantic_id="improvement.apply",
            updates=({"status": "done"},),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        rollback=improvement.rollback,
    )
    return features


async def run_goal_loop(root: Path, scenario: GoalLoopScenario) -> GoalLoopRun:
    root.mkdir(parents=True, exist_ok=True)
    visits: list[str] = []
    dispatches: list[str] = []
    keys: dict[str, list[str]] = {}
    features = _scenario_features(scenario, visits, dispatches, keys)
    graphs = _product_graphs(features)
    payload = _public_input(
        scenario.entrypoint,
        budgets={
            "review_rounds": 2,
            "coverage_rounds": scenario.coverage_rounds,
            "healing_rounds": scenario.healing_rounds,
            "execution_retries": 1,
        },
    )
    state = await asyncio.to_thread(
        invoke_product_root,
        graphs,
        scenario.entrypoint,
        payload,
    )
    epochs = tuple(int(item.rsplit(":", 1)[1]) for item in keys.get("intake.case-design", ()))
    return GoalLoopRun(
        state=state,
        node_visits=tuple(visits),
        task_dispatches=tuple(dispatches),
        coverage_epochs=epochs,
        attempt_keys={name: tuple(values) for name, values in keys.items()},
        artifact_paths=tuple(sorted(root.rglob("*"))),
    )


__all__ = ["GoalLoopRun", "GoalLoopScenario", "run_goal_loop"]
