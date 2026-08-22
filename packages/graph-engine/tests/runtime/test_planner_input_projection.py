from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap_fixtures import synthetic_invocation_started

from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    CapabilityRegistry,
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceSnapshot,
)
from graph_engine.composition.models import AuthenticatedContribution, ContributionAuthority, ExecutableAuthority
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import PluginContribution, PluginDescriptor
from graph_engine.runtime.events import GraphStarted, InvocationStarted, TokenOffered
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.planner import PlanningError, activation_id, plan_next


class _PlaceholderHandler:
    async def execute(self, _request: object, _context: object) -> object:
        raise AssertionError("planner projection tests never execute task handlers")


def _registry() -> CapabilityRegistry:
    source = SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=Path("/sources/test.tasks"),
            distribution="test-tasks",
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="test.tasks",
            entrypoint_value="test_tasks:provider",
            declaration_path="test_tasks/plugin-declaration.json",
            import_roots=("",),
            plugin_id="test.tasks",
            plugin_version="1.0.0",
        ),
        (),
    )
    contribution = PluginContribution(task_handlers={"test.tasks.run": _PlaceholderHandler()})
    source_key = SourceKey(SourceRole.PLUGIN, "test.tasks")
    provenance = ExecutableProvenance.create(
        kind=ExecutableKind.TASK_HANDLER,
        registry_id="test.tasks.run",
        owner_id="test.tasks",
        source_key=source_key,
        source_digest=source.digest,
        module=ExecutableModuleProvenance(
            module_name="test_tasks.implementation",
            standard_loader=StandardLoader.SOURCE,
            standard_is_package=False,
            relative_origin="implementation.py",
            authenticated_locations=(),
            physical_sha256="0" * 64,
            source_digest=source.digest,
        ),
        callable_path="test_tasks.implementation:Handler.execute",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="test.tasks",
        plugin_version="1.0.0",
        engine_api="1.0.0",
        task_handlers=("test.tasks.run",),
        commit_validators=(),
    )
    handler = contribution.task_handlers["test.tasks.run"]
    authority_set = ContributionAuthority(
        provider_binding=object(),
        descriptor=descriptor,
        owner_id="test.tasks",
        source_key=source_key,
        source_digest=source.digest,
        contribution=contribution,
        authorities=(
            ExecutableAuthority(
                executable=handler,
                function=type(handler).__dict__["execute"],
                bound_self=handler,
                descriptor=type(handler).__dict__["execute"],
                provenance=provenance,
            ),
        ),
    )
    authenticated = AuthenticatedContribution(
        owner_id="test.tasks",
        source_key=source_key,
        source_digest=source.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=(provenance,),
        authority=authority_set,
    )
    return _build_registries((source,), (authenticated,), ("test.tasks",)).capabilities


def _compiled(workflow_text: str) -> CompiledWorkflow:
    return compile_workflow(parse_workflow(workflow_text), _registry())


def _projection(*events: object) -> InvocationProjection:
    from graph_engine.runtime.events import EventEnvelope

    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    return fold_events(envelopes)


def _invocation() -> InvocationStarted:
    return synthetic_invocation_started()


def _root(*, input_payload: object = None) -> GraphStarted:
    return GraphStarted(graph_instance_id="root", graph_id="root", input=input_payload)


def _canonical_start_token(compiled: CompiledWorkflow, *, payload: object = None) -> TokenOffered:
    graph = compiled.graphs["root"]
    return TokenOffered(
        token_id=canonical_digest(
            {
                "graph_instance_id": "root",
                "kind": "graph_start",
                "target": graph.start,
            }
        ),
        graph_instance_id="root",
        source=None,
        target=graph.start,
        payload=payload,
    )


def _bootstrapped_projection(compiled: CompiledWorkflow, *, root_input: object = None) -> InvocationProjection:
    return _projection(
        _invocation(),
        _root(input_payload=root_input),
        _canonical_start_token(compiled, payload=root_input),
    )


def test_planner_uses_projected_task_input_when_declared() -> None:
    compiled = _compiled(
        """
name: planner-projection
entrypoints: {main: root}
retry: {policy: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 5}}
graphs:
  root:
    max_activations: 2
    start: work
    nodes:
      work:
        kind: task
        capability: test.tasks.run
        retry: policy
        timeout: short
        input: {policy: strict}
        input_projection:
          type: object
          fields:
            change_id: {type: root_pointer, pointer: /change_id}
            policy: {type: config_pointer, pointer: /policy}
      done: {kind: end}
    edges:
      - {from: work, to: done}
"""
    )
    root_input = {"change_id": "CH-1", "ignored": "x"}
    projection = _bootstrapped_projection(compiled, root_input=root_input)

    plan = plan_next(compiled, projection)

    assert [task.node_id for task in plan.tasks] == ["work"]
    assert plan.tasks[0].model_dump(mode="json")["input"] == {
        "change_id": "CH-1",
        "policy": "strict",
    }


def test_planner_preserves_legacy_config_tokens_without_projection() -> None:
    compiled = _compiled(
        """
name: planner-projection
entrypoints: {main: root}
retry: {policy: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 5}}
graphs:
  root:
    max_activations: 2
    start: work
    nodes:
      work:
        kind: task
        capability: test.tasks.run
        retry: policy
        timeout: short
        input: {mode: strict}
      done: {kind: end}
    edges:
      - {from: work, to: done}
"""
    )
    projection = _projection(_invocation())

    plan = plan_next(compiled, projection)

    assert plan.tasks[0].model_dump(mode="json")["input"] == {
        "config": {"mode": "strict"},
        "tokens": [None],
    }


def test_planner_projection_error_fails_before_dispatch() -> None:
    compiled = _compiled(
        """
name: planner-projection
entrypoints: {main: root}
retry: {policy: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 5}}
graphs:
  root:
    max_activations: 2
    start: work
    nodes:
      work:
        kind: task
        capability: test.tasks.run
        retry: policy
        timeout: short
        input_projection: {type: root_pointer, pointer: /missing}
      done: {kind: end}
    edges:
      - {from: work, to: done}
"""
    )
    projection = _bootstrapped_projection(compiled, root_input={"present": True})

    with pytest.raises(PlanningError, match="invalid_input"):
        plan_next(compiled, projection)


def test_planner_gate_scope_uses_projected_input() -> None:
    compiled = _compiled(
        """
name: planner-projection
entrypoints: {main: root}
retry: {policy: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 5}}
graphs:
  root:
    max_activations: 2
    start: gate
    nodes:
      gate:
        kind: gate
        expression: 'value'
        input_projection:
          type: object
          fields:
            value: {type: root_pointer, pointer: /enabled}
      done: {kind: end}
    edges:
      - {from: gate, to: done, condition: 'true'}
"""
    )
    projection = _bootstrapped_projection(compiled, root_input={"enabled": True})

    plan = plan_next(compiled, projection)
    completed = next(event for event in plan.events if event.kind == "node_completed")

    assert completed.model_dump(mode="json")["output"] == {"value": True}
