"""Read-only Eval history adapters for Retro evidence collection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.eval_projection import EvalRunProjection
from assurance_agent.retro.types import RetroIntegrity, RetroSourceDescriptor

if TYPE_CHECKING:
    from assurance_agent.retro.window import ResolvedRetroWindow

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EvalReportRecord(BaseModel):
    """Validated eval ``report.json`` fields used as Retro evidence."""

    model_config = _FROZEN

    run_id: str = Field(min_length=1)
    suite: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    source_change_ids: tuple[str, ...] | None = None
    failure_signature: str | None = None
    sample_ids: tuple[str, ...] = ()
    sha256: str = Field(min_length=1)


class EvalEvidenceSlice(BaseModel):
    """Digest-pinned Eval reports for one Retro window."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    sources: tuple[RetroSourceDescriptor, ...] = ()
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))
    reports: tuple[EvalReportRecord, ...] = ()

    def resolvable_ids(self) -> frozenset[str]:
        return frozenset(report.run_id for report in self.reports)


@runtime_checkable
class EvalHistoryReader(Protocol):
    def read_window(self, window: ResolvedRetroWindow) -> EvalEvidenceSlice: ...


def _runs_dir(project_root: Path) -> Path:
    """Compact Eval projection root; Retro never reads raw ``eval/out``."""
    return project_root / "qa" / "eval" / "runs"


def _ts_in_closed_range(ts: str, since: str | None, until: str | None) -> bool:
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def _started_at_hint(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    started_at = payload.get("started_at")
    if isinstance(started_at, str) and started_at:
        return started_at
    return None


def _parse_report(
    path: Path, *, run_id_fallback: str
) -> tuple[EvalReportRecord | None, str | None, str | None]:
    """Return ``(record, error_reason, started_at_hint)``.

    ``started_at_hint`` is populated when a timestamp is recoverable even if the
    report is otherwise corrupt, so out-of-window dirt can be ignored.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None, f"eval_report_missing:{run_id_fallback}", None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, f"eval_report_corrupt:{run_id_fallback}", None
    hint = _started_at_hint(payload)
    if not isinstance(payload, dict):
        return None, f"eval_report_corrupt:{run_id_fallback}", hint
    if payload.get("schema_version") == "2":
        return None, f"eval_projection_unsupported:{run_id_fallback}", hint
    try:
        projection = EvalRunProjection.model_validate(payload)
        if projection.run_id != run_id_fallback:
            return None, f"eval_report_corrupt:{run_id_fallback}", hint
        record = EvalReportRecord(
            run_id=projection.run_id,
            suite=projection.suite,
            verdict=projection.verdict,
            started_at=projection.started_at,
            completed_at=projection.completed_at,
            source_change_ids=projection.source_change_ids,
            failure_signature=projection.failure_signature,
            sample_ids=projection.sample_ids,
            sha256=sha256_bytes(raw),
        )
    except ValidationError:
        return None, f"eval_report_corrupt:{run_id_fallback}", hint
    return record, None, record.started_at


def _build_slice(
    reports: Sequence[EvalReportRecord],
    reasons: Sequence[str],
) -> EvalEvidenceSlice:
    ordered = tuple(sorted(reports, key=lambda item: (item.started_at, item.run_id)))
    sources = tuple(
        RetroSourceDescriptor(
            kind="eval_run",
            change_id=None,
            head_event_id=report.run_id,
            sha256=report.sha256,
            evidence_ids=(report.run_id,),
        )
        for report in ordered
    )
    integrity = (
        RetroIntegrity(status="incomplete", reasons=tuple(reasons))
        if reasons
        else RetroIntegrity(status="complete")
    )
    return EvalEvidenceSlice(sources=sources, integrity=integrity, reports=ordered)


class FileEvalHistoryReader:
    """Production adapter over compact ``qa/eval/runs/*/report.json`` projections."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def read_window(self, window: ResolvedRetroWindow) -> EvalEvidenceSlice:
        root = _runs_dir(self._root)
        if not root.is_dir():
            if window.batch_scope is None:
                return _build_slice((), ("eval_runs_missing",))
            return _build_slice((), ())

        reports: list[EvalReportRecord] = []
        reasons: list[str] = []
        unbounded = window.since is None and window.until is None
        association_required = window.selection.mode in {"change_ids", "last"}
        selected_change_ids = set(window.change_ids)
        for run_path in sorted(p for p in root.iterdir() if p.is_dir()):
            report_path = run_path / "report.json"
            if not report_path.is_file():
                # Missing report has no resolvable timestamp; only selected
                # (unbounded) windows treat it as an integrity defect.
                if unbounded:
                    reasons.append(f"eval_report_missing:{run_path.name}")
                continue
            record, error, started_hint = _parse_report(report_path, run_id_fallback=run_path.name)
            if error is not None:
                if error.startswith("eval_projection_unsupported:"):
                    continue
                if started_hint is not None:
                    if _ts_in_closed_range(started_hint, window.since, window.until):
                        if window.batch_scope is None:
                            reasons.append(error)
                        else:
                            reasons.extend(
                                f"batch_member_evidence_gap:{member.change_id}:"
                                f"{member.execution_status}:eval:projection_corrupt"
                                for member in window.batch_scope.members
                            )
                elif unbounded:
                    if window.batch_scope is None:
                        reasons.append(error)
                    else:
                        reasons.extend(
                            f"batch_member_evidence_gap:{member.change_id}:"
                            f"{member.execution_status}:eval:projection_corrupt"
                            for member in window.batch_scope.members
                        )
                continue
            assert record is not None
            if not _ts_in_closed_range(record.started_at, window.since, window.until):
                continue
            if association_required and (
                record.source_change_ids is None or selected_change_ids.isdisjoint(record.source_change_ids)
            ):
                continue
            reports.append(record)

        # Empty directory that exists with zero reports is a complete empty set.
        # Only in-window missing/corrupt reports keep concrete incomplete reasons.
        return _build_slice(reports, tuple(dict.fromkeys(reasons)))


class InMemoryEvalHistoryReader:
    """Test adapter that replays a frozen EvalEvidenceSlice."""

    def __init__(self, slice_: EvalEvidenceSlice) -> None:
        self._slice = slice_

    @classmethod
    def from_slice(cls, slice_: EvalEvidenceSlice) -> InMemoryEvalHistoryReader:
        return cls(slice_)

    def read_window(self, window: ResolvedRetroWindow) -> EvalEvidenceSlice:
        filtered = tuple(
            report
            for report in self._slice.reports
            if _ts_in_closed_range(report.started_at, window.since, window.until)
            and (
                window.selection.mode == "time_range"
                or (
                    report.source_change_ids is not None
                    and not set(window.change_ids).isdisjoint(report.source_change_ids)
                )
            )
        )
        # Preserve original incomplete reasons when replaying the same source set.
        if filtered == self._slice.reports:
            return self._slice
        # Narrowed windows must not inherit out-of-window per-run integrity dirt.
        kept_run_ids = {report.run_id for report in filtered}
        reasons = tuple(
            reason
            for reason in self._slice.integrity.reasons
            if reason == "eval_runs_missing" or any(run_id in reason for run_id in kept_run_ids)
        )
        return _build_slice(filtered, reasons)
