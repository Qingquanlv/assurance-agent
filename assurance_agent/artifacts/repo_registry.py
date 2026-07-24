"""Repo-level artifact path -> pydantic model registry (spec C4).

Unlike change-relative ``artifacts/registry.py``, these paths resolve from
project root (e.g. ``.aa/data-knowledge.yaml``).
"""

import re
from functools import lru_cache

from pydantic import BaseModel

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge


class RepoArtifactSpec(BaseModel):
    artifact_type: str
    pattern: str
    model: type[BaseModel]


REPO_REGISTRY: list[RepoArtifactSpec] = [
    RepoArtifactSpec(
        artifact_type="data_knowledge",
        pattern=".aa/data-knowledge.yaml",
        model=DataKnowledge,
    ),
]


@lru_cache(maxsize=None)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_repo_artifact(relpath: str) -> RepoArtifactSpec | None:
    """Return the first repo registry spec whose glob matches the project-relative path."""
    norm = relpath.replace("\\", "/")
    for spec in REPO_REGISTRY:
        if _pattern_regex(spec.pattern).match(norm):
            return spec
    return None


def resolve_repo_model(pattern: str) -> type[BaseModel] | None:
    """Resolve a registered repo artifact pattern to its pydantic model."""
    for spec in REPO_REGISTRY:
        if spec.pattern == pattern:
            return spec.model
    matched = match_repo_artifact(pattern)
    return matched.model if matched is not None else None
