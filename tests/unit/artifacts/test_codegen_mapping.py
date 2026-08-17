import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import CodegenMapping
from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.verification.generated_entries import (
    extract_layer_mapping,
    mapped_case_ids_for_path,
)


def _cases(*case_ids: str, case_type: str = "API") -> list[dict[str, object]]:
    return [
        {
            "added": [
                {
                    "case_id": case_id,
                    "type": case_type,
                    "automation": {"required": True},
                }
                for case_id in case_ids
            ],
            "modified": [],
        }
    ]


def _mapping(*, layer: str = "api", extra: list[dict[str, str]] | None = None) -> dict[str, object]:
    entries = [
        {
            "case_id": "API_001",
            "symbol": "test_api_001",
            "target_file": "tests/api/test_a.py",
        }
    ]
    if extra:
        entries.extend(extra)
    return {"schema_version": "1", "layer": layer, "entries": entries}


def test_codegen_mapping_is_registered_must_compat() -> None:
    spec = match_artifact("plans/api-codegen-mapping.json")
    assert spec is not None
    assert spec.artifact_type == "codegen_mapping_v1"
    assert spec.compat == "must_compat"
    assert spec.model is CodegenMapping
    assert spec.wire == "json"
    assert match_artifact("plans/e2e-codegen-mapping.json") is not None
    assert match_artifact("plans/fuzz-codegen-mapping.json") is not None
    assert match_artifact("plans/performance-codegen-mapping.json") is not None
    assert match_artifact("plans/api-codegen-mapping.json") is spec


def test_codegen_mapping_rejects_empty_entries() -> None:
    with pytest.raises(ValidationError):
        CodegenMapping.model_validate({"schema_version": "1", "layer": "api", "entries": []})


def test_extract_layer_mapping_prefers_structured_document() -> None:
    relation = extract_layer_mapping(
        layer="api",
        plan_text="# stale markdown without a mapping table\n",
        cases=_cases("API_001", "API_002"),
        mapping=_mapping(
            extra=[
                {
                    "case_id": "API_002",
                    "symbol": "test_api_002",
                    "target_file": "tests/api/test_b.py",
                }
            ]
        ),
    )
    assert [entry.case_id for entry in relation.entries] == ["API_001", "API_002"]
    assert mapped_case_ids_for_path(relation, "tests/api/test_a.py") == ("API_001",)


def test_extract_layer_mapping_rejects_layer_mismatch() -> None:
    with pytest.raises(Exception, match="layer"):
        extract_layer_mapping(
            layer="e2e",
            plan_text="",
            cases=_cases("API_001", case_type="E2E"),
            mapping=_mapping(layer="api"),
        )


def test_fuzz_structured_mapping_requires_schema_case_alignment() -> None:
    mapping = {
        "schema_version": "1",
        "layer": "fuzz",
        "entries": [
            {
                "case_id": "FUZZ_001",
                "symbol": "test_fuzz_001",
                "target_file": "tests/fuzz/test_a.py",
            }
        ],
        "schema_case_ids": ["FUZZ_002"],
    }
    with pytest.raises(Exception, match="Schema Acquisition"):
        extract_layer_mapping(
            layer="fuzz",
            plan_text="",
            cases=_cases("FUZZ_001", case_type="Fuzz"),
            mapping=mapping,
        )
