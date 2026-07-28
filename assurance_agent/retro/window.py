"""Deterministic Retro window selection and resolution (no consumed-change state)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.retro_batch import RetroBatchMember, RetroBatchScope
from assurance_agent.exceptions import AaError
from assurance_agent.retro.types import RetroIntegrity, RetroSelectionSnapshot, RetroWindow
from assurance_agent.workflow.issues.history_models import IssueWindowSelection

if TYPE_CHECKING:
    from assurance_agent.retro.workflow_history import WorkflowHistoryReader

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class BatchScopeContractError(AaError):
    """Structured failure raised before any evidence read for an invalid Batch scope."""

    error_kind: Literal["batch_scope_invalid"] = "batch_scope_invalid"

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or f"invalid Retro batch scope: {reason_code}")


class RetroWindowSelection(BaseModel):
    """Exactly one of: explicit change_ids, since/until time range, or last N."""

    model_config = _FROZEN

    change_ids: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    last: int | None = Field(default=None, ge=1)
    batch_scope: RetroBatchScope | None = None

    @model_validator(mode="after")
    def exactly_one_mode(self) -> Self:
        modes = (
            bool(self.change_ids),
            self.since is not None or self.until is not None,
            self.last is not None,
        )
        if sum(modes) != 1:
            if self.batch_scope is not None:
                raise BatchScopeContractError("window_mode_conflict")
            raise ValueError("exactly one window mode is required")
        validate_batch_selection(self)
        return self


def validate_batch_selection(selection: RetroWindowSelection) -> RetroBatchScope | None:
    """Require the selected Change sequence to exactly match the canonical manifest."""
    scope = selection.batch_scope
    if scope is None:
        return None
    if selection.last is not None or selection.since is not None or selection.until is not None:
        raise BatchScopeContractError("window_mode_conflict")
    manifest_ids = tuple(member.change_id for member in scope.members)
    if selection.change_ids != manifest_ids:
        raise BatchScopeContractError("member_set_or_order_mismatch")
    return scope


def selection_from_options(
    *,
    change_ids: tuple[str, ...] = (),
    since: str | None = None,
    until: str | None = None,
    last: int | None = None,
    batch_scope: RetroBatchScope | None = None,
) -> RetroWindowSelection:
    """Map CLI/graph window fields onto the mutually exclusive constructor."""
    if change_ids:
        return RetroWindowSelection(change_ids=tuple(change_ids), last=None, batch_scope=batch_scope)
    if since is not None or until is not None:
        return RetroWindowSelection(since=since, until=until, last=None)
    return RetroWindowSelection(last=last)


class ResolvedRetroWindow(BaseModel):
    """Frozen Change ID list and effective bounds for one Retro collect run."""

    model_config = _FROZEN

    selection: RetroSelectionSnapshot
    change_ids: tuple[str, ...]
    since: str | None = None
    until: str | None = None
    workflow_sources: tuple[str, ...] = ()
    batch_scope: RetroBatchScope | None = None
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))

    def to_issue_selection(self) -> IssueWindowSelection:
        member_statuses = (
            tuple((member.change_id, member.execution_status) for member in self.batch_scope.members)
            if self.batch_scope is not None
            else ()
        )
        if self.selection.mode == "time_range":
            return IssueWindowSelection(
                change_ids=self.change_ids,
                event_since=self.since,
                event_until=self.until,
                include_late_review_closure=True,
                allow_member_gaps=self.batch_scope is not None,
                member_execution_statuses=member_statuses,
            )
        return IssueWindowSelection(
            change_ids=self.change_ids,
            allow_member_gaps=self.batch_scope is not None,
            member_execution_statuses=member_statuses,
        )

    def to_context_window(self) -> RetroWindow:
        return RetroWindow(
            selection=self.selection,
            change_ids=self.change_ids,
            since=self.since,
            until=self.until,
            project_event_through=None,
            batch_scope=self.batch_scope,
        )


def _resolved_batch_scope(
    scope: RetroBatchScope,
    *,
    known_ids: frozenset[str],
    terminal_ids: frozenset[str],
) -> RetroBatchScope:
    """Reconcile declared availability with observations without ever upgrading it."""
    members: list[RetroBatchMember] = []
    for member in scope.members:
        availability = member.evidence_availability
        if availability == "complete":
            if member.execution_status == "not_started" or member.change_id not in known_ids:
                availability = "absent"
            elif member.execution_status == "running" or member.change_id not in terminal_ids:
                availability = "partial"
        members.append(member.model_copy(update={"evidence_availability": availability}))
    resolved_status = (
        "complete"
        if scope.status == "complete"
        and all(member.evidence_availability == "complete" for member in members)
        else "incomplete"
    )
    return scope.model_copy(update={"status": resolved_status, "members": tuple(members)})


def _snapshot(selection: RetroWindowSelection) -> RetroSelectionSnapshot:
    if selection.change_ids:
        mode = "change_ids"
    elif selection.since is not None or selection.until is not None:
        mode = "time_range"
    else:
        mode = "last"
    return RetroSelectionSnapshot(
        mode=mode,
        requested_change_ids=tuple(sorted(set(selection.change_ids))),
        requested_since=selection.since,
        requested_until=selection.until,
        requested_last=selection.last,
    )


def _ts_in_closed_range(ts: str, since: str | None, until: str | None) -> bool:
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def resolve_retro_window(
    selection: RetroWindowSelection,
    *,
    workflow_history: WorkflowHistoryReader,
) -> ResolvedRetroWindow:
    """Resolve a selection into sorted Change IDs without reading or writing consumed state."""
    snapshot = _snapshot(selection)
    scope = validate_batch_selection(selection)
    terminals = (
        workflow_history.list_terminal_changes(
            tuple(
                member.change_id
                for member in scope.members
                if member.execution_status not in {"running", "not_started"}
            ),
            tolerate_member_errors=True,
        )
        if scope is not None
        else workflow_history.list_terminal_changes()
    )
    known = workflow_history.discover_change_ids()
    reasons: list[str] = []

    if snapshot.mode == "change_ids":
        change_ids = selection.change_ids if scope is not None else tuple(sorted(set(selection.change_ids)))
        for change_id in change_ids:
            if change_id not in known:
                reasons.append(f"workflow_source_missing:{change_id}")
        workflow_heads = tuple(
            ref.ledger_sha256 for ref in terminals if ref.change_id in change_ids and ref.ledger_sha256
        )
        integrity = (
            RetroIntegrity(status="incomplete", reasons=tuple(reasons))
            if reasons
            else RetroIntegrity(status="complete")
        )
        resolved_scope = (
            _resolved_batch_scope(
                scope,
                known_ids=known,
                terminal_ids=frozenset(ref.change_id for ref in terminals),
            )
            if scope is not None
            else None
        )
        if resolved_scope is not None and resolved_scope.status == "incomplete" and not reasons:
            integrity = RetroIntegrity(status="incomplete", reasons=("batch_evidence_incomplete",))
        return ResolvedRetroWindow(
            selection=snapshot,
            change_ids=change_ids,
            since=None,
            until=None,
            workflow_sources=workflow_heads,
            integrity=integrity,
            batch_scope=resolved_scope,
        )

    if snapshot.mode == "time_range":
        since = selection.since
        until = selection.until
        if until is None:
            if terminals:
                until = max(ref.terminal_ts for ref in terminals)
            else:
                reasons.append("workflow_source_head_unavailable")
        selected = [ref for ref in terminals if _ts_in_closed_range(ref.terminal_ts, since, until)]
        change_ids = tuple(sorted({ref.change_id for ref in selected}))
        integrity = (
            RetroIntegrity(status="incomplete", reasons=tuple(reasons))
            if reasons
            else RetroIntegrity(status="complete")
        )
        return ResolvedRetroWindow(
            selection=snapshot,
            change_ids=change_ids,
            since=since,
            until=until,
            workflow_sources=tuple(ref.ledger_sha256 for ref in selected if ref.ledger_sha256),
            integrity=integrity,
        )

    # last-N: order by authoritative terminal event ts, tie-break by Change ID.
    assert selection.last is not None
    chosen = terminals[-selection.last :] if selection.last < len(terminals) else terminals
    change_ids = tuple(sorted({ref.change_id for ref in chosen}))
    since = min((ref.terminal_ts for ref in chosen), default=None)
    until = max((ref.terminal_ts for ref in chosen), default=None)
    return ResolvedRetroWindow(
        selection=snapshot,
        change_ids=change_ids,
        since=since,
        until=until,
        workflow_sources=tuple(ref.ledger_sha256 for ref in chosen if ref.ledger_sha256),
        integrity=RetroIntegrity(status="complete"),
    )
