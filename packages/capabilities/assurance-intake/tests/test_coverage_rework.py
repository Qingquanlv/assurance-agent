"""Coverage-rework writes a case-rework document, and case-design reads it by handle."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, PrepareContext
from agent_runtime_contracts.wire.models import prompt_model_json
from graph_engine.artifacts import stage_json_artifact
from graph_engine.attempts.resolutions import ReceiptRef

from assurance_intake.contracts.coverage_rework import (
    COVERAGE_REWORK_HANDOFF_PATH,
    REWORK_CONTEXT_PATH,
    CoverageReworkHandoffV1,
    CoverageReworkInputV1,
)
from assurance_intake.ops.case_review.hooks import REVIEWED_CASE_PATH
from assurance_intake.contracts.review import ReviewRepairActionV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.handoff import REWORK_CONTEXT
from assurance_intake.ops.case_design import op as case_design
from assurance_intake.ops.case_design import hooks as case_hooks
from assurance_intake.ops.case_design.hooks import before as design_before
from assurance_intake.ops.case_design.models import CaseDesignInputV1
from assurance_intake.ops.case_repair import op as case_repair
from assurance_intake.ops.case_repair import hooks as repair_hooks
from assurance_intake.ops.case_repair.hooks import before as repair_before
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, ReviewRepairContractV1
from assurance_intake.ops.case_review import op as case_review
from assurance_intake.ops.case_review import hooks as review_hooks
from assurance_intake.ops.case_review.hooks import MATRIX_PATH, before as review_before
from assurance_intake.ops.case_review.models import CaseReviewInputV1
from assurance_intake.ops.coverage_rework import hooks

_SHA = "a" * 64
_CHANGE = "CH-DEMO-001"
_PLAN = "b" * 64
_CASE = "qa/cases/menus/case.yaml"


def _ref(path: str, body: bytes = b"x") -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(body).hexdigest())


def _write(root: Path, ref: EvidenceArtifactRefV1, body: bytes = b"x") -> None:
    target = root.joinpath(*ref.path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def _reviewed() -> ReviewedCaseV1:
    plan = _ref(f"qa/results/plan/{_PLAN}/resolved-assurance-plan.json", b"plan")
    return ReviewedCaseV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        plan_digest=_PLAN,
        plan_ref=plan,
        preparation_refs=(plan, _ref("qa/results/preparation/context.json", b"prep")),
        case_refs=(_ref(_CASE, b"case"),),
        review_ref=_ref("qa/results/review/case-review.json", b"review"),
        selection_ref=_ref("qa/results/cases/epochs/0/selection.json", b"selection"),
    )


def _input(root: Path, **overrides: object) -> CoverageReworkInputV1:
    reviewed = _reviewed()
    gap = _ref("qa/results/inspect/epochs/0/gaps.json", b"gap")
    assessment_refs = overrides.pop("assessment_refs", (gap,))
    source_epoch = overrides.pop("source_epoch", 0)
    case = stage_json_artifact(root, REVIEWED_CASE_PATH, reviewed)
    handoff_path = root / COVERAGE_REWORK_HANDOFF_PATH
    if handoff_path.exists():
        handoff_path.unlink()
    handoff = stage_json_artifact(
        root,
        COVERAGE_REWORK_HANDOFF_PATH,
        CoverageReworkHandoffV1(source_epoch=source_epoch, assessment_refs=assessment_refs),  # type: ignore[arg-type]
    )
    payload: dict[str, object] = {
        "change_id": _CHANGE,
        "coverage_epoch": 1,
        "handoff_ref": {"path": handoff.path, "digest": handoff.digest},
        "reviewed_case_ref": {"path": case.path, "digest": case.digest},
        "inspect_receipt": ReceiptRef(receipt_id="inspect-0", receipt_digest=_SHA),
    }
    payload.update(overrides)
    return CoverageReworkInputV1.model_validate(payload)


class _Task:
    def __init__(self, root: Path) -> None:
        self.write_root = root
        self.project_root = root


def _design(root: Path, ref: EvidenceArtifactRefV1 | None, *, epoch: int = 1) -> CaseDesignInputV1:
    reviewed = _reviewed()
    return CaseDesignInputV1(
        change_id=_CHANGE,
        capability_leafs=("auth.session",),
        artifact_paths=(_CASE,),
        plan_digest=_PLAN,
        plan_ref=reviewed.plan_ref,
        selected_test_families=("api",),
        coverage_epoch=epoch,
        case_delta_paths=(_CASE,),
        rework_ref=ref,
    )


def test_coverage_rework_writes_the_context_and_case_design_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewed = _reviewed()
    for ref, body in (
        (reviewed.plan_ref, b"plan"),
        (reviewed.preparation_refs[1], b"prep"),
        (reviewed.case_refs[0], b"case"),
        (_ref("qa/results/inspect/epochs/0/gaps.json", b"gap"), b"gap"),
    ):
        _write(tmp_path, ref, body)
    output = hooks.run(_Task(tmp_path), _input(tmp_path))  # type: ignore[arg-type]
    assert output.rework_ref.path == REWORK_CONTEXT_PATH
    business = _design(tmp_path, output.rework_ref)
    loaded = REWORK_CONTEXT.load(tmp_path, business)
    assert loaded is not None
    assert loaded.target_case_paths == (_CASE,)
    assert loaded.previous_case.coverage_epoch == 0
    tampered = tmp_path / "qa/results/inspect/epochs/0/gaps.json"
    tampered.write_bytes(b"tampered")
    with pytest.raises(InputError, match="digest"):
        REWORK_CONTEXT.load(tmp_path, business)
    tampered.write_bytes(b"gap")

    class _Plan:
        selected_test_families = ("api",)

    def _quiet(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(case_hooks, "bind_case_delta_evidence", lambda value, *_args, **_kwargs: value)
    monkeypatch.setattr(case_hooks, "build_planning_facts", _quiet)
    monkeypatch.setattr(review_hooks, "build_planning_facts", _quiet)
    monkeypatch.setattr(repair_hooks, "bind_case_delta_evidence", lambda value, *_args, **_kwargs: value)
    monkeypatch.setattr(repair_hooks, "build_planning_facts", _quiet)
    monkeypatch.setattr(
        repair_hooks,
        "_review_repair_contract",
        lambda *_args, **_kwargs: ReviewRepairContractV1(
            review_path="qa/results/review/case-review.json",
            review_sha256=_SHA,
            baseline_file_digests={_CASE: _PLAN},
            actions=(
                ReviewRepairActionV1(
                    finding_id="f1",
                    artifact=_CASE,
                    case_id="menu-list",
                    allowed_paths=("title",),
                    instructions=("repair the title",),
                ),
            ),
        ),
    )
    expected = loaded.model_dump(mode="json")
    seen: list[dict[str, object]] = []
    for op, runner, value in (
        (case_design, design_before, business),
        (
            case_review,
            review_before,
            CaseReviewInputV1(
                change_id=_CHANGE,
                capability_leafs=("auth.session",),
                artifact_paths=(_CASE,),
                plan_digest=_PLAN,
                plan_ref=reviewed.plan_ref,
                coverage_epoch=1,
                case_delta_paths=(_CASE,),
                case_refs=reviewed.case_refs,
                preparation_refs=(_ref(MATRIX_PATH, b"matrix"),),
                rework_ref=output.rework_ref,
            ),
        ),
        (
            case_repair,
            repair_before,
            CaseRepairInputV1(
                change_id=_CHANGE,
                capability_leafs=("auth.session",),
                artifact_paths=(_CASE,),
                plan_digest=_PLAN,
                plan_ref=reviewed.plan_ref,
                selected_test_families=("api",),
                coverage_epoch=1,
                case_delta_paths=(_CASE,),
                rework_ref=output.rework_ref,
            ),
        ),
    ):
        ctx = PrepareContext(op, _Task(tmp_path))  # type: ignore[arg-type]
        ctx._business = value
        ctx._deps[("artifact", "intake.plan")] = _Plan()
        ctx._deps[("artifact", "intake.rework")] = loaded
        ctx._deps[("artifact", "intake.case-inventory")] = object()
        ctx._deps[("artifact", "intake.case-exploration")] = None
        prepared = runner(ctx, value)  # type: ignore[operator]
        payload = prompt_model_json(prepared)
        payload.update(ctx._extra)
        document = payload["case_rework_context"]
        assert document == expected
        assert "case_rework_context" not in type(prepared).model_fields
        seen.append(document)
    assert seen[0] == seen[1] == seen[2]
    assert isinstance(seen[0]["gaps_ref"], dict)
    assert seen[0]["gaps_ref"]["path"].endswith("/gaps.json")


def test_missing_rework_file_leaves_case_design_unchanged(tmp_path: Path) -> None:
    business = _design(tmp_path, None, epoch=0)
    assert REWORK_CONTEXT.load(tmp_path, business) is None


def test_rework_check_rejects_a_mismatched_round(tmp_path: Path) -> None:
    reviewed = _reviewed()
    for ref, body in (
        (reviewed.plan_ref, b"plan"),
        (reviewed.preparation_refs[1], b"prep"),
        (reviewed.case_refs[0], b"case"),
        (_ref("qa/results/inspect/epochs/0/gaps.json", b"gap"), b"gap"),
    ):
        _write(tmp_path, ref, body)
    output = hooks.run(_Task(tmp_path), _input(tmp_path))  # type: ignore[arg-type]
    business = _design(tmp_path, output.rework_ref, epoch=2)
    with pytest.raises(InputError, match="exactly once"):
        REWORK_CONTEXT.load(tmp_path, business)


def test_case_design_preparation_refs_fall_back_to_artifacts() -> None:
    artifact = _ref("qa/requirement.md", b"req")
    filled = CaseDesignInputV1(
        change_id=_CHANGE,
        capability_leafs=("auth.session",),
        artifact_paths=(_CASE,),
        plan_digest=_PLAN,
        plan_ref=_reviewed().plan_ref,
        artifacts=(artifact,),
    )
    assert filled.preparation_refs == (artifact,)
    empty = CaseDesignInputV1(
        change_id=_CHANGE,
        capability_leafs=("auth.session",),
        artifact_paths=(_CASE,),
        plan_digest=_PLAN,
        plan_ref=_reviewed().plan_ref,
    )
    assert empty.preparation_refs == ()


def test_rework_task_rejects_the_source_epoch_and_gap_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source epoch"):
        hooks.run(_Task(tmp_path), _input(tmp_path, coverage_epoch=0))  # type: ignore[arg-type]
    extra = _ref("qa/results/inspect/epochs/0/coverage-gaps.json", b"other")
    gap = _ref("qa/results/inspect/epochs/0/gaps.json", b"gap")
    with pytest.raises((ValueError, ValidationError), match="exactly one"):
        hooks.run(
            _Task(tmp_path),  # type: ignore[arg-type]
            _input(tmp_path, assessment_refs=(gap, extra)),
        )
