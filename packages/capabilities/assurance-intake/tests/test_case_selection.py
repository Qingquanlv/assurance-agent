from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from assurance_intake.contracts.case_selection import CaseSelectionV1, SelectedCaseV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.operations.case_selection import (
    InputError,
    load_selected_cases,
    require_selected_ids,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "case-authoring-valid.yaml"
_PLAN_DIGEST = "a" * 64


def test_reuse_is_part_of_current_scope() -> None:
    require_selected_ids(("CASE-NEW", "CASE-OLD"), ("CASE-NEW", "CASE-OLD"))
    with pytest.raises(ValueError, match="selected"):
        require_selected_ids(("CASE-NEW",), ("CASE-NEW", "CASE-OLD"))


def test_duplicate_generated_ids_are_rejected() -> None:
    with pytest.raises(InputError, match="selected"):
        require_selected_ids(("CASE-NEW", "CASE-NEW"), ("CASE-NEW",))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(root: Path, relative: str, data: bytes) -> EvidenceArtifactRefV1:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=_digest(data))


def _reviewed_reuse(tmp_path: Path) -> ReviewedCaseV1:
    historical = _write(tmp_path, "qa/cases/auth/login/case.yaml", _FIXTURE.read_bytes())
    plan = _write(
        tmp_path,
        f"qa/results/plan/{_PLAN_DIGEST}/resolved-assurance-plan.json",
        b'{"schema_version":"1"}',
    )
    review = _write(tmp_path, "qa/results/review/case-review.json", b'{"schema_version":"1"}')
    selection_doc = CaseSelectionV1(
        schema_version="1",
        change_id="CH-1",
        coverage_epoch=0,
        plan_digest=_PLAN_DIGEST,
        inventory_ref=plan,
        cases=(
            SelectedCaseV1(
                case_id="TC_MENU_001",
                origin="reuse",
                source_ref=historical,
                source_locator="added[0]",
                mrc_ids=("MRC-API-001",),
            ),
        ),
    )
    selection = _write(
        tmp_path,
        "qa/results/cases/epochs/0/selection.json",
        selection_doc.model_dump_json().encode(),
    )
    return ReviewedCaseV1(
        change_id="CH-1",
        coverage_epoch=0,
        plan_digest=_PLAN_DIGEST,
        plan_ref=plan,
        preparation_refs=(plan,),
        case_refs=(historical,),
        review_ref=review,
        selection_ref=selection,
    )


def test_pure_reuse_loads_historical_source_without_rewriting(tmp_path: Path) -> None:
    original = _digest(_FIXTURE.read_bytes())
    reviewed = _reviewed_reuse(tmp_path)
    loaded = load_selected_cases(tmp_path, reviewed)
    assert [entry.case_id for entry in loaded] == ["TC_MENU_001"]
    assert _digest(tmp_path.joinpath("qa/cases/auth/login/case.yaml").read_bytes()) == original


def test_digest_drift_is_rejected(tmp_path: Path) -> None:
    reviewed = _reviewed_reuse(tmp_path)
    tmp_path.joinpath("qa/cases/auth/login/case.yaml").write_bytes(b"changed: true\n")
    with pytest.raises(InputError, match="digest"):
        load_selected_cases(tmp_path, reviewed)
