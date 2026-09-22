from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from assurance_product.bootstrap.spec import SpecOverrideError, load_run_spec, merge_run_spec
from tests.product.test_bootstrap_contracts import _spec


def _write_spec(path: Path, **overrides: object) -> Path:
    document = _spec(**overrides).model_dump(mode="json")
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_load_run_spec_reads_yaml(tmp_path: Path) -> None:
    path = _write_spec(tmp_path / "run-spec.yaml")
    loaded = load_run_spec(path)
    assert loaded.requirement.startswith("Cover dept")
    assert loaded.case_modules == ("system/dept",)


def test_merge_allows_only_three_fields() -> None:
    base = _spec()
    merged = merge_run_spec(
        base,
        {
            "requirement": "Cover users.",
            "candidate_test_families": ["api", "e2e"],
            "case_modules": ["system/user"],
        },
    )
    assert merged.requirement == "Cover users."
    assert merged.candidate_test_families == ("api", "e2e")
    assert merged.case_modules == ("system/user",)
    assert merged.sut.base_url == base.sut.base_url


def test_merge_rejects_unknown_override_keys() -> None:
    with pytest.raises(SpecOverrideError, match="product"):
        merge_run_spec(_spec(), {"product": "assurance-opencode"})


def test_load_rejects_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "run-spec.yaml"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(SpecOverrideError):
        load_run_spec(path)
