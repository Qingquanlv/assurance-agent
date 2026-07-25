"""Deterministic Retro collect: typed readers → immutable schema-v2 context.json."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.context import (
    RetroContextImmutableError,
    build_retro_context,
)
from assurance_agent.retro.eval_history import EvalHistoryReader, FileEvalHistoryReader
from assurance_agent.retro.types import RetroContext
from assurance_agent.retro.window import RetroWindowSelection, selection_from_nightly_options
from assurance_agent.retro.workflow_history import (
    LedgerWorkflowHistoryReader,
    WorkflowHistoryReader,
)
from assurance_agent.workflow.issues.history import IssueHistoryReader, LedgerIssueHistoryReader

ContextBuilder = Callable[..., RetroContext]


@dataclass(frozen=True)
class RetroCollectResult:
    retro_id: str
    retro_dir: Path
    signal_count: int
    context: RetroContext


def _canonical_context_bytes(context: RetroContext) -> bytes:
    payload = context.model_dump(mode="json")
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _write_context_json(path: Path, context: RetroContext) -> None:
    canonical = _canonical_context_bytes(context)
    if path.is_file():
        existing = path.read_bytes()
        if existing == canonical:
            return
        raise RetroContextImmutableError(
            f"context.json at {path} already exists with different bytes"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical)


def run_retro_collect(
    sut: Path,
    *,
    retro_id: str,
    selection: RetroWindowSelection | None = None,
    last: int = 10,
    change_ids: tuple[str, ...] = (),
    since: str | None = None,
    until: str | None = None,
    write_root: Path | None = None,
    issue_history: IssueHistoryReader | None = None,
    workflow_history: WorkflowHistoryReader | None = None,
    eval_history: EvalHistoryReader | None = None,
    now: datetime | None = None,
    # Half-cutover: absorb legacy nightly/test kwargs until Tasks 7–12 rewrite callers.
    is_terminal: object | None = None,
    context_builder: ContextBuilder | None = None,
) -> RetroCollectResult:
    """Collect Retro evidence through typed readers and write ``context.json``.

    ``sut`` is the evidence read root; ``write_root`` (default: ``sut``) receives
    the immutable current-run ``context.json``. Does not read or write consumed
    change state. ``IssueHistoryIntegrityError`` propagates as a hard failure.
    Incomplete Issue analysis/sync marks context incomplete but still succeeds.
    """
    _ = (is_terminal, context_builder)
    assert_path_segment_safe(retro_id, label="retro id")
    out_root = write_root or sut
    retro_dir = out_root / "qa" / "retro" / retro_id
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if selection is None:
        if change_ids or since is not None or until is not None:
            from assurance_agent.retro.nightly.types import NightlyOptions

            selection = selection_from_nightly_options(
                NightlyOptions(
                    sut=str(sut),
                    last=last,
                    change_ids=change_ids,
                    since=since,
                    until=until,
                )
            )
        else:
            selection = RetroWindowSelection(last=last)

    issues = issue_history or LedgerIssueHistoryReader(sut)
    workflow = workflow_history or LedgerWorkflowHistoryReader(sut)
    evaluation = eval_history or FileEvalHistoryReader(sut)

    context = build_retro_context(
        selection,
        issue_history=issues,
        workflow_history=workflow,
        eval_history=evaluation,
        retro_id=retro_id,
        generated_at=generated_at,
    )
    _write_context_json(retro_dir / "context.json", context)
    return RetroCollectResult(
        retro_id=retro_id,
        retro_dir=retro_dir,
        signal_count=context.signal_count,
        context=context,
    )
