from __future__ import annotations

import json

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import ValidationResult

from assurance_generation.contracts import CodegenMapping
from assurance_generation.plugin import GenerationPlugin
from assurance_generation.validators.generated_files import (
    CodegenMappingValidator,
    GeneratedFilesValidator,
)
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    VALID_LEAFS,
    candidate_with,
    durable_oracle_path,
    family_test_file,
    generated_candidate,
    mapping_document,
    validation_context,
)


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_generated_files_require_exact_closed_mapping(family: str) -> None:
    extra = "qa/tests/unmapped_test.py"
    candidate = generated_candidate(family, extra_file=extra)
    result = GeneratedFilesValidator().validate(candidate, validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="generated test file is absent from the closed mapping: qa/tests/unmapped_test.py",
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_accept_exact_family_mapping(family: str) -> None:
    path = durable_oracle_path(family=family)
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file=path))
    result = GeneratedFilesValidator(family=family, mapping=mapping).validate(
        candidate_with(path), validation_context()
    )
    assert result == ValidationResult(accepted=True)


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_reject_unprefixed_sut_test_writes(family: str) -> None:
    path = durable_oracle_path(family=family)
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file=path))
    result = GeneratedFilesValidator(family=family, mapping=mapping).validate(
        candidate_with(family_test_file(family)), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None


def test_generated_files_accept_support_write_under_family_root() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    result = GeneratedFilesValidator(family="api", mapping=mapping).validate(
        candidate_with(
            durable_oracle_path(),
            "qa/tests/api/conftest.py",
        ),
        validation_context(),
    )
    assert result == ValidationResult(accepted=True)


def test_mapping_validator_accepts_support_write_under_family_root() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    result = CodegenMappingValidator(mapping=mapping).validate(
        candidate_with(
            durable_oracle_path(),
            "qa/tests/api/conftest.py",
        ),
        validation_context(),
    )
    assert result == ValidationResult(accepted=True)


def test_mapping_validator_rejects_unprefixed_sut_test_writes() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    result = CodegenMappingValidator(mapping=mapping).validate(
        candidate_with("tests/api/test_users.py"),
        validation_context(),
    )
    assert result.accepted is False
    assert result.reason is not None


def test_generated_files_accept_shared_builder_under_family_root() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    result = GeneratedFilesValidator(family="api", mapping=mapping).validate(
        candidate_with(
            durable_oracle_path(),
            "qa/tests/testdata/domain/users.py",
        ),
        validation_context(),
    )
    assert result == ValidationResult(accepted=True)


def test_generated_files_reject_nested_change_generated_suffix() -> None:
    sneak = "qa/changes/CH-DEMO-001/generated/api/files/qa/tests/api/test_users.py"
    mapping = CodegenMapping.model_validate(mapping_document("api", target_file=durable_oracle_path()))
    result = GeneratedFilesValidator(family="api", mapping=mapping).validate(
        candidate_with(sneak), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_reject_outside_family_root(family: str) -> None:
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file="qa/tests/other/app.py"))
    result = GeneratedFilesValidator(family=family, mapping=mapping).validate(
        candidate_with("qa/tests/other/app.py"), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_reject_unknown_capability_leaf(family: str) -> None:
    path = durable_oracle_path(family=family)
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file=path))
    document = {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "layer": family,
        "files": [
            {
                "repo_path": path,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": [mapping.entries[0].case_id],
                "content_sha256": "sha256:" + "a" * 64,
            }
        ],
        "mapping": mapping.model_dump(mode="json"),
        "required_capabilities": ["auth.fake"],
    }
    manifest = f"qa/results/codegen/{family}-generated-files.json"
    result = GeneratedFilesValidator(
        family=family,
        mapping=mapping,
        capability_leafs=frozenset(VALID_LEAFS),
        file_bytes={manifest: json.dumps(document).encode()},
        write_roots=(
            "qa/tests/api/",
            "qa/tests/e2e/",
            "qa/tests/fuzz/",
            "qa/tests/perf/",
            "qa/tests/testdata/",
            "qa/results/",
        ),
    ).validate(candidate_with(path, manifest), validation_context())
    assert result.accepted is False
    assert result.reason is not None
    assert "auth.fake" in result.reason


def test_mapping_validator_rejects_missing_extra_stale_and_duplicate() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    context = validation_context()
    missing = CodegenMappingValidator(mapping=mapping).validate(candidate_with(), context)
    assert missing.accepted is False
    assert missing.reason is not None
    assert "missing" in missing.reason
    extra = CodegenMappingValidator(mapping=mapping).validate(
        candidate_with(
            durable_oracle_path(),
            "qa/tests/api/test_extra.py",
        ),
        context,
    )
    assert extra.accepted is False
    assert extra.reason is not None
    assert "extra" in extra.reason
    stale = CodegenMappingValidator(
        mapping=mapping,
        file_bytes={durable_oracle_path(): b"changed-bytes"},
    ).validate(candidate_with(durable_oracle_path()), context)
    assert stale.accepted is False
    assert stale.reason is not None
    assert "stale" in stale.reason
    raw = mapping_document("api")
    raw["entries"] = [
        raw["entries"][0],
        {**raw["entries"][0], "symbol": "test_duplicate"},
    ]
    with pytest.raises(Exception, match="unique"):
        CodegenMapping.model_validate(raw)


def test_plugin_contributed_codegen_validators_allowlist_registered_paths() -> None:
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    generated = contribution.commit_validators["assurance.generation.validator.generated-files.v1"]
    mapping = contribution.commit_validators["assurance.generation.validator.codegen-mapping.v1"]
    context = validation_context()
    allowed = candidate_with(durable_oracle_path())
    assert generated.validate(allowed, context) == ValidationResult(accepted=True)
    assert mapping.validate(allowed, context) == ValidationResult(accepted=True)
    assert generated.validate(candidate_with("tests/api/test_users.py"), context).accepted is False
    assert mapping.validate(candidate_with("tests/api/test_users.py"), context).accepted is False
    assert generated.validate(candidate_with("src/app.py"), context).accepted is False
    assert mapping.validate(candidate_with("../secret.py"), context).accepted is False
