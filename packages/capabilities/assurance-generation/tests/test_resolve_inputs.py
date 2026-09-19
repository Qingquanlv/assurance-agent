from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_generation.operations.resolve_inputs import InputError, resolve_generation_input
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from tests.acg_plan_fixture import install_plan


def _write(root: Path, relative: str, data: bytes) -> EvidenceArtifactRefV1:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _review(decision: str = "pass") -> bytes:
    return json.dumps(
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-DEMO-001",
            "decision": decision,
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "minimum_coverage": {
                "total_required": 0,
                "covered": 0,
                "skipped_by_scope": 0,
                "missing": [],
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["src/app.py"],
                "verified_claims": [{"claim": "item persists", "evidence_files": ["src/app.py"]}],
            },
        },
        sort_keys=True,
    ).encode()


def _fixture(
    root: Path, *, decision: str = "pass", coverage_epoch: int = 0
) -> tuple[ReviewedCaseV1, EvidenceArtifactRefV1]:
    plan, plan_ref_payload = install_plan(root, "CH-DEMO-001")
    plan_ref = EvidenceArtifactRefV1.model_validate(plan_ref_payload)
    preparation = _write(root, "qa/requirement.md", b"requirement")
    case = _write(root, "qa/cases/menus/case.yaml", b"case")
    review = _write(root, "qa/results/review/case-review.json", _review(decision))
    reviewed = ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=coverage_epoch,
        plan_digest=plan.plan_digest,
        plan_ref=plan_ref,
        preparation_refs=tuple(sorted((plan_ref, preparation), key=lambda item: item.path)),
        case_refs=(case,),
        review_ref=review,
        selection_ref=_write(
            root,
            f"qa/results/cases/epochs/{coverage_epoch}/selection.json",
            b'{"schema_version":"1"}',
        ),
    )
    manifest = _write(
        root,
        "qa/cases/reviewed-case.json",
        reviewed.model_dump_json().encode(),
    )
    return reviewed, manifest


def test_standalone_generation_requires_reviewed_case_manifest(tmp_path: Path) -> None:
    plan, plan_ref = install_plan(tmp_path, "CH-DEMO-001")
    with pytest.raises(InputError, match="exactly one"):
        resolve_generation_input(
            {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": 0,
                "plan_digest": plan.plan_digest,
                "plan_ref": plan_ref,
                "source_artifacts": [],
            },
            tmp_path,
        )


def test_generation_rejects_a_nonpassing_review(tmp_path: Path) -> None:
    reviewed, manifest = _fixture(tmp_path, decision="reject")
    with pytest.raises(InputError, match="passing"):
        resolve_generation_input(
            {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": 0,
                "plan_digest": reviewed.plan_digest,
                "plan_ref": reviewed.plan_ref.model_dump(mode="json"),
                "source_artifacts": [manifest.model_dump(mode="json")],
            },
            tmp_path,
        )


def test_generation_rejects_case_bytes_changed_after_review(tmp_path: Path) -> None:
    reviewed, manifest = _fixture(tmp_path)
    (tmp_path / "qa/cases/menus/case.yaml").write_bytes(b"changed")
    with pytest.raises(InputError, match="digest changed"):
        resolve_generation_input(
            {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": 0,
                "plan_digest": reviewed.plan_digest,
                "plan_ref": reviewed.plan_ref.model_dump(mode="json"),
                "source_artifacts": [manifest.model_dump(mode="json")],
            },
            tmp_path,
        )


def test_standalone_generation_rebinds_verified_historical_review_to_epoch_zero(
    tmp_path: Path,
) -> None:
    reviewed, manifest = _fixture(tmp_path, coverage_epoch=3)
    resolved = resolve_generation_input(
        {
            "change_id": "CH-DEMO-001",
            "coverage_epoch": 0,
            "plan_digest": reviewed.plan_digest,
            "plan_ref": reviewed.plan_ref.model_dump(mode="json"),
            "source_artifacts": [manifest.model_dump(mode="json")],
        },
        tmp_path,
    )
    assert reviewed.coverage_epoch == 3
    assert resolved.coverage_epoch == 0
    assert resolved.case_refs == reviewed.case_refs
    assert resolved.review_ref == reviewed.review_ref
