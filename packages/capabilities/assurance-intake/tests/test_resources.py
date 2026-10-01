from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import cast

import pytest
from pydantic import ValidationError

from agent_runtime_contracts import validate_structured_result
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_intake.contracts import CaseReviewResultV1
from assurance_intake.domain.artifacts import ArtifactListResultV1
from assurance_intake.ops import router
from assurance_intake.plugin import IntakePlugin

resource_bytes = router.resource_bytes
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


def _resource_files() -> Iterator[str]:
    spec = IntakePlugin.spec
    yield from sorted({*spec.resource_files.values(), *spec.schema_files.values()})


def test_intake_resources_forbid_legacy_and_provider_names() -> None:
    texts = {relative: resource_bytes(relative).decode("utf-8") for relative in _resource_files()}
    assert len(texts) == 22
    hits = [relative for relative, text in texts.items() if _TOKEN.search(text)]
    assert hits == [], f"forbidden provider/legacy tokens in resources: {hits}"
    lowered = "\n".join(text.lower() for text in texts.values())
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contracts_match_capability_schemas() -> None:
    assert resource_bytes("ops/case_review/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, CaseReviewResultV1.model_json_schema())
    )
    assert resource_bytes("ops/case_design/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )
    assert resource_bytes("ops/case_repair/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )
    assert resource_bytes("ops/intake/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )
    assert resource_bytes("ops/explore/result.schema.json") == canonical_json_bytes(
        cast(JSONValue, ArtifactListResultV1.model_json_schema())
    )


def test_artifact_list_model_and_result_contracts_reject_an_empty_receipt() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        ArtifactListResultV1.model_validate({"output_files": []})
    assert ArtifactListResultV1.model_validate(
        {"output_files": ["qa/.qa.yaml", "qa/requirement.md"]}
    ).output_files == (
        "qa/.qa.yaml",
        "qa/requirement.md",
    )

    for name in ("intake", "explore", "case_design", "case_repair"):
        schema = cast(
            dict[str, JSONValue],
            json.loads(resource_bytes(f"ops/{name}/result.schema.json")),
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
