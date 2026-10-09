from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel

from graph_engine.attempts.models.context import AttemptExecutionContext
from graph_engine.attempts.models.contracts import (
    ResolvedAttemptContract,
)
from graph_engine.attempts.models.errors import AttemptIdentityDrift, AttemptIntegrityError
from graph_engine.attempts.orchestration.handlers import AttemptHandlers
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.models.resolutions import (
    AttemptResolution,
)
from graph_engine.attempts.resources.resource_arbiter import ResourceArbiterPort
from graph_engine.attempts.orchestration.runtime import AttemptRuntime
from graph_engine.attempts.models.runtime_evidence import RuntimeEvidenceSource
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointStore
from graph_engine.plugin_api import (
    CommitValidator,
    WorkspaceProvider,
)


def _noop_cut(_name: str) -> None:
    return None


_MISSING_CUT = object()


class AssuranceAttemptKernel:
    def __init__(
        self,
        *,
        checkpoints: AttemptCheckpointStore,
        arbiter: ResourceArbiterPort,
        workspace: WorkspaceProvider,
        graph_revision: str,
        validators: Mapping[str, CommitValidator] | None = None,
        transaction_cut: Callable[[str], None] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        runtime_evidence: RuntimeEvidenceSource | None = None,
    ) -> None:
        self.checkpoints = checkpoints
        self.arbiter = arbiter
        self.workspace = workspace
        self.graph_revision = graph_revision
        self.validators = dict(validators or {})
        self._transaction_cut = transaction_cut or _noop_cut
        self._pause_requested = pause_requested
        self.runtime_evidence = runtime_evidence

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: ResolvedAttemptContract[Any, Any],
        validated_input: BaseModel,
        context: AttemptExecutionContext,
        *,
        trace: list[str] | None = None,
        transaction_cut: Callable[[str], None] | None | object = _MISSING_CUT,
    ) -> AttemptResolution:
        if transaction_cut is _MISSING_CUT:
            cut = self._transaction_cut
        elif transaction_cut is None:
            cut = _noop_cut
        else:
            cut = transaction_cut
        handlers = AttemptHandlers(
            checkpoints=self.checkpoints,
            arbiter=self.arbiter,
            workspace=self.workspace,
            graph_revision=self.graph_revision,
            validators=self.validators,
            runtime_evidence=self.runtime_evidence,
            pause_requested=self._pause_requested,
            attempt_key=attempt_key,
            contract=contract,
            validated_input=validated_input,
            context=context,
            trace=[] if trace is None else trace,
            cut=cast(Callable[[str], None], cut),
        )
        return await AttemptRuntime(handlers).run()


__all__ = [
    "AssuranceAttemptKernel",
    "AttemptIdentityDrift",
    "AttemptIntegrityError",
]
