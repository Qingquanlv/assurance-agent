from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from assurance_intake.contracts.case_selection import CaseSelectionV1, SelectedCaseV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_generation.operations.selected_cases import (
    InputError,
    load_selected_case_authoring,
    load_selected_cases,
    require_selected_ids,
)

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "assurance-intake"
    / "tests"
    / "fixtures"
    / "case-authoring-valid.yaml"
)
_PLAN_DIGEST = "a" * 64


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
    selection = CaseSelectionV1(
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
    selection_ref = _write(
        tmp_path,
        "qa/results/cases/epochs/0/selection.json",
        selection.model_dump_json().encode(),
    )
    return ReviewedCaseV1(
        change_id="CH-1",
        coverage_epoch=0,
        plan_digest=_PLAN_DIGEST,
        plan_ref=plan,
        preparation_refs=(plan,),
        case_refs=(historical,),
        review_ref=review,
        selection_ref=selection_ref,
    )


def test_generation_scope_includes_reused_ids(tmp_path: Path) -> None:
    reviewed = _reviewed_reuse(tmp_path)
    loaded = load_selected_cases(tmp_path, reviewed)
    require_selected_ids(tuple(entry.case_id for entry in loaded), ("TC_MENU_001",))
    assert loaded[0].module == "menus"
    assert _digest(tmp_path.joinpath("qa/cases/auth/login/case.yaml").read_bytes()) == _digest(
        _FIXTURE.read_bytes()
    )

    authored, ids_by_path = load_selected_case_authoring(
        tmp_path,
        reviewed,
        family="api",
        capability_leafs=("entities.item.create",),
    )
    assert [entry.case_id for entry in authored.added] == ["TC_MENU_001"]
    assert ids_by_path == {"qa/cases/auth/login/case.yaml": ("TC_MENU_001",)}


def test_generation_scope_rejects_subset_without_reuse(tmp_path: Path) -> None:
    _reviewed_reuse(tmp_path)
    with pytest.raises(InputError, match="selected"):
        require_selected_ids(("TC_MENU_001",), ("TC_MENU_001", "TC_OLD"))
