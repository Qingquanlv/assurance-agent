from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS as EXECUTION_JOBS
from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS as EXECUTION_TASKS
from assurance_execution.graphs.factory import build_execution_graphs
from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS as GENERATION_JOBS
from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS as GENERATION_TASKS
from assurance_generation.graphs.factory import build_generation_graphs
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS as HEALING_JOBS
from assurance_healing.graphs.factory import build_healing_graphs
from assurance_improvement.contracts.attempts import AGENT_JOB_CONTRACTS as IMPROVEMENT_JOBS
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS as IMPROVEMENT_TASKS
from assurance_improvement.graphs.factory import build_improvement_graphs
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS as INTAKE_JOBS
from assurance_intake.contracts.attempts import TASK_ATTEMPT_CONTRACTS as INTAKE_TASKS
from assurance_intake.graphs.factory import build_intake_graphs
from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS as QUALITY_JOBS
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS as QUALITY_TASKS
from assurance_quality.graphs.factory import build_quality_graphs
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.boot.graph_revision import FeatureFactoryRef
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.testing import GraphHarness, RecordingCapabilityBuildContext

PUBLIC_BUNDLE_FIELDS: dict[str, tuple[str, ...]] = {
    "assurance.intake": ("prepare", "load_plan", "case"),
    "assurance.generation": ("generation",),
    "assurance.execution": ("execute", "rerun"),
    "assurance.quality": ("assess", "issue_review", "issue_analyze", "issue_reconcile", "report"),
    "assurance.healing": ("repair_failure", "repair_coverage"),
    "assurance.improvement": (
        "archive",
        "retro",
        "review",
        "evaluate",
        "export",
        "apply",
        "rollback",
    ),
}

EXPECTED_BUNDLE_COUNTS = {
    "assurance.intake": 3,
    "assurance.generation": 1,
    "assurance.execution": 2,
    "assurance.quality": 5,
    "assurance.healing": 2,
    "assurance.improvement": 7,
}

IMPLEMENTED_BUNDLE_FIELDS: dict[str, tuple[str, ...]] = {
    "assurance.intake": ("prepare", "load_plan", "case"),
    "assurance.generation": (
        "generation",
        "api",
        "e2e",
        "fuzz",
        "performance",
        "init_runtime",
        "resolve_inputs",
    ),
    "assurance.execution": ("execute", "rerun"),
    "assurance.quality": (
        "assess",
        "issue_review",
        "issue_analyze",
        "issue_reconcile",
        "report",
        "fact_baseline",
        "surface_baseline",
    ),
    "assurance.healing": ("repair_failure", "repair_coverage"),
    "assurance.improvement": (
        "archive",
        "retro",
        "review",
        "evaluate",
        "export",
        "apply",
        "rollback",
    ),
}

_FACTORY_BUILDERS: dict[str, Callable[[RecordingCapabilityBuildContext], object]] = {
    "assurance.intake": build_intake_graphs,
    "assurance.generation": build_generation_graphs,
    "assurance.execution": build_execution_graphs,
    "assurance.quality": build_quality_graphs,
    "assurance.healing": build_healing_graphs,
    "assurance.improvement": build_improvement_graphs,
}

_FORBIDDEN_CONTEXT_ATTRS = (
    "checkpointer",
    "handler",
    "handlers",
    "inject_handler",
    "validator",
    "validators",
    "bind_validator",
    "runtime",
    "runtime_ports",
)

_TOPOLOGY_FORBIDDEN_NAMES = frozenset(
    {
        "environ",
        "getenv",
        "time",
        "monotonic",
        "random",
        "SystemRandom",
        "now",
        "utcnow",
        "today",
        "uuid4",
        "uuid1",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FEATURE_GRAPH_TREES = (
    ("assurance.intake", "packages/capabilities/assurance-intake/assurance_intake/graphs"),
    ("assurance.generation", "packages/capabilities/assurance-generation/assurance_generation/graphs"),
    ("assurance.execution", "packages/capabilities/assurance-execution/assurance_execution/graphs"),
    ("assurance.quality", "packages/capabilities/assurance-quality/assurance_quality/graphs"),
    ("assurance.healing", "packages/capabilities/assurance-healing/assurance_healing/graphs"),
    ("assurance.improvement", "packages/capabilities/assurance-improvement/assurance_improvement/graphs"),
)


def _job_contracts(jobs: Mapping[str, Any]) -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in jobs.values()}


def _contracts_for(owner_id: str) -> dict[str, TaskAttemptContract[Any, Any]]:
    if owner_id == "assurance.intake":
        return {
            **_job_contracts(INTAKE_JOBS),
            **{task.contract_id: task for task in INTAKE_TASKS.values()},
        }
    if owner_id == "assurance.generation":
        return {
            **_job_contracts(GENERATION_JOBS),
            **{task.contract_id: task for task in GENERATION_TASKS.values()},
        }
    if owner_id == "assurance.execution":
        return {
            **_job_contracts(EXECUTION_JOBS),
            **{task.contract_id: task for task in EXECUTION_TASKS.values()},
        }
    if owner_id == "assurance.quality":
        return {
            **_job_contracts(QUALITY_JOBS),
            **{task.contract_id: task for task in QUALITY_TASKS.values()},
        }
    if owner_id == "assurance.healing":
        return _job_contracts(HEALING_JOBS)
    contracts = _job_contracts(IMPROVEMENT_JOBS)
    for task in IMPROVEMENT_TASKS.values():
        contracts[task.contract_id] = task
    return contracts


def _spy_context(owner_id: str) -> RecordingCapabilityBuildContext:
    return GraphHarness().recording_context(owner_id=owner_id, contracts=_contracts_for(owner_id))


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


def _bundle_fields(bundle: object) -> tuple[str, ...]:
    return tuple(item.name for item in fields(cast(Any, bundle)))


def _bundle_projection(bundle: object, context: RecordingCapabilityBuildContext) -> dict[str, JSONValue]:
    field_names = _bundle_fields(bundle)
    return cast(
        dict[str, JSONValue],
        {
            "fields": list(field_names),
            "bound_contract_ids": list(context.bound_contract_ids),
            "checkpointers": list(context.compiled_subgraph_checkpointers),
            "nodes": {
                name: sorted(_node_names(getattr(bundle, name)))
                for name in field_names
                if isinstance(getattr(bundle, name), CompiledStateGraph)
            },
        },
    )


def _build_owner(owner_id: str) -> tuple[object, RecordingCapabilityBuildContext, str]:
    context = _spy_context(owner_id)
    bundle = _FACTORY_BUILDERS[owner_id](context)
    projection = _bundle_projection(bundle, context)
    return bundle, context, canonical_digest(projection)


def test_agent_contract_occurrence_inventory_is_exact() -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    agent_contract_ids = set(all_feature_agent_contracts())
    occurrences = tuple(
        contract_id
        for owner_id in PUBLIC_BUNDLE_FIELDS
        for contract_id in _build_owner(owner_id)[1].bound_contract_ids
        if contract_id in agent_contract_ids
    )

    duplicated_contract_ids = {
        "assurance.intake.agent.case-design.v1",
    }
    expected = Counter(
        {
            contract_id: 2 if contract_id in duplicated_contract_ids else 1
            for contract_id in agent_contract_ids
        }
    )

    assert Counter(occurrences) == expected
    assert expected.total() == 27


def test_all_attempt_occurrences_are_exact() -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts

    agents = all_feature_agent_contracts()
    tasks = all_feature_task_contracts()
    ids = set(agents) | {item.contract_id for item in tasks.values()}
    expected = Counter({contract_id: 1 for contract_id in ids})
    expected["assurance.intake.agent.case-design.v1"] = 2
    expected["assurance.generation.resolve-inputs"] = 2
    expected["assurance.improvement.task.evaluate-memory-improvement"] = 2
    actual = Counter(
        contract_id
        for owner in _FACTORY_BUILDERS
        for contract_id in _build_owner(owner)[1].bound_contract_ids
    )
    assert len(ids) == 45
    assert expected.total() == 48
    assert actual == expected
    assert sum(len(names) for names in IMPLEMENTED_BUNDLE_FIELDS.values()) == 28


def test_all_graph_modules_delegate_attempt_registration_to_helper(monkeypatch) -> None:
    from inspect import isfunction

    from graph_engine.stategraph import add_attempt_node

    calls: list[tuple[str, str]] = []

    def record(builder, context, node_id, *, contract_id, activation, select, publish) -> None:
        assert node_id
        assert activation is not None
        assert callable(select)
        assert callable(publish)
        calls.append((node_id, contract_id))
        add_attempt_node(
            builder,
            context,
            node_id,
            contract_id=contract_id,
            activation=activation,
            select=select,
            publish=publish,
        )

    visited: set[int] = set()

    def patch_graph_function(function: object) -> None:
        if not isfunction(function) or id(function.__globals__) in visited:
            return
        namespace = function.__globals__
        visited.add(id(namespace))
        if namespace.get("add_attempt_node") is add_attempt_node:
            monkeypatch.setitem(namespace, "add_attempt_node", record)
        for value in tuple(namespace.values()):
            if isfunction(value) and ".graphs." in value.__module__:
                patch_graph_function(value)

    for factory in _FACTORY_BUILDERS.values():
        patch_graph_function(factory)
    contexts = [_build_owner(owner)[1] for owner in _FACTORY_BUILDERS]
    assert len(visited) >= 12
    assert len(calls) == 48
    assert Counter(contract_id for _, contract_id in calls) == Counter(
        contract_id for context in contexts for contract_id in context.bound_contract_ids
    )


def test_product_allowlist_pairs_match_the_six_factory_builders() -> None:
    assert tuple((item.owner_id, item.symbol) for item in FEATURE_GRAPH_FACTORIES) == (
        ("assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"),
        ("assurance.generation", "assurance_generation.graphs.factory:build_generation_graphs"),
        ("assurance.execution", "assurance_execution.graphs.factory:build_execution_graphs"),
        ("assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"),
        ("assurance.healing", "assurance_healing.graphs.factory:build_healing_graphs"),
        ("assurance.improvement", "assurance_improvement.graphs.factory:build_improvement_graphs"),
    )
    assert all(isinstance(item, FeatureFactoryRef) for item in FEATURE_GRAPH_FACTORIES)
    assert set(_FACTORY_BUILDERS) == {item.owner_id for item in FEATURE_GRAPH_FACTORIES}


def test_bundle_inventory_is_exact() -> None:
    assert {owner: len(names) for owner, names in PUBLIC_BUNDLE_FIELDS.items()} == EXPECTED_BUNDLE_COUNTS
    for owner_id, public_fields in PUBLIC_BUNDLE_FIELDS.items():
        bundle, _context, _digest = _build_owner(owner_id)
        implemented = _bundle_fields(bundle)
        assert implemented == IMPLEMENTED_BUNDLE_FIELDS[owner_id]
        assert implemented[: len(public_fields)] == public_fields
        for name in public_fields:
            assert isinstance(getattr(bundle, name), CompiledStateGraph)
        assert not hasattr(bundle, "nodes")


def test_factories_build_twice_with_identical_public_digests(monkeypatch, tmp_path: Path) -> None:
    first = {owner_id: _build_owner(owner_id) for owner_id in PUBLIC_BUNDLE_FIELDS}
    monkeypatch.setenv("AA_FEATURE_GRAPH_FACTORIES", "assurance.rogue:rogue.graphs:build")
    monkeypatch.setenv("LANGGRAPH_DEBUG", "1")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sut_graph.py").write_text("def build():\n    raise RuntimeError('sut')\n", encoding="utf-8")
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text("policy: ambient\n", encoding="utf-8")
    second = {owner_id: _build_owner(owner_id) for owner_id in PUBLIC_BUNDLE_FIELDS}
    for owner_id in PUBLIC_BUNDLE_FIELDS:
        first_bundle, first_context, first_digest = first[owner_id]
        second_bundle, second_context, second_digest = second[owner_id]
        assert first_digest == second_digest
        assert _bundle_projection(first_bundle, first_context) == _bundle_projection(
            second_bundle, second_context
        )
        assert first_context.bound_contract_ids == second_context.bound_contract_ids


def test_bound_contract_ids_are_owner_scoped() -> None:
    for owner_id in PUBLIC_BUNDLE_FIELDS:
        _bundle, context, _digest = _build_owner(owner_id)
        prefix = f"{owner_id}."
        assert context.bound_contract_ids
        assert all(item.startswith(prefix) for item in context.bound_contract_ids)


def test_spy_contexts_have_no_handler_validator_or_runtime_injection() -> None:
    for owner_id in PUBLIC_BUNDLE_FIELDS:
        context = _spy_context(owner_id)
        for name in _FORBIDDEN_CONTEXT_ATTRS:
            assert not hasattr(context, name)
        _FACTORY_BUILDERS[owner_id](context)
        for name in _FORBIDDEN_CONTEXT_ATTRS:
            assert not hasattr(context, name)


def test_recording_context_records_the_compile_checkpointer(monkeypatch) -> None:
    class Marker(TypedDict, total=False):
        value: str

    def noop(state: Marker) -> Marker:
        return state

    saver = InMemorySaver()
    original = StateGraph.compile

    def forced(graph: StateGraph[Any], checkpointer: object = None, **kwargs: Any) -> object:
        return original(graph, checkpointer=saver, **kwargs)

    monkeypatch.setattr(StateGraph, "compile", forced)
    context = RecordingCapabilityBuildContext(owner_id="assurance.intake", contracts={})
    builder = StateGraph(Marker)
    builder.add_node("noop", noop)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    compiled = context.compile_subgraph(builder)
    assert compiled.checkpointer is saver
    assert context.compiled_subgraph_checkpointers == (saver,)


def test_every_child_checkpointer_is_none() -> None:
    for owner_id in PUBLIC_BUNDLE_FIELDS:
        bundle, context, _digest = _build_owner(owner_id)
        graphs = [
            getattr(bundle, name)
            for name in _bundle_fields(bundle)
            if isinstance(getattr(bundle, name), CompiledStateGraph)
        ]
        assert graphs
        assert all(graph.checkpointer is None for graph in graphs)
        assert context.compiled_subgraph_checkpointers
        assert all(item is None for item in context.compiled_subgraph_checkpointers)


def test_graph_python_has_no_env_time_random_or_sut_topology() -> None:
    violations: list[str] = []
    for owner_id, relative in _FEATURE_GRAPH_TREES:
        del owner_id
        root = _REPO_ROOT / relative
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in _TOPOLOGY_FORBIDDEN_NAMES:
                    violations.append(f"{path}:{node.lineno}:{node.id}")
                if isinstance(node, ast.Attribute) and node.attr in _TOPOLOGY_FORBIDDEN_NAMES:
                    violations.append(f"{path}:{node.lineno}:{node.attr}")
    assert violations == []
