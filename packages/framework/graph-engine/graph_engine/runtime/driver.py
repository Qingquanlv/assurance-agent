from __future__ import annotations

from dataclasses import dataclass

from graph_engine.composition import FrozenComposition
from graph_engine.plugin_api import InvocationWorkspaceBinding
from graph_engine.runtime.engine import Engine, EngineError, InvocationHandle
from graph_engine.runtime.secret_sources import InvocationRuntimeAuthorization
from graph_engine.runtime.seed import InvocationSeed


@dataclass(frozen=True, slots=True)
class StartSpec:
    """The seed material required to start a fresh invocation."""

    entrypoint: str
    seed: InvocationSeed


def acquire_invocation(
    engine: Engine,
    composition: FrozenComposition,
    *,
    invocation_id: str,
    authorization: InvocationRuntimeAuthorization,
    workspace_binding: InvocationWorkspaceBinding,
    start: StartSpec | None,
) -> InvocationHandle:
    """Open an existing invocation, otherwise start a new one from ``start``.

    This is the single open-vs-start policy shared by every driver (CLI, tests,
    benchmarks). It is fail-closed: opening a missing invocation without a
    :class:`StartSpec` raises rather than silently bootstrapping. When the
    invocation already exists the seed in ``start`` is ignored and the run
    resumes from the persisted ledger.
    """
    if engine.invocation_exists(invocation_id):
        return engine.open(
            invocation_id,
            composition,
            authorization=authorization,
            workspace_binding=workspace_binding,
        )
    if start is None:
        raise EngineError(f"invocation is missing: {invocation_id}")
    return engine.start(
        composition,
        entrypoint=start.entrypoint,
        invocation_id=invocation_id,
        seed=start.seed,
        authorization=authorization,
        workspace_binding=workspace_binding,
    )
