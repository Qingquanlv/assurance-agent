from __future__ import annotations

from collections.abc import Mapping
from itertools import combinations
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast
import uuid

import pytest

from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import freeze_json, thaw_json
from tests.product.product_runner import project_task_input
from tests.product.product_runner import Engine
from graph_engine.attempts.activity import InvocationProjection
from tests.product.product_runner import empty_invocation_seed

from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families
from assurance_product.product import prepare_change_workspace

from tests.product.product_runner import (
    FAMILY_TERMINALS,
    _product_input,
    modular_product_composition,
)


def _scripted_host():
    from tests.product.product_runner import _ScriptedTaskHost, _scripted_authorization

    del _scripted_authorization
    return _ScriptedTaskHost(
        execution_sequence=(),
        coverage_sequence=(),
        threshold=0.90,
        coverage_rounds=1,
        review_decision="pass",
        healing_decision="allowed",
    )


def _authorization():
    from tests.product.product_runner import _scripted_authorization

    return _scripted_authorization()


def _seed_input(selected: tuple[str, ...], *, validate_product: bool = True) -> dict[str, object]:
    from assurance_product.models import ProductInputV1

    families = selected if selected else ("api",)
    payload = _product_input(selected_test_families=families)
    dumped = (
        ProductInputV1.model_validate(payload).model_dump(mode="json")
        if validate_product
        else json.loads(json.dumps(payload))
    )
    dumped["selected_test_families"] = list(selected)
    dumped["artifacts"] = [{"path": "qa/changes", "digest": "a" * 64}]
    dumped["decision"] = "pass"
    return dumped


def _dispatched_families(projection: InvocationProjection) -> set[str]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    dispatched: set[str] = set()
    for activation in projection.activations:
        if activation.status != "completed":
            continue
        graph = graphs[activation.graph_instance_id]
        if graph.graph_id.endswith(".generation") or graph.graph_id == "generation":
            if activation.node_id in GENERATION_FAMILIES:
                dispatched.add(activation.node_id)
    return dispatched


def _join_output(
    projection: InvocationProjection,
    composition,
    root_input: Mapping[str, object],
    *,
    completion_order: Literal["forward", "reverse"],
) -> object:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    join = next(
        activation
        for activation in projection.activations
        if activation.status == "completed"
        and activation.node_id == "join-selected"
        and (
            graphs[activation.graph_instance_id].graph_id == "generation"
            or graphs[activation.graph_instance_id].graph_id.endswith(".generation")
        )
    )
    compiled = None
    for graph_id, graph in composition.workflow.graphs.items():
        if graph_id == "generation" or graph_id.endswith(".generation"):
            if "join-selected" in graph.nodes:
                compiled = graph.nodes["join-selected"]
                break
    assert compiled is not None
    tokens = {item.token_id: item for item in projection.offered_tokens}
    raw: dict[str, object] = {}
    for token_id in join.token_ids:
        token = tokens[token_id]
        if token.source is not None:
            raw[token.source] = thaw_json(token.payload)
    order = FAMILY_TERMINALS if completion_order == "forward" else tuple(reversed(FAMILY_TERMINALS))
    missing = [source for source in order if source not in raw]
    extra = sorted(set(raw) - set(order))
    assert not missing and not extra, f"join predecessors missing={missing} extra={extra}"
    predecessor_tokens = {source: raw[source] for source in order}
    projection_def = compiled.definition.input_projection
    if projection_def is None:
        return freeze_json(join.output)
    return freeze_json(
        project_task_input(
            projection_def,
            root_input=root_input,
            graph_input=root_input,
            node_config=dict(compiled.definition.input),
            predecessor_tokens=predecessor_tokens,
        )
    )


def _run_execute(
    selected: tuple[str, ...],
    installed_sources,
    *,
    completion_order: Literal["forward", "reverse"] = "forward",
):
    from tests.product.product_runner import ProductRun

    composition = modular_product_composition(installed_sources)
    seed_input = _seed_input(selected)
    with TemporaryDirectory(prefix="generation-product-") as tmp:
        result = ProductRun(
            entrypoint="execute",
            selected_test_families=selected,
            review_decision="pass",
            healing_decision="allowed",
            completion_order=completion_order,
            engine_root=Path(tmp),
            composition=composition,
        ).run_to_terminal()
        if result.status != "completed":
            raise AssertionError(f"execute failed: {result.status} {result.stop_reason}")
        return (
            "succeeded",
            _dispatched_families(result.projection),
            _join_output(
                result.projection,
                composition,
                seed_input,
                completion_order=completion_order,
            ),
        )


def _start_execute_raw(selected: tuple[str, ...], installed_sources) -> None:
    composition = modular_product_composition(installed_sources)
    seed_input = _seed_input(selected, validate_product=False)
    with TemporaryDirectory(prefix="generation-invalid-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        workspace = prepare_change_workspace(project, "CH-DEMO-001")
        engine = Engine(workspace.paths.runtime_root, host=_scripted_host())
        try:
            handle = engine.start(
                composition,
                entrypoint="execute",
                invocation_id=f"generation-invalid-{uuid.uuid4().hex}",
                seed=empty_invocation_seed(root_input=cast(JSONValue, seed_input)),
                authorization=_authorization(),
                workspace_binding=workspace.runtime_binding(),
            )
            result = engine.run_until_blocked(handle)
            if result.status != "failed":
                raise AssertionError(f"invalid families reached {result.status}")
            raise RuntimeError(result.reason or "failed")
        finally:
            engine.close()


@pytest.mark.parametrize("family", list(GENERATION_FAMILIES))
def test_single_family_runs_only_its_generation_branch(family: str, installed_sources) -> None:
    status, dispatched, join_output = _run_execute((family,), installed_sources)
    assert status == "succeeded"
    assert dispatched == {family}
    assert set(join_output["selected_families"]) == {family}  # type: ignore[index]


@pytest.mark.parametrize(
    "families",
    [combo for size in range(1, 5) for combo in combinations(GENERATION_FAMILIES, size)],
)
def test_selected_subset_runs_exactly_those_families(families: tuple[str, ...], installed_sources) -> None:
    status, dispatched, join_output = _run_execute(families, installed_sources)
    assert status == "succeeded"
    assert dispatched == set(families)
    assert set(join_output["selected_families"]) == set(families)  # type: ignore[index]


@pytest.mark.parametrize("selected", [(), ("api", "api"), ("api", "mobile")])
def test_invalid_family_selection_fails_at_feature_input(
    selected: tuple[str, ...], installed_sources
) -> None:
    with pytest.raises(ValueError):
        validate_selected_families(selected)
    if selected == ("api", "api"):
        return
    with pytest.raises(Exception):
        _start_execute_raw(selected, installed_sources)
