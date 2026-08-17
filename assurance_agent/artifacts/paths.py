"""Canonical machine-artifact paths (JSON) and historical YAML aliases.

Phase 6 of the validation taxonomy: new graphs write JSON. Replay and on-disk
historical changes may still carry the old YAML names. Readers prefer JSON and
fall back to the sibling YAML file; writers emit JSON only.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW_STATE_REL = "workflow-state.json"
WORKFLOW_STATE_HISTORICAL_REL = "workflow-state.yaml"

EXECUTION_MANIFEST_REL = "execution/execution-manifest.json"
EXECUTION_MANIFEST_HISTORICAL_REL = "execution/execution-manifest.yaml"
EXECUTION_MANIFEST_NAME = "execution-manifest.json"
EXECUTION_MANIFEST_HISTORICAL_NAME = "execution-manifest.yaml"

MINIMUM_COVERAGE_MATRIX_REL = "trace/minimum-coverage-matrix.json"
MINIMUM_COVERAGE_MATRIX_HISTORICAL_REL = "trace/minimum-coverage-matrix.yaml"

DISCOVERY_CAMPAIGN_SPEC_REL = "discovery/campaign-spec.json"
DISCOVERY_ORACLE_SET_REL = "discovery/oracle-set.json"
DISCOVERY_CAMPAIGN_RESULT_REL = "discovery/campaign-result.json"
DISCOVERY_CANDIDATE_NAME = "candidate.json"
DISCOVERY_CANDIDATE_HISTORICAL_NAME = "candidate.yaml"
DISCOVERY_PROMOTION_MANIFEST_NAME = "promotion-manifest.json"
DISCOVERY_PROMOTION_MANIFEST_HISTORICAL_NAME = "promotion-manifest.yaml"

# Registry historical globs. New specs use the JSON equivalent.
HISTORICAL_YAML_PATTERNS: tuple[str, ...] = (
    "plans/*-codegen-mapping.yaml",
    "execution/execution-manifest.yaml",
    "trace/minimum-coverage-matrix.yaml",
    "workflow-state.yaml",
    "discovery/campaign-spec.yaml",
    "discovery/oracle-set.yaml",
    "discovery/counterexamples/*.yaml",
    "discovery/campaign-result.yaml",
    "discovery/candidates/*/candidate.yaml",
    "discovery/candidates/*/promotion-manifest.yaml",
)

_EXACT_BASENAME_PAIRS: tuple[tuple[str, str], ...] = (
    ("workflow-state.yaml", "workflow-state.json"),
    ("execution-manifest.yaml", "execution-manifest.json"),
    ("minimum-coverage-matrix.yaml", "minimum-coverage-matrix.json"),
    ("campaign-spec.yaml", "campaign-spec.json"),
    ("oracle-set.yaml", "oracle-set.json"),
    ("campaign-result.yaml", "campaign-result.json"),
    ("candidate.yaml", "candidate.json"),
    ("promotion-manifest.yaml", "promotion-manifest.json"),
)
_YAML_TO_JSON_NAME: dict[str, str] = dict(_EXACT_BASENAME_PAIRS)
_JSON_TO_YAML_NAME: dict[str, str] = {json_name: yaml_name for yaml_name, json_name in _EXACT_BASENAME_PAIRS}


def codegen_mapping_rel(layer: str) -> str:
    return f"plans/{layer}-codegen-mapping.json"


def codegen_mapping_historical_rel(layer: str) -> str:
    return f"plans/{layer}-codegen-mapping.yaml"


def codegen_mapping_logical(layer: str) -> str:
    return f"change:{codegen_mapping_rel(layer)}"


def historical_json_pattern(yaml_pattern: str) -> str:
    if not yaml_pattern.endswith(".yaml"):
        raise ValueError(f"historical pattern must end with .yaml: {yaml_pattern!r}")
    return yaml_pattern[: -len(".yaml")] + ".json"


def alternate_name(path: Path) -> str | None:
    """Return the sibling wire filename for a converted machine artifact, if any."""
    name = path.name
    mapped = _YAML_TO_JSON_NAME.get(name) or _JSON_TO_YAML_NAME.get(name)
    if mapped is not None:
        return mapped
    if name.endswith("-codegen-mapping.yaml"):
        return name[: -len(".yaml")] + ".json"
    if name.endswith("-codegen-mapping.json"):
        return name[: -len(".json")] + ".yaml"
    parent = path.parent
    if parent.name == "counterexamples" and parent.parent.name == "discovery":
        if name.endswith(".yaml"):
            return name[: -len(".yaml")] + ".json"
        if name.endswith(".json"):
            return name[: -len(".json")] + ".yaml"
    return None


def alternate_rel(rel: str) -> str | None:
    name = alternate_name(Path(rel.replace("\\", "/")))
    if name is None:
        return None
    parent = Path(rel.replace("\\", "/")).parent
    return name if parent.as_posix() in {".", ""} else f"{parent.as_posix()}/{name}"


def existing_with_alias(path: Path) -> Path | None:
    """Prefer ``path`` when it exists; otherwise the historical/canonical sibling."""
    if path.is_file():
        return path
    other = alternate_name(path)
    if other is None:
        return None
    sibling = path.with_name(other)
    return sibling if sibling.is_file() else None


def require_existing(path: Path, *, missing: str | None = None) -> Path:
    found = existing_with_alias(path)
    if found is None:
        raise FileNotFoundError(missing or str(path))
    return found


def discovery_receipt_files(directory: Path) -> list[Path]:
    """JSON receipts first; historical YAML kept when no JSON sibling exists."""
    if not directory.is_dir():
        return []
    chosen: dict[str, Path] = {}
    for pattern in ("*.json", "*.yaml", "*.yml"):
        for path in sorted(directory.glob(pattern)):
            chosen.setdefault(path.stem, path)
    return [chosen[key] for key in sorted(chosen)]


def candidate_rels(rel: str) -> tuple[str, ...]:
    """Search order for a declared relative path: JSON first, then historical YAML."""
    norm = rel.replace("\\", "/")
    alt = alternate_rel(norm)
    if alt is None:
        return (norm,)
    json_rel = norm if norm.endswith(".json") else alt
    yaml_rel = alt if norm.endswith(".json") else norm
    if json_rel == yaml_rel:
        return (norm,)
    return (json_rel, yaml_rel)
