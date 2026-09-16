from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from assurance_generation.contracts import CodegenMapping, CodegenResultV1, GeneratedFilesV1
from assurance_generation.validators.generated_files import GeneratedFilesValidator
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    VALID_LEAFS,
    candidate_with,
    durable_oracle_path,
    family_case_id,
    family_symbol,
    mapping_document,
    validation_context,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SHA = "a" * 64


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _valid_files(family: str) -> dict[str, Any]:
    path = durable_oracle_path(family=family)
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "files": [
            {
                "repo_path": path,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": [family_case_id(family)],
                "content_sha256": "sha256:" + _SHA,
            }
        ],
    }


@pytest.mark.parametrize("family", FAMILIES)
def test_generation_accepts_valid_mapping(family: str) -> None:
    current = CodegenMapping.model_validate(mapping_document(family))
    assert current.layer == family


@pytest.mark.parametrize("family", FAMILIES)
def test_generation_accepts_valid_generated_files(family: str) -> None:
    current = GeneratedFilesV1.model_validate(_valid_files(family))
    assert current.change_id == "CH-DEMO-001"


@pytest.mark.parametrize("family", FAMILIES)
def test_family_codegen_characterization_missing_extra_and_unknown_leaf(family: str) -> None:
    valid = _load(f"{family}-codegen-valid.json")
    missing = _load(f"{family}-codegen-missing-file.json")
    extra = _load(f"{family}-codegen-extra-file.json")
    unknown = _load(f"{family}-codegen-unknown-leaf.json")
    path = cast(str, valid["mapping"]["entries"][0]["target_file"])
    staged = path
    mapping = CodegenMapping.model_validate(valid["mapping"])
    accepted = GeneratedFilesValidator(
        family=family,
        mapping=mapping,
        capability_leafs=frozenset(VALID_LEAFS),
        file_bytes={
            f"qa/results/codegen/{family}-generated-files.json": json.dumps(
                valid, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        },
        write_roots=(f"{path.rsplit('/', 1)[0]}/", "qa/tests/testdata/", "qa/results/"),
    ).validate(
        candidate_with(staged, f"qa/results/codegen/{family}-generated-files.json"),
        validation_context(),
    )
    assert accepted.accepted is True
    missing_result = GeneratedFilesValidator(
        family=family,
        mapping=CodegenMapping.model_validate(missing["mapping"]),
    ).validate(candidate_with(), validation_context())
    assert missing_result.accepted is False
    extra_result = GeneratedFilesValidator(
        family=family,
        mapping=CodegenMapping.model_validate(extra["mapping"]),
    ).validate(
        candidate_with(staged, extra["extra_file"]),
        validation_context(),
    )
    assert extra_result.accepted is False
    with pytest.raises(ValidationError, match="unknown capability leaf"):
        CodegenResultV1.model_validate(unknown, context={"capability_leafs": frozenset(VALID_LEAFS)})
    assert family_symbol(family)
