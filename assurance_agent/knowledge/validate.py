"""Repo-level and change-level data-knowledge validation (spec C4).

Models are resolved from ``artifacts/repo_registry`` (L1) and
``artifacts/registry`` (L2) — no parallel path→model mapping.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.registry import ArtifactSpec, match_artifact
from assurance_agent.artifacts.repo_registry import RepoArtifactSpec, match_repo_artifact
from assurance_agent.artifacts.validate import ArtifactResult, ValidationReport, _format_errors
from assurance_agent.change_location import resolve_change
from assurance_agent.exceptions import AaError
from assurance_agent.knowledge.merge import EXPORT_ENVELOPE_FIELDS

L1_REL_PATH = ".aa/data-knowledge.yaml"
L2_GLOB = "plans/data-knowledge.proposal.*.yaml"
L2_LAYER_ORDER = ("api", "e2e")


class KnowledgeValidationError(AaError):
    pass


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _validate_with_spec(spec: ArtifactSpec | RepoArtifactSpec, abs_path: Path, rel: str) -> ArtifactResult:
    try:
        raw = _load_yaml(abs_path)
    except yaml.YAMLError as err:
        return ArtifactResult(
            path=rel,
            artifact_type=spec.artifact_type,
            ok=False,
            errors=[f"parse error: {err}"],
        )
    if isinstance(raw, dict):
        for key in EXPORT_ENVELOPE_FIELDS:
            raw.pop(key, None)
    try:
        spec.model.model_validate(raw)
    except ValidationError as err:
        errors = _format_errors(err)
        errors.extend(_remediation_hints(err, rel))
        return ArtifactResult(path=rel, artifact_type=spec.artifact_type, ok=False, errors=errors)
    return ArtifactResult(path=rel, artifact_type=spec.artifact_type, ok=True, errors=[])


def _remediation_hints(err: ValidationError, rel: str) -> list[str]:
    hints: list[str] = []
    for issue in err.errors():
        loc = ".".join(str(part) for part in issue["loc"])
        if loc == "version" or (loc == "(root)" and "version" in issue["msg"]):
            hints.append("remediation: add top-level `version: 1`")
        if "discovered_candidates" in loc or "needs_review" in loc:
            hints.append(
                "remediation: move proposal metadata (discovered_candidates, needs_review, "
                "promotion_checklist) out of L1 into plans/data-knowledge.proposal.<layer>.yaml"
            )
        if "capabilities.auth" in loc or loc.startswith("capabilities") and "auth" in issue["msg"]:
            hints.append("remediation: move `capabilities.auth.*` entries to top-level `auth.*`")
    if hints and rel == L1_REL_PATH:
        hints.append(
            "remediation: canonical L1 requires version, accounts, auth, entities, capabilities "
            "(domain_factories/adapters/cleanup only)"
        )
    return list(dict.fromkeys(hints))


def validate_data_knowledge(project_root: Path) -> ArtifactResult:
    """Validate L1 ``.aa/data-knowledge.yaml`` using ``REPO_REGISTRY``."""
    spec = match_repo_artifact(L1_REL_PATH)
    if spec is None:
        raise KnowledgeValidationError(f"no repo registry entry for {L1_REL_PATH}")
    abs_path = project_root / L1_REL_PATH
    if not abs_path.is_file():
        return ArtifactResult(
            path=L1_REL_PATH,
            artifact_type=spec.artifact_type,
            ok=False,
            errors=["file not found"],
        )
    return _validate_with_spec(spec, abs_path, L1_REL_PATH)


def validate_proposal(path: Path, *, rel: str | None = None) -> ArtifactResult:
    """Validate a single L2 proposal file using change-relative ``REGISTRY``."""
    display = rel or path.as_posix()
    matched = match_artifact(display)
    if matched is None:
        matched = match_artifact(path.name)
    if matched is None and path.name.endswith(".proposal.yaml"):
        # Retro export-knowledge and other out-of-plans paths still use the L2 model.
        matched = match_artifact("plans/data-knowledge.proposal.api.yaml")
    if matched is None:
        return ArtifactResult(
            path=display,
            artifact_type="unregistered",
            ok=False,
            errors=[f"no registered artifact contract for {display}"],
        )
    if not path.is_file():
        return ArtifactResult(
            path=display,
            artifact_type=matched.artifact_type,
            ok=False,
            errors=["file not found"],
        )
    return _validate_with_spec(matched, path, display)


def _collect_change_proposals(change_dir: Path) -> list[tuple[str, Path]]:
    plans = change_dir / "plans"
    if not plans.is_dir():
        return []
    found: dict[str, Path] = {}
    for path in sorted(plans.glob("data-knowledge.proposal.*.yaml")):
        layer = path.name.removeprefix("data-knowledge.proposal.").removesuffix(".yaml")
        found[layer] = path
    ordered: list[tuple[str, Path]] = []
    for layer in L2_LAYER_ORDER:
        if layer in found:
            ordered.append((f"plans/{found[layer].name}", found[layer]))
    for layer in sorted(found):
        rel = f"plans/{found[layer].name}"
        if rel not in {item[0] for item in ordered}:
            ordered.append((rel, found[layer]))
    return ordered


def validate_knowledge(
    project_root: Path,
    *,
    change_id: str | None = None,
    proposal_path: Path | None = None,
) -> ValidationReport:
    """Validate L1 and optionally L2 proposal artifacts."""
    project_root = project_root.resolve()
    results: list[ArtifactResult] = [validate_data_knowledge(project_root)]

    if proposal_path is not None:
        proposal_path = proposal_path.resolve()
        try:
            rel = proposal_path.relative_to(project_root).as_posix()
        except ValueError:
            rel = proposal_path.as_posix()
        results.append(validate_proposal(proposal_path, rel=rel))
    elif change_id is not None:
        change_dir = resolve_change(project_root, change_id).path
        proposals = _collect_change_proposals(change_dir)
        if not proposals:
            results.append(
                ArtifactResult(
                    path=L2_GLOB,
                    artifact_type="data_knowledge_proposal",
                    ok=False,
                    errors=["no proposal files found"],
                )
            )
        else:
            for rel, path in proposals:
                results.append(validate_proposal(path, rel=rel))

    return ValidationReport(ok=all(r.ok for r in results), results=results)
