from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from assurance_generation.contracts import CodegenMapping, GeneratedFilesV1
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    durable_oracle_path,
    family_case_id,
    mapping_document,
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
