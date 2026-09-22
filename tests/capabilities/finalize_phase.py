"""Run real finalizers through the executor's staging-write boundary."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

from agent_runtime_contracts import AgentExecutionContract, ResolvedRawAgentExecutor
from graph_engine.attempts import AuthorizedAttemptScope, PermanentTaskFailure

T = TypeVar("T")


async def checked_finalize(
    contract: AgentExecutionContract[Any, Any, Any],
    write_root: Path,
    action: Callable[[], Awaitable[T]],
) -> T:
    # Only the filesystem and write claims participate in this boundary check;
    # provider dispatch and graph scheduling are not replaced or exercised here.
    scope = cast(
        AuthorizedAttemptScope,
        SimpleNamespace(
            workspace=SimpleNamespace(
                write_root=write_root,
                identity=SimpleNamespace(output_paths=contract.resources.writes),
            )
        ),
    )
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=cast(Any, None),
        runtime=cast(Any, None),
        finalize=cast(Any, None),
    )
    result = await executor._run_phase("finalize", action, scope)
    assert not isinstance(result, PermanentTaskFailure), result
    return cast(T, result)
