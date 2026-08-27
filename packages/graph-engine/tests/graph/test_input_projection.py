from __future__ import annotations

import unicodedata

import pytest
from pydantic import ValidationError

from graph_engine.graph.compiler import CompileError, compile_workflow
from graph_engine.graph.input_projection import (
    AllPredecessorTokensProjection,
    ConfigPointerProjection,
    InputProjectionError,
    LiteralProjection,
    ObjectProjection,
    PredecessorPointerProjection,
    PredecessorValueProjection,
    RootPointerProjection,
    TupleProjection,
    project_task_input,
    validate_input_projection_compile,
)
from graph_engine.graph.schema import NodeDef, parse_workflow


class _PlaceholderHandler:
    async def execute(self, _request: object, _context: object) -> object:
        raise AssertionError("input projection tests never execute task handlers")


def _registry():
    from pathlib import Path

    from graph_engine.composition import (
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
    from graph_engine.composition.models import (
        AuthenticatedContribution,
        ContributionAuthority,
        ExecutableAuthority,
    )
    from graph_engine.composition.provenance import StandardLoader
    from graph_engine.composition.registries import _build_registries
    from graph_engine.plugin_api import PluginContribution, PluginDescriptor

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


def test_object_projection_combines_only_declared_sources():
    projection = ObjectProjection(
        fields={
            "change_id": RootPointerProjection(pointer="/change_id"),
            "policy": ConfigPointerProjection(pointer="/policy"),
            "inputs": AllPredecessorTokensProjection(),
        }
    )
    assert project_task_input(
        projection,
        root_input={"change_id": "CH-1", "ignored": "x"},
        node_config={"policy": "strict"},
        predecessor_tokens={"left": {"a": 1}, "right": {"b": 2}},
    ) == {"change_id": "CH-1", "policy": "strict", "inputs": ({"a": 1}, {"b": 2})}


def test_projection_rejects_unknown_operator():
    with pytest.raises(ValidationError):
        NodeDef.model_validate(
            {"id": "n", "kind": "task", "input_projection": {"type": "python", "callable": "x:y"}}
        )


def test_literal_projection_returns_exact_value():
    projection = LiteralProjection(value={"mode": "strict"})
    assert project_task_input(
        projection,
        root_input={"ignored": True},
        node_config={"ignored": True},
        predecessor_tokens={},
    ) == {"mode": "strict"}


def test_root_pointer_projection_reads_root_input():
    projection = RootPointerProjection(pointer="/change_id")
    assert (
        project_task_input(
            projection,
            root_input={"change_id": "CH-9"},
            node_config={},
            predecessor_tokens={},
        )
        == "CH-9"
    )


def test_config_pointer_projection_reads_node_config():
    projection = ConfigPointerProjection(pointer="/policy/mode")
    assert (
        project_task_input(
            projection,
            root_input={},
            node_config={"policy": {"mode": "strict"}},
            predecessor_tokens={},
        )
        == "strict"
    )


def test_predecessor_pointer_projection_reads_named_token():
    projection = PredecessorPointerProjection(predecessor="left", pointer="/value")
    assert (
        project_task_input(
            projection,
            root_input={},
            node_config={},
            predecessor_tokens={"left": {"value": 42}},
        )
        == 42
    )


def test_predecessor_value_projection_returns_whole_token():
    projection = PredecessorValueProjection(predecessor="right")
    assert project_task_input(
        projection,
        root_input={},
        node_config={},
        predecessor_tokens={"right": {"ok": True}},
    ) == {"ok": True}


def test_tuple_projection_builds_ordered_values():
    projection = TupleProjection(
        items=(
            RootPointerProjection(pointer="/id"),
            ConfigPointerProjection(pointer="/mode"),
        )
    )
    assert project_task_input(
        projection,
        root_input={"id": "CH-1"},
        node_config={"mode": "x"},
        predecessor_tokens={},
    ) == ("CH-1", "x")


def test_all_predecessor_tokens_are_sorted_by_predecessor_id():
    projection = AllPredecessorTokensProjection()
    assert project_task_input(
        projection,
        root_input={},
        node_config={},
        predecessor_tokens={"right": "R", "left": "L"},
    ) == ("L", "R")


def test_projection_rejects_missing_pointer():
    projection = RootPointerProjection(pointer="/missing")
    with pytest.raises(InputProjectionError, match="missing pointer"):
        project_task_input(projection, root_input={"present": 1}, node_config={}, predecessor_tokens={})


def test_projection_rejects_non_canonical_pointer():
    with pytest.raises(ValidationError):
        RootPointerProjection(pointer="change_id")


def test_projection_rejects_missing_predecessor_token():
    projection = PredecessorValueProjection(predecessor="left")
    with pytest.raises(InputProjectionError, match="missing predecessor"):
        project_task_input(projection, root_input={}, node_config={}, predecessor_tokens={})


def test_projection_rejects_non_json_literal():
    with pytest.raises(ValidationError):
        LiteralProjection(value=float("nan"))


def test_projection_rejects_extra_fields_on_operator():
    with pytest.raises(ValidationError):
        LiteralProjection.model_validate({"type": "literal", "value": 1, "extra": True})


def test_object_projection_rejects_duplicate_normalized_field_names():
    key_a = "caf" + "e\u0301"
    key_b = "caf\u00e9"
    assert unicodedata.normalize("NFC", key_a) == unicodedata.normalize("NFC", key_b)
    assert key_a != key_b
    with pytest.raises(ValidationError, match="duplicate object projection field"):
        ObjectProjection.model_validate(
            {
                "type": "object",
                "fields": {key_a: {"type": "literal", "value": 1}, key_b: {"type": "literal", "value": 2}},
            }
        )


def test_compile_rejects_non_direct_predecessor():
    projection = ObjectProjection(
        fields={
            "value": PredecessorValueProjection(predecessor="missing"),
        }
    )
    with pytest.raises(CompileError, match="non-direct predecessor"):
        validate_input_projection_compile(
            projection,
            node_kind="task",
            join_kind=None,
            direct_predecessors=frozenset({"left"}),
            location="root/work",
        )


def test_compile_rejects_all_predecessor_tokens_on_non_all_join():
    projection = AllPredecessorTokensProjection()
    with pytest.raises(CompileError, match="all_predecessor_tokens"):
        validate_input_projection_compile(
            projection,
            node_kind="task",
            join_kind=None,
            direct_predecessors=frozenset({"left", "right"}),
            location="root/work",
        )


def test_compile_rejects_all_predecessor_tokens_on_any_join():
    projection = AllPredecessorTokensProjection()
    with pytest.raises(CompileError, match="all_predecessor_tokens"):
        validate_input_projection_compile(
            projection,
            node_kind="join",
            join_kind="any",
            direct_predecessors=frozenset({"left", "right"}),
            location="root/joined",
        )


def test_compile_rejects_projection_cycles():
    shared: dict[str, object] = {"type": "object", "fields": {}}
    shared["fields"] = {"loop": shared}
    with pytest.raises(CompileError, match="cycle"):
        validate_input_projection_compile(
            shared,  # type: ignore[arg-type]
            node_kind="task",
            join_kind=None,
            direct_predecessors=frozenset(),
            location="root/work",
        )


def test_compiler_rejects_input_projection_on_end_node():
    text = """
name: projection-test
entrypoints: {main: root}
retry: {policy: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 1}}
graphs:
  root:
    max_activations: 1
    start: done
    nodes:
      done: {kind: end, input_projection: {type: literal, value: 1}}
    edges: []
"""
    with pytest.raises(ValidationError, match="does not accept input_projection"):
        parse_workflow(text)


def test_compiler_validates_predecessor_names_at_compile_time():
    text = """
name: projection-test
entrypoints: {main: root}
retry: {policy: {max_attempts: 1, retry_on: []}}
timeout: {short: {run_seconds: 1}}
graphs:
  root:
    max_activations: 3
    start: left
    nodes:
      left: {kind: gate, expression: 'true', input_projection: {type: literal, value: 1}}
      right: {kind: gate, expression: 'true'}
      joined:
        kind: join
        join: all
        input_projection:
          type: predecessor
          predecessor: missing
      done: {kind: end}
    edges:
      - {from: left, to: right, condition: 'false'}
      - {from: left, to: joined, condition: 'false'}
      - {from: right, to: joined}
      - {from: joined, to: done}
"""
    with pytest.raises(CompileError, match="non-direct predecessor"):
        compile_workflow(parse_workflow(text), _registry())
