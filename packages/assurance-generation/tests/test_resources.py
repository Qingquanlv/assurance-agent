from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_generation.contracts import PlanReviewAuthoring
from assurance_generation.contracts.plans import PlanResultV1
from assurance_generation.resource_loader import resource_bytes

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_generation" / "resources"
_REQUIRED = (
    "skills/aa-api-plan/SKILL.md",
    "skills/aa-api-plan-reviewer/SKILL.md",
    "skills/aa-e2e-plan/SKILL.md",
    "skills/aa-e2e-plan-reviewer/SKILL.md",
    "skills/aa-fuzz-plan/SKILL.md",
    "skills/aa-fuzz-plan-reviewer/SKILL.md",
    "skills/aa-performance-plan/SKILL.md",
    "skills/aa-performance-plan-reviewer/SKILL.md",
    "personas/test-author.md",
    "personas/reviewer.md",
    "result-contracts/plan.v1.schema.json",
    "result-contracts/plan-review.v1.schema.json",
)
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "workflow-state.json",
    "aa risk",
    "claude code",
    "codex",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|workflow-state\.json|aa risk|claude code|\bcodex\b",
    re.IGNORECASE,
)


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def test_generation_resources_forbid_legacy_and_provider_names() -> None:
    missing = [item for item in _REQUIRED if not (_RESOURCES / item).is_file()]
    assert missing == [], f"missing generation resources: {missing}"
    hits: list[str] = []
    for path in _resource_files():
        if _TOKEN.search(path.read_text(encoding="utf-8")):
            hits.append(path.relative_to(_RESOURCES).as_posix())
    assert hits == [], f"forbidden provider/legacy tokens in resources: {hits}"
    lowered = "\n".join(path.read_text(encoding="utf-8").lower() for path in _resource_files())
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contracts_match_capability_schemas() -> None:
    assert resource_bytes("result-contracts/plan.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, PlanResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/plan-review.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, PlanReviewAuthoring.model_json_schema())
    )
