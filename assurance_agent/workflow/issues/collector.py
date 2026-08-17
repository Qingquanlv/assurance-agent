"""Deterministic Observation collector for authoritative execution batches.

Reads ``execution/execution-manifest.yaml`` (NOT filesystem mtime) as the
authoritative batch selector, loads per-target result files, and emits
``Observation`` objects for every abnormal signal.

No LLM calls are made.  Returns an immutable ``ObservationCollectionResult``
containing the observation list, a digest-pinned ``IssueEvidenceManifest``,
and the bundle digest.

Secret-redaction is applied to all evidence content before hashing so the
digest is stable across environments without leaking credentials.

Supported signal categories (v1):
- Test failures, skipped/xfail cases, and runner-level anomalies from
  API/E2E/Fuzz target results.
- Coverage gaps from ``coverage-result.json``.
- Performance regression signals from ``performance-result.json``.
- Plan/review warnings from ``review/*.json`` (``findings`` with risk ≥ medium
  or decision ≠ pass/approved).
- Healing apply summaries from ``healing/*-apply-summary.json`` (workarounds).

Not collected in v1 (noted as INCOMPLETE_SIGNALS in the returned object):
- Fact-baseline anomalies (format not yet specified).
- Explicit workaround annotations embedded in generated test source.
- Raw-log environment signals beyond the structured case fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from assurance_agent.artifacts.models.execution import ExecutionManifest
from assurance_agent.artifacts.paths import EXECUTION_MANIFEST_REL, existing_with_alias
from assurance_agent.artifacts.models.issues import (
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    Observation,
    ObservationSource,
)
from assurance_agent.evidence.digests import (
    EvidenceEntryPathError,
    evidence_bundle_digest_v1,
    read_evidence_entry_v1,
)
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.execution.results import CoverageResult, PerformanceResult, TargetResult
from assurance_agent.evidence.issue_identity import (
    ObservationIdentityInput,
    observation_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_WORKAROUND_MARKERS = frozenset(["workaround", "xfail", "known_issue", "known-issue", "skip_reason"])


def _is_workaround_skip(message: str) -> bool:
    lower = message.lower()
    return any(marker in lower for marker in _WORKAROUND_MARKERS)


def _normalize_evidence_ref(ref: str, *, change_dir: Path, change_id: str) -> str:
    """Return a durable Change-relative evidence ref and require its target to exist."""
    clean_ref, anchor, fragment = ref.partition("#")
    normalized_input = clean_ref.replace("\\", "/")
    task_marker = f"/qa/changes/{change_id}/"

    if task_marker in normalized_input:
        relative = Path(normalized_input.rsplit(task_marker, 1)[1])
    else:
        path = Path(clean_ref)
        if path.is_absolute():
            try:
                relative = path.relative_to(change_dir)
            except ValueError as exc:
                raise EvidenceError(
                    f"referenced evidence path is outside Change workspace: {clean_ref}"
                ) from exc
        else:
            relative = path

    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceError(f"invalid referenced evidence path: {clean_ref}")
    relative_ref = relative.as_posix()
    evidence_path = change_dir / relative
    if not evidence_path.is_file():
        raise EvidenceError(f"referenced evidence file missing: {relative_ref}")
    resolved_change_dir = change_dir.resolve()
    try:
        resolved_evidence = evidence_path.resolve(strict=True)
        resolved_evidence.relative_to(resolved_change_dir)
    except ValueError as exc:
        raise EvidenceError(
            f"referenced evidence path resolves outside Change workspace: {relative_ref}"
        ) from exc
    except OSError as exc:
        raise EvidenceError(f"referenced evidence file unreadable: {relative_ref}") from exc
    return f"{relative_ref}{anchor}{fragment}" if anchor else relative_ref


def _normalize_observation_evidence(
    observations: list[Observation], *, change_dir: Path, change_id: str
) -> list[Observation]:
    normalized: list[Observation] = []
    for observation in observations:
        refs = [
            _normalize_evidence_ref(ref, change_dir=change_dir, change_id=change_id)
            for ref in observation.evidence_refs
        ]
        normalized.append(observation.model_copy(update={"evidence_refs": list(dict.fromkeys(refs))}))
    return normalized


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationCollectionResult:
    """Immutable result of one Observation collection pass.

    Attributes:
        batch_id: Authoritative execution batch ID.
        observations: All abnormal Observations (empty for a clean batch).
        manifest: Digest-pinned evidence manifest covering all evidence_refs.
        evidence_bundle_digest: ``sha256:<hex>`` digest of the evidence bundle.
        incomplete_signals: Human-readable notes on signals not collected in v1.
    """

    batch_id: str
    observations: tuple[Observation, ...]
    manifest: IssueEvidenceManifest
    evidence_bundle_digest: str
    incomplete_signals: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Internal observation builders
# ---------------------------------------------------------------------------


def _make_observation(
    *,
    change_id: str,
    batch_id: str,
    kind: str,
    target: str,
    case_id: str | None,
    source_artifact: str,
    source_json_pointer: str,
    signature: str,
    evidence_refs: list[str],
    observed_at: str,
) -> Observation:
    obs_input = ObservationIdentityInput(
        change_id=change_id,
        batch_id=batch_id,
        kind=kind,
        target=target,
        case_id=case_id,
        source_artifact=source_artifact,
        source_json_pointer=source_json_pointer,
        signature=signature,
    )
    obs_id = observation_id(obs_input)
    return Observation(
        observation_id=obs_id,
        change_id=change_id,
        batch_id=batch_id,
        kind=kind,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        case_id=case_id,
        source=ObservationSource(
            artifact=source_artifact,
            json_pointer=source_json_pointer,
        ),
        evidence_refs=list(dict.fromkeys(r for r in evidence_refs if r)),
        signature=signature,
        observed_at=observed_at,
    )


def _collect_from_target_result(
    *,
    raw_data: dict,
    target: str,
    batch_id: str,
    change_id: str,
    observed_at: str,
    result_rel: str,  # path relative to change_dir
) -> list[Observation]:
    """Collect Observations from a structured target result dict."""
    observations: list[Observation] = []

    cases: list[dict] = raw_data.get("cases") or []
    unmapped: list[dict] = raw_data.get("unmapped_tests") or []
    runner_status: str = raw_data.get("status", "passed")

    def _process_case(case: dict, list_name: str, idx: int) -> None:
        status = case.get("status", "")
        json_ptr = f"/{list_name}/{idx}"
        case_id = case.get("case_id") or None

        refs = [result_rel]
        raw_log = case.get("raw_log_ref", "")
        trace = case.get("trace", "")
        screenshot = case.get("screenshot", "")
        video = case.get("video", "")
        for extra in (raw_log, trace, screenshot, video):
            if extra:
                refs.append(extra)

        if status == "failed":
            test_name = case.get("test_name") or case_id or "unknown"
            sig = f"{target} test failure {test_name}"
            observations.append(
                _make_observation(
                    change_id=change_id,
                    batch_id=batch_id,
                    kind="test_failure",
                    target=target,
                    case_id=case_id,
                    source_artifact=result_rel,
                    source_json_pointer=json_ptr,
                    signature=sig,
                    evidence_refs=refs,
                    observed_at=observed_at,
                )
            )
        elif status == "skipped":
            message = case.get("message", "")
            kind = "workaround" if _is_workaround_skip(message) else "anomaly"
            test_name = case.get("test_name") or case_id or "unknown"
            sig = f"{target} test skipped {test_name}"
            observations.append(
                _make_observation(
                    change_id=change_id,
                    batch_id=batch_id,
                    kind=kind,
                    target=target,
                    case_id=case_id,
                    source_artifact=result_rel,
                    source_json_pointer=json_ptr,
                    signature=sig,
                    evidence_refs=[result_rel],
                    observed_at=observed_at,
                )
            )

    for idx, case in enumerate(cases):
        _process_case(case, "cases", idx)
    for idx, case in enumerate(unmapped):
        _process_case(case, "unmapped_tests", idx)

    # Runner-level anomaly: result says failed but no individual case failures
    has_case_failure = any(c.get("status") == "failed" for c in [*cases, *unmapped])
    if runner_status == "failed" and not has_case_failure:
        sig = f"{target} runner anomaly"
        observations.append(
            _make_observation(
                change_id=change_id,
                batch_id=batch_id,
                kind="anomaly",
                target=target,
                case_id=None,
                source_artifact=result_rel,
                source_json_pointer="/status",
                signature=sig,
                evidence_refs=[result_rel],
                observed_at=observed_at,
            )
        )

    return observations


def _collect_coverage_signals(
    *,
    raw_data: dict,
    batch_id: str,
    change_id: str,
    observed_at: str,
    result_rel: str,
) -> list[Observation]:
    """Collect coverage gap observations from a coverage result dict."""
    observations: list[Observation] = []
    status = raw_data.get("status", "PASS")
    if status != "FAIL":
        return observations

    uncovered: list[dict] = raw_data.get("uncovered_critical_files") or []
    if uncovered:
        for idx, file_entry in enumerate(uncovered):
            file_path = str(file_entry.get("file") or "unknown_file")
            sig = f"coverage gap {file_path}"
            observations.append(
                _make_observation(
                    change_id=change_id,
                    batch_id=batch_id,
                    kind="coverage_gap",
                    target="coverage",
                    case_id=None,
                    source_artifact=result_rel,
                    source_json_pointer=f"/uncovered_critical_files/{idx}",
                    signature=sig,
                    evidence_refs=[result_rel],
                    observed_at=observed_at,
                )
            )
    else:
        # Coverage failed but no specific files listed
        line_cov = raw_data.get("line_coverage", 0.0)
        sig = f"coverage below threshold {line_cov}"
        observations.append(
            _make_observation(
                change_id=change_id,
                batch_id=batch_id,
                kind="coverage_gap",
                target="coverage",
                case_id=None,
                source_artifact=result_rel,
                source_json_pointer="/status",
                signature=sig,
                evidence_refs=[result_rel],
                observed_at=observed_at,
            )
        )
    return observations


def _collect_performance_signals(
    *,
    raw_data: dict,
    batch_id: str,
    change_id: str,
    observed_at: str,
    result_rel: str,
) -> list[Observation]:
    """Collect performance signals from a performance result dict."""
    observations: list[Observation] = []
    status = raw_data.get("status", "PASS")
    if status != "FAIL":
        return observations

    scenarios: list[dict] = raw_data.get("scenarios") or []
    if scenarios:
        for idx, scenario in enumerate(scenarios):
            scenario_name = str(
                scenario.get("name")
                or scenario.get("scenario_id")
                or scenario.get("capability")
                or f"scenario_{idx}"
            )
            verdict = scenario.get("verdict") or scenario.get("status") or "unknown"
            if str(verdict).upper() in ("PASS", "SKIPPED"):
                continue
            sig = f"performance degradation {scenario_name}"
            observations.append(
                _make_observation(
                    change_id=change_id,
                    batch_id=batch_id,
                    kind="performance_signal",
                    target="performance",
                    case_id=None,
                    source_artifact=result_rel,
                    source_json_pointer=f"/scenarios/{idx}",
                    signature=sig,
                    evidence_refs=[result_rel],
                    observed_at=observed_at,
                )
            )
    else:
        sig = "performance regression detected"
        observations.append(
            _make_observation(
                change_id=change_id,
                batch_id=batch_id,
                kind="performance_signal",
                target="performance",
                case_id=None,
                source_artifact=result_rel,
                source_json_pointer="/status",
                signature=sig,
                evidence_refs=[result_rel],
                observed_at=observed_at,
            )
        )
    return observations


_REVIEW_FILE_TARGETS: dict[str, str] = {
    "api-plan-review.json": "api",
    "plan-review.json": "e2e",
    "fuzz-plan-review.json": "api",
    "case-review.json": "api",
}
_REVIEW_POST_CODEGEN_EVIDENCE: dict[str, tuple[str, ...]] = {
    "api-plan-review.json": (
        "codegen/api-codegen-summary.md",
        "codegen/api-generated-files.json",
    ),
    "plan-review.json": (
        "codegen/e2e-codegen-summary.md",
        "codegen/e2e-generated-files.json",
    ),
    "fuzz-plan-review.json": (
        "codegen/fuzz-codegen-summary.md",
        "codegen/fuzz-generated-files.json",
    ),
    "case-review.json": (
        "codegen/api-codegen-summary.md",
        "codegen/api-generated-files.json",
    ),
    "performance-plan-review.json": (
        "codegen/performance-codegen-summary.md",
        "codegen/performance-generated-files.json",
    ),
}
_WARN_DECISIONS: frozenset[str] = frozenset(
    ["needs_fix", "needs_human_review", "changes_requested", "reject"]
)
_WARN_RISK_LEVELS: frozenset[str] = frozenset(["medium", "high", "critical"])


def _collect_review_signals(
    *,
    review_dir: Path,
    batch_id: str,
    change_id: str,
    observed_at: str,
    change_dir: Path,
) -> list[Observation]:
    """Collect review_finding observations from review/*.json files."""
    observations: list[Observation] = []
    if not review_dir.is_dir():
        return observations

    for review_file, default_target in _REVIEW_FILE_TARGETS.items():
        rpath = review_dir / review_file
        if not rpath.is_file():
            continue
        try:
            data = json.loads(rpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue

        decision = str(data.get("decision", "pass"))
        risk_level = str(data.get("risk_level") or "low")
        findings = data.get("findings") or []
        result_rel = str(rpath.relative_to(change_dir))
        post_codegen_paths = _REVIEW_POST_CODEGEN_EVIDENCE.get(review_file, ())
        post_codegen_complete = bool(post_codegen_paths) and all(
            (change_dir / relpath).is_file() for relpath in post_codegen_paths
        )
        evidence_refs = [result_rel]
        evidence_refs.extend(relpath for relpath in post_codegen_paths if (change_dir / relpath).is_file())
        if post_codegen_complete and decision not in _WARN_DECISIONS:
            # A passing plan/case review is point-in-time advisory evidence.
            # Once downstream Codegen completed, execution evidence—not the
            # pre-Codegen finding—defines the current abnormal state.
            continue

        # Emit a review_finding if decision is problematic or risk is medium+
        if decision in _WARN_DECISIONS or risk_level in _WARN_RISK_LEVELS:
            sig = f"review warning {review_file} {decision}"
            observations.append(
                _make_observation(
                    change_id=change_id,
                    batch_id=batch_id,
                    kind="review_finding",
                    target=default_target,
                    case_id=None,
                    source_artifact=result_rel,
                    source_json_pointer="/decision",
                    signature=sig,
                    evidence_refs=evidence_refs,
                    observed_at=observed_at,
                )
            )
        # Also emit per finding if there are explicit findings
        for idx, finding in enumerate(findings[:10]):  # cap at 10
            if not isinstance(finding, dict):
                continue
            finding_type = str(finding.get("type") or finding.get("category") or "warning")
            severity = str(finding.get("severity") or finding.get("risk") or "low")
            if severity in _WARN_RISK_LEVELS or finding_type == "error":
                sig = f"review finding {finding_type}"
                observations.append(
                    _make_observation(
                        change_id=change_id,
                        batch_id=batch_id,
                        kind="review_finding",
                        target=default_target,
                        case_id=None,
                        source_artifact=result_rel,
                        source_json_pointer=f"/findings/{idx}",
                        signature=sig,
                        evidence_refs=evidence_refs,
                        observed_at=observed_at,
                    )
                )

    return observations


_HEALING_APPLY_FILES: dict[str, str] = {
    "api-apply-summary.json": "api",
    "e2e-apply-summary.json": "e2e",
}


def _collect_healing_signals(
    *,
    healing_dir: Path,
    batch_id: str,
    change_id: str,
    observed_at: str,
    change_dir: Path,
) -> list[Observation]:
    """Collect workaround observations from healing/*-apply-summary.json files."""
    observations: list[Observation] = []
    if not healing_dir.is_dir():
        return observations

    for apply_file, target in _HEALING_APPLY_FILES.items():
        apath = healing_dir / apply_file
        if not apath.is_file():
            continue
        try:
            data = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("applied", False):
            continue

        result_rel = str(apath.relative_to(change_dir))
        sig = f"healing apply {target} workaround"
        observations.append(
            _make_observation(
                change_id=change_id,
                batch_id=batch_id,
                kind="workaround",
                target=target,
                case_id=None,
                source_artifact=result_rel,
                source_json_pointer="/applied",
                signature=sig,
                evidence_refs=[result_rel],
                observed_at=observed_at,
            )
        )

    # Check safety check for skip/xfail additions
    safety_path = healing_dir / "fixer-safety-check.json"
    if safety_path.is_file():
        try:
            safety = json.loads(safety_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            safety = {}
        if isinstance(safety, dict) and safety.get("skip_or_xfail_added") is True:
            result_rel = str(safety_path.relative_to(change_dir))
            sig = "healing skip or xfail added workaround"
            observations.append(
                _make_observation(
                    change_id=change_id,
                    batch_id=batch_id,
                    kind="workaround",
                    target="api",
                    case_id=None,
                    source_artifact=result_rel,
                    source_json_pointer="/skip_or_xfail_added",
                    signature=sig,
                    evidence_refs=[result_rel],
                    observed_at=observed_at,
                )
            )

    return observations


# ---------------------------------------------------------------------------
# Evidence manifest construction
# ---------------------------------------------------------------------------


def _build_evidence_manifest(
    *,
    change_id: str,
    batch_id: str,
    anchor_path: str,  # always included (e.g. execution manifest)
    observations: list[Observation],
    change_dir: Path,
) -> IssueEvidenceManifest:
    """Build a digest-pinned evidence manifest for all referenced artifacts."""
    # Collect unique paths, stripping line anchors for hashing
    all_refs: dict[str, str] = {}  # clean_path → original ref (for path field)
    all_refs[anchor_path] = anchor_path

    for obs in observations:
        for ref in obs.evidence_refs:
            clean = ref.split("#")[0]
            if clean and clean not in all_refs:
                all_refs[clean] = ref

    entries: list[IssueEvidenceManifestEntry] = []
    seen_paths: set[str] = set()
    for clean_path in sorted(all_refs):
        try:
            validated = read_evidence_entry_v1(change_dir, clean_path)
        except EvidenceEntryPathError as exc:
            raise EvidenceError(f"referenced evidence file unreadable: {clean_path}") from exc
        if validated.path in seen_paths:
            raise EvidenceError(f"duplicate evidence path: {validated.path}")
        seen_paths.add(validated.path)
        entries.append(
            IssueEvidenceManifestEntry(
                path=validated.path,
                digest=validated.entry_digest,
            )
        )

    return IssueEvidenceManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        digest=evidence_bundle_digest_v1(entries),
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def collect_observations(
    change_dir: Path,
    change_id: str,
    *,
    clock: Callable[[], str] | None = None,
) -> ObservationCollectionResult:
    """Collect Observations from the authoritative execution batch.

    Reads ``execution/execution-manifest.yaml`` as the authoritative batch
    selector (NOT filesystem mtime).  Loads per-target result files from the
    batch directory and emits Observations for every abnormal signal.

    Args:
        change_dir: Root of the Change workspace (``qa/changes/<change-id>/``).
        change_id: The Change identifier string.
        clock: Injectable UTC timestamp supplier for deterministic tests.

    Returns:
        An immutable ``ObservationCollectionResult`` value object.

    Raises:
        EvidenceError: If ``execution/execution-manifest.yaml`` is missing,
            unreadable, or contains an invalid schema.
    """
    observed_at = (clock or _utc_now)()
    execution_dir = change_dir / "execution"

    # ------------------------------------------------------------------
    # 1. Load the authoritative execution manifest (hard failure if missing)
    # ------------------------------------------------------------------
    manifest_path = existing_with_alias(execution_dir / "execution-manifest.json")
    if manifest_path is None:
        raise EvidenceError(
            f"{EXECUTION_MANIFEST_REL} not found; cannot collect observations. Run `aa run` first."
        )
    try:
        raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest = ExecutionManifest.model_validate(raw_manifest)
    except Exception as exc:
        raise EvidenceError(f"{EXECUTION_MANIFEST_REL} invalid: {exc}") from exc

    batch_id = manifest.batch_id
    # Anchor evidence path (always in manifest, relative to change_dir)
    manifest_rel = EXECUTION_MANIFEST_REL

    observations: list[Observation] = []
    incomplete_signals: list[str] = []

    # ------------------------------------------------------------------
    # 2. Collect from selected target result files
    # ------------------------------------------------------------------
    _PYTEST_TARGETS = (
        ("api", manifest.selected_targets.api),
        ("e2e", manifest.selected_targets.e2e),
        ("fuzz", manifest.selected_targets.fuzz),
    )
    for target_name, selected in _PYTEST_TARGETS:
        if not selected:
            continue
        rel = manifest.result_files.get(target_name)
        if not rel:
            raise EvidenceError(f"selected {target_name} result path missing from execution manifest")
        abs_path = execution_dir / rel
        if not abs_path.is_file():
            raise EvidenceError(f"selected {target_name} result file missing: {rel}")
        try:
            raw_data = json.loads(abs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"cannot parse selected {target_name} result: {exc}") from exc
        try:
            target_result = TargetResult.model_validate(raw_data)
        except ValueError as exc:
            raise EvidenceError(f"invalid selected {target_name} result: {exc}") from exc
        if (
            target_result.target != target_name
            or target_result.batch_id != batch_id
            or target_result.change_id != change_id
        ):
            raise EvidenceError(
                f"invalid selected {target_name} result: identity mismatch with execution manifest"
            )
        result_rel = f"execution/{rel}"
        observations.extend(
            _collect_from_target_result(
                raw_data=target_result.model_dump(mode="json"),
                target=target_name,
                batch_id=batch_id,
                change_id=change_id,
                observed_at=observed_at,
                result_rel=result_rel,
            )
        )

    # ------------------------------------------------------------------
    # 3. Coverage signals
    # ------------------------------------------------------------------
    if manifest.selected_targets.api:
        cov_rel = manifest.result_files.get("coverage")
        if cov_rel:
            cov_path = execution_dir / cov_rel
            if not cov_path.is_file():
                raise EvidenceError(f"declared coverage result file missing: {cov_rel}")
            else:
                try:
                    cov_data = CoverageResult.model_validate_json(cov_path.read_text(encoding="utf-8"))
                    if cov_data.batch_id != batch_id or cov_data.change_id != change_id:
                        raise ValueError("identity mismatch with execution manifest")
                    observations.extend(
                        _collect_coverage_signals(
                            raw_data=cov_data.model_dump(mode="json"),
                            batch_id=batch_id,
                            change_id=change_id,
                            observed_at=observed_at,
                            result_rel=f"execution/{cov_rel}",
                        )
                    )
                except (OSError, ValueError) as exc:
                    raise EvidenceError(f"invalid declared coverage result: {exc}") from exc

    # ------------------------------------------------------------------
    # 4. Performance signals
    # ------------------------------------------------------------------
    if manifest.selected_targets.performance:
        perf_rel = manifest.result_files.get("performance")
        if not perf_rel:
            raise EvidenceError("selected performance result path missing from execution manifest")
        else:
            perf_path = execution_dir / perf_rel
            if not perf_path.is_file():
                raise EvidenceError(f"selected performance result file missing: {perf_rel}")
            else:
                try:
                    perf_data = PerformanceResult.model_validate_json(perf_path.read_text(encoding="utf-8"))
                    if perf_data.batch_id != batch_id or perf_data.change_id != change_id:
                        raise ValueError("identity mismatch with execution manifest")
                    observations.extend(
                        _collect_performance_signals(
                            raw_data=perf_data.model_dump(mode="json"),
                            batch_id=batch_id,
                            change_id=change_id,
                            observed_at=observed_at,
                            result_rel=f"execution/{perf_rel}",
                        )
                    )
                except (OSError, ValueError) as exc:
                    raise EvidenceError(f"invalid selected performance result: {exc}") from exc

    # ------------------------------------------------------------------
    # 5. Plan/review warnings
    # ------------------------------------------------------------------
    review_dir = change_dir / "review"
    observations.extend(
        _collect_review_signals(
            review_dir=review_dir,
            batch_id=batch_id,
            change_id=change_id,
            observed_at=observed_at,
            change_dir=change_dir,
        )
    )

    # ------------------------------------------------------------------
    # 6. Healing apply summaries (workarounds)
    # ------------------------------------------------------------------
    healing_dir = change_dir / "healing"
    observations.extend(
        _collect_healing_signals(
            healing_dir=healing_dir,
            batch_id=batch_id,
            change_id=change_id,
            observed_at=observed_at,
            change_dir=change_dir,
        )
    )

    # ------------------------------------------------------------------
    # 7. Not collected in v1 (fact-baseline, test-source annotations)
    # ------------------------------------------------------------------
    incomplete_signals.extend(
        [
            "fact-baseline anomalies not collected in v1",
            "test-source workaround annotations not collected in v1",
        ]
    )

    # ------------------------------------------------------------------
    # 8. Normalize evidence references before freezing Observations
    # ------------------------------------------------------------------
    observations = _normalize_observation_evidence(
        observations,
        change_dir=change_dir,
        change_id=change_id,
    )

    # ------------------------------------------------------------------
    # 9. Build evidence manifest
    # ------------------------------------------------------------------
    ev_manifest = _build_evidence_manifest(
        change_id=change_id,
        batch_id=batch_id,
        anchor_path=manifest_rel,
        observations=observations,
        change_dir=change_dir,
    )

    return ObservationCollectionResult(
        batch_id=batch_id,
        observations=tuple(observations),
        manifest=ev_manifest,
        evidence_bundle_digest=ev_manifest.digest,
        incomplete_signals=tuple(incomplete_signals),
    )
