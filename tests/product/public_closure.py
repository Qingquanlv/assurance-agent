from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_engine.frozen_json import freeze_json, thaw_json
from graph_engine.graph.output_projection import project_subgraph_output
from graph_engine.runtime.models import InvocationProjection

from tests.product.product_runner import ProductRun, _product_alias

PUBLIC_CLOSURE_GOLDEN = Path(__file__).resolve().parent / "goldens" / "public-closure.json"

PUBLIC_ENTRYPOINTS = (
    "intake",
    "case",
    "full",
    "execute",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)

STANDALONE_PASS_SCENARIOS: tuple[dict[str, Any], ...] = tuple(
    {"entrypoint": name} for name in PUBLIC_ENTRYPOINTS if name not in {"full", "execute"}
)

PUBLIC_CLOSURE_SCENARIOS: tuple[dict[str, Any], ...] = (
    *STANDALONE_PASS_SCENARIOS,
    {"entrypoint": "intake", "review_decision": "needs_human_review"},
)


def requires_terminal_output(scenario: dict[str, Any]) -> bool:
    return str(scenario["entrypoint"]) in {"full", "execute"}


def run_public_scenario(composition, engine_root: Path, scenario: dict[str, Any]):
    run = ProductRun(
        entrypoint=str(scenario["entrypoint"]),
        selected_test_families=("api",) if scenario["entrypoint"] in {"full", "execute"} else (),
        review_decision=str(scenario.get("review_decision", "pass")),
        healing_decision=str(scenario.get("healing_decision", "allowed")),
        execution_sequence=tuple(scenario.get("execution_sequence", ())),
        coverage_sequence=tuple(scenario.get("coverage_sequence", ())),
        threshold=float(scenario.get("threshold", 0.90)),
        coverage_rounds=scenario.get("coverage_rounds"),
        engine_root=engine_root,
        composition=composition,
    )
    return run.run_to_terminal()


def declared_public_projection(composition, entrypoint: str):
    graph_id = composition.workflow.entrypoints[entrypoint]
    graph = composition.workflow.graphs[graph_id]
    ends = {node_id for node_id, node in graph.nodes.items() if node.definition.kind == "end"}
    for edge in graph.edges:
        if edge.to not in ends:
            continue
        predecessor = graph.nodes[edge.from_]
        if predecessor.definition.kind == "subgraph" and predecessor.definition.output_projection is not None:
            return predecessor.definition.output_projection
    return None


def apply_public_projection(raw: object, public_projection) -> object:
    if public_projection is None or raw is None:
        return raw
    return thaw_json(project_subgraph_output(public_projection, child_output=freeze_json(raw)))


def public_closure_trace(
    composition,
    engine_root: Path,
    scenario: dict[str, Any],
    *,
    public_projection=None,
) -> dict[str, Any]:
    engine_root.mkdir(parents=True, exist_ok=True)
    result = run_public_scenario(composition, engine_root, scenario)
    projection = result.projection
    raw_terminal = thaw_json(
        next(item.output for item in projection.graph_instances if item.parent_graph_instance_id is None)
    )
    return {
        "dispatches": list(_task_dispatches(projection, composition)),
        "interrupts": [[reason, list(actions)] for reason, actions in _interrupts(projection)],
        "effects": [list(item) for item in _effects(projection)],
        "terminal": [result.status, result.stop_reason],
        "terminal_output": apply_public_projection(raw_terminal, public_projection),
    }


def record_public_closure_traces(composition, engine_root: Path) -> dict[str, Any]:
    recorded: list[dict[str, Any]] = []
    for scenario in PUBLIC_CLOSURE_SCENARIOS:
        projection = (
            declared_public_projection(composition, str(scenario["entrypoint"]))
            if requires_terminal_output(scenario)
            else None
        )
        recorded.append(
            {
                "scenario": jsonable_scenario(scenario),
                "require_terminal_output": requires_terminal_output(scenario),
                "trace": public_closure_trace(
                    composition,
                    engine_root / _scenario_key(scenario),
                    scenario,
                    public_projection=projection,
                ),
            }
        )
    return {"schema_version": "1", "scenarios": recorded}


def jsonable_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"entrypoint": scenario["entrypoint"]}
    for key in ("review_decision", "healing_decision", "threshold", "coverage_rounds"):
        if key in scenario:
            payload[key] = scenario[key]
    for key in ("execution_sequence", "coverage_sequence"):
        if key in scenario:
            payload[key] = list(scenario[key])
    return payload


def _scenario_key(scenario: dict[str, Any]) -> str:
    parts = [str(scenario["entrypoint"])]
    for key in (
        "review_decision",
        "healing_decision",
        "execution_sequence",
        "coverage_sequence",
        "threshold",
        "coverage_rounds",
    ):
        if key in scenario:
            parts.append(f"{key}={scenario[key]}")
    return "__".join(parts).replace(" ", "")


def _task_dispatches(projection: InvocationProjection, composition) -> tuple[str, ...]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    dispatches: list[str] = []
    for activation in projection.activations:
        graph = graphs[activation.graph_instance_id]
        node = composition.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "task" or node.definition.capability is None:
            continue
        if not activation.attempts:
            continue
        dispatches.append(_product_alias(node.definition.capability))
    return tuple(dispatches)


def _interrupts(projection: InvocationProjection) -> tuple[tuple[str, tuple[str, ...]], ...]:
    seen: list[tuple[str, tuple[str, ...]]] = []
    for activation in projection.activations:
        if activation.interrupt_reason is None:
            continue
        seen.append((activation.interrupt_reason, tuple(activation.interrupt_actions)))
    if projection.pending_interrupt is not None:
        pending = (
            projection.pending_interrupt.reason,
            tuple(projection.pending_interrupt.actions),
        )
        if pending not in seen:
            seen.append(pending)
    return tuple(seen)


def _effects(projection: InvocationProjection) -> tuple[tuple[str, str], ...]:
    return tuple((item.kind, item.status) for item in projection.effects)
