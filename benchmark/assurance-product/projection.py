from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator

JSONValue: TypeAlias = None | bool | int | float | str | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]

NOISE_KEYS = frozenset(
    {
        "session_id",
        "conversation",
        "timestamp",
        "timestamps",
        "duration",
        "durations",
        "created_at",
        "updated_at",
        "seq",
        "sequence",
        "event_id",
        "event_seq",
        "token_count",
        "tokens",
        "cost",
        "log",
        "logs",
        "transcript",
        "chain_of_thought",
        "process_id",
        "cursor_session",
        "opencode_session",
        "engine_state",
        "invocation_root",
    }
)


class ProjectionError(ValueError):
    """Raised when an export tree cannot be projected from files under its root."""


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


ModelT = TypeVar("ModelT", bound=ProjectionModel)


class GateDecisionV1(ProjectionModel):
    semantic_role: str
    decision: str
    input_digest: str


class ArtifactObservationV1(ProjectionModel):
    artifact_id: str
    media_type: str
    sha256: str
    semantic_projection: object

    @field_validator("semantic_projection", mode="before")
    @classmethod
    def _semantic_projection(cls, value: object) -> JSONValue:
        return freeze_json(value)


class ChangedFileV1(ProjectionModel):
    path: str
    sha256: str


class ExecutionEvidenceV1(ProjectionModel):
    summary: object
    digest: str

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: object) -> JSONValue:
        return freeze_json(value)


class QualityMetricsV1(ProjectionModel):
    coverage: object
    trace: object
    quality: object

    @field_validator("coverage", "trace", "quality", mode="before")
    @classmethod
    def _metric(cls, value: object) -> JSONValue:
        return freeze_json(value)


class IssueHealingDecisionV1(ProjectionModel):
    issue_class: str
    decision: str
    evidence_digest: str


class EffectProjectionV1(ProjectionModel):
    effect_id: str
    idempotency_key: str
    status: str
    receipt_digest: str | None
    observed_external_projection: object

    @field_validator("observed_external_projection", mode="before")
    @classmethod
    def _observed(cls, value: object) -> JSONValue:
        return freeze_json(value)


class SemanticArtifactProjectionV1(ProjectionModel):
    present: bool
    digest: str | None
    semantic_fields: object

    @field_validator("semantic_fields", mode="before")
    @classmethod
    def _semantic_fields(cls, value: object) -> JSONValue:
        return freeze_json(value)


ReportProjectionV1 = SemanticArtifactProjectionV1
RetroProjectionV1 = SemanticArtifactProjectionV1
ImprovementProjectionV1 = SemanticArtifactProjectionV1
ArchiveProjectionV1 = SemanticArtifactProjectionV1


class SemanticCountsV1(ProjectionModel):
    retries: Mapping[str, int]
    interrupts: Mapping[str, int]
    stops: Mapping[str, int]


class RedactedDiagnosticV1(ProjectionModel):
    category: str
    message: str


class BehavioralProjectionV1(ProjectionModel):
    schema_version: Literal["1"]
    case_id: str
    input_digest: str
    runtime_identity: str
    terminal_class: str
    terminal_reason_category: str | None
    selected_families: frozenset[str]
    activated_families: frozenset[str]
    completed_families: frozenset[str]
    skipped_families: frozenset[str]
    gate_decisions: tuple[GateDecisionV1, ...]
    artifact_contract: tuple[ArtifactObservationV1, ...]
    changed_files: tuple[ChangedFileV1, ...]
    execution_evidence: ExecutionEvidenceV1
    quality_metrics: QualityMetricsV1
    issue_healing_decisions: tuple[IssueHealingDecisionV1, ...]
    durable_effects: tuple[EffectProjectionV1, ...]
    report: ReportProjectionV1
    retro: RetroProjectionV1 | None
    improvement: ImprovementProjectionV1 | None
    archive: ArchiveProjectionV1 | None
    semantic_counts: SemanticCountsV1
    diagnostics: tuple[RedactedDiagnosticV1, ...]


def project_legacy_export(path: Path) -> BehavioralProjectionV1:
    return _project_export(path)


def project_new_export(path: Path) -> BehavioralProjectionV1:
    return _project_export(path)


def digest_export(root: Path) -> str:
    files = {
        relative: hashlib.sha256(path.read_bytes()).hexdigest() for path, relative in walk_export_files(root)
    }
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def digest_directory(root: Path) -> str:
    return digest_export(root)


def walk_export_files(root: Path) -> tuple[tuple[Path, str], ...]:
    export_root = _open_export_root(root)
    found: list[tuple[Path, str]] = []
    for current, dirnames, filenames in os.walk(export_root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise ProjectionError("symlink is not allowed")
        for name in list(dirnames):
            if (current_path / name).is_symlink():
                raise ProjectionError(f"symlink is not allowed: {name}")
        for name in filenames:
            child = current_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ProjectionError(f"symlink is not allowed: {name}")
            if not stat.S_ISREG(info.st_mode):
                raise ProjectionError(f"path is not a regular file: {name}")
            relative = child.relative_to(export_root).as_posix()
            if ".." in Path(relative).parts:
                raise ProjectionError("export path escapes the export root")
            found.append((child, relative))
    return tuple(sorted(found, key=lambda item: item[1]))


def freeze_json(value: object) -> JSONValue:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProjectionError("non-finite float is not allowed")
        return value
    if isinstance(value, Mapping):
        return {str(key): freeze_json(item) for key, item in value.items() if str(key) not in NOISE_KEYS}
    if isinstance(value, (list, tuple, frozenset, set)):
        return tuple(freeze_json(item) for item in value)
    raise ProjectionError(f"unsupported JSON value: {type(value)!r}")


def jsonable(value: object) -> JSONValue:
    if isinstance(value, BaseModel):
        return freeze_json(value.model_dump(mode="json"))
    if isinstance(value, frozenset):
        return tuple(sorted(str(item) for item in value))
    if isinstance(value, Mapping):
        return {
            str(key): jsonable(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple):
        return tuple(jsonable(item) for item in value)
    return freeze_json(value)


def _project_export(path: Path) -> BehavioralProjectionV1:
    root = _open_export_root(path)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ProjectionError("export root must contain manifest.json")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ProjectionError("manifest.json must contain a mapping")
    source = freeze_json(raw)
    if not isinstance(source, Mapping):
        raise ProjectionError("manifest.json must contain a mapping")
    if source.get("schema_version") != "1":
        raise ProjectionError("schema_version must be the string 1")
    return BehavioralProjectionV1(
        schema_version="1",
        case_id=_text(source, "case_id"),
        input_digest=_text(source, "input_digest"),
        runtime_identity=_text(source, "runtime_identity"),
        terminal_class=_text(source, "terminal_class"),
        terminal_reason_category=_optional_text(source.get("terminal_reason_category")),
        selected_families=_families(source.get("selected_families")),
        activated_families=_families(source.get("activated_families")),
        completed_families=_families(source.get("completed_families")),
        skipped_families=_families(source.get("skipped_families")),
        gate_decisions=_models(
            source.get("gate_decisions"), GateDecisionV1, "semantic_role", field="gate_decisions"
        ),
        artifact_contract=_models(
            source.get("artifact_contract"),
            ArtifactObservationV1,
            "artifact_id",
            field="artifact_contract",
        ),
        changed_files=_models(source.get("changed_files"), ChangedFileV1, "path", field="changed_files"),
        execution_evidence=ExecutionEvidenceV1.model_validate(source.get("execution_evidence")),
        quality_metrics=QualityMetricsV1.model_validate(source.get("quality_metrics")),
        issue_healing_decisions=_models(
            source.get("issue_healing_decisions"),
            IssueHealingDecisionV1,
            "issue_class",
            field="issue_healing_decisions",
        ),
        durable_effects=_models(
            source.get("durable_effects"), EffectProjectionV1, "effect_id", field="durable_effects"
        ),
        report=SemanticArtifactProjectionV1.model_validate(source.get("report")),
        retro=_optional_artifact(source.get("retro")),
        improvement=_optional_artifact(source.get("improvement")),
        archive=_optional_artifact(source.get("archive")),
        semantic_counts=SemanticCountsV1.model_validate(source.get("semantic_counts")),
        diagnostics=_models(source.get("diagnostics"), RedactedDiagnosticV1, "category", field="diagnostics"),
    )


def _open_export_root(path: Path) -> Path:
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise ProjectionError("export root must be a directory")
    return root


def _text(source: Mapping[str, JSONValue], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise ProjectionError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: JSONValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionError("optional text must be a string or null")
    return value


def _families(value: JSONValue) -> frozenset[str]:
    if not isinstance(value, tuple):
        raise ProjectionError("family set must be an array")
    families: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ProjectionError("family name must be a non-empty string")
        families.append(item)
    return frozenset(families)


def _models(value: JSONValue, model: type[ModelT], sort_key: str, *, field: str) -> tuple[ModelT, ...]:
    if value is None:
        raise ProjectionError(f"{field} must be an array")
    if not isinstance(value, tuple):
        raise ProjectionError(f"{field} must be an array")
    items = [model.model_validate(item) for item in value]
    return tuple(sorted(items, key=lambda item: str(getattr(item, sort_key))))


def _optional_artifact(value: JSONValue) -> SemanticArtifactProjectionV1 | None:
    if value is None:
        return None
    return SemanticArtifactProjectionV1.model_validate(value)
