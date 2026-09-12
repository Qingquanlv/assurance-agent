from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from assurance_execution.contracts import ExecutionCycleResultV1
from assurance_execution.graphs.factory import ExecutionGraphs
from assurance_generation.graphs.factory import GenerationGraphs
from assurance_healing.graphs.factory import HealingGraphs
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_intake.graphs.factory import IntakeGraphs
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_product.graphs.factory import build_product_graphs, invoke_product_root, product_invoke_config
from assurance_quality.graphs.factory import QualityGraphs
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from tests.product.test_product_stategraph_flow import (
    _applied,
    _analysis_result,
    _build_context,
    _case,
    _diagnostic_report,
    _echo,
    _execution,
    _flow_features,
    _generation,
    _inspection,
    _public_input,
    _ref,
    _report,
    _reviewed,
)
from tests.acg_plan_fixture import install_plan

CrashPoint = Literal[
    "after_coverage_advance",
    "after_application_commit",
    "after_inspect_commit",
]
_SCENARIO_FILE = "goal-loop-scenario.json"
_CHECKPOINT_FILE = "goal-loop-checkpoints.sqlite"
_EVENT_FILE = "goal-loop-events.jsonl"
_THREAD_ID = "goal-loop-invocation"


class InjectedGoalLoopCrash(RuntimeError):
    def __init__(self, cut: CrashPoint) -> None:
        super().__init__(f"injected goal-loop crash: {cut}")
        self.cut = cut


@dataclass(frozen=True, slots=True)
class GoalLoopScenario:
    coverage_values: tuple[float, ...] = (1.0,)
    coverage_rounds: int = 2
    execution_failures: int = 0
    execution_disposition: Literal["repairable_execution_failure", "blocked", "needs_human"] = (
        "repairable_execution_failure"
    )
    healing_rounds: int = 2
    report_fails: bool = False
    proposal_only: bool = False
    unchanged_case_bytes: bool = False
    entrypoint: Literal["full", "execute"] = "full"
    candidate_families: tuple[str, ...] = ("api", "e2e")
    proposed_families: tuple[str, ...] = ("api",)


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


def _event_path(root: Path) -> Path:
    return root / _EVENT_FILE


def _append_event(root: Path, *, label: str, semantic_id: str | None, key: str) -> None:
    payload = {"label": label, "semantic_id": semantic_id, "attempt_key": key}
    with _event_path(root).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")


def _read_events(root: Path) -> tuple[dict[str, object], ...]:
    path = _event_path(root)
    if not path.exists():
        return ()
    return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())


def _state_epoch(state: Mapping[str, object]) -> int:
    value = state.get("coverage_epoch", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _scenario_plan(root: Path, scenario: GoalLoopScenario) -> tuple[ResolvedAssurancePlan, dict[str, str]]:
    return install_plan(
        root,
        "CH-DEMO-001",
        candidates=cast(Any, scenario.candidate_families),
        proposed=cast(Any, scenario.proposed_families),
    )


def _bind_plan(
    value: Mapping[str, object],
    plan: ResolvedAssurancePlan,
    ref: Mapping[str, str],
) -> dict[str, object]:
    result = deepcopy(dict(value))
    plan_digest = str(getattr(plan, "plan_digest"))

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if "plan_digest" in item:
                item["plan_digest"] = plan_digest
            if "plan_ref" in item:
                item["plan_ref"] = dict(ref)
            if "selected_test_families" in item:
                item["selected_test_families"] = list(getattr(plan, "selected_test_families"))
            preparation = item.get("preparation_refs")
            if isinstance(preparation, list):
                without_plan = [
                    candidate
                    for candidate in preparation
                    if not isinstance(candidate, Mapping) or "/plan/" not in str(candidate.get("path", ""))
                ]
                item["preparation_refs"] = sorted(
                    [dict(ref), *without_plan],
                    key=lambda candidate: (
                        str(candidate.get("path", "")),
                        str(candidate.get("digest", "")),
                    ),
                )
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(result)
    return result


def _recording_graph(
    *,
    root: Path,
    label: str,
    semantic_id: str | None,
    updates: tuple[Mapping[str, object], ...],
    visits: list[str],
    dispatches: list[str],
    attempt_keys: dict[str, list[str]],
    select_index: Callable[[Mapping[str, object]], int] = _state_epoch,
    pause_after: CrashPoint | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
):
    def update(raw_state: object) -> dict[str, object]:
        state = raw_state if isinstance(raw_state, Mapping) else {}
        index = min(max(select_index(state), 0), len(updates) - 1)
        key = f"{semantic_id or label}:{index}"
        visits.append(label)
        _append_event(root, label=label, semantic_id=semantic_id, key=key)
        if semantic_id is not None:
            dispatches.append(semantic_id)
            attempt_keys.setdefault(semantic_id, []).append(key)
        return dict(updates[index])

    builder = StateGraph(cast(Any, dict))
    builder.add_node("record", cast(Callable[..., Any], update))
    builder.add_edge(START, "record")
    if pause_after is None:
        builder.add_edge("record", END)
    else:

        def pause(state: object) -> dict[str, object]:
            marker = root / f".{pause_after}.triggered"
            if not marker.exists():
                marker.write_text("triggered\n", encoding="utf-8")
                raise InjectedGoalLoopCrash(pause_after)
            return dict(state) if isinstance(state, Mapping) else {}

        builder.add_node("crash-point", cast(Callable[..., Any], pause))
        builder.add_edge("record", "crash-point")
        builder.add_edge("crash-point", END)
    return builder.compile(checkpointer=checkpointer if pause_after is not None else None)


def _scenario_features(
    root: Path,
    scenario: GoalLoopScenario,
    visits: list[str],
    dispatches: list[str],
    attempt_keys: dict[str, list[str]],
    *,
    crash_at: CrashPoint | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> dict[str, object]:
    plan, plan_ref = _scenario_plan(root, scenario)
    coverage = scenario.coverage_values or (1.0,)
    epochs = min(len(coverage), scenario.coverage_rounds + 1)
    cases = tuple(_bind_plan(_case(epoch), plan, plan_ref) for epoch in range(epochs))
    generations = tuple(_bind_plan(_generation(epoch), plan, plan_ref) for epoch in range(epochs))
    executions: list[Mapping[str, object]] = []
    inspections: list[Mapping[str, object]] = []
    if scenario.execution_failures:
        executions.append(_bind_plan(_execution(0, status="FAIL"), plan, plan_ref))
        inspections.append(_bind_plan(_inspection(0, scenario.execution_disposition), plan, plan_ref))
        executions.append(_bind_plan(_execution(0, repair_round=1), plan, plan_ref))
        inspections.append(
            _bind_plan(
                _inspection(
                    0,
                    "satisfied" if coverage[0] >= 0.90 else "coverage_insufficient",
                ),
                plan,
                plan_ref,
            )
        )
        for epoch, value in enumerate(coverage[1:epochs], start=1):
            executions.append(_bind_plan(_execution(epoch), plan, plan_ref))
            inspections.append(
                _bind_plan(
                    _inspection(epoch, "satisfied" if value >= 0.90 else "coverage_insufficient"),
                    plan,
                    plan_ref,
                )
            )
    else:
        for epoch, value in enumerate(coverage[:epochs]):
            executions.append(_bind_plan(_execution(epoch), plan, plan_ref))
            inspections.append(
                _bind_plan(
                    _inspection(epoch, "satisfied" if value >= 0.90 else "coverage_insufficient"),
                    plan,
                    plan_ref,
                )
            )
    report_epoch = max(0, epochs - 1)
    features = _flow_features()
    features["assurance.intake"] = IntakeGraphs(
        prepare=_recording_graph(
            root=root,
            label="prepare",
            semantic_id="intake.resolve-plan",
            updates=(
                {
                    "plan_digest": plan.plan_digest,
                    "plan_ref": plan_ref,
                    "selected_test_families": list(plan.selected_test_families),
                    "status": "prepared",
                    "preparation_refs": _bind_plan(
                        {
                            "preparation_refs": [
                                ref.model_dump(mode="json") for ref in _reviewed().preparation_refs
                            ]
                        },
                        plan,
                        plan_ref,
                    )["preparation_refs"],
                },
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
        ),
        load_plan=_recording_graph(
            root=root,
            label="load-plan",
            semantic_id="intake.load-plan",
            updates=(
                {
                    "plan_digest": plan.plan_digest,
                    "plan_ref": plan_ref,
                    "selected_test_families": list(plan.selected_test_families),
                    "status": "prepared",
                    "reviewed_case": _bind_plan(_reviewed().model_dump(mode="json"), plan, plan_ref),
                },
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
        ),
        case=_recording_graph(
            root=root,
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
            root=root,
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
        init_runtime=_echo({"status": "completed"}),
    )
    features["assurance.execution"] = ExecutionGraphs(
        execute=_recording_graph(
            root=root,
            label="execution",
            semantic_id="execution.execute",
            updates=tuple(executions),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=(
                (lambda state: 0 if _state_epoch(state) == 0 else _state_epoch(state) + 1)
                if scenario.execution_failures
                else _state_epoch
            ),
        ),
        rerun=_recording_graph(
            root=root,
            label="rerun",
            semantic_id="execution.run",
            updates=(executions[1] if scenario.execution_failures else executions[-1],),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
        ),
    )

    def inspection_index(state: Mapping[str, object]) -> int:
        if not scenario.execution_failures:
            return _state_epoch(state)
        try:
            execution = ExecutionCycleResultV1.model_validate(state.get("execution_result"))
        except (TypeError, ValueError):
            return 0
        if execution.repair_round:
            return 1
        return 0 if execution.coverage_epoch == 0 else execution.coverage_epoch + 1

    features["assurance.quality"] = QualityGraphs(
        assess=_recording_graph(
            root=root,
            label="inspect",
            semantic_id="quality.inspect",
            updates=tuple(inspections),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=inspection_index,
            pause_after="after_inspect_commit" if crash_at == "after_inspect_commit" else None,
            checkpointer=checkpointer,
        ),
        issue_review=_echo({}),
        issue_analyze=_recording_graph(
            root=root,
            label="issue-analyze",
            semantic_id="quality.issue-analyze",
            updates=(
                {
                    **_analysis_result("product_bug"),
                    "classification": "product_bug",
                    "fix_eligible": False,
                    "evidence_refs": [_ref("qa/results/inspect/issue-analysis.json").model_dump(mode="json")],
                    "status": "passed",
                },
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
        ),
        issue_reconcile=_echo({}),
        report=_recording_graph(
            root=root,
            label="report",
            semantic_id="quality.report",
            updates=(
                {"status": "failed", "attempt_failure": {"kind": "runtime"}}
                if scenario.report_fails
                else _bind_plan(
                    (
                        _diagnostic_report(report_epoch)
                        if scenario.execution_failures and scenario.execution_disposition == "blocked"
                        else _report(report_epoch)
                    ),
                    plan,
                    plan_ref,
                ),
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
        ),
        fact_baseline=_recording_graph(
            root=root,
            label="fact-baseline",
            semantic_id="quality.fact-baseline",
            updates=(
                {"fact_baseline_ref": _ref("qa/results/facts/fact-baseline.json").model_dump(mode="json")},
            ),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=_state_epoch,
        ),
    )
    repair = (
        {"proposal_result": {"change_id": "CH-DEMO-001"}, "status": "passed"}
        if scenario.proposal_only
        else (
            _bind_plan(_applied(), plan, plan_ref)
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
            root=root,
            label="fix-proposal",
            semantic_id="healing.apply-test-repair",
            updates=(repair,),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
            pause_after=("after_application_commit" if crash_at == "after_application_commit" else None),
            checkpointer=checkpointer,
        ),
        repair_coverage=_echo({}),
    )
    improvement = features["assurance.improvement"]
    assert isinstance(improvement, ImprovementGraphs)
    features["assurance.improvement"] = ImprovementGraphs(
        archive=improvement.archive,
        retro=_recording_graph(
            root=root,
            label="retro",
            semantic_id="improvement.retro",
            updates=({"status": "done"},),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
        ),
        review=improvement.review,
        evaluate=improvement.evaluate,
        export=improvement.export,
        apply=_recording_graph(
            root=root,
            label="improvement",
            semantic_id="improvement.apply",
            updates=({"status": "done"},),
            visits=visits,
            dispatches=dispatches,
            attempt_keys=attempt_keys,
            select_index=lambda state: 0,
        ),
        rollback=improvement.rollback,
    )
    return features


def _config(entrypoint: str) -> RunnableConfig:
    config = dict(product_invoke_config(entrypoint))
    config["configurable"] = {
        "thread_id": _THREAD_ID,
        "assurance_revision_id": "goal-loop-state-v2",
        "assurance_fencing_token": 1,
        "assurance_entrypoint": entrypoint,
    }
    return cast(RunnableConfig, config)


def _observed_run(root: Path, state: Mapping[str, object]) -> GoalLoopRun:
    events = _read_events(root)
    dispatches = tuple(
        str(item["semantic_id"]) for item in events if isinstance(item.get("semantic_id"), str)
    )
    keys: dict[str, list[str]] = {}
    for item in events:
        semantic = item.get("semantic_id")
        key = item.get("attempt_key")
        if isinstance(semantic, str) and isinstance(key, str):
            keys.setdefault(semantic, []).append(key)
    epochs = tuple(int(key.rsplit(":", 1)[1]) for key in keys.get("intake.case-design", ()))
    return GoalLoopRun(
        state=dict(state),
        node_visits=tuple(str(item["label"]) for item in events),
        task_dispatches=dispatches,
        coverage_epochs=epochs,
        attempt_keys={name: tuple(values) for name, values in keys.items()},
        artifact_paths=tuple(sorted(path for path in root.rglob("*") if path.is_file())),
    )


def _scenario_document(scenario: GoalLoopScenario, crash_at: CrashPoint | None) -> dict[str, object]:
    return {"scenario": asdict(scenario), "crash_at": crash_at}


def _load_scenario(root: Path) -> tuple[GoalLoopScenario, CrashPoint | None]:
    document = json.loads((root / _SCENARIO_FILE).read_text(encoding="utf-8"))
    raw = dict(document["scenario"])
    raw["coverage_values"] = tuple(raw["coverage_values"])
    raw["candidate_families"] = tuple(raw["candidate_families"])
    raw["proposed_families"] = tuple(raw["proposed_families"])
    return GoalLoopScenario(**raw), cast(CrashPoint | None, document.get("crash_at"))


def _invoke_persisted(
    root: Path,
    scenario: GoalLoopScenario,
    *,
    crash_at: CrashPoint | None,
    resume: bool,
) -> GoalLoopRun:
    visits: list[str] = []
    dispatches: list[str] = []
    keys: dict[str, list[str]] = {}
    checkpoint = root / _CHECKPOINT_FILE
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        features = _scenario_features(
            root,
            scenario,
            visits,
            dispatches,
            keys,
            crash_at=crash_at,
            checkpointer=saver,
        )
        graphs = build_product_graphs(context=_build_context(saver), features=features)
        graph = graphs.entrypoints[scenario.entrypoint]
        config = _config(scenario.entrypoint)
        if resume:
            state = graph.invoke(None, config=config)
        else:
            _plan, plan_ref = _scenario_plan(root, scenario)
            payload = _public_input(
                scenario.entrypoint,
                candidate_test_families=(
                    scenario.candidate_families if scenario.entrypoint == "full" else ()
                ),
                resolved_plan_ref=(plan_ref if scenario.entrypoint == "execute" else None),
                budgets={
                    "review_rounds": 2,
                    "coverage_rounds": scenario.coverage_rounds,
                    "healing_rounds": scenario.healing_rounds,
                    "execution_retries": 1,
                },
            )
            try:
                state = (
                    graph.invoke(payload, config=config, interrupt_after=["advance-coverage"])
                    if crash_at == "after_coverage_advance"
                    else graph.invoke(payload, config=config)
                )
            except InjectedGoalLoopCrash as error:
                if error.cut != crash_at:
                    raise
                state = graph.get_state(config).values
    if not isinstance(state, Mapping):
        raise TypeError("goal loop state must be a mapping")
    return _observed_run(root, state)


async def run_goal_loop(
    root: Path,
    scenario: GoalLoopScenario,
    *,
    crash_at: CrashPoint | None = None,
) -> GoalLoopRun:
    root.mkdir(parents=True, exist_ok=True)
    for name in (_EVENT_FILE, _CHECKPOINT_FILE, f"{_CHECKPOINT_FILE}-shm", f"{_CHECKPOINT_FILE}-wal"):
        path = root / name
        if path.exists():
            path.unlink()
    (root / _SCENARIO_FILE).write_text(
        json.dumps(_scenario_document(scenario, crash_at), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if crash_at is not None:
        return await asyncio.to_thread(
            _invoke_persisted,
            root,
            scenario,
            crash_at=crash_at,
            resume=False,
        )

    visits: list[str] = []
    dispatches: list[str] = []
    keys: dict[str, list[str]] = {}
    features = _scenario_features(root, scenario, visits, dispatches, keys)
    graphs = build_product_graphs(context=_build_context(), features=features)
    _plan, plan_ref = _scenario_plan(root, scenario)
    payload = _public_input(
        scenario.entrypoint,
        candidate_test_families=(scenario.candidate_families if scenario.entrypoint == "full" else ()),
        resolved_plan_ref=(plan_ref if scenario.entrypoint == "execute" else None),
        budgets={
            "review_rounds": 2,
            "coverage_rounds": scenario.coverage_rounds,
            "healing_rounds": scenario.healing_rounds,
            "execution_retries": 1,
        },
    )
    state = await asyncio.to_thread(invoke_product_root, graphs, scenario.entrypoint, payload)
    return _observed_run(root, state)


def _resume_in_process(root: Path) -> GoalLoopRun:
    scenario, crash_at = _load_scenario(root)
    return _invoke_persisted(root, scenario, crash_at=crash_at, resume=True)


async def resume_goal_loop(root: Path) -> GoalLoopRun:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.product.goal_loop_fixture",
        "--root",
        str(root),
        "--resume",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace"))
    state = json.loads(stdout.decode("utf-8"))
    return _observed_run(root, state)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "--root" or args[2] != "--resume":
        raise SystemExit("usage: python -m tests.product.goal_loop_fixture --root PATH --resume")
    run = _resume_in_process(Path(args[1]))
    print(json.dumps(run.state, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CrashPoint",
    "GoalLoopRun",
    "GoalLoopScenario",
    "main",
    "resume_goal_loop",
    "run_goal_loop",
]
