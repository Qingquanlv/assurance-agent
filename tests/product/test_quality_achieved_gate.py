from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.product.test_achieved_terminal import (
    CHANGE_ID,
    _execute_gate_for,
    _quality_gate_for,
    _ready_change,
    _render,
    _status_snapshot,
    valid_status,
)


def _install_quality(project: Path) -> tuple[dict[str, Any], bytes]:
    gate = _quality_gate_for(project, _execute_gate_for(project))
    report = (project / "qa/results/report/report.md").read_bytes()
    return cast(dict[str, Any], gate), report


def test_render_status_binds_current_inspection_and_report_receipts(tmp_path: Path) -> None:
    project = _ready_change(tmp_path)
    gate, _ = _install_quality(project)
    status = _render(_status_snapshot(project), project)
    assert status.quality_gate is not None
    assert status.quality_gate.model_dump(mode="json") == gate


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


def _coverage_snapshot(project: Path, *, current: int, group: int) -> SimpleNamespace:
    return _status_snapshot(project, current=current, epoch=group)


def test_round_zero_achieved_reads_the_current_cycle(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    status = _render(_coverage_snapshot(project, current=0, group=0), project)
    assert status.coverage_progress is not None
    assert status.coverage_progress.round == 0
    assert status.execution_gate is not None
    assert status.quality_gate is not None
    finalized = finalize_achieved(project, CHANGE_ID, ("api",), invocation=status)
    assert finalized.change.state == "achieved"
    assert finalized.execution_gate == status.execution_gate
    assert finalized.quality_gate == status.quality_gate


def test_rework_round_achieved_reads_the_new_cycle(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    status = _render(_coverage_snapshot(project, current=1, group=1), project)
    assert status.coverage_progress is not None
    assert status.coverage_progress.round == 1
    assert status.execution_gate is not None
    assert status.quality_gate is not None
    finalized = finalize_achieved(project, CHANGE_ID, ("api",), invocation=status)
    assert finalized.execution_gate is not None
    assert finalized.quality_gate is not None
    assert finalized.quality_gate.inspection.coverage_epoch == 1


def test_round_before_execute_hides_the_previous_cycle(tmp_path: Path) -> None:
    project = _ready_change(tmp_path)
    status = _render(_coverage_snapshot(project, current=1, group=0), project)
    assert status.coverage_progress is None
    assert status.execution_gate is None
    assert status.quality_gate is None


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


def test_rework_round_checkpoint_reads_the_current_cycle() -> None:
    asyncio.run(_real_round(achieved=True))


def test_fact_baseline_failure_hides_the_previous_cycle() -> None:
    asyncio.run(_real_round(achieved=False))


async def _real_round(*, achieved: bool) -> None:
    from agent_runtime_contracts.wire.schema import canonical_digest
    from graph_engine.canonical import JSONValue
    from graph_engine.testing.graph_harness import committed

    from assurance_execution.contracts.evidence import ExecutionEvidenceV1
    from tests.product.test_achieved_terminal import _execution_evidence
    from tests.product.test_execute_tail_flow import _SHA
    from tests.product.test_full_flow import (
        _RECEIPT,
        _case,
        _failure,
        _front,
        _invoke,
        _round_execution,
        _tail_until_inspect,
    )
    from tests.product.test_product_stategraph_flow import _PLAN_DIGEST, _execution, _plan_ref, _report

    script: dict[str, list[object]] = {}
    _front(script)
    _case(script, "pass", "pass", epochs=(0, 1))
    if achieved:
        _tail_until_inspect(script, ("coverage_insufficient", 0), ("satisfied", 1))
        execution = _execution(1)["execution_result"]
        assert isinstance(execution, dict)
        evidence = ExecutionEvidenceV1.model_validate(
            _execution_evidence(
                batch_id=str(execution["batch_id"]),
                status="passed",
                plan_digest=_PLAN_DIGEST,
                plan_ref=_plan_ref().model_dump(mode="json"),
            )
        )
        document = evidence.model_dump(mode="json")
        output = _round_execution(1)
        output["execution_evidence"] = document
        output["execution_digest"] = canonical_digest(cast(JSONValue, document))
        script["execution.execute"][1] = committed(output, _RECEIPT)
        report = _report(1)
        script["quality.report"] = [
            committed(
                {
                    "publication": "reported",
                    "report_outcome": report["report_outcome"],
                    "report_refs": report["report_refs"],
                    "coverage_state": "satisfied",
                },
                _RECEIPT,
            )
        ]
    else:
        _tail_until_inspect(script, ("coverage_insufficient", 0))
        script["quality.fact-baseline"].append(_failure())
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    done = await _invoke(
        script,
        budgets={
            "review_rounds": 2,
            "coverage_rounds": 1,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    )
    if achieved:
        assert done.outcome == "achieved"
    else:
        assert done.outcome == "not_achieved"
