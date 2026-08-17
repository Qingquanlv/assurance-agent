"""Deterministic Counterexample → Observation → IssueCandidate bridge (Phase 1).

Confirmed hard-oracle counterexamples enter the existing Issue lifecycle as
Observations and provisional ``product_bug`` candidates. Classification
authority remains ``llm_provisional`` so human/reconciler review stays the
gate — the model never self-marks a product bug as final.

Non-confirmed findings (``needs_review``, ``inconclusive_evidence``) are
skipped: they produce neither Observations nor Problem-bound candidates.
Invalid or unreadable CE files fail closed with ``CeBridgeError``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from assurance_agent.artifacts.models.discovery import Counterexample
from assurance_agent.artifacts.paths import discovery_receipt_files
from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueAnalysisStatus,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    IssueReconcileStatus,
    Observation,
    ObservationDocument,
    ObservationSource,
    ProblemProjection,
    ChangeIssueSnapshot,
)
from assurance_agent.workflow.issues.events import ObservationRecordedEvent
from assurance_agent.workflow.issues.identity import (
    ObservationIdentityInput,
    candidate_document_digest,
    observation_id,
)
from assurance_agent.workflow.issues.ledger import ChangeIssueStore, ProjectProblemStore
from assurance_agent.workflow.issues.projection import dump_projection
from assurance_agent.workflow.issues.reconciler import (
    ReconciliationValidationError,
    plan_reconciliation,
)

_CE_REL_DIR = "discovery/counterexamples"


class CeBridgeError(Exception):
    """Typed failure while bridging counterexamples into the Issue lifecycle."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True)
class CeIngestResult:
    """Outcome of one ``ingest_confirmed_counterexamples`` pass."""

    batch_id: str
    observation_count: int
    candidate_count: int
    evidence_bundle_digest: str
    skipped_non_confirmed: int
    reconcile_status: str | None = None


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fmt_digest(hex_digest: str) -> str:
    return f"sha256:{hex_digest}"


def _canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def counterexample_rel_path(ce: Counterexample, *, filename: str | None = None) -> str:
    """Change-relative path for a counterexample artifact."""
    return f"{_CE_REL_DIR}/{filename or f'{ce.counterexample_id}.json'}"


def counterexample_signature(ce: Counterexample) -> str:
    """Deterministic signature from CE identity fields (hash-helper style)."""
    canonical = {
        "campaign_id": ce.campaign_id,
        "obligation_ids": list(ce.obligation_ids),
        "oracle_id": ce.oracle_id,
        "seed": ce.seed,
    }
    return f"discovery_ce_{_canonical_sha256(canonical)[:16]}"


def counterexample_to_observation(
    ce: Counterexample,
    *,
    change_id: str,
    batch_id: str,
    observed_at: str,
    artifact_rel: str | None = None,
) -> Observation:
    """Map one Counterexample to an ``anomaly`` Observation targeting ``api``."""
    rel = artifact_rel or counterexample_rel_path(ce)
    signature = counterexample_signature(ce)
    obs_input = ObservationIdentityInput(
        change_id=change_id,
        batch_id=batch_id,
        kind="anomaly",
        target="api",
        case_id=None,
        source_artifact=rel,
        source_json_pointer="/finding_status",
        signature=signature,
    )
    return Observation(
        observation_id=observation_id(obs_input),
        change_id=change_id,
        batch_id=batch_id,
        kind="anomaly",
        target="api",
        case_id=None,
        source=ObservationSource(artifact=rel, json_pointer="/finding_status"),
        evidence_refs=[rel],
        signature=signature,
        observed_at=observed_at,
    )


def _surface_from_ce(ce: Counterexample) -> AffectedSurface:
    for action in ce.actions:
        if not isinstance(action, dict):
            continue
        method = action.get("method")
        path = action.get("path")
        if isinstance(method, str) and isinstance(path, str) and method.strip() and path.strip():
            return AffectedSurface(
                kind="endpoint",
                value=f"{method.strip().upper()} {path.strip()}",
            )
    return AffectedSurface(kind="module", value=ce.oracle_id)


def _candidate_id_for_ce(ce: Counterexample) -> str:
    digest = _canonical_sha256(
        {
            "campaign_id": ce.campaign_id,
            "counterexample_id": ce.counterexample_id,
            "obligation_ids": list(ce.obligation_ids),
            "oracle_id": ce.oracle_id,
            "seed": ce.seed,
        }
    )
    return f"CAND-{digest[:16]}"


def _candidate_from_ce(ce: Counterexample, observation: Observation) -> IssueCandidate:
    surface = _surface_from_ce(ce)
    symptom = f"oracle violation {ce.oracle_id}"
    obligations = ", ".join(ce.obligation_ids) if ce.obligation_ids else "none"
    return IssueCandidate(
        candidate_id=_candidate_id_for_ce(ce),
        observation_ids=[observation.observation_id],
        proposed=IssueCandidateProposed(
            title=f"Confirmed discovery counterexample: {ce.oracle_id}",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis=(
                f"Hard oracle {ce.oracle_id} violated under campaign {ce.campaign_id} "
                f"(obligations: {obligations}; seed={ce.seed}). "
                "Provisional product_bug pending human/reconciler authority."
            ),
        ),
        affected_surface=surface,
        fingerprint_inputs=FingerprintInputs(
            surface=surface.kind,
            symptom=symptom,
            qualifiers=[ce.campaign_id, *ce.obligation_ids] or None,
        ),
        possible_problem_ids=[],
        confidence=0.7,
        recommended_action="triage confirmed counterexample as provisional product_bug",
    )


def confirmed_ces_to_candidate_document(
    ces: Sequence[Counterexample],
    observations: Sequence[Observation],
    *,
    change_id: str,
    batch_id: str,
    evidence_bundle_digest: str,
) -> IssueCandidateDocument:
    """Build provisional ``product_bug`` candidates for confirmed CEs only.

    Authority is expressed at Occurrence time as ``llm_provisional`` by the
    reconciler; this document supplies the deterministic proposed classification.
    """
    obs_by_id = {Path(obs.source.artifact).stem: obs for obs in observations}
    candidates: list[IssueCandidate] = []
    for ce in ces:
        if ce.finding_status != "confirmed":
            continue
        obs = obs_by_id.get(ce.counterexample_id)
        if obs is None:
            raise CeBridgeError(
                "incomplete_evidence",
                f"missing Observation for confirmed counterexample {ce.counterexample_id}",
                path=counterexample_rel_path(ce),
            )
        candidates.append(_candidate_from_ce(ce, obs))

    return IssueCandidateDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        evidence_bundle_digest=evidence_bundle_digest,
        candidates=candidates,
    )


def _build_evidence_manifest(
    *,
    change_id: str,
    batch_id: str,
    evidence_paths: Sequence[str],
    change_dir: Path,
) -> IssueEvidenceManifest:
    entries: list[IssueEvidenceManifestEntry] = []
    for rel in sorted(set(evidence_paths)):
        abs_path = change_dir / rel
        if not abs_path.is_file():
            raise CeBridgeError(
                "incomplete_evidence",
                f"referenced evidence file missing: {rel}",
                path=rel,
            )
        try:
            digest = _fmt_digest(_sha256_bytes(abs_path.read_bytes()))
        except OSError as exc:
            raise CeBridgeError(
                "incomplete_evidence",
                f"referenced evidence file unreadable: {rel}",
                path=rel,
            ) from exc
        entries.append(IssueEvidenceManifestEntry(path=rel, digest=digest))

    if not entries:
        # Manifest requires min_length=1; pin an empty-anchor digest over the CE dir marker.
        # Prefer a stable empty-batch sentinel entry derived from batch identity.
        sentinel = {
            "batch_id": batch_id,
            "change_id": change_id,
            "kind": "discovery_ce_empty",
        }
        entries.append(
            IssueEvidenceManifestEntry(
                path=f"{_CE_REL_DIR}/.empty-batch",
                digest=_fmt_digest(_canonical_sha256(sentinel)),
            )
        )

    bundle_canonical = json.dumps(
        [{"digest": e.digest, "path": e.path} for e in entries],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return IssueEvidenceManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        digest=_fmt_digest(_sha256_bytes(bundle_canonical.encode("utf-8"))),
        entries=entries,
    )


def _load_counterexamples(change_dir: Path) -> tuple[list[tuple[Counterexample, str]], int]:
    """Load and validate all ``discovery/counterexamples/*.{json,yaml}`` files.

    Returns (all_valid_ces_with_relpath, skipped_non_confirmed_count). Invalid files raise.
    """
    ce_dir = change_dir / _CE_REL_DIR
    if not ce_dir.is_dir():
        return [], 0

    unique_paths = discovery_receipt_files(ce_dir)

    ces: list[tuple[Counterexample, str]] = []
    skipped = 0
    for path in unique_paths:
        rel = f"{_CE_REL_DIR}/{path.name}"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CeBridgeError(
                "unreadable_counterexample",
                f"counterexample file unreadable: {rel}",
                path=rel,
            ) from exc
        try:
            data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            kind = "JSON" if path.suffix == ".json" else "YAML"
            raise CeBridgeError(
                "invalid_counterexample",
                f"counterexample {kind} parse failed: {rel}: {exc}",
                path=rel,
            ) from exc
        if not isinstance(data, dict):
            raise CeBridgeError(
                "invalid_counterexample",
                f"counterexample must be a mapping: {rel}",
                path=rel,
            )
        try:
            ce = Counterexample.model_validate(data)
        except Exception as exc:
            raise CeBridgeError(
                "invalid_counterexample",
                f"counterexample validation failed: {rel}: {exc}",
                path=rel,
            ) from exc
        # Filename should match counterexample_id for stable evidence refs.
        allowed = {
            f"{ce.counterexample_id}.json",
            f"{ce.counterexample_id}.yaml",
            f"{ce.counterexample_id}.yml",
        }
        if path.name not in allowed:
            raise CeBridgeError(
                "incomplete_evidence",
                f"counterexample filename {path.name!r} does not match "
                f"counterexample_id {ce.counterexample_id!r}",
                path=rel,
            )
        if ce.finding_status != "confirmed":
            skipped += 1
            continue
        ces.append((ce, rel))
    return ces, skipped


def _write_json(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _empty_snapshot(change_id: str, batch_id: str) -> ChangeIssueSnapshot:
    return ChangeIssueSnapshot(
        schema_version="1.0",
        change_id=change_id,
        authoritative_batch_id=batch_id,
        observations=[],
        occurrences=[],
        analysis_status=None,
        project_sync_status="completed",
        batches=[batch_id],
    )


def _load_problems(project_root: Path) -> ProblemProjection:
    problems_path = project_root / "qa" / "issues" / "problems.json"
    if not problems_path.is_file():
        return ProblemProjection(
            schema_version="1.0",
            generated_at="1970-01-01T00:00:00Z",
            problems=[],
        )
    try:
        data = json.loads(problems_path.read_text(encoding="utf-8"))
        return ProblemProjection.model_validate(data)
    except Exception as exc:
        raise CeBridgeError(
            "incomplete_evidence",
            f"corrupt project problem projection: {exc}",
            path="qa/issues/problems.json",
        ) from exc


def ingest_confirmed_counterexamples(
    change_dir: Path,
    *,
    change_id: str,
    batch_id: str,
    project_root: Path | None = None,
    reconcile: bool = True,
    observed_at: str | None = None,
) -> CeIngestResult:
    """Scan confirmed CEs, record Observations/candidates, optionally reconcile.

    Writes under ``inspect/`` (observations, evidence manifest, candidates,
    analysis status) and appends ``ObservationRecordedEvent`` entries. When
    ``reconcile`` is true and ``project_root`` is set, runs the pure reconciler
    and appends Occurrence/Problem ledger events.
    """
    ts = observed_at or _utc_now()
    loaded, skipped = _load_counterexamples(change_dir)

    observations = [
        counterexample_to_observation(
            ce,
            change_id=change_id,
            batch_id=batch_id,
            observed_at=ts,
            artifact_rel=rel,
        )
        for ce, rel in loaded
    ]
    evidence_paths = [rel for _, rel in loaded]
    confirmed = [ce for ce, _rel in loaded]
    manifest = _build_evidence_manifest(
        change_id=change_id,
        batch_id=batch_id,
        evidence_paths=evidence_paths,
        change_dir=change_dir,
    )
    candidates_doc = confirmed_ces_to_candidate_document(
        confirmed,
        observations,
        change_id=change_id,
        batch_id=batch_id,
        evidence_bundle_digest=manifest.digest,
    )
    obs_doc = ObservationDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        observations=list(observations),
    )
    candidate_digest = candidate_document_digest(candidates_doc)
    analysis_status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        status="completed",
        evidence_bundle_digest=manifest.digest,
        candidate_count=len(candidates_doc.candidates),
        candidate_digest=candidate_digest,
    )

    inspect_dir = change_dir / "inspect"
    _write_json(inspect_dir / "observations.json", dump_projection(obs_doc))
    _write_json(inspect_dir / "issue-evidence-manifest.json", dump_projection(manifest))
    _write_json(inspect_dir / "issue-candidates.json", dump_projection(candidates_doc))
    _write_json(inspect_dir / "issue-analysis-status.json", dump_projection(analysis_status))

    issues_dir = change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)

    if observations:
        events = []
        for obs in observations:
            idem = f"observation_recorded:{change_id}:{batch_id}:{obs.observation_id}"
            events.append(
                ObservationRecordedEvent(
                    schema_version="1.0",
                    seq=1,
                    event_id=_event_id(idem),
                    idempotency_key=idem,
                    ts=ts,
                    evidence_digest=manifest.digest,
                    change_id=change_id,
                    batch_id=batch_id,
                    type="observation_recorded",
                    observation=obs,
                )
            )
        ChangeIssueStore(change_dir).append_and_rebuild(events)
    else:
        _write_json(
            issues_dir / "snapshot.json",
            dump_projection(_empty_snapshot(change_id, batch_id)),
        )

    reconcile_status_value: str | None = None
    if reconcile:
        if project_root is None:
            raise CeBridgeError(
                "incomplete_evidence",
                "project_root is required when reconcile=True",
            )
        reconcile_status_value = _reconcile_batch(
            change_dir=change_dir,
            project_root=project_root,
            change_id=change_id,
            batch_id=batch_id,
            obs_doc=obs_doc,
            candidates_doc=candidates_doc,
            manifest=manifest,
            candidate_digest=candidate_digest,
        )

    return CeIngestResult(
        batch_id=batch_id,
        observation_count=len(observations),
        candidate_count=len(candidates_doc.candidates),
        evidence_bundle_digest=manifest.digest,
        skipped_non_confirmed=skipped,
        reconcile_status=reconcile_status_value,
    )


def _reconcile_batch(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
    batch_id: str,
    obs_doc: ObservationDocument,
    candidates_doc: IssueCandidateDocument,
    manifest: IssueEvidenceManifest,
    candidate_digest: str,
) -> str:
    snapshot_path = change_dir / "issues" / "snapshot.json"
    if snapshot_path.is_file():
        try:
            change_snapshot = ChangeIssueSnapshot.model_validate(
                json.loads(snapshot_path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            raise CeBridgeError(
                "incomplete_evidence",
                f"corrupt change issue snapshot: {exc}",
                path="issues/snapshot.json",
            ) from exc
    else:
        change_snapshot = _empty_snapshot(change_id, batch_id)

    problems = _load_problems(project_root)
    inspect_dir = change_dir / "inspect"

    try:
        plan = plan_reconciliation(
            candidates_doc,
            obs_doc,
            change_snapshot,
            problems,
            manifest=manifest,
            expected_change_id=change_id,
            candidate_digest=candidate_digest,
        )
    except ReconciliationValidationError as exc:
        status = IssueReconcileStatus(
            schema_version="1.0",
            change_id=change_id,
            batch_id=batch_id,
            status="failed",
            evidence_bundle_digest=manifest.digest,
            error=str(exc),
        )
        _write_json(inspect_dir / "issue-reconcile-status.json", dump_projection(status))
        return "failed"

    ChangeIssueStore(change_dir).append_and_rebuild(list(plan.change_events))
    if plan.problem_events:
        ProjectProblemStore(project_root).append_and_rebuild(list(plan.problem_events))

    status = IssueReconcileStatus(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        status="completed",
        evidence_bundle_digest=manifest.digest,
        candidate_digest=plan.candidate_digest,
        occurrence_count=plan.occurrence_count,
    )
    _write_json(inspect_dir / "issue-reconcile-status.json", dump_projection(status))
    return "completed"


__all__ = [
    "CeBridgeError",
    "CeIngestResult",
    "confirmed_ces_to_candidate_document",
    "counterexample_rel_path",
    "counterexample_signature",
    "counterexample_to_observation",
    "ingest_confirmed_counterexamples",
]
