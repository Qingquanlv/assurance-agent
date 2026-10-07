from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast

import pytest
from pydantic import ValidationError

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from graph_engine.canonical import JSONValue, canonical_digest
from tests.acg_plan_fixture import install_plan

CHANGE_ID = "CH-DEMO-001"
TARGET = "qa/tests/api/test_users.py"
_SHA = "a" * 64
_TEST_PLAN_DIGEST = "d" * 64
_PUBLICATION_STATES = ("not_ready", "ready", "published", "drifted")


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _canonical(value: object) -> str:
    return canonical_digest(cast(JSONValue, value))


def _producer_shaped(payload: dict[str, object]) -> dict[str, object]:
    try:
        model = ExecutionEvidenceV1.model_validate(payload)
    except ValidationError:
        # Some tests deliberately seal evidence the contract rejects and assert
        # the terminal refuses it. Leave those bytes exactly as written.
        return payload
    return cast(dict[str, object], model.model_dump(mode="json"))


def _write(project: Path, relative: str, content: bytes) -> Path:
    path = project.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _staged_path(family: str, target: str) -> str:
    del family
    return target if target.startswith("qa/tests/") else f"qa/tests/{target.removeprefix('tests/')}"


def _promote(project: Path, family: str, target: str, content: bytes) -> None:
    _write(project, _staged_path(family, target), content)
    digest = _digest(content)
    _write(
        project,
        f"qa/results/codegen/{family}-generated-files.json",
        json.dumps(
            {
                "schema_version": "1",
                "change_id": CHANGE_ID,
                "layer": family,
                "files": [
                    {
                        "target_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": [f"TC_{family.upper()}_001"],
                        "content_sha256": digest,
                    }
                ],
            }
        ).encode("utf-8"),
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa").mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    return project


def valid_status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "invocation_id": "inv-achieved-001",
        "lock_digest": _SHA,
        "root_input_digest": _SHA,
        "status": "completed",
        "entrypoint": "full",
        "graph_hierarchy": (),
        "node_states": (),
        "selected_test_families": ("api",),
        "coverage_progress": None,
        "adapter_evidence": (),
        "execution_gate": None,
        "quality_gate": None,
        "pending_interrupt": None,
        "terminal_reason": None,
        "change": {"change_id": CHANGE_ID, "state": "achieved"},
        "apply": {"manifest_digest": None, "file_count": 0},
        "publication": {"status": "not_ready"},
    }
    payload.update(overrides)
    return payload


def _execution_evidence(
    *,
    batch_id: str,
    status: str,
    plan_digest: str = _TEST_PLAN_DIGEST,
    plan_ref: dict[str, str] | None = None,
) -> dict[str, object]:
    if plan_ref is None:
        plan_ref = {
            "path": (f"qa/results/plan/{plan_digest}/resolved-assurance-plan.json"),
            "digest": "e" * 64,
        }
    mapping: dict[str, object] = {
        "schema_version": "1",
        "selected": [TARGET],
        "mappings": [
            {
                "test": TARGET,
                "case_id": "TC_API_001",
                "capability": "entities.item.create",
                "layer": "api",
            }
        ],
    }
    receipt: dict[str, object] = {
        "commands": [
            {
                "family": "api",
                "command": ["pytest", TARGET],
                "exit_code": 0 if status == "passed" else 1,
                "collected": 1,
                "passed": 1 if status == "passed" else 0,
                "failed": 0 if status == "passed" else 1,
                "skipped": 0,
            }
        ]
    }
    # Hand-written evidence omits defaulted fields, so normalize it the way a
    # real producer does. The bytes on disk and the gate digest must agree.
    return _producer_shaped(
        {
            "schema_version": "1",
            "status": status,
            "change_id": CHANGE_ID,
            "batch_id": batch_id,
            "executed_at": "2026-09-05T00:00:00Z",
            "plan_digest": plan_digest,
            "plan_ref": plan_ref,
            "selected_targets": {
                "api": True,
                "e2e": False,
                "fuzz": False,
                "performance": False,
            },
            "family_outcomes": [{"family": "api", "state": "executed"}],
            "mapping": mapping,
            "mapping_digest": _canonical(mapping),
            "baseline_tree_id": "b" * 64,
            "runner_profile_digest": "c" * 64,
            "receipt_digest": _canonical(receipt),
            "receipt": receipt,
            "results": [
                {
                    "test": TARGET,
                    "case_id": "TC_API_001",
                    "status": status,
                    "duration_ms": 1,
                    "message": "" if status == "passed" else "assertion failed",
                }
            ],
        }
    )


def _execution_gate(
    evidence: dict[str, object],
    *,
    semantic_node_id: str,
) -> dict[str, object]:
    return {
        "semantic_node_id": semantic_node_id,
        "batch_id": evidence["batch_id"],
        "execution_digest": _canonical(evidence),
    }


class _PlanBinding(TypedDict):
    plan_digest: str
    plan_ref: dict[str, str]


def _plan_binding_for(project: Path) -> _PlanBinding:
    evidence = json.loads(
        (project / "qa" / "results/execution" / "execute-result.json").read_text(encoding="utf-8")
    )
    return {
        "plan_digest": cast(str, evidence["plan_digest"]),
        "plan_ref": cast(dict[str, str], evidence["plan_ref"]),
    }


def _ready_change(tmp_path: Path, *, execution_status: str = "passed") -> Path:
    project = _project(tmp_path)
    plan, plan_ref = install_plan(project, CHANGE_ID)
    _promote(project, "api", TARGET, b"generated-candidate\n")
    _write(project, "tests/api/test_users.py", b"original-sut\n")
    evidence = _execution_evidence(
        batch_id="batch-execute",
        status=execution_status,
        plan_digest=plan.plan_digest,
        plan_ref=plan_ref,
    )
    _write(
        project,
        "qa/results/execution/execute-result.json",
        json.dumps(evidence).encode("utf-8"),
    )
    _write(
        project,
        "qa/results/inspect/inspection.json",
        json.dumps(
            {
                "coverage": {
                    "measured": 1.0,
                    "threshold": 0.9,
                    "rounds_used": 1,
                    "rounds_budget": 1,
                    "decision": True,
                }
            }
        ).encode("utf-8"),
    )
    _write(project, "qa/results/report/report.md", b"# report\n")
    return project


def _execute_gate_for(project: Path) -> dict[str, object]:
    evidence = json.loads(
        (project / "qa" / "results/execution" / "execute-result.json").read_text(encoding="utf-8")
    )
    return _execution_gate(evidence, semantic_node_id="execution.execute")


def _quality_gate_for(
    project: Path,
    execution_gate: dict[str, object],
) -> dict[str, object]:
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
    from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1
    from graph_engine.attempts.resolutions import ReceiptRef

    prefix = "qa"
    results = f"{prefix}/results"

    def ref(relative: str, content: bytes | None = None) -> EvidenceArtifactRefV1:
        path = project / relative
        if content is not None:
            _write(project, relative, content)
        return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(path.read_bytes()).hexdigest())

    batch_id = cast(str, execution_gate["batch_id"])
    execution_document = json.loads(
        (project / prefix / "results" / "execution" / "execute-result.json").read_text(encoding="utf-8")
    )
    plan_digest = cast(str, execution_document["plan_digest"])
    plan_ref = EvidenceArtifactRefV1.model_validate(execution_document["plan_ref"])
    reviewed = ReviewedCaseV1(
        change_id=CHANGE_ID,
        coverage_epoch=0,
        plan_digest=plan_digest,
        plan_ref=plan_ref,
        preparation_refs=tuple(
            sorted(
                (plan_ref, ref(f"{prefix}/requirement.md", b"Reviewed requirement")),
                key=lambda item: (item.path, item.digest),
            )
        ),
        case_refs=(ref(f"{prefix}/cases/items/case.yaml", b"reviewed cases"),),
        review_ref=ref(f"{results}/review/case-review.json", b'{"decision":"pass"}'),
        selection_ref=ref(f"{prefix}/results/cases/epochs/0/selection.json", b'{"schema_version":"1"}'),
    )
    filename = (
        "run-result.json" if execution_gate["semantic_node_id"] == "execution.run" else "execute-result.json"
    )
    inspection_receipt = ReceiptRef(receipt_id="inspection", receipt_digest=_SHA)
    inspection = InspectionOutcomeV1(
        change_id=CHANGE_ID,
        coverage_epoch=0,
        batch_id=batch_id,
        plan_digest=plan_digest,
        plan_ref=plan_ref,
        disposition="satisfied",
        coverage_state="satisfied",
        inspection_receipt=inspection_receipt,
        reviewed_case=reviewed,
        mapping_ref=ref(f"{results}/generation/epochs/0/mapping.json", b"{}"),
        assessment_refs=(ref(f"{results}/execution/{filename}"),),
        reason_codes=("coverage.satisfied",),
    )
    report = ReportOutcomeV1(
        change_id=CHANGE_ID,
        coverage_epoch=0,
        batch_id=batch_id,
        inspection_receipt=inspection_receipt,
        plan_digest=plan_digest,
        plan_ref=plan_ref,
        report_refs=(ref(f"{results}/report/report.md"),),
        report_receipt=ReceiptRef(receipt_id="report", receipt_digest=_SHA),
    )
    return {"inspection": inspection.model_dump(mode="json"), "report": report.model_dump(mode="json")}


def _stage_status_ledger(
    project: Path,
    *,
    semantic: str = "execution.execute",
    epoch: int = 0,
    quality: bool = True,
) -> dict[str, object]:
    from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_execution.contracts.workflow import (
        EXECUTION_CYCLE_PATH,
        ExecutionCycleDocumentV1,
        ExecutionSemanticNodeId,
        execution_evidence_path,
    )
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
    from assurance_quality.contracts.assessment import (
        INSPECTION_OUTCOME_PATH,
        REPORT_OUTCOME_PATH,
        InspectionDocumentV1,
        ReportOutcomeDocumentV1,
    )
    from assurance_quality.ops.inspect import op as inspect_op
    from assurance_quality.ops.report import op as report_op
    from graph_engine.artifacts import stage_json_artifact

    evidence_path = execution_evidence_path(cast(ExecutionSemanticNodeId, semantic))
    evidence_bytes = project.joinpath(*evidence_path.split("/")).read_bytes()
    evidence = ExecutionEvidenceV1.model_validate_json(evidence_bytes)
    evidence_ref = EvidenceArtifactRefV1(
        path=evidence_path,
        digest=hashlib.sha256(evidence_bytes).hexdigest(),
    )
    execution_gate = _execution_gate(evidence.model_dump(mode="json"), semantic_node_id=semantic)
    gate = _quality_gate_for(project, execution_gate)
    raw_inspection = gate["inspection"]
    raw_report = gate["report"]
    if not isinstance(raw_inspection, dict) or not isinstance(raw_report, dict):
        raise AssertionError("quality gate fixture must be mappings")
    inspection = dict(raw_inspection)
    report = dict(raw_report)
    inspection["coverage_epoch"] = epoch
    report["coverage_epoch"] = epoch
    raw_reviewed = inspection["reviewed_case"]
    if not isinstance(raw_reviewed, dict):
        raise AssertionError("reviewed case fixture must be a mapping")
    reviewed = {**raw_reviewed, "coverage_epoch": epoch}
    if epoch != 0:
        selection = f"qa/results/cases/epochs/{epoch}/selection.json"
        source = project / "qa/results/cases/epochs/0/selection.json"
        destination = project / selection
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        raw_selection = reviewed["selection_ref"]
        if not isinstance(raw_selection, dict):
            raise AssertionError("selection ref fixture must be a mapping")
        reviewed["selection_ref"] = {
            **raw_selection,
            "path": selection,
            "digest": hashlib.sha256(destination.read_bytes()).hexdigest(),
        }
    inspection["reviewed_case"] = reviewed
    mapping_ref = EvidenceArtifactRefV1.model_validate(inspection["mapping_ref"])
    case_refs = reviewed["case_refs"]
    if not isinstance(case_refs, list | tuple):
        raise AssertionError("case refs fixture must be a sequence")
    source_refs = tuple(EvidenceArtifactRefV1.model_validate(item) for item in case_refs)
    if evidence.executed_at is None:
        raise AssertionError("status fixture evidence must bind executed_at")
    cycle = ExecutionCycleDocumentV1.model_validate(
        {
            "change_id": evidence.change_id,
            "plan_digest": evidence.plan_digest,
            "plan_ref": evidence.plan_ref.model_dump(mode="json"),
            "coverage_epoch": epoch,
            "repair_round": 0,
            "batch_id": evidence.batch_id,
            "executed_at": evidence.executed_at,
            "final_status": "PASS" if evidence.status == "passed" else "FAIL",
            "evidence_ref": evidence_ref.model_dump(mode="json"),
            "mapping_ref": mapping_ref.model_dump(mode="json"),
            "source_refs": [item.model_dump(mode="json") for item in source_refs],
            "family_outcomes": [item.model_dump(mode="json") for item in evidence.family_outcomes],
        }
    )
    cycle_ref = stage_json_artifact(project, EXECUTION_CYCLE_PATH, cycle)
    ledger: dict[str, object] = {
        TASK_ATTEMPT_CONTRACTS["execute"].artifact("cycle").ledger_key: {
            "refs": [{"path": cycle_ref.path, "digest": cycle_ref.digest}],
            "receipt": {"receipt_id": "execution", "receipt_digest": _SHA},
        }
    }
    if quality:
        inspection_receipt = inspection.pop("inspection_receipt")
        report_receipt = report.pop("report_receipt")
        inspection_ref = stage_json_artifact(
            project, INSPECTION_OUTCOME_PATH, InspectionDocumentV1.model_validate(inspection)
        )
        report_ref = stage_json_artifact(
            project, REPORT_OUTCOME_PATH, ReportOutcomeDocumentV1.model_validate(report)
        )
        ledger[inspect_op.artifact("inspection-outcome").ledger_key] = {
            "refs": [{"path": inspection_ref.path, "digest": inspection_ref.digest}],
            "receipt": inspection_receipt,
        }
        ledger[report_op.artifact("report-outcome").ledger_key] = {
            "refs": [{"path": report_ref.path, "digest": report_ref.digest}],
            "receipt": report_receipt,
        }
    return ledger


def _status_snapshot(
    project: Path,
    *,
    semantic: str = "execution.execute",
    current: int = 0,
    epoch: int | None = None,
    quality: bool = True,
    families: list[str] | None = None,
    terminal: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> SimpleNamespace:
    if epoch is None:
        epoch = current
    values: dict[str, object] = {
        "artifact_ledger": _stage_status_ledger(project, semantic=semantic, epoch=epoch, quality=quality),
        "budgets": {"coverage_rounds": 1},
        "flow_control": {"loops": {"coverage": current}},
        "selected_test_families": ["api"] if families is None else families,
        "terminal": {"status": "completed", "reason": "achieved"} if terminal is None else terminal,
    }
    if extra:
        values.update(extra)
    return SimpleNamespace(next=(), interrupts=(), values=values)


def _render(snapshot: SimpleNamespace, project: Path):
    from assurance_product.status import render_status_from_langgraph

    return render_status_from_langgraph(
        invocation_id="inv-achieved-001",
        lock_digest=_SHA,
        root_input_digest=_SHA,
        entrypoint="full",
        change_id=CHANGE_ID,
        status="completed",
        snapshot=snapshot,
        project_root=project,
    )


def test_status_v1_rejects_tree_fields():
    from assurance_product.models import StatusV1

    with pytest.raises(ValidationError):
        StatusV1.model_validate(valid_status(initial_tree_id=_SHA))
    with pytest.raises(ValidationError):
        StatusV1.model_validate(valid_status(current_head_tree_id=_SHA))


@pytest.mark.parametrize("state", _PUBLICATION_STATES)
def test_status_v1_accepts_closed_publication_status(state: str):
    from assurance_product.models import StatusV1

    value = StatusV1.model_validate(valid_status(publication={"status": state}))
    assert value.publication.status == state


def test_status_v1_rejects_unknown_publication_status():
    from assurance_product.models import StatusV1

    with pytest.raises(ValidationError):
        StatusV1.model_validate(valid_status(publication={"status": "exported"}))
    with pytest.raises(ValidationError):
        StatusV1.model_validate({key: value for key, value in valid_status().items() if key != "publication"})


def test_render_status_binds_terminal_execution_checkpoint(tmp_path: Path) -> None:
    project = _ready_change(tmp_path)
    rerun = _execution_evidence(batch_id="batch-rerun", status="passed", **_plan_binding_for(project))
    _write(project, "qa/results/execution/run-result.json", json.dumps(rerun).encode("utf-8"))
    status = _render(_status_snapshot(project, semantic="execution.run", quality=False), project)

    assert status.execution_gate is not None
    assert status.execution_gate.semantic_node_id == "execution.run"
    assert status.execution_gate.batch_id == "batch-rerun"
    assert status.execution_gate.execution_digest == _canonical(rerun)


def test_status_maps_execute_and_rerun_evidence_paths_to_semantic_nodes(tmp_path: Path) -> None:
    execute_project = _ready_change(tmp_path / "execute")
    execute = _render(
        _status_snapshot(execute_project, semantic="execution.execute", quality=False),
        execute_project,
    )
    assert execute.execution_gate is not None
    assert execute.execution_gate.semantic_node_id == "execution.execute"

    run_project = _ready_change(tmp_path / "run")
    rerun = _execution_evidence(batch_id="batch-rerun", status="passed", **_plan_binding_for(run_project))
    _write(run_project, "qa/results/execution/run-result.json", json.dumps(rerun).encode("utf-8"))
    run = _render(_status_snapshot(run_project, semantic="execution.run", quality=False), run_project)
    assert run.execution_gate is not None
    assert run.execution_gate.semantic_node_id == "execution.run"


def test_status_ignores_the_previous_epoch_execution_file(tmp_path: Path) -> None:
    project = _ready_change(tmp_path)
    status = _render(_status_snapshot(project, current=1, epoch=0), project)
    assert status.coverage_progress is None
    assert status.execution_gate is None
    assert status.quality_gate is None


def test_status_rejects_a_changed_execution_file_whose_digest_does_not_match(
    tmp_path: Path,
) -> None:
    import pytest

    from assurance_execution.contracts.workflow import EXECUTION_CYCLE_PATH

    project = _ready_change(tmp_path)
    snapshot = _status_snapshot(project, quality=False)
    (project / EXECUTION_CYCLE_PATH).write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=EXECUTION_CYCLE_PATH):
        _render(snapshot, project)


def test_execution_publish_records_checkpoint_authority() -> None:
    from assurance_execution.contracts.evidence import ExecutionEvidenceV1
    from assurance_execution.operations.cycle import seal_execution

    evidence = ExecutionEvidenceV1.model_validate(
        _execution_evidence(batch_id="batch-rerun", status="passed")
    )
    published = seal_execution(
        evidence,
        change_id=evidence.change_id,
        coverage_epoch=0,
        repair_round=0,
        execution_kind="run",
        generation=None,
    )

    assert published.execution_semantic_node_id == "execution.run"


def test_finalize_achieved_writes_status_without_apply_manifest(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    original = (project / TARGET).read_bytes()
    execution_gate = _execute_gate_for(project)

    status = finalize_achieved(
        project,
        CHANGE_ID,
        ("api",),
        invocation=valid_status(
            execution_gate=execution_gate,
            quality_gate=_quality_gate_for(project, execution_gate),
        ),
    )

    assert status.change.change_id == CHANGE_ID
    assert status.change.state == "achieved"
    assert status.publication.status == "not_ready"
    assert status.apply.file_count == 0
    assert status.apply.manifest_digest is None
    status_path = project / "qa" / "status.json"
    manifest_path = project / "qa" / "apply-manifest.json"
    assert status_path.is_file()
    assert not manifest_path.exists()
    written_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert written_status["publication"]["status"] == "not_ready"
    assert written_status["change"]["state"] == "achieved"
    assert (project / TARGET).read_bytes() == original
    assert (project / "tests" / "api" / "test_users.py").read_bytes() == b"original-sut\n"
    assert not (project / "qa" / "archive").exists()


def test_finalize_achieved_accepts_authenticated_successful_rerun(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path, execution_status="failed")
    rerun = _execution_evidence(batch_id="batch-rerun", status="passed", **_plan_binding_for(project))
    _write(
        project,
        "qa/results/execution/run-result.json",
        json.dumps(rerun).encode("utf-8"),
    )

    execution_gate = _execution_gate(rerun, semantic_node_id="execution.run")
    status = finalize_achieved(
        project,
        CHANGE_ID,
        ("api",),
        invocation=valid_status(
            execution_gate=execution_gate,
            quality_gate=_quality_gate_for(project, execution_gate),
        ),
    )

    assert status.change.state == "achieved"


def test_finalize_achieved_accepts_file_aggregated_execution_counts(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    path = project / "qa" / "results/execution" / "execute-result.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    receipt = cast(dict[str, object], evidence["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    command["collected"] = 2
    command["passed"] = 2
    evidence["receipt_digest"] = _canonical(receipt)
    path.write_text(json.dumps(evidence), encoding="utf-8")

    execution_gate = _execution_gate(evidence, semantic_node_id="execution.execute")
    status = finalize_achieved(
        project,
        CHANGE_ID,
        ("api",),
        invocation=valid_status(
            execution_gate=execution_gate,
            quality_gate=_quality_gate_for(project, execution_gate),
        ),
    )

    assert status.change.state == "achieved"


def test_finalize_achieved_rejects_unbound_successful_rerun(tmp_path: Path) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path, execution_status="failed")
    rerun = _execution_evidence(batch_id="batch-rerun", status="passed", **_plan_binding_for(project))
    _write(
        project,
        "qa/results/execution/run-result.json",
        json.dumps(rerun).encode("utf-8"),
    )

    with pytest.raises(ValueError, match="execution gate reference"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())


@pytest.mark.parametrize("drift", ["node", "change", "batch", "digest", "receipt", "counts"])
def test_finalize_achieved_rejects_drifted_rerun_authority(tmp_path: Path, drift: str) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path, execution_status="failed")
    rerun = _execution_evidence(batch_id="batch-rerun", status="passed", **_plan_binding_for(project))
    _write(
        project,
        "qa/results/execution/run-result.json",
        json.dumps(rerun).encode("utf-8"),
    )
    gate = _execution_gate(rerun, semantic_node_id="execution.run")
    if drift == "node":
        gate["semantic_node_id"] = "execution.execute"
    elif drift == "change":
        rerun["change_id"] = "CH-FOREIGN-001"
        gate["execution_digest"] = _canonical(rerun)
        _write(
            project,
            "qa/results/execution/run-result.json",
            json.dumps(rerun).encode("utf-8"),
        )
    elif drift == "batch":
        gate["batch_id"] = "foreign-batch"
    elif drift == "digest":
        gate["execution_digest"] = "e" * 64
    elif drift == "receipt":
        rerun["receipt_digest"] = "e" * 64
        gate["execution_digest"] = _canonical(rerun)
        _write(
            project,
            "qa/results/execution/run-result.json",
            json.dumps(rerun).encode("utf-8"),
        )
    else:
        receipt = cast(dict[str, object], rerun["receipt"])
        command = cast(list[dict[str, object]], receipt["commands"])[0]
        command["collected"] = 2
        rerun["receipt_digest"] = _canonical(receipt)
        gate["execution_digest"] = _canonical(rerun)
        _write(
            project,
            "qa/results/execution/run-result.json",
            json.dumps(rerun).encode("utf-8"),
        )

    with pytest.raises(ValueError, match="execution"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(execution_gate=gate),
        )


@pytest.mark.parametrize("execution_status", ["failed", "product_issue", "infrastructure_failure"])
def test_finalize_achieved_rejects_failed_execution_without_writing(tmp_path: Path, execution_status: str):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path, execution_status=execution_status)

    with pytest.raises(ValueError, match="execution"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())

    change = project / "qa"
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()
    assert (project / TARGET).read_bytes() == b"generated-candidate\n"
    assert (project / "tests" / "api" / "test_users.py").read_bytes() == b"original-sut\n"


def test_finalize_achieved_rejects_invalid_merge_without_writing(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    (project / TARGET).write_bytes(b"tampered-generated\n")

    with pytest.raises(ValueError, match="digest"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())

    change = project / "qa"
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()


@pytest.mark.parametrize(
    "coverage",
    [
        {
            "measured": 0.40,
            "threshold": 0.9,
            "rounds_used": 1,
            "rounds_budget": 1,
            "decision": False,
            "coverage_state": "exhausted",
        },
        {
            "measured": 0.40,
            "threshold": 0.9,
            "rounds_used": 0,
            "rounds_budget": 1,
            "decision": False,
            "coverage_state": "inconclusive",
        },
        {
            "measured": 0.95,
            "threshold": 0.9,
            "rounds_used": 0,
            "rounds_budget": 1,
            "decision": True,
            "coverage_state": "needs_human",
        },
    ],
)
def test_finalize_achieved_rejects_unsatisfied_coverage_without_writing(
    tmp_path: Path, coverage: dict[str, object]
) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    _write(
        project,
        "qa/results/inspect/inspection.json",
        json.dumps({"coverage": coverage, "coverage_state": coverage["coverage_state"]}).encode("utf-8"),
    )

    with pytest.raises(ValueError, match="quality"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(execution_gate=_execute_gate_for(project)),
        )

    change = project / "qa"
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()


def test_finalize_achieved_rejects_failed_or_exhausted_invocation(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    with pytest.raises(ValueError, match="failed|exhausted"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(terminal_reason="exhausted"),
        )
    change = project / "qa"
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()


def test_finalize_achieved_rejects_missing_report_even_with_improvement_artifacts(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    execution_gate = _execute_gate_for(project)
    quality_gate = _quality_gate_for(project, execution_gate)
    (project / "qa" / "results/report" / "report.md").unlink()
    _write(
        project,
        "qa/results/improvements/delivery.json",
        b'{"schema_version":"1","improvement_id":"IMP-1"}\n',
    )

    with pytest.raises(ValueError, match="quality|report"):
        finalize_achieved(
            project,
            CHANGE_ID,
            ("api",),
            invocation=valid_status(execution_gate=execution_gate, quality_gate=quality_gate),
        )

    change = project / "qa"
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()


def test_finalize_achieved_requires_terminal_full_success(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    pending = {
        "node_id": "human-review",
        "actions": ("approve", "reject"),
        "reason_category": "needs_human_review",
    }

    with pytest.raises(ValueError, match="terminal"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status(status="failed"))
    with pytest.raises(ValueError, match="interrupt"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status(pending_interrupt=pending))
    with pytest.raises(ValueError, match="full"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status(entrypoint="init"))

    change = project / "qa"
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()
