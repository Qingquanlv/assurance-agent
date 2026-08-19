"""Publish/read one execution batch.

Publication is two phases, and the split is load-bearing rather than cosmetic:

1. ``publish_target_results`` writes the per-target result JSONs into the batch
   archive and returns the ``result_files`` map.
2. ``publish_execution_manifest`` writes the summary, the quality gate and the
   manifest, then refreshes the top-level latest copies from the batch bytes.

The runner folds a trace projection *between* the two, and that fold reads the
current batch's result files. Phase 1 therefore has to complete before the gate
is built, while the manifest cannot exist until after it; keeping one combined
publisher would have meant writing the same result JSONs twice, and two writes
of one artifact within a run is two byte states a reader could observe.
``publish_execution_evidence`` composes both phases for callers that have
nothing to do in between.

Everything a batch owns therefore lands under ``runs/<batch_id>/`` before the
top-level manifest names it, and the convenience copies beside that manifest move
only afterwards: a crash can leave them stale, never contradicting the marker.

Every file is written temp-then-replace, so a reader either sees the previous
bytes or the complete new ones.

load reads the top-level manifest or an explicitly selected safe batch manifest,
loads each selected target result, and flags selected-but-missing results as
integrity issues.
"""

import json
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from assurance_agent.artifacts.batch_id import is_valid_batch_id
from assurance_agent.artifacts.models import (
    ExecutionManifest,
    QualityGateResultLike,
    SelectedTargets,
)
from assurance_agent.artifacts.paths import EXECUTION_MANIFEST_NAME, existing_with_alias
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.atomic_io import atomic_write_bytes
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    TargetResult,
)
from assurance_agent.workflow.report.quality_gate import load_quality_gate_result_file

# Result documents, in the order they are published. `coverage` trails the
# executed targets because it is derived from the api run rather than a run of
# its own; `summary` is not here because it quotes the gate verdict and so
# cannot exist until phase 2.
_RESULT_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("api", "api-result.json"),
    ("e2e", "e2e-result.json"),
    ("fuzz", "fuzz-result.json"),
    ("performance", "performance-result.json"),
    ("coverage", "coverage-result.json"),
)

# Every top-level copy a batch may own, and therefore every one that has to be
# either refreshed or removed once the authoritative manifest is published. The
# manifest is not one of them: it is published directly, and is what makes the
# rest safe to move.
_LATEST_COPIES: tuple[str, ...] = (
    *(name for _, name in _RESULT_DOCUMENTS),
    "summary.md",
    "quality-gate-result.json",
)


class EvidenceError(AaError):
    pass


class IntegrityIssue(BaseModel):
    target: str
    path: str
    reason: str


class ExecutionEvidence(BaseModel):
    manifest: ExecutionManifest
    batch_id: str
    selected_targets: SelectedTargets
    api: TargetResult | None
    e2e: TargetResult | None
    fuzz: TargetResult | None
    coverage: CoverageResult | None
    performance: PerformanceResult | None
    quality_gate: QualityGateResultLike | None
    result_paths: dict[str, str]
    integrity_issues: list[IntegrityIssue]


def _atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def _write_json(path: Path, model: BaseModel) -> None:
    _atomic_write_text(path, model.model_dump_json(indent=2))


def _write_manifest(path: Path, manifest: ExecutionManifest) -> None:
    _write_json(path, manifest)


def publish_target_results(
    *,
    execution_dir: Path,
    batch_id: str,
    api: TargetResult | None,
    e2e: TargetResult | None,
    fuzz: TargetResult | None,
    coverage: CoverageResult | None,
    performance: PerformanceResult | None,
) -> dict[str, str]:
    """Publish this batch's result documents and return the ``result_files`` map.

    Phase 1 of publication. Call this before anything that reads the batch's
    results off disk — the trace fold does — and pass the returned map to
    ``publish_execution_manifest`` rather than writing the documents again.

    Batch-scoped only. Phase 1 runs before the gate verdict exists, so a
    top-level latest copy written here would advertise a batch that no manifest
    yet names; ``publish_execution_manifest`` moves those copies once the
    authoritative marker is down.
    """
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    values: dict[str, BaseModel | None] = {
        "api": api,
        "e2e": e2e,
        "fuzz": fuzz,
        "performance": performance,
        "coverage": coverage,
    }
    result_files: dict[str, str] = {}
    for key, name in _RESULT_DOCUMENTS:
        value = values[key]
        if value is None:
            continue
        _write_json(batch_dir / name, value)
        result_files[key] = f"runs/{batch_id}/{name}"
    return result_files


def publish_execution_manifest(
    *,
    execution_dir: Path,
    change_id: str,
    batch_id: str,
    selected_targets: SelectedTargets,
    result_files: dict[str, str],
    quality_gate: QualityGateResultLike,
    summary: str,
    executed_at: datetime | None = None,
    tests_tree_sha256: str | None = None,
    test_files_sha256: dict[str, str] | None = None,
    product_tree_sha256: str | None = None,
) -> ExecutionManifest:
    """Publish the batch's summary, gate and manifest, then move the latest copies.

    Phase 2 of publication. The top-level manifest goes down before those copies
    because it is the authoritative marker: nothing beside it may claim this
    batch until it does. ``executed_at`` must be the same aware instant any
    pre-manifest trace fold was given: the fold projects the manifest onto a
    logical view keyed on that value and on ``test_files_sha256``, so publishing
    a different value — or none — makes the two folds disagree.
    """
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    execution_dir.mkdir(parents=True, exist_ok=True)

    result_files = {**result_files, "summary": f"runs/{batch_id}/summary.md"}
    _atomic_write_text(batch_dir / "summary.md", summary)
    _write_json(batch_dir / "quality-gate-result.json", quality_gate)

    manifest = ExecutionManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        executed_at=executed_at,
        selected_targets=selected_targets,
        result_files=result_files,
        tests_tree_sha256=tests_tree_sha256,
        test_files_sha256=test_files_sha256,
        product_tree_sha256=product_tree_sha256,
        final_status=quality_gate.final_status,
    )
    _write_manifest(batch_dir / "execution-manifest.json", manifest)

    _write_manifest(execution_dir / "execution-manifest.json", manifest)
    _refresh_latest_copies(execution_dir, batch_dir, result_files)
    return manifest


def _refresh_latest_copies(execution_dir: Path, batch_dir: Path, result_files: dict[str, str]) -> None:
    """Point the top-level convenience copies at the batch the manifest names.

    Called only after that manifest is on disk. Doing it earlier opens a window
    where a crash leaves a copy removed, or advanced, while the authoritative
    manifest still names the previous batch — an inconsistency a reader cannot
    detect. In this order the worst a crash leaves is a stale copy beside a
    correct manifest, and everything that resolves evidence through the manifest
    is unaffected.

    Which documents are current comes from ``result_files``, the manifest's own
    account of the batch, rather than from whatever the batch directory happens
    to contain. Batch ids are second-resolution, so two runs of one change can
    share one, and a rerun that selects fewer targets finds the previous run's
    files still in its own directory; promoting those would advertise evidence
    the manifest does not list. Anything not named is removed, so an unselected
    target cannot leave a predecessor standing as if it were current.

    Byte copies of the batch files rather than a second serialization of the same
    models, so the copy and the archive it stands for cannot drift.
    """
    current = {Path(rel).name for rel in result_files.values()} | {"quality-gate-result.json"}
    for name in _LATEST_COPIES:
        source = batch_dir / name
        pointer = execution_dir / name
        if name in current and source.is_file():
            atomic_write_bytes(pointer, source.read_bytes())
        else:
            pointer.unlink(missing_ok=True)


def write_batch_result_files(
    batch_dir: Path,
    *,
    api: TargetResult | None,
    e2e: TargetResult | None,
    fuzz: TargetResult | None,
    performance: PerformanceResult | None,
) -> None:
    """Write selected target result JSON under runs/<batch>/ before trace fold."""
    batch_dir.mkdir(parents=True, exist_ok=True)
    named: list[tuple[str, BaseModel | None]] = [
        ("api-result.json", api),
        ("e2e-result.json", e2e),
        ("fuzz-result.json", fuzz),
        ("performance-result.json", performance),
    ]
    for name, value in named:
        if value is not None:
            _write_json(batch_dir / name, value)


def publish_execution_evidence(
    *,
    execution_dir: Path,
    change_id: str,
    batch_id: str,
    selected_targets: SelectedTargets,
    api: TargetResult | None,
    e2e: TargetResult | None,
    fuzz: TargetResult | None,
    coverage: CoverageResult | None,
    performance: PerformanceResult | None,
    quality_gate: QualityGateResultLike,
    summary: str,
    executed_at: datetime | None = None,
    tests_tree_sha256: str | None = None,
    test_files_sha256: dict[str, str] | None = None,
    product_tree_sha256: str | None = None,
) -> ExecutionManifest:
    """Both phases back to back, for callers with nothing to do in between."""
    result_files = publish_target_results(
        execution_dir=execution_dir,
        batch_id=batch_id,
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        coverage=coverage,
        performance=performance,
    )
    return publish_execution_manifest(
        execution_dir=execution_dir,
        change_id=change_id,
        batch_id=batch_id,
        selected_targets=selected_targets,
        result_files=result_files,
        quality_gate=quality_gate,
        summary=summary,
        executed_at=executed_at,
        tests_tree_sha256=tests_tree_sha256,
        test_files_sha256=test_files_sha256,
        product_tree_sha256=product_tree_sha256,
    )


def _load_target(path: Path) -> TargetResult | None:
    if not path.is_file():
        return None
    try:
        return TargetResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_execution_evidence(
    execution_dir: Path,
    *,
    batch_id: str | None = None,
) -> ExecutionEvidence:
    if batch_id is not None and not is_valid_batch_id(batch_id):
        raise EvidenceError(f"unsafe execution batch id: {batch_id!r}")
    declared = (
        execution_dir / EXECUTION_MANIFEST_NAME
        if batch_id is None
        else execution_dir / "runs" / batch_id / EXECUTION_MANIFEST_NAME
    )
    manifest_path = existing_with_alias(declared)
    if manifest_path is None:
        raise EvidenceError(f"{EXECUTION_MANIFEST_NAME} not found under {execution_dir}. Run `aa run` first.")
    try:
        text = manifest_path.read_text(encoding="utf-8")
        payload = json.loads(text) if manifest_path.suffix == ".json" else yaml.safe_load(text)
        manifest = ExecutionManifest.model_validate(payload)
    except (OSError, ValueError, yaml.YAMLError) as err:
        raise EvidenceError(f"{manifest_path.name} invalid: {err}") from err

    if not is_valid_batch_id(manifest.batch_id):
        raise EvidenceError(f"unsafe manifest batch id: {manifest.batch_id!r}")
    if batch_id is not None and manifest.batch_id != batch_id:
        raise EvidenceError(
            f"execution manifest batch id mismatch: requested {batch_id}, got {manifest.batch_id}"
        )

    execution_root = execution_dir.resolve()

    def safe_path(rel: str) -> Path:
        candidate = Path(rel)
        resolved = candidate.resolve() if candidate.is_absolute() else (execution_root / candidate).resolve()
        if not resolved.is_relative_to(execution_root):
            raise EvidenceError(f"manifest result path escapes execution directory: {rel!r}")
        return resolved

    result_paths = {k: str(safe_path(v)) for k, v in manifest.result_files.items()}

    def abs_path(key: str, fallback: str) -> Path:
        rel = manifest.result_files.get(key, fallback)
        return safe_path(rel)

    api = _load_target(abs_path("api", "")) if manifest.selected_targets.api else None
    e2e = _load_target(abs_path("e2e", "")) if manifest.selected_targets.e2e else None
    fuzz = _load_target(abs_path("fuzz", "")) if manifest.selected_targets.fuzz else None

    coverage: CoverageResult | None = None
    if manifest.selected_targets.api:
        cov_path = abs_path("coverage", "")
        if cov_path.is_file():
            try:
                coverage = CoverageResult.model_validate_json(cov_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                coverage = None

    performance: PerformanceResult | None = None
    if manifest.selected_targets.performance:
        perf_path = abs_path("performance", "")
        if perf_path.is_file():
            try:
                performance = PerformanceResult.model_validate_json(perf_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                performance = None

    integrity: list[IntegrityIssue] = []
    for target, selected, result in (
        ("api", manifest.selected_targets.api, api),
        ("e2e", manifest.selected_targets.e2e, e2e),
        ("fuzz", manifest.selected_targets.fuzz, fuzz),
        ("performance", manifest.selected_targets.performance, performance),
    ):
        if selected and result is None:
            integrity.append(
                IntegrityIssue(
                    target=target,
                    path=result_paths.get(target, ""),
                    reason="selected target declared in manifest but result file is absent or unreadable",
                )
            )
        elif result is not None and (
            result.batch_id != manifest.batch_id or result.change_id != manifest.change_id
        ):
            integrity.append(
                IntegrityIssue(
                    target=target,
                    path=result_paths.get(target, ""),
                    reason="result identity mismatch with execution manifest",
                )
            )

    gate_path = execution_dir / "runs" / manifest.batch_id / "quality-gate-result.json"
    quality_gate = load_quality_gate_result_file(gate_path)
    if quality_gate is not None and (
        quality_gate.batch_id != manifest.batch_id or quality_gate.change_id != manifest.change_id
    ):
        integrity.append(
            IntegrityIssue(
                target="quality_gate",
                path=str(gate_path),
                reason="quality gate identity mismatch with execution manifest",
            )
        )

    return ExecutionEvidence(
        manifest=manifest,
        batch_id=manifest.batch_id,
        selected_targets=manifest.selected_targets,
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        coverage=coverage,
        performance=performance,
        quality_gate=quality_gate,
        result_paths=result_paths,
        integrity_issues=integrity,
    )
