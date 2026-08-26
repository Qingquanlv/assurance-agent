from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunResult, ResultContract
from agent_runtime_contracts.schema import (
    bound_redacted_diagnostics,
    canonical_digest,
    canonical_json_bytes,
    resolve_result_schema,
    validate_structured_result,
)


_STRICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "artifact"],
    "properties": {
        "status": {"type": "string", "const": "ok"},
        "artifact": {"type": "string"},
    },
}


def test_canonical_encoding_is_stable_and_sorted() -> None:
    value = {"b": 2, "a": [1, {"z": True, "y": None}]}
    expected = b'{"a":[1,{"y":null,"z":true}],"b":2}'
    assert canonical_json_bytes(value) == expected
    assert canonical_digest(value) == canonical_digest({"a": [1, {"y": None, "z": True}], "b": 2})
    assert canonical_json_bytes(value) == canonical_json_bytes({"a": [1, {"y": None, "z": True}], "b": 2})


def test_validate_structured_result_accepts_exact_strict_schema() -> None:
    schema_digest = canonical_digest(_STRICT_SCHEMA)
    payload = {"status": "ok", "artifact": "result.json"}
    assert (
        validate_structured_result(
            payload,
            schema=_STRICT_SCHEMA,
            schema_digest=schema_digest,
        )
        == payload
    )


def test_validate_structured_result_rejects_extra_properties_and_bad_digest() -> None:
    schema_digest = canonical_digest(_STRICT_SCHEMA)
    with pytest.raises(ValueError, match="additional"):
        validate_structured_result(
            {"status": "ok", "artifact": "result.json", "tokens": 3},
            schema=_STRICT_SCHEMA,
            schema_digest=schema_digest,
        )
    with pytest.raises(ValueError, match="digest"):
        validate_structured_result(
            {"status": "ok", "artifact": "result.json"},
            schema=_STRICT_SCHEMA,
            schema_digest="0" * 64,
        )


def test_validate_structured_result_honors_authenticated_open_schema() -> None:
    open_schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {"status": {"type": "string"}},
    }
    payload = {"status": "ok", "provider_extension": {"accepted": True}}
    assert (
        validate_structured_result(
            payload,
            schema=open_schema,
            schema_digest=canonical_digest(open_schema),
        )
        == payload
    )


def test_validate_structured_result_rejects_unsupported_remote_reference() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_structured_result(
            {"status": "ok"},
            schema={"$ref": "https://example.invalid/result.schema.json"},
            schema_digest=canonical_digest(
                {"$ref": "https://example.invalid/result.schema.json"}
            ),
        )


def test_validate_structured_result_supports_product_schema_dialect() -> None:
    schema = {
        "$defs": {
            "Entry": {
                "additionalProperties": False,
                "properties": {
                    "case_id": {"pattern": "^[A-Z0-9_]+$", "type": "string"},
                    "note": {
                        "anyOf": [{"minLength": 1, "type": "string"}, {"type": "null"}],
                        "default": None,
                    },
                    "trace": {
                        "additionalProperties": True,
                        "minProperties": 1,
                        "type": "object",
                    },
                },
                "required": ["case_id", "trace"],
                "type": "object",
            }
        },
        "additionalProperties": False,
        "prompt_notes": ["Return complete authoring fields"],
        "properties": {
            "entries": {
                "items": {"$ref": "#/$defs/Entry"},
                "minItems": 1,
                "type": "array",
            },
            "payload": {"items": {}, "type": "array"},
        },
        "required": ["entries", "payload"],
        "type": "object",
    }
    payload = {
        "entries": [{"case_id": "TC_CASE_001", "note": None, "trace": {"leaf": 1}}],
        "payload": [{"arbitrary": [1, True, None]}],
    }
    digest = canonical_digest(schema)

    assert validate_structured_result(payload, schema=schema, schema_digest=digest) == payload
    with pytest.raises(ValueError, match="pattern"):
        validate_structured_result(
            {**payload, "entries": [{"case_id": "bad-id", "trace": {}}]},
            schema=schema,
            schema_digest=digest,
        )
    with pytest.raises(ValueError, match="additional"):
        validate_structured_result(
            {**payload, "entries": [{"case_id": "TC_OK", "trace": {}, "extra": True}]},
            schema=schema,
            schema_digest=digest,
        )
    with pytest.raises(ValueError, match="minProperties"):
        validate_structured_result(
            {**payload, "entries": [{"case_id": "TC_OK", "trace": {}}]},
            schema=schema,
            schema_digest=digest,
        )


def test_validate_structured_result_enforces_exclusive_minimum() -> None:
    schema = {"exclusiveMinimum": 0, "type": "number"}
    digest = canonical_digest(schema)

    assert validate_structured_result(0.5, schema=schema, schema_digest=digest) == 0.5
    with pytest.raises(ValueError, match="exclusiveMinimum"):
        validate_structured_result(0, schema=schema, schema_digest=digest)


_LIVE_FIXTURE_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "artifact": {"type": "string"},
        "status": {"const": "ok", "type": "string"},
    },
    "required": ["artifact", "status"],
    "type": "object",
}
_LIVE_FIXTURE_SCHEMA_DIGEST = "56e341c9d4dd6ccaac2bc2038dad7539ff03d3895c3d66d02b24172d4ba9141e"


def test_resolve_result_schema_includes_phase3_live_fixture() -> None:
    assert canonical_digest(_LIVE_FIXTURE_SCHEMA) == _LIVE_FIXTURE_SCHEMA_DIGEST
    assert resolve_result_schema(_LIVE_FIXTURE_SCHEMA_DIGEST) == _LIVE_FIXTURE_SCHEMA


_INTAKE_RESULT_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "output_files": {
            "items": {"title": "Output Files", "type": "string"},
            "type": "array",
        }
    },
    "required": ["output_files"],
    "title": "ArtifactListResultV1",
    "type": "object",
}


def test_product_result_schema_is_not_in_the_fixture_registry() -> None:
    digest = canonical_digest(_INTAKE_RESULT_SCHEMA)
    with pytest.raises(ValueError, match="result schema is missing"):
        resolve_result_schema(digest)


def test_resolve_result_schema_accepts_authenticated_document() -> None:
    digest = canonical_digest(_INTAKE_RESULT_SCHEMA)
    assert resolve_result_schema(digest, schema_document=_INTAKE_RESULT_SCHEMA) == _INTAKE_RESULT_SCHEMA
    with pytest.raises(ValueError, match="digest"):
        resolve_result_schema("0" * 64, schema_document=_INTAKE_RESULT_SCHEMA)


def test_validate_structured_result_allows_title_and_description_annotations() -> None:
    schema = {
        **_INTAKE_RESULT_SCHEMA,
        "description": "intake artifact list",
    }
    payload = {"output_files": ["qa/changes/CH-1/proposal.md"]}
    assert (
        validate_structured_result(
            payload,
            schema=schema,
            schema_digest=canonical_digest(schema),
        )
        == payload
    )


def test_result_contract_carries_schema_document_when_digest_matches() -> None:
    digest = canonical_digest(_INTAKE_RESULT_SCHEMA)
    contract = ResultContract(
        schema_id="assurance.intake.result.intake.v1",
        schema_digest=digest,
        extraction_mode="structured",
        schema_document=_INTAKE_RESULT_SCHEMA,
    )
    assert contract.schema_document is not None
    assert resolve_result_schema(contract.schema_digest, schema_document=contract.schema_document) == (
        _INTAKE_RESULT_SCHEMA
    )
    omitted = ResultContract(
        schema_id="fixture.result.v1",
        schema_digest=canonical_digest(_STRICT_SCHEMA),
        extraction_mode="structured",
    )
    assert "schema_document" not in omitted.model_dump(mode="json")
    with pytest.raises(ValidationError, match="digest"):
        ResultContract(
            schema_id="assurance.intake.result.intake.v1",
            schema_digest="0" * 64,
            extraction_mode="structured",
            schema_document=_INTAKE_RESULT_SCHEMA,
        )


def test_result_contract_digest_must_match_schema() -> None:
    contract = ResultContract(
        schema_id="fixture.result.v1",
        schema_digest=canonical_digest(_STRICT_SCHEMA),
        extraction_mode="structured",
    )
    assert contract.schema_digest == canonical_digest(_STRICT_SCHEMA)
    with pytest.raises(ValidationError):
        ResultContract(
            schema_id="fixture.result.v1",
            schema_digest="not-a-digest",
            extraction_mode="structured",
        )
    unsupported = {"type": "object", "unevaluatedProperties": False}
    with pytest.raises(ValidationError, match="unsupported schema keys"):
        ResultContract(
            schema_id="fixture.result.unsupported.v1",
            schema_digest=canonical_digest(unsupported),
            extraction_mode="structured",
            schema_document=unsupported,
        )


def test_agent_run_result_requires_schema_valid_structured_output() -> None:
    structured = {"status": "ok", "artifact": "result.json"}
    result = AgentRunResult.model_validate(
        {
            "structured_result": structured,
            "result_digest": canonical_digest(structured),
            "evidence_digest": "2" * 64,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    assert (
        validate_structured_result(
            result.structured_result,
            schema=_STRICT_SCHEMA,
            schema_digest=canonical_digest(_STRICT_SCHEMA),
        )
        == structured
    )
    with pytest.raises(ValueError, match="additional"):
        validate_structured_result(
            {**structured, "cost": 1.5},
            schema=_STRICT_SCHEMA,
            schema_digest=canonical_digest(_STRICT_SCHEMA),
        )


def test_bound_redacted_diagnostics_redact_before_limiting() -> None:
    messages = bound_redacted_diagnostics(
        (
            "cookie=secret; Authorization: Bearer sk-secret-canary",
            "x" * 512,
        )
    )
    assert "sk-secret-canary" not in messages[0]
    assert "[redacted]" in messages[0]
    assert len(messages[1]) <= 240
    with pytest.raises(ValueError, match="bound"):
        bound_redacted_diagnostics(tuple(f"note-{index}" for index in range(17)))
