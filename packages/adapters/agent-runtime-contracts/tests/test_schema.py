from __future__ import annotations

import json
import pytest
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunResult, ResultContract
from agent_runtime_contracts.wire.schema import (
    bound_redacted_diagnostics,
    canonical_digest,
    canonical_json_bytes,
    reject_credentials_in_digest_input,
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


@pytest.mark.parametrize(
    "value",
    [
        {"admin_password": "source pass 123"},
        '{"admin_password": "source-pass-123"}',
        "{'admin_password': 'source pass 123'}",
        'admin_password="source pass 123"',
    ],
)
def test_password_values_cannot_enter_transport_digest_inputs(value: object) -> None:
    with pytest.raises(ValueError, match="credential"):
        reject_credentials_in_digest_input(value)


@pytest.mark.parametrize(
    "diagnostic",
    [
        '{"admin_password": "source pass 123"}',
        "{'password': 'source pass 123'}",
        'admin_password="source pass 123"',
    ],
)
def test_password_diagnostics_redact_complete_quoted_values(diagnostic: str) -> None:
    redacted = bound_redacted_diagnostics((diagnostic,))[0]
    assert "[redacted]" in redacted
    assert "source" not in redacted
    assert "123" not in redacted


def test_password_comparison_does_not_trigger_transport_redaction() -> None:
    prose = 'The code checks admin_password == "source pass 123".'
    assert bound_redacted_diagnostics((prose,)) == (prose,)
    reject_credentials_in_digest_input({"claim": prose})


def test_canonical_encoding_is_stable_and_sorted() -> None:
    value = {"b": 2, "a": [1, {"z": True, "y": None}]}
    expected = b'{"a":[1,{"y":null,"z":true}],"b":2}'
    assert canonical_json_bytes(value) == expected
    assert canonical_digest(value) == canonical_digest({"a": [1, {"y": None, "z": True}], "b": 2})
    assert canonical_json_bytes(value) == canonical_json_bytes({"a": [1, {"y": None, "z": True}], "b": 2})


@pytest.mark.parametrize("value", ["Bearer x", "Bearer abcdefgh!suffix", "Basic x:y"])
def test_explicit_authorization_is_closed_in_diagnostics_and_digest_inputs(value: str) -> None:
    header = f"Authorization: {value}"
    assert bound_redacted_diagnostics((header,)) == ("[redacted]",)
    with pytest.raises(ValueError, match="credential"):
        reject_credentials_in_digest_input({"diagnostic": header})


@pytest.mark.parametrize("style", ["json", "repr"])
@pytest.mark.parametrize("value", ["Bearer x", "Basic x:y"])
def test_serialized_authorization_cannot_enter_digest_inputs(style: str, value: str) -> None:
    headers = {"Authorization": value}
    diagnostic = json.dumps(headers) if style == "json" else repr(headers)
    with pytest.raises(ValueError, match="credential"):
        reject_credentials_in_digest_input({"diagnostic": diagnostic})


@pytest.mark.parametrize("style", ["json", "repr"])
@pytest.mark.parametrize("value", ["Bearer x", "Basic x:y", "Bearer abcdefgh!suffix"])
def test_serialized_authorization_is_fully_redacted_in_diagnostics(style: str, value: str) -> None:
    headers = {"Authorization": value}
    diagnostic = json.dumps(headers) if style == "json" else repr(headers)
    redacted = bound_redacted_diagnostics((diagnostic,))[0]
    assert "[redacted]" in redacted
    assert value not in redacted
    assert value.split(" ", 1)[1] not in redacted


def test_quoted_authorization_comparison_is_not_a_digest_credential() -> None:
    prose = "'Authorization' == \"Bearer x\" is a comparison, not a header assignment."
    assert bound_redacted_diagnostics((prose,)) == (prose,)
    reject_credentials_in_digest_input({"claim": prose})


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
            schema_digest=canonical_digest({"$ref": "https://example.invalid/result.schema.json"}),
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
    payload = {"output_files": ["qa/proposal.md"]}
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
        delivery_mode="assistant_json_local_v1",
        schema_document=_INTAKE_RESULT_SCHEMA,
    )
    assert contract.schema_document is not None
    assert resolve_result_schema(contract.schema_digest, schema_document=contract.schema_document) == (
        _INTAKE_RESULT_SCHEMA
    )
    omitted = ResultContract(
        schema_id="fixture.result.v1",
        schema_digest=canonical_digest(_STRICT_SCHEMA),
        delivery_mode="assistant_json_local_v1",
    )
    assert "schema_document" not in omitted.model_dump(mode="json")
    with pytest.raises(ValidationError, match="digest"):
        ResultContract(
            schema_id="assurance.intake.result.intake.v1",
            schema_digest="0" * 64,
            delivery_mode="assistant_json_local_v1",
            schema_document=_INTAKE_RESULT_SCHEMA,
        )


def test_result_contract_digest_must_match_schema() -> None:
    contract = ResultContract(
        schema_id="fixture.result.v1",
        schema_digest=canonical_digest(_STRICT_SCHEMA),
        delivery_mode="assistant_json_local_v1",
    )
    assert contract.schema_digest == canonical_digest(_STRICT_SCHEMA)
    with pytest.raises(ValidationError):
        ResultContract(
            schema_id="fixture.result.v1",
            schema_digest="not-a-digest",
            delivery_mode="assistant_json_local_v1",
        )
    unsupported = {"type": "object", "unevaluatedProperties": False}
    with pytest.raises(ValidationError, match="unsupported schema keys"):
        ResultContract(
            schema_id="fixture.result.unsupported.v1",
            schema_digest=canonical_digest(unsupported),
            delivery_mode="assistant_json_local_v1",
            schema_document=unsupported,
        )


def test_agent_run_result_requires_schema_valid_structured_output() -> None:
    structured = {"status": "ok", "artifact": "result.json"}
    result = AgentRunResult.model_validate(
        {
            "result_payload": structured,
            "result_digest": canonical_digest(structured),
            "evidence_digest": "2" * 64,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    assert (
        validate_structured_result(
            result.result_payload,
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


def test_result_schema_from_model_is_derived_from_installed_result_model() -> None:
    from pydantic import BaseModel

    from agent_runtime_contracts.wire.schema import result_schema_from_model

    class ArtifactListResult(BaseModel):
        output_files: tuple[str, ...]

    schema = result_schema_from_model(ArtifactListResult)
    assert schema == ArtifactListResult.model_json_schema()
    assert canonical_digest(schema) == canonical_digest(ArtifactListResult.model_json_schema())


def test_validate_local_agent_result_digests_exact_payload_before_pydantic() -> None:
    from pydantic import BaseModel

    from agent_runtime_contracts.wire.schema import validate_local_agent_result

    class ResultWithDefault(BaseModel):
        status: str
        note: str = "filled-by-model"

    payload = {"status": "ok"}
    exact, digest, validated = validate_local_agent_result(payload, result_model=ResultWithDefault)
    assert exact == payload
    assert digest == canonical_digest(payload)
    assert digest != canonical_digest(validated.model_dump(mode="json"))
    assert validated.note == "filled-by-model"
    with pytest.raises(ValueError, match="missing required"):
        validate_local_agent_result({"note": "only-default"}, result_model=ResultWithDefault)


def test_validate_local_agent_result_forwards_feature_owned_context() -> None:
    from typing import Self

    from pydantic import BaseModel, ValidationError, ValidationInfo, model_validator

    from agent_runtime_contracts.wire.schema import validate_local_agent_result

    class LeafAwareResult(BaseModel):
        leaf: str

        @model_validator(mode="after")
        def _require_declared_leaf(self, info: ValidationInfo) -> Self:
            context = info.context or {}
            leafs = context.get("capability_leafs")
            if not isinstance(leafs, frozenset) or self.leaf not in leafs:
                raise ValueError("unknown capability leaf")
            return self

    payload = {"leaf": "e2e"}
    with pytest.raises(ValidationError, match="capability leaf"):
        validate_local_agent_result(payload, result_model=LeafAwareResult)
    exact, digest, validated = validate_local_agent_result(
        payload,
        result_model=LeafAwareResult,
        context={"capability_leafs": frozenset({"api", "e2e"})},
    )
    assert exact == payload
    assert digest == canonical_digest(payload)
    assert validated.leaf == "e2e"


def test_review_prose_is_not_a_digest_credential() -> None:
    reject_credentials_in_digest_input(
        {
            "claim": (
                'AuthControl.is_authed treats literal token == "dev" as authenticated. '
                "The API documents a Bearer scheme, not a raw cookie header."
            )
        }
    )


def test_task_optimize_path_is_not_a_digest_credential() -> None:
    reject_credentials_in_digest_input(
        {
            "project_scope": (
                "/Users/example/.codex/worktrees/task-optimize/assurance-agent/"
                ".worktrees/vue-fastapi-admin/BENCH-item"
            )
        }
    )


def test_bearer_header_is_still_a_digest_credential() -> None:
    with pytest.raises(ValueError, match="credential"):
        reject_credentials_in_digest_input("Authorization: Bearer sk-secret-canary")


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


def test_authorization_mapping_cannot_enter_digest_inputs() -> None:
    with pytest.raises(ValueError, match="credentials"):
        reject_credentials_in_digest_input({"headers": {"Authorization": "Bearer x"}})
