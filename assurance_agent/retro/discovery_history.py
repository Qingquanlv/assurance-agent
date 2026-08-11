"""Read-only Discovery / Coverage-gap history adapters for Retro evidence.

Retro consumes **compact archived projections** only — campaign ids, CE ids,
promotion receipt digests, replay rate summaries, and problem escape refs.
Full counterexample bodies and raw discovery workspace trees are never read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.coverage_gaps import (
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.discovery import CampaignResult, Counterexample
from assurance_agent.artifacts.models.issues import ChangeIssueSnapshot, ProblemProjection
from assurance_agent.artifacts.models.promotion import PromotionReceipt, RegressionCandidate
from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.evidence.coverage_gaps import GapIdentity, diff_coverage_gaps
from assurance_agent.evidence.promotion import count_promoted_counterexamples
from assurance_agent.evidence.replay_telemetry import compute_seed_replay_rate
from assurance_agent.retro.types import RetroIntegrity, RetroSourceDescriptor
from assurance_agent.retro.window import RetroWindowSelection
from assurance_agent.workflow.discovery.replay_receipts import (
    ReplayReceiptIntegrityError,
    load_replay_attempt_receipts,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

GapIdentityAxes = tuple[str, str, str, str, str]


class CompactGapIdentity(BaseModel):
    """One gap identity axis tuple for compact archived projections."""

    model_config = _FROZEN

    kind: str = Field(min_length=1)
    case_id: str = ""
    constraint_key: str = ""
    cell: str = ""
    cluster_key: str = ""

    def as_tuple(self) -> GapIdentityAxes:
        return (self.kind, self.case_id, self.constraint_key, self.cell, self.cluster_key)


class CompactDiscoveryRecord(BaseModel):
    """Archived-style compact discovery projection row (no CE bodies)."""

    model_config = _FROZEN

    change_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    counterexample_ids: tuple[str, ...] = ()
    promotion_receipt_digests: tuple[str, ...] = ()
    replay_success: int | None = Field(default=None, ge=0)
    replay_attempts: int | None = Field(default=None, ge=0)
    replay_rate: float | None = None
    problem_escape_refs: tuple[str, ...] = ()
    promoted_count: int | None = Field(default=None, ge=0)
    total_counterexamples: int | None = Field(default=None, ge=0)
    sha256: str = Field(min_length=1)


class DiscoveryHistorySlice(BaseModel):
    """Digest-pinned Discovery evidence for one Retro window."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_ids: tuple[str, ...]
    sources: tuple[RetroSourceDescriptor, ...] = ()
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))
    records: tuple[CompactDiscoveryRecord, ...] = ()


class CompactCoverageGapRecord(BaseModel):
    """Archived/current ``coverage-gaps.json`` digest + identity axes."""

    model_config = _FROZEN

    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    projection_digest: str = Field(min_length=1)
    document_digest: str = Field(min_length=1)
    gap_identities: tuple[CompactGapIdentity, ...] = ()
    sha256: str = Field(min_length=1)


class CoverageGapHistoryEvent(BaseModel):
    """Closed / opened / reopened event from ``diff_coverage_gaps``."""

    model_config = _FROZEN

    event_kind: Literal["closed", "opened", "reopened"]
    change_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    case_id: str = ""
    constraint_key: str = ""
    cell: str = ""
    cluster_key: str = ""

    @property
    def locator_fingerprint(self) -> str:
        return "|".join((self.kind, self.case_id, self.constraint_key, self.cell, self.cluster_key))


class CoverageGapHistorySlice(BaseModel):
    """Digest-pinned Coverage-gap evidence + diff events for one Retro window."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_ids: tuple[str, ...]
    sources: tuple[RetroSourceDescriptor, ...] = ()
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))
    records: tuple[CompactCoverageGapRecord, ...] = ()
    events: tuple[CoverageGapHistoryEvent, ...] = ()


@runtime_checkable
class DiscoveryHistoryReader(Protocol):
    def read_discovery_slice(self, selection: RetroWindowSelection) -> DiscoveryHistorySlice | None: ...


@runtime_checkable
class CoverageGapHistoryReader(Protocol):
    def read_coverage_gap_slice(self, selection: RetroWindowSelection) -> CoverageGapHistorySlice | None: ...


def _selected_change_ids(selection: RetroWindowSelection) -> tuple[str, ...]:
    if selection.change_ids:
        return tuple(selection.change_ids)
    if selection.batch_scope is not None:
        return tuple(member.change_id for member in selection.batch_scope.members)
    return ()


def _discovery_gap_reason(change_id: str, reason_code: str) -> str:
    return f"discovery_projection_{reason_code}:{change_id}"


def _coverage_gap_reason(change_id: str, reason_code: str) -> str:
    return f"coverage_gap_projection_{reason_code}:{change_id}"


def _build_discovery_slice(
    *,
    change_ids: Sequence[str],
    records: Sequence[CompactDiscoveryRecord],
    reasons: Sequence[str],
) -> DiscoveryHistorySlice:
    ordered = tuple(sorted(records, key=lambda item: (item.change_id, item.campaign_id)))
    sources = tuple(
        RetroSourceDescriptor(
            kind="discovery_projection",
            change_id=record.change_id,
            head_event_id=record.campaign_id,
            sha256=record.sha256,
            evidence_ids=(f"{record.change_id}:{record.campaign_id}",),
        )
        for record in ordered
    )
    integrity = (
        RetroIntegrity(status="incomplete", reasons=tuple(reasons))
        if reasons
        else RetroIntegrity(status="complete")
    )
    return DiscoveryHistorySlice(
        change_ids=tuple(change_ids),
        sources=sources,
        integrity=integrity,
        records=ordered,
    )


def _record_to_gaps_document(record: CompactCoverageGapRecord) -> CoverageGapsDocument:
    gaps: list[CoverageGap] = []
    for identity in record.gap_identities:
        locator = CoverageGapLocator(
            case_id=identity.case_id or None,
            constraint_key=identity.constraint_key or None,
            cell=identity.cell or None,
            cluster_key=identity.cluster_key or None,
        )
        gaps.append(
            CoverageGap(
                kind=identity.kind,  # type: ignore[arg-type]
                locator=locator,
                layer="execution",
                batch_id=record.batch_id,
                evidence_refs=(record.projection_digest,),
            )
        )
    return CoverageGapsDocument(
        schema_version="1",
        change_id=record.change_id,
        batch_id=record.batch_id,
        projection_digest=record.projection_digest,
        gaps=tuple(gaps),
    )


def _event_from_identity(
    *,
    event_kind: Literal["closed", "opened", "reopened"],
    change_id: str,
    identity: GapIdentity,
) -> CoverageGapHistoryEvent:
    kind, case_id, constraint_key, cell, cluster_key = identity
    return CoverageGapHistoryEvent(
        event_kind=event_kind,
        change_id=change_id,
        kind=kind,
        case_id=case_id,
        constraint_key=constraint_key,
        cell=cell,
        cluster_key=cluster_key,
    )


def _build_coverage_gap_slice(
    *,
    change_ids: Sequence[str],
    records: Sequence[CompactCoverageGapRecord],
    events: Sequence[CoverageGapHistoryEvent],
    reasons: Sequence[str],
) -> CoverageGapHistorySlice:
    ordered = tuple(sorted(records, key=lambda item: (item.change_id, item.batch_id)))
    sources = tuple(
        RetroSourceDescriptor(
            kind="coverage_gap_projection",
            change_id=record.change_id,
            head_event_id=record.batch_id,
            sha256=record.sha256,
            evidence_ids=(f"{record.change_id}:{record.batch_id}",),
        )
        for record in ordered
    )
    integrity = (
        RetroIntegrity(status="incomplete", reasons=tuple(reasons))
        if reasons
        else RetroIntegrity(status="complete")
    )
    ordered_events = tuple(
        sorted(
            events,
            key=lambda item: (
                item.change_id,
                item.event_kind,
                item.kind,
                item.case_id,
                item.constraint_key,
                item.cell,
                item.cluster_key,
            ),
        )
    )
    return CoverageGapHistorySlice(
        change_ids=tuple(change_ids),
        sources=sources,
        integrity=integrity,
        records=ordered,
        events=ordered_events,
    )


class InMemoryDiscoveryHistoryReader:
    """Fixture adapter over frozen compact discovery records."""

    def __init__(
        self,
        records: Sequence[CompactDiscoveryRecord] = (),
        *,
        corrupt_change_ids: frozenset[str] = frozenset(),
        missing_change_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._records = tuple(records)
        self._corrupt = corrupt_change_ids
        self._missing = missing_change_ids

    def read_discovery_slice(self, selection: RetroWindowSelection) -> DiscoveryHistorySlice:
        selected = _selected_change_ids(selection)
        selected_set = set(selected)
        kept: list[CompactDiscoveryRecord] = []
        reasons: list[str] = []
        for change_id in selected:
            if change_id in self._missing:
                reasons.append(_discovery_gap_reason(change_id, "missing"))
                continue
            if change_id in self._corrupt:
                reasons.append(_discovery_gap_reason(change_id, "corrupt"))
                continue
        for record in self._records:
            if record.change_id not in selected_set:
                continue
            if record.change_id in self._missing or record.change_id in self._corrupt:
                continue
            kept.append(record)
        return _build_discovery_slice(change_ids=selected, records=kept, reasons=reasons)


class InMemoryCoverageGapHistoryReader:
    """Fixture adapter over compact coverage-gap digests + diff feedstock."""

    def __init__(
        self,
        current_records: Sequence[CompactCoverageGapRecord] = (),
        *,
        previous_by_change_id: Mapping[str, CompactCoverageGapRecord] | None = None,
        historically_closed_by_change_id: Mapping[str, Sequence[GapIdentityAxes]] | None = None,
        missing_change_ids: frozenset[str] = frozenset(),
        corrupt_change_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._current = tuple(current_records)
        self._previous = dict(previous_by_change_id or {})
        self._historically_closed = {
            change_id: tuple(items) for change_id, items in (historically_closed_by_change_id or {}).items()
        }
        self._missing = missing_change_ids
        self._corrupt = corrupt_change_ids

    def read_coverage_gap_slice(self, selection: RetroWindowSelection) -> CoverageGapHistorySlice:
        selected = _selected_change_ids(selection)
        selected_set = set(selected)
        kept: list[CompactCoverageGapRecord] = []
        events: list[CoverageGapHistoryEvent] = []
        reasons: list[str] = []
        for change_id in selected:
            if change_id in self._missing:
                reasons.append(_coverage_gap_reason(change_id, "missing"))
                continue
            if change_id in self._corrupt:
                reasons.append(_coverage_gap_reason(change_id, "corrupt"))
                continue
        for record in self._current:
            if record.change_id not in selected_set:
                continue
            if record.change_id in self._missing or record.change_id in self._corrupt:
                continue
            kept.append(record)
            previous = self._previous.get(record.change_id)
            if previous is None:
                continue
            diff = diff_coverage_gaps(
                _record_to_gaps_document(previous),
                _record_to_gaps_document(record),
                historically_closed=self._historically_closed.get(record.change_id, ()),
            )
            for identity in diff.closed:
                events.append(
                    _event_from_identity(event_kind="closed", change_id=record.change_id, identity=identity)
                )
            for identity in diff.opened:
                events.append(
                    _event_from_identity(event_kind="opened", change_id=record.change_id, identity=identity)
                )
            for identity in diff.reopened:
                events.append(
                    _event_from_identity(event_kind="reopened", change_id=record.change_id, identity=identity)
                )
        return _build_coverage_gap_slice(change_ids=selected, records=kept, events=events, reasons=reasons)


def _read_yaml_model(path: Path, model: type[BaseModel]) -> tuple[BaseModel, bytes]:
    raw = path.read_bytes()
    return model.model_validate(yaml.safe_load(raw.decode("utf-8"))), raw


def _problem_escape_refs(project_root: Path, change_dir: Path, change_id: str) -> tuple[str, ...]:
    path = project_root / "qa" / "issues" / "problems.json"
    if not path.is_file():
        return ()
    projection = ProblemProjection.model_validate_json(path.read_text(encoding="utf-8"))
    linked_ids: set[str] = set()
    snapshot_path = change_dir / "issues" / "snapshot.json"
    if snapshot_path.is_file():
        snapshot = ChangeIssueSnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        linked_ids.update(item.problem_id for item in snapshot.occurrences)
    return tuple(
        sorted(
            problem.problem_id
            for problem in projection.problems
            if problem.escape_analysis is not None
            and problem.escape_analysis.authority == "human_confirmed"
            and problem.escape_analysis.is_escape is True
            and (
                problem.problem_id in linked_ids
                or problem.first_seen.change_id == change_id
                or problem.last_seen.change_id == change_id
            )
        )
    )


def _read_discovery_record(
    project_root: Path,
    change_id: str,
) -> tuple[CompactDiscoveryRecord | None, str | None]:
    try:
        change_dir = resolve_change(project_root, change_id, prefer="archive").path
    except ChangeNotFoundError:
        return None, _discovery_gap_reason(change_id, "missing")
    campaign_path = change_dir / "discovery" / "campaign-result.yaml"
    if not campaign_path.is_file():
        return None, _discovery_gap_reason(change_id, "missing")
    try:
        campaign_model, campaign_bytes = _read_yaml_model(campaign_path, CampaignResult)
        assert isinstance(campaign_model, CampaignResult)
        campaign = campaign_model
        if campaign.change_id != change_id:
            raise ValueError("campaign change_id mismatch")

        ce_ids: list[str] = []
        ce_digests: list[str] = []
        ce_root = change_dir / "discovery" / "counterexamples"
        if ce_root.is_dir():
            for path in sorted((*ce_root.glob("*.yaml"), *ce_root.glob("*.yml"))):
                ce_model, raw = _read_yaml_model(path, Counterexample)
                assert isinstance(ce_model, Counterexample)
                if ce_model.campaign_id != campaign.campaign_id:
                    continue
                ce_ids.append(ce_model.counterexample_id)
                ce_digests.append(sha256_bytes(raw))
        if len(ce_ids) != campaign.counterexample_count:
            raise ValueError(
                f"campaign counterexample_count={campaign.counterexample_count} != receipts={len(ce_ids)}"
            )

        receipts: list[PromotionReceipt] = []
        candidates: list[RegressionCandidate] = []
        receipt_digests: list[str] = []
        candidate_root = change_dir / "discovery" / "candidates"
        if candidate_root.is_dir():
            for candidate_dir in sorted(path for path in candidate_root.iterdir() if path.is_dir()):
                candidate_path = candidate_dir / "candidate.yaml"
                if candidate_path.is_file():
                    candidate_model, _ = _read_yaml_model(candidate_path, RegressionCandidate)
                    assert isinstance(candidate_model, RegressionCandidate)
                    candidates.append(candidate_model)
                receipt_path = candidate_dir / "promotion-receipt.json"
                if receipt_path.is_file():
                    raw = receipt_path.read_bytes()
                    receipts.append(PromotionReceipt.model_validate_json(raw))
                    receipt_digests.append(sha256_bytes(raw))

        ce_id_set = set(ce_ids)
        replay_receipts = tuple(
            receipt
            for receipt in load_replay_attempt_receipts(change_dir)
            if receipt.counterexample_id in ce_id_set
        )
        replay_success, replay_attempts, replay_rate = compute_seed_replay_rate(replay_receipts)
        promoted = count_promoted_counterexamples(receipts, candidates)
        escape_refs = _problem_escape_refs(project_root, change_dir, change_id)
        identity = {
            "campaign": sha256_bytes(campaign_bytes),
            "counterexamples": sorted(ce_digests),
            "promotion_receipts": sorted(receipt_digests),
            "replay_attempts": replay_attempts,
            "escape_refs": escape_refs,
        }
        return (
            CompactDiscoveryRecord(
                change_id=change_id,
                campaign_id=campaign.campaign_id,
                counterexample_ids=tuple(sorted(ce_ids)),
                promotion_receipt_digests=tuple(sorted(receipt_digests)),
                replay_success=replay_success if replay_attempts else None,
                replay_attempts=replay_attempts if replay_attempts else None,
                replay_rate=replay_rate,
                problem_escape_refs=escape_refs,
                promoted_count=promoted,
                total_counterexamples=campaign.counterexample_count,
                sha256=sha256_bytes(canonical_json_bytes(identity)),
            ),
            None,
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        ValidationError,
        yaml.YAMLError,
        ReplayReceiptIntegrityError,
    ):
        return None, _discovery_gap_reason(change_id, "corrupt")


class FileDiscoveryHistoryReader:
    """Production adapter over archived/current compact discovery artifacts."""

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)

    def read_discovery_slice(self, selection: RetroWindowSelection) -> DiscoveryHistorySlice | None:
        selected = _selected_change_ids(selection)
        records: list[CompactDiscoveryRecord] = []
        reasons: list[str] = []
        for change_id in selected:
            record, reason = _read_discovery_record(self._root, change_id)
            if record is not None:
                records.append(record)
            if reason is not None:
                reasons.append(reason)
        if (
            not records
            and reasons
            and all(reason.startswith("discovery_projection_missing:") for reason in reasons)
        ):
            return None
        if not selected:
            return None
        return _build_discovery_slice(change_ids=selected, records=records, reasons=reasons)


def _read_coverage_record(
    project_root: Path,
    change_id: str,
) -> tuple[CompactCoverageGapRecord | None, CoverageGapsDocument | None, str | None]:
    try:
        change_dir = resolve_change(project_root, change_id, prefer="archive").path
    except ChangeNotFoundError:
        return None, None, _coverage_gap_reason(change_id, "missing")
    path = change_dir / "inspect" / "coverage-gaps.json"
    if not path.is_file():
        return None, None, _coverage_gap_reason(change_id, "missing")
    try:
        raw = path.read_bytes()
        document = CoverageGapsDocument.model_validate_json(raw)
        if document.change_id != change_id:
            raise ValueError("coverage-gap change_id mismatch")
        identities = tuple(
            CompactGapIdentity(
                kind=gap.kind,
                case_id=gap.locator.case_id or "",
                constraint_key=gap.locator.constraint_key or "",
                cell=gap.locator.cell or "",
                cluster_key=gap.locator.cluster_key or "",
            )
            for gap in document.gaps
        )
        digest = sha256_bytes(raw)
        return (
            CompactCoverageGapRecord(
                change_id=change_id,
                batch_id=document.batch_id,
                projection_digest=document.projection_digest,
                document_digest=digest,
                gap_identities=identities,
                sha256=digest,
            ),
            document,
            None,
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return None, None, _coverage_gap_reason(change_id, "corrupt")


class FileCoverageGapHistoryReader:
    """Production adapter that diffs ordered final projections in the Retro window."""

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)

    def read_coverage_gap_slice(self, selection: RetroWindowSelection) -> CoverageGapHistorySlice | None:
        selected = _selected_change_ids(selection)
        records: list[CompactCoverageGapRecord] = []
        events: list[CoverageGapHistoryEvent] = []
        reasons: list[str] = []
        previous: CoverageGapsDocument | None = None
        historically_closed: set[GapIdentity] = set()
        for change_id in selected:
            record, document, reason = _read_coverage_record(self._root, change_id)
            if reason is not None:
                reasons.append(reason)
                previous = None
                continue
            assert record is not None and document is not None
            records.append(record)
            if previous is not None:
                diff = diff_coverage_gaps(previous, document, historically_closed=historically_closed)
                for event_kind, identities in (
                    ("closed", diff.closed),
                    ("opened", diff.opened),
                    ("reopened", diff.reopened),
                ):
                    for identity in identities:
                        events.append(
                            _event_from_identity(
                                event_kind=event_kind,  # type: ignore[arg-type]
                                change_id=change_id,
                                identity=identity,
                            )
                        )
                historically_closed.update(diff.closed)
            previous = document
        if (
            not records
            and reasons
            and all(reason.startswith("coverage_gap_projection_missing:") for reason in reasons)
        ):
            return None
        if not selected:
            return None
        return _build_coverage_gap_slice(
            change_ids=selected,
            records=records,
            events=events,
            reasons=reasons,
        )


__all__ = [
    "CompactCoverageGapRecord",
    "CompactDiscoveryRecord",
    "CompactGapIdentity",
    "CoverageGapHistoryEvent",
    "CoverageGapHistoryReader",
    "CoverageGapHistorySlice",
    "DiscoveryHistoryReader",
    "DiscoveryHistorySlice",
    "FileCoverageGapHistoryReader",
    "FileDiscoveryHistoryReader",
    "InMemoryCoverageGapHistoryReader",
    "InMemoryDiscoveryHistoryReader",
]
