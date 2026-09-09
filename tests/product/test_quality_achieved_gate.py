from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.product.test_achieved_terminal import (
    CHANGE_ID,
    _execute_gate_for,
    _quality_gate_for,
    _ready_change,
    valid_status,
)


def _install_quality(project: Path) -> tuple[dict[str, Any], bytes]:
    gate = _quality_gate_for(project, _execute_gate_for(project))
    report = (project / f"qa/changes/{CHANGE_ID}/report/report.md").read_bytes()
    return cast(dict[str, Any], gate), report


def test_render_status_binds_current_inspection_and_report_receipts(tmp_path: Path) -> None:
    from assurance_product.status import render_status_from_langgraph

    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    snapshot = SimpleNamespace(
        next=(),
        interrupts=(),
        values={
            "terminal": {"status": "completed", "reason": "achieved"},
            "batch_id": gate["inspection"]["batch_id"],
            "coverage_epoch": 0,
            "inspection_outcome": gate["inspection"],
            "report_outcome": gate["report"],
        },
    )
    status = render_status_from_langgraph(
        invocation_id="inv-achieved-001",
        lock_digest="a" * 64,
        root_input_digest="a" * 64,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="completed",
        snapshot=snapshot,
    )
    assert status.quality_gate is not None
    assert status.quality_gate.model_dump(mode="json") == gate


def test_render_status_projects_quality_gate_from_diagnostic_report(tmp_path: Path) -> None:
    from assurance_product.status import render_status_from_langgraph

    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    snapshot = SimpleNamespace(
        next=(),
        interrupts=(),
        values={
            "terminal": {"status": "failed", "reason": "not_achieved"},
            "batch_id": gate["inspection"]["batch_id"],
            "coverage_epoch": 0,
            "inspection_outcome": gate["inspection"],
            "report_refs": gate["report"]["report_refs"],
            "report_receipt": gate["report"]["report_receipt"],
        },
    )
    status = render_status_from_langgraph(
        invocation_id="inv-diagnostic-001",
        lock_digest="a" * 64,
        root_input_digest="a" * 64,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="failed",
        snapshot=snapshot,
    )
    assert status.quality_gate is not None
    assert status.quality_gate.inspection.model_dump(mode="json") == gate["inspection"]
    assert status.quality_gate.report.model_dump(mode="json") == gate["report"]


def test_finalize_achieved_accepts_the_bound_quality_outcomes(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    status = finalize_achieved(
        project,
        CHANGE_ID,
        ("api",),
        invocation=valid_status(execution_gate=_execute_gate_for(project), quality_gate=gate),
    )
    assert status.change.state == "achieved"


def test_finalize_achieved_rejects_modified_plan_bytes(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    refs = gate["inspection"]["reviewed_case"]["preparation_refs"]
    plan_ref = next(ref for ref in refs if "/plan/" in ref["path"])
    (project / plan_ref["path"]).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="quality|plan|inspection"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(
                execution_gate=_execute_gate_for(project),
                quality_gate=gate,
            ),
        )


@pytest.mark.parametrize("tamper", ["mapping", "case", "report", "batch", "execution", "receipt", "epoch"])
def test_finalize_achieved_rejects_tampered_or_stale_quality_evidence(
    tmp_path: Path,
    tamper: str,
) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    inspection = gate["inspection"]
    if tamper in {"mapping", "case", "report"}:
        refs = {
            "mapping": inspection["mapping_ref"],
            "case": inspection["reviewed_case"]["case_refs"][0],
            "report": gate["report"]["report_refs"][0],
        }
        (project / refs[tamper]["path"]).write_bytes(b"tampered")
    elif tamper == "batch":
        inspection["batch_id"] = "stale-batch"
    elif tamper == "execution":
        inspection["assessment_refs"][0]["digest"] = "f" * 64
    elif tamper == "receipt":
        gate["report"]["inspection_receipt"]["receipt_digest"] = "f" * 64
    else:
        gate["report"]["coverage_epoch"] = 1

    with pytest.raises(ValueError, match="quality|report|inspection"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(execution_gate=_execute_gate_for(project), quality_gate=gate),
        )


def test_finalize_achieved_rejects_insufficient_inspection(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    gate["inspection"]["disposition"] = "coverage_insufficient"
    gate["inspection"]["coverage_state"] = "repair_required"
    with pytest.raises(ValueError, match="quality gate failed"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(execution_gate=_execute_gate_for(project), quality_gate=gate),
        )
