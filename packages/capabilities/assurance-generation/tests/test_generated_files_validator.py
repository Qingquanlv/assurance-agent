from __future__ import annotations

import json

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import ValidationResult

from assurance_generation.contracts import CodegenMapping
from assurance_generation.plugin import GenerationPlugin
from assurance_generation.validators.generated_files import (
    CodegenFixCandidateValidator,
    CodegenMappingValidator,
    GeneratedFilesValidator,
)
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    VALID_LEAFS,
    candidate_with,
    family_test_file,
    generated_candidate,
    mapping_document,
    staged_generated_file,
    validation_context,
)


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_generated_files_require_exact_closed_mapping(family: str) -> None:
    extra = staged_generated_file(family, "tests/unmapped_test.py")
    candidate = generated_candidate(family, extra_file=extra)
    result = GeneratedFilesValidator().validate(candidate, validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="generated test file is absent from the closed mapping: tests/unmapped_test.py",
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_accept_exact_family_mapping(family: str) -> None:
    path = family_test_file(family)
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file=path))
    result = GeneratedFilesValidator(family=family, mapping=mapping).validate(
        candidate_with(staged_generated_file(family, path)), validation_context()
    )
    assert result == ValidationResult(accepted=True)


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_reject_unprefixed_sut_test_writes(family: str) -> None:
    path = family_test_file(family)
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file=path))
    result = GeneratedFilesValidator(family=family, mapping=mapping).validate(
        candidate_with(path), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None


def test_generated_files_accept_support_write_under_family_root() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    result = GeneratedFilesValidator(family="api", mapping=mapping).validate(
        candidate_with(
            staged_generated_file("api", "tests/api/test_users.py"),
            staged_generated_file("api", "tests/api/conftest.py"),
        ),
        validation_context(),
    )
    assert result == ValidationResult(accepted=True)


def test_mapping_validator_accepts_support_write_under_family_root() -> None:
    mapping = CodegenMapping.model_validate(mapping_document("api"))
    result = CodegenMappingValidator(mapping=mapping).validate(
        candidate_with(
            staged_generated_file("api", "tests/api/test_users.py"),
            staged_generated_file("api", "tests/api/conftest.py"),
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
            staged_generated_file("api", "tests/api/test_users.py"),
            staged_generated_file("api", "tests/testdata/domain/users.py"),
        ),
        validation_context(),
    )
    assert result == ValidationResult(accepted=True)


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_reject_outside_family_root(family: str) -> None:
    mapping = CodegenMapping.model_validate(mapping_document(family, target_file="src/app.py"))
    result = GeneratedFilesValidator(family=family, mapping=mapping).validate(
        candidate_with("src/app.py"), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None


@pytest.mark.parametrize("family", FAMILIES)
def test_generated_files_reject_unknown_capability_leaf(family: str) -> None:
    path = family_test_file(family)
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
    manifest = f"qa/changes/CH-DEMO-001/codegen/{family}-generated-files.json"
    result = GeneratedFilesValidator(
        family=family,
        mapping=mapping,
        capability_leafs=frozenset(VALID_LEAFS),
        file_bytes={manifest: json.dumps(document).encode()},
        write_roots=(
            "tests/api/",
            "tests/e2e/",
            "tests/fuzz/",
            "tests/perf/",
            "tests/testdata/",
            "qa/changes/",
        ),
    ).validate(candidate_with(staged_generated_file(family, path), manifest), validation_context())
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
            staged_generated_file("api"),
            staged_generated_file("api", "tests/api/test_extra.py"),
        ),
        context,
    )
    assert extra.accepted is False
    assert extra.reason is not None
    assert "extra" in extra.reason
    stale = CodegenMappingValidator(
        mapping=mapping,
        file_bytes={staged_generated_file("api"): b"changed-bytes"},
    ).validate(candidate_with(staged_generated_file("api")), context)
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


def test_committed_files_reject_two_cases_collapsed_to_one_bridge() -> None:
    target = family_test_file("api")
    raw = mapping_document("api", target_file=target)
    raw["entries"] = [
        raw["entries"][0],
        {**raw["entries"][0], "case_id": "TC_API_002"},
    ]
    mapping_path = "qa/changes/CH-DEMO-001/plans/api-codegen-mapping.json"
    result = GeneratedFilesValidator(
        family="api",
        file_bytes={mapping_path: json.dumps(raw).encode()},
        write_roots=("tests/api/", "qa/changes/"),
    ).validate(
        candidate_with(staged_generated_file("api", target), mapping_path),
        validation_context(),
    )

    assert result.accepted is False
    assert result.reason is not None
    assert "target_file" in result.reason


def test_fix_candidate_validator_authenticates_proposal_baseline_and_allowed_set() -> None:
    path = family_test_file("api")
    mapping = CodegenMapping.model_validate(mapping_document("api", target_file=path))
    context = validation_context()
    staged = staged_generated_file("api", path)
    accepted = CodegenFixCandidateValidator(
        family="api",
        baseline_tree_id="0" * 64,
        allowed_paths=(path,),
        mapping=mapping,
        approved_proposal={"status": "approved", "files_to_modify": [path]},
    ).validate(candidate_with(staged), context)
    assert accepted == ValidationResult(accepted=True)
    sut_write = CodegenFixCandidateValidator(
        family="api",
        baseline_tree_id="0" * 64,
        allowed_paths=(path,),
        mapping=mapping,
        approved_proposal={"status": "approved"},
    ).validate(candidate_with(path), context)
    assert sut_write.accepted is False
    outside = CodegenFixCandidateValidator(
        family="api",
        baseline_tree_id="0" * 64,
        allowed_paths=(path,),
        mapping=mapping,
        approved_proposal={"status": "approved"},
    ).validate(candidate_with(staged_generated_file("e2e")), context)
    assert outside.accepted is False
    unapproved = CodegenFixCandidateValidator(
        family="api",
        baseline_tree_id="0" * 64,
        allowed_paths=(path,),
        mapping=mapping,
        approved_proposal={"status": "draft"},
    ).validate(candidate_with(staged), context)
    assert unapproved.accepted is False
    drifted = CodegenFixCandidateValidator(
        family="api",
        baseline_tree_id="1" * 64,
        allowed_paths=(path,),
        mapping=mapping,
        approved_proposal={"status": "approved"},
    ).validate(candidate_with(staged), context)
    assert drifted.accepted is False


def test_plugin_contributed_codegen_validators_allowlist_registered_paths() -> None:
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    generated = contribution.commit_validators["assurance.generation.validator.generated-files.v1"]
    mapping = contribution.commit_validators["assurance.generation.validator.codegen-mapping.v1"]
    fix = contribution.commit_validators["assurance.generation.validator.codegen-fix-candidate.v1"]
    context = validation_context()
    allowed = candidate_with(staged_generated_file("api"))
    assert generated.validate(allowed, context) == ValidationResult(accepted=True)
    assert mapping.validate(allowed, context) == ValidationResult(accepted=True)
    assert fix.validate(allowed, context) == ValidationResult(accepted=True)
    assert generated.validate(candidate_with("tests/api/test_users.py"), context).accepted is False
    assert mapping.validate(candidate_with("tests/api/test_users.py"), context).accepted is False
    assert fix.validate(candidate_with("tests/api/test_users.py"), context).accepted is False
    assert generated.validate(candidate_with("src/app.py"), context).accepted is False
    assert mapping.validate(candidate_with("../secret.py"), context).accepted is False
    assert fix.validate(candidate_with("src/app.py"), context).accepted is False
