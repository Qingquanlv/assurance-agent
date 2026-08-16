"""Shared root/child invocation start: started event pins plus runtime meta."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from assurance_agent.workflow.core.graph_events import GraphInvocationStartedEvent
from assurance_agent.workflow.graph.models import RuntimeContext


def write_invocation_runtime_meta(
    txn: Any,
    invocation_id: str,
    context: RuntimeContext,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Persist host paths for a new invocation. Does not append ledger events."""
    payload: dict[str, object] = {
        "project_root": str(context.project_root),
        "repo_root": str(context.repo_root),
        "change_id": context.change_id,
        "parent_session_id": context.parent_session_id,
    }
    if extra:
        payload.update(dict(extra))
    txn.write_runtime_file(
        f".graph-runtime/invocations/{invocation_id}.json",
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    )


def append_invocation_bootstrap(
    txn: Any,
    *,
    started: GraphInvocationStartedEvent,
    context: RuntimeContext,
    stage_pins: Callable[[Any], None],
    extra_meta: Mapping[str, object] | None = None,
) -> None:
    """Append started, pin definitions, and write invocation meta in one txn."""
    txn.append_strict(started)
    stage_pins(txn)
    write_invocation_runtime_meta(txn, started.invocation_id, context, extra_meta)
