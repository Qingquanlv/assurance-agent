"""Pure preview of the next selected wave, including existing child invocations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.compiler import canonical_digest
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.models import (
    ArtifactReader,
    CompiledWorkflow,
    ExecutableTask,
    GraphProjection,
    PlanResult,
    RuntimeContext,
)
from assurance_agent.workflow.graph.planner import PlanError, plan_superstep
from assurance_agent.workflow.graph.scheduler import select_wave


class SelectedWaveDriftError(AaError):
    """Reserved selected wave no longer matches replanned task identity or resources."""


@dataclass(frozen=True)
class SelectedInvocationWave:
    invocation_id: str
    projection: GraphProjection
    plan: PlanResult
    selected_tasks: tuple[ExecutableTask, ...]
    child_waves: tuple["SelectedInvocationWave", ...]
    lock_tokens: tuple[str, ...]
    synchronized_paths: tuple[ResourcePath, ...]
    identity_digest: str


@dataclass(frozen=True)
class PreparedInvocationWave:
    preview: SelectedInvocationWave
    prepared_tree_id: str
    child_prepared: tuple["PreparedInvocationWave", ...]


@dataclass(frozen=True)
class PreparedWaveLease:
    lock_tokens: tuple[str, ...]
    synchronized_paths: tuple[ResourcePath, ...]
    tree_overlays: tuple[tuple[str, str], ...]
    invocations: tuple[PreparedInvocationWave, ...]
    capture_sealed: bool

    def for_invocation(self, invocation_id: str) -> PreparedInvocationWave | None:
        for prepared in self.invocations:
            found = _find_prepared(prepared, invocation_id)
            if found is not None:
                return found
        return None


def derive_child_invocation_id(parent_task: ExecutableTask, graph_id: str) -> str:
    """Deterministic child invocation ID from parent task identity and subgraph name."""
    return canonical_digest({"parent_task_id": parent_task.task_id, "graph_id": graph_id})


def iter_selected_waves(wave: SelectedInvocationWave) -> Iterator[SelectedInvocationWave]:
    """Depth-first traversal of a previewed invocation tree."""
    yield wave
    for child in wave.child_waves:
        yield from iter_selected_waves(child)


def assert_same_selected_wave(
    expected: SelectedInvocationWave,
    actual: SelectedInvocationWave | None,
) -> None:
    """Fail closed when replanned identity diverges from the reserved preview."""
    if actual is None:
        raise SelectedWaveDriftError("replan produced no selected wave")
    if expected.identity_digest != actual.identity_digest:
        raise SelectedWaveDriftError(
            "selected wave identity drifted: "
            f"expected {expected.identity_digest}, got {actual.identity_digest}"
        )
    expected_children = sorted(expected.child_waves, key=lambda item: item.invocation_id)
    actual_children = sorted(actual.child_waves, key=lambda item: item.invocation_id)
    if len(expected_children) != len(actual_children):
        raise SelectedWaveDriftError(
            "selected wave child count drifted: "
            f"expected {len(expected_children)}, got {len(actual_children)}"
        )
    for expected_child, actual_child in zip(expected_children, actual_children, strict=True):
        if expected_child.invocation_id != actual_child.invocation_id:
            raise SelectedWaveDriftError(
                "selected wave child invocation drifted: "
                f"expected {expected_child.invocation_id}, got {actual_child.invocation_id}"
            )
        assert_same_selected_wave(expected_child, actual_child)


def preview_selected_wave(
    compiled: CompiledWorkflow,
    projection: GraphProjection,
    context: RuntimeContext,
    artifacts: ArtifactReader,
    *,
    max_parallel_tasks: int,
    child_projections: Mapping[str, GraphProjection] | None = None,
) -> SelectedInvocationWave | None:
    """Plan and select the next wave without appending ledger events."""
    if _has_uncommitted_succeeded_tasks(projection):
        return None
    try:
        plan = plan_superstep(compiled, projection, context, artifacts)
    except PlanError:
        return None
    if plan.terminal is not None or not plan.tasks:
        return None
    selected = select_wave(plan.tasks, max_parallel_tasks=max_parallel_tasks)
    if not selected:
        return None
    child_lookup = child_projections or {}
    child_waves = _child_waves_for_selected(
        compiled,
        selected,
        context,
        artifacts,
        max_parallel_tasks=max_parallel_tasks,
        child_projections=child_lookup,
    )
    lock_tokens = _lock_tokens_for_wave(compiled, selected)
    synchronized_paths = _aggregate_synchronized_paths(
        compiled,
        selected,
        context,
        artifacts,
        max_parallel_tasks=max_parallel_tasks,
        child_projections=child_lookup,
    )
    identity_digest = _compute_identity_digest(projection.invocation_id, selected, child_waves)
    return SelectedInvocationWave(
        invocation_id=projection.invocation_id,
        projection=projection,
        plan=plan,
        selected_tasks=selected,
        child_waves=child_waves,
        lock_tokens=lock_tokens,
        synchronized_paths=synchronized_paths,
        identity_digest=identity_digest,
    )


def build_prepared_wave_tree(
    wave: SelectedInvocationWave,
    tree_overlays: Mapping[str, str],
) -> PreparedInvocationWave:
    prepared_tree_id = tree_overlays.get(wave.projection.current_tree_id, wave.projection.current_tree_id)
    return PreparedInvocationWave(
        preview=wave,
        prepared_tree_id=prepared_tree_id,
        child_prepared=tuple(
            build_prepared_wave_tree(child, tree_overlays) for child in wave.child_waves
        ),
    )


def flatten_prepared_invocations(root: PreparedInvocationWave) -> tuple[PreparedInvocationWave, ...]:
    ordered: list[PreparedInvocationWave] = []

    def walk(prepared: PreparedInvocationWave) -> None:
        ordered.append(prepared)
        for child in prepared.child_prepared:
            walk(child)

    walk(root)
    return tuple(ordered)


def _find_prepared(prepared: PreparedInvocationWave, invocation_id: str) -> PreparedInvocationWave | None:
    if prepared.preview.invocation_id == invocation_id:
        return prepared
    for child in prepared.child_prepared:
        found = _find_prepared(child, invocation_id)
        if found is not None:
            return found
    return None


def _has_uncommitted_succeeded_tasks(projection: GraphProjection) -> bool:
    return any(
        task.status == "succeeded" and not task.outputs_committed for task in projection.tasks.values()
    )


def _resources_payload(claims: ResourceClaims) -> dict[str, object]:
    return {
        "reads": [f"{path.root}:{path.pattern}" for path in claims.reads],
        "writes": [f"{path.root}:{path.pattern}" for path in claims.writes],
        "synchronized": [f"{path.root}:{path.pattern}" for path in claims.synchronized],
        "exclusive": list(claims.exclusive),
        "authorization_writes": [f"{path.root}:{path.pattern}" for path in claims.authorization_writes],
    }


def _compute_identity_digest(
    invocation_id: str,
    selected_tasks: tuple[ExecutableTask, ...],
    child_waves: tuple[SelectedInvocationWave, ...],
) -> str:
    return canonical_digest(
        {
            "invocation_id": invocation_id,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "target": task.target,
                    "contract_digest": task.contract_digest,
                    "resources": _resources_payload(task.resources),
                }
                for task in selected_tasks
            ],
            "child_identities": [child.identity_digest for child in child_waves],
        }
    )


def _lock_tokens_for_task(compiled: CompiledWorkflow, task: ExecutableTask) -> frozenset[str]:
    prefix, _, graph_id = task.target.partition(":")
    if prefix == "graph" and graph_id in compiled.graphs:
        footprint = compiled.graphs[graph_id].resource_footprint
        return frozenset(token for token in footprint.exclusive if token.startswith("project:"))
    if task.resources.synchronized:
        return frozenset(token for token in task.resources.exclusive if token.startswith("project:"))
    return frozenset()


def _lock_tokens_for_wave(
    compiled: CompiledWorkflow,
    selected_tasks: tuple[ExecutableTask, ...],
) -> tuple[str, ...]:
    tokens: set[str] = set()
    for task in selected_tasks:
        tokens.update(_lock_tokens_for_task(compiled, task))
    return tuple(sorted(tokens))


def _synchronized_paths_for_task(
    compiled: CompiledWorkflow,
    task: ExecutableTask,
    context: RuntimeContext,
    artifacts: ArtifactReader,
    *,
    max_parallel_tasks: int,
    child_projections: Mapping[str, GraphProjection],
) -> tuple[ResourcePath, ...]:
    prefix, _, graph_id = task.target.partition(":")
    if prefix == "graph":
        child_id = derive_child_invocation_id(task, graph_id)
        child_projection = child_projections.get(child_id)
        if child_projection is None:
            return ()
        child_wave = preview_selected_wave(
            compiled,
            child_projection,
            context,
            artifacts,
            max_parallel_tasks=max_parallel_tasks,
            child_projections=child_projections,
        )
        if child_wave is None:
            return ()
        return child_wave.synchronized_paths
    if task.resources.synchronized:
        return tuple(sorted(task.resources.synchronized, key=lambda path: (path.root, path.pattern)))
    return ()


def _aggregate_synchronized_paths(
    compiled: CompiledWorkflow,
    selected_tasks: tuple[ExecutableTask, ...],
    context: RuntimeContext,
    artifacts: ArtifactReader,
    *,
    max_parallel_tasks: int,
    child_projections: Mapping[str, GraphProjection],
) -> tuple[ResourcePath, ...]:
    paths: set[ResourcePath] = set()
    for task in selected_tasks:
        paths.update(
            _synchronized_paths_for_task(
                compiled,
                task,
                context,
                artifacts,
                max_parallel_tasks=max_parallel_tasks,
                child_projections=child_projections,
            )
        )
    return tuple(sorted(paths, key=lambda path: (path.root, path.pattern)))


def _child_waves_for_selected(
    compiled: CompiledWorkflow,
    selected_tasks: tuple[ExecutableTask, ...],
    context: RuntimeContext,
    artifacts: ArtifactReader,
    *,
    max_parallel_tasks: int,
    child_projections: Mapping[str, GraphProjection],
) -> tuple[SelectedInvocationWave, ...]:
    waves: list[SelectedInvocationWave] = []
    for task in selected_tasks:
        prefix, _, graph_id = task.target.partition(":")
        if prefix != "graph":
            continue
        child_id = derive_child_invocation_id(task, graph_id)
        child_projection = child_projections.get(child_id)
        if child_projection is None:
            continue
        child_wave = preview_selected_wave(
            compiled,
            child_projection,
            context,
            artifacts,
            max_parallel_tasks=max_parallel_tasks,
            child_projections=child_projections,
        )
        if child_wave is not None:
            waves.append(child_wave)
    waves.sort(key=lambda wave: wave.invocation_id)
    return tuple(waves)


__all__ = [
    "PreparedInvocationWave",
    "PreparedWaveLease",
    "SelectedInvocationWave",
    "SelectedWaveDriftError",
    "assert_same_selected_wave",
    "build_prepared_wave_tree",
    "derive_child_invocation_id",
    "flatten_prepared_invocations",
    "iter_selected_waves",
    "preview_selected_wave",
]
