"""Deterministic artifact validation for qa/changes/<id>/ (spec 4a).

Registered artifacts are validated deterministically. Free-form files remain
outside schema validation, but an explicit unregistered `--artifact` and a scan
that finds no registered artifact both fail closed so CI cannot report a false
green. A requested-but-missing file is also a validation failure. Phase
filtering matches artifacts against the phase's `produces` declarations.

M3 seam: `schema` accepts any object implementing WorkflowSchemaLike; the M3
WorkflowSchema loader must implement phase_produces(). Until then (and as the
default fallback) the packaged workflow-schema.yaml is read with a thin YAML
pass that only extracts phases[].id and phases[].produces — no M3 semantics.
"""

import json
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ValidationError

from assurance_agent import resources
from assurance_agent.artifacts.registry import ArtifactSpec, match_artifact
from assurance_agent.change_location import ChangeNotFoundError
from assurance_agent.exceptions import AaError

# Re-export for callers that imported from artifacts.validate
__all__ = [
    "ChangeNotFoundError",
    "UnknownPhaseError",
    "ValidationReport",
    "ArtifactResult",
    "validate_change",
]


class UnknownPhaseError(AaError):
    pass


class WorkflowSchemaLike(Protocol):
    def phase_produces(self, phase_id: str) -> list[str] | None:
        """Return the phase's produces list, or None when the phase is unknown."""
        ...


class ArtifactResult(BaseModel):
    path: str
    artifact_type: str
    ok: bool
    errors: list[str]


class ValidationReport(BaseModel):
    ok: bool
    results: list[ArtifactResult]


def validate_change(
    change_dir: Path,
    phase: str | None = None,
    artifact: str | None = None,
    schema: WorkflowSchemaLike | None = None,
) -> ValidationReport:
    if not change_dir.is_dir():
        raise ChangeNotFoundError(f"change directory not found: {change_dir}")

    if artifact is not None:
        rel_paths = [artifact.replace("\\", "/")]
    elif phase is not None:
        produces = schema.phase_produces(phase) if schema is not None else _packaged_phase_produces(phase)
        if produces is None:
            raise UnknownPhaseError(f"unknown phase '{phase}'")
        rel_paths = [
            rel
            for rel in _collect_artifact_paths(change_dir)
            if any(_belongs_to_produce(rel, produce) for produce in produces)
        ]
    else:
        rel_paths = _collect_artifact_paths(change_dir)

    results: list[ArtifactResult] = []
    for rel in rel_paths:
        abs_path = change_dir / rel
        spec = match_artifact(rel)
        if not abs_path.is_file():
            results.append(
                ArtifactResult(
                    path=rel,
                    artifact_type=spec.artifact_type if spec else "unknown",
                    ok=False,
                    errors=["file not found"],
                )
            )
            continue
        if spec is None:
            # Explicit --artifact must never become a successful no-op.
            results.append(
                ArtifactResult(
                    path=rel,
                    artifact_type="unregistered",
                    ok=False,
                    errors=["no registered artifact contract"],
                )
            )
            continue
        results.append(_validate_file(spec, abs_path, rel))

    if not results:
        results.append(
            ArtifactResult(
                path=f"(phase: {phase})" if phase is not None else "(change)",
                artifact_type="unregistered",
                ok=False,
                errors=["no registered artifacts found"],
            )
        )

    return ValidationReport(ok=all(r.ok for r in results), results=results)


def _collect_artifact_paths(change_dir: Path) -> list[str]:
    out: list[str] = []
    for path in change_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(change_dir).as_posix()
        if match_artifact(rel) is not None:
            out.append(rel)
    return sorted(out)


def _belongs_to_produce(rel: str, produce: str) -> bool:
    """Mirror the TS artifactBelongsToProduce: registered produces match exactly,
    directory produces (trailing slash) match by prefix, others match exactly."""
    prod = produce.replace("\\", "/")
    if match_artifact(prod) is not None:
        return rel == prod
    if prod.endswith("/"):
        return rel.startswith(prod)
    return rel == prod


def _packaged_phase_produces(phase_id: str) -> list[str] | None:
    doc = yaml.safe_load(resources.read_text("schemas", "workflow-schema.yaml"))
    for entry in doc.get("phases", []):
        if entry.get("id") == phase_id:
            return [str(p) for p in entry.get("produces", [])]
    return None


def _validate_file(spec: ArtifactSpec, abs_path: Path, rel: str) -> ArtifactResult:
    text = abs_path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text) if abs_path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as err:
        return ArtifactResult(
            path=rel, artifact_type=spec.artifact_type, ok=False, errors=[f"parse error: {err}"]
        )
    try:
        spec.model.model_validate(raw)
    except ValidationError as err:
        return ArtifactResult(
            path=rel, artifact_type=spec.artifact_type, ok=False, errors=_format_errors(err)
        )
    return ArtifactResult(path=rel, artifact_type=spec.artifact_type, ok=True, errors=[])


def _format_errors(err: ValidationError) -> list[str]:
    out: list[str] = []
    for issue in err.errors():
        loc = ".".join(str(part) for part in issue["loc"]) or "(root)"
        out.append(f"{loc}: {issue['msg']}")
    return out
