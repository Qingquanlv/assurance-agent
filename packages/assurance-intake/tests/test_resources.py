from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from agent_runtime_contracts import validate_structured_result
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_intake.contracts import CaseReviewResultV1
from assurance_intake.contracts.agent import ArtifactListResultV1
from assurance_intake.resource_loader import resource_bytes

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_intake" / "resources"
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "claude code",
    "codex",
    "gemini cli",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|claude code|\bcodex\b|gemini cli",
    re.IGNORECASE,
)


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def test_intake_resources_forbid_legacy_and_provider_names() -> None:
    hits: list[str] = []
    for path in _resource_files():
        text = path.read_text(encoding="utf-8")
        if _TOKEN.search(text):
            hits.append(path.relative_to(_RESOURCES).as_posix())
    assert hits == [], f"forbidden provider/legacy tokens in resources: {hits}"
    lowered = "\n".join(path.read_text(encoding="utf-8").lower() for path in _resource_files())
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contracts_match_capability_schemas() -> None:
    assert resource_bytes("result-contracts/case-review.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, CaseReviewResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/case-design.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/intake.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )
    assert resource_bytes("result-contracts/explore.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )


def test_artifact_list_model_and_result_contracts_reject_an_empty_receipt() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        ArtifactListResultV1.model_validate({"output_files": []})
    assert ArtifactListResultV1.model_validate(
        {"output_files": ["qa/changes/CH-1/.qa.yaml", "qa/changes/CH-1/requirement.md"]}
    ).output_files == (
        "qa/changes/CH-1/.qa.yaml",
        "qa/changes/CH-1/requirement.md",
    )

    for name in ("intake", "explore", "case-design"):
        schema = cast(
            dict[str, JSONValue],
            json.loads(resource_bytes(f"result-contracts/{name}.v1.schema.json")),
        )
        output_files = cast(dict[str, JSONValue], schema["properties"])["output_files"]
        assert isinstance(output_files, dict)
        assert output_files["minItems"] == 1
        with pytest.raises(ValueError, match="minItems"):
            validate_structured_result(
                {"output_files": []},
                schema=schema,
                schema_digest=canonical_digest(schema),
            )
