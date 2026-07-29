"""Publish/read one execution batch.

publish writes the append-only runs/<batch-id>/ archive plus the top-level
latest pointers; the manifest (written last) is the primary evidence marker.
load reads the top-level manifest or an explicitly selected safe batch manifest,
loads each selected target result, and flags selected-but-missing results as
integrity issues.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic.types import AwareDatetime

from assurance_agent.artifacts.models import ExecutionManifest, QualityGateResult, SelectedTargets
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    TargetResult,
)


class EvidenceError(AaError):
    pass


_BATCH_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}")


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
    quality_gate: QualityGateResult | None
    result_paths: dict[str, str]
    integrity_issues: list[IntegrityIssue]


def _write_json(path: Path, model: BaseModel) -> None:
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


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
    quality_gate: QualityGateResult,
    summary: str,
    tests_tree_sha256: str | None = None,
    test_files_sha256: dict[str, str] | None = None,
    product_tree_sha256: str | None = None,
    executed_at: AwareDatetime | None = None,
) -> ExecutionManifest:
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    execution_dir.mkdir(parents=True, exist_ok=True)

    result_files: dict[str, str] = {}
    named: list[tuple[str, str, BaseModel | None]] = [
        ("api", "api-result.json", api),
        ("e2e", "e2e-result.json", e2e),
        ("fuzz", "fuzz-result.json", fuzz),
        ("performance", "performance-result.json", performance),
        ("coverage", "coverage-result.json", coverage),
    ]
    for key, name, value in named:
        if value is None:
            continue
        _write_json(batch_dir / name, value)
        result_files[key] = f"runs/{batch_id}/{name}"

    (batch_dir / "summary.md").write_text(summary, encoding="utf-8")
    result_files["summary"] = f"runs/{batch_id}/summary.md"
    _write_json(batch_dir / "quality-gate-result.json", quality_gate)

    manifest = ExecutionManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        selected_targets=selected_targets,
        result_files=result_files,
        tests_tree_sha256=tests_tree_sha256,
        test_files_sha256=test_files_sha256,
        product_tree_sha256=product_tree_sha256,
        final_status=quality_gate.final_status,
        executed_at=executed_at,
    )
    (batch_dir / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )

    # Latest pointers (overwritten each run).
    for _key, name, value in named:
        pointer = execution_dir / name
        if pointer.exists():
            pointer.unlink()
        if value is not None:
            _write_json(pointer, value)
    (execution_dir / "summary.md").write_text(summary, encoding="utf-8")
    _write_json(execution_dir / "quality-gate-result.json", quality_gate)
    (execution_dir / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return manifest


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
    if batch_id is not None and not _BATCH_ID_RE.fullmatch(batch_id):
        raise EvidenceError(f"unsafe execution batch id: {batch_id!r}")
    manifest_path = (
        execution_dir / "execution-manifest.yaml"
        if batch_id is None
        else execution_dir / "runs" / batch_id / "execution-manifest.yaml"
    )
    if not manifest_path.is_file():
        raise EvidenceError(f"execution-manifest.yaml not found under {execution_dir}. Run `aa run` first.")
    try:
        manifest = ExecutionManifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as err:
        raise EvidenceError(f"execution-manifest.yaml invalid: {err}") from err

    if not _BATCH_ID_RE.fullmatch(manifest.batch_id):
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
    quality_gate: QualityGateResult | None = None
    if gate_path.is_file():
        try:
            quality_gate = QualityGateResult.model_validate_json(gate_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            quality_gate = None
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
