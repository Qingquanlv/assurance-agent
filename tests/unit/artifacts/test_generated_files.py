import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import (
    ApiGeneratedFilesV1,
    E2eGeneratedFilesV1,
    FuzzGeneratedFilesV1,
    PerformanceGeneratedFilesV1,
)
from assurance_agent.verification.generated_files import get_generated_files_contract


def valid_generated_files_payload(*, layer: str = "api", repo_path: str = "tests/api/test_login.py") -> dict:
    return {
        "schema_version": "1",
        "change_id": "CH-123",
        "layer": layer,
        "files": [
            {
                "repo_path": repo_path,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": ["CASE_001"],
                "content_sha256": "sha256:" + "a" * 64,
            }
        ],
    }


@pytest.mark.parametrize(
    ("model", "layer", "repo_path"),
    [
        (ApiGeneratedFilesV1, "api", "tests/api/test_login.py"),
        (E2eGeneratedFilesV1, "e2e", "tests/e2e/test_login.py"),
        (FuzzGeneratedFilesV1, "fuzz", "tests/fuzz/test_login.py"),
        (PerformanceGeneratedFilesV1, "performance", "tests/perf/test_login.py"),
    ],
)
def test_layer_generated_files_accepts_only_its_literal(model: type, layer: str, repo_path: str) -> None:
    manifest = model.model_validate(valid_generated_files_payload(layer=layer, repo_path=repo_path))
    assert manifest.layer == layer
    assert canonical_json_bytes(manifest) == canonical_json_bytes(manifest)


def test_api_generated_files_rejects_e2e_layer() -> None:
    payload = valid_generated_files_payload(layer="e2e", repo_path="tests/e2e/test_login.py")
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)


@pytest.mark.parametrize(
    "bad_path", ["../tests/api/test_login.py", "tests\\api\\test_login.py", "/tests/api/x.py"]
)
def test_generated_files_reject_non_normalized_repository_paths(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(valid_generated_files_payload(repo_path=bad_path))


def test_generated_files_reject_duplicate_or_unsorted_paths() -> None:
    payload = valid_generated_files_payload()
    duplicate = payload["files"][0].copy()
    payload["files"].append(duplicate)
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)

    payload["files"] = [
        {**duplicate, "repo_path": "tests/api/test_z.py"},
        {**duplicate, "repo_path": "tests/api/test_a.py"},
    ]
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)


def test_generated_files_reject_duplicate_or_unsorted_case_ids_and_extra_fields() -> None:
    payload = valid_generated_files_payload()
    payload["files"][0]["case_ids"] = ["CASE_002", "CASE_001"]
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)

    payload["files"][0]["case_ids"] = ["CASE_001", "CASE_001"]
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)

    payload = valid_generated_files_payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)


@pytest.mark.parametrize("digest", ["a" * 64, "sha256:" + "A" * 64, "sha256:abc"])
def test_generated_files_require_prefixed_lowercase_sha256(digest: str) -> None:
    payload = valid_generated_files_payload()
    payload["files"][0]["content_sha256"] = digest
    with pytest.raises(ValidationError):
        ApiGeneratedFilesV1.model_validate(payload)


def test_generated_files_contracts_are_exact_and_unknown_layer_fails() -> None:
    contract = get_generated_files_contract("performance")
    assert contract.summary_path == "change:codegen/performance-codegen-summary.md"
    assert contract.manifest_path == "change:codegen/performance-generated-files.json"
    assert contract.private_test_root == "tests/perf"
    with pytest.raises(ValueError):
        get_generated_files_contract("unit")
