from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import BaseModel

from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import (
    ExecutionCycleDocumentV1,
    execution_evidence_path,
    execution_semantic_node,
)
from assurance_quality.contracts.assessment import (
    InspectionDocumentV1,
    InspectionOutcomeV1,
    ReportOutcomeDocumentV1,
    ReportOutcomeV1,
)
from assurance_quality.ops.inspect import op as inspect_op
from assurance_quality.ops.report import op as report_op
from graph_engine.artifacts import ArtifactReadError, open_artifact
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.stategraph.ledger import ledger_entry_receipt, ledger_refs

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.generated_merge import merge_generated
from assurance_product.models import (
    AdapterEvidenceRefV1,
    ExecutionGateRefV1,
    GraphStatusV1,
    NodeStatusV1,
    PendingInterruptStatusV1,
    QualityGateRefV1,
    StatusV1,
)

_STATUS_NAME = "status.json"


def render_status_from_langgraph(
    *,
    invocation_id: str,
    lock_digest: str,
    root_input_digest: str,
    entrypoint: str,
    change_id: str,
    status: str,
    snapshot: object | None = None,
    journal_events: Sequence[object] = (),
    project_root: Path,
) -> StatusV1:
    mapped = "completed" if status in {"succeeded", "completed"} else status
    if mapped not in {"running", "blocked", "interrupted", "stopped", "failed", "completed"}:
        mapped = "failed"
    terminal_reason, full_achieved = _terminal_projection(entrypoint, snapshot)
    achieved = mapped == "completed" and (entrypoint != "full" or full_achieved)
    change_state = "achieved" if achieved else mapped
    if change_state == "completed":
        change_state = "stopped"
    active_hierarchy, active_nodes, pending = _langgraph_snapshot_fields(invocation_id, snapshot)
    journal_hierarchy, journal_nodes = _journal_attempt_fields(invocation_id, journal_events)
    execution_gate = _execution_gate_from_snapshot(snapshot, project_root)
    quality_gate = _quality_gate_from_snapshot(snapshot, change_id, project_root, execution_gate)
    return StatusV1.model_validate(
        {
            "schema_version": "1",
            "invocation_id": invocation_id,
            "lock_digest": lock_digest,
            "root_input_digest": root_input_digest,
            "status": mapped,
            "entrypoint": entrypoint,
            "graph_hierarchy": journal_hierarchy + active_hierarchy,
            "node_states": journal_nodes + active_nodes,
            "selected_test_families": _selected_test_families_from_snapshot(snapshot),
            "coverage_progress": _coverage_progress_from_snapshot(snapshot, project_root),
            "adapter_evidence": _journal_adapter_evidence(invocation_id, journal_events),
            "execution_gate": execution_gate,
            "quality_gate": quality_gate,
            "pending_interrupt": pending,
            "terminal_reason": terminal_reason,
            "change": {"change_id": change_id, "state": change_state},
            "apply": {"manifest_digest": None, "file_count": 0},
            "publication": {"status": "not_ready"},
        }
    )


def load_persisted_status(workspace: ChangeWorkspace) -> StatusV1 | None:
    path = workspace.paths.qa_root / _STATUS_NAME
    if not path.exists():
        return None
    try:
        return StatusV1.model_validate_json(_read_regular_file(path, _STATUS_NAME))
    except (OSError, ValueError) as error:
        raise ValueError("persisted status is invalid") from error


def _langgraph_snapshot_fields(
    invocation_id: str, snapshot: object | None
) -> tuple[tuple[GraphStatusV1, ...], tuple[NodeStatusV1, ...], PendingInterruptStatusV1 | None]:
    if snapshot is None:
        return (), (), None
    nxt = tuple(getattr(snapshot, "next", ()) or ())
    hierarchy = tuple(
        GraphStatusV1(
            graph_instance_id=f"{invocation_id}:active:{node}",
            graph_id=str(node),
            parent_graph_instance_id=None,
            state="running",
        )
        for node in nxt
    )
    nodes = tuple(
        NodeStatusV1(
            graph_instance_id=f"{invocation_id}:active:{node}",
            node_id=str(node),
            state="running",
            attempt=None,
            lease_state=None,
            failure_category=None,
            activity_reference_digest=None,
        )
        for node in nxt
    )
    interrupts = tuple(getattr(snapshot, "interrupts", ()) or ())
    pending = None
    if interrupts:
        first = interrupts[0]
        value = getattr(first, "value", first)
        node_id = getattr(first, "id", None)
        actions: tuple[str, ...] = ()
        reason = "system"
        if isinstance(value, Mapping):
            raw_actions = value.get("actions")
            if isinstance(raw_actions, list | tuple):
                actions = tuple(str(item) for item in raw_actions)
            raw_reason = value.get("reason") or value.get("kind")
            if isinstance(raw_reason, str) and raw_reason:
                reason = raw_reason
        pending = PendingInterruptStatusV1(
            node_id=str(node_id or (nxt[0] if nxt else "interrupt")),
            actions=actions,
            reason_category=reason,
        )
    return hierarchy, nodes, pending


def _journal_adapter_evidence(
    invocation_id: str,
    events: Sequence[object],
) -> tuple[AdapterEvidenceRefV1, ...]:
    from graph_engine.attempts.events import (
        ActivityBound,
        ActivityTerminalObserved,
        AttemptOpened,
        AttemptTerminated,
    )

    opened: AttemptOpened | None = None
    pending: dict[str, AdapterEvidenceRefV1] = {}
    refs: list[AdapterEvidenceRefV1] = []
    for event in events:
        if isinstance(event, AttemptOpened):
            refs.extend(pending.values())
            pending = {}
            opened = event
            continue
        if opened is None or opened.invocation_id != invocation_id:
            continue
        if isinstance(event, ActivityBound) and isinstance(event.reference, Mapping):
            session_id = event.reference.get("session_id")
            if isinstance(session_id, str) and session_id:
                pending[event.activity_id] = AdapterEvidenceRefV1(
                    activation_id=event.activity_id,
                    activity_id=session_id,
                    reference_digest=event.reference_digest,
                    terminal_receipt_digest=None,
                )
        elif isinstance(event, ActivityTerminalObserved) and event.activity_id in pending:
            prior = pending[event.activity_id]
            pending[event.activity_id] = prior.model_copy(
                update={
                    "terminal_receipt_digest": event.source_receipt_digest or None,
                }
            )
        elif isinstance(event, AttemptTerminated):
            refs.extend(pending.values())
            pending = {}
            opened = None
    refs.extend(pending.values())
    return tuple(refs)


def _journal_attempt_fields(
    invocation_id: str,
    events: Sequence[object],
) -> tuple[tuple[GraphStatusV1, ...], tuple[NodeStatusV1, ...]]:
    from graph_engine.attempts.events import ActivityBound, AttemptOpened, AttemptTerminated

    opened: AttemptOpened | None = None
    activity_reference_digest: str | None = None
    projected: dict[str, tuple[GraphStatusV1, NodeStatusV1]] = {}
    precedence = {"failed": 1, "stopped": 2, "succeeded": 3}
    for event in events:
        if isinstance(event, AttemptOpened):
            opened = event
            activity_reference_digest = None
            continue
        if opened is None:
            continue
        if isinstance(event, ActivityBound):
            activity_reference_digest = event.reference_digest
            continue
        if not isinstance(event, AttemptTerminated):
            continue
        if opened.invocation_id == invocation_id:
            node_state, graph_state, failure = _attempt_terminal_state(event)
            semantic_node_id = opened.semantic_node_id
            graph_instance_id = f"{invocation_id}:attempt:{semantic_node_id}"
            candidate = (
                GraphStatusV1(
                    graph_instance_id=graph_instance_id,
                    graph_id=semantic_node_id,
                    parent_graph_instance_id=None,
                    state=graph_state,
                ),
                NodeStatusV1(
                    graph_instance_id=graph_instance_id,
                    node_id=f"{semantic_node_id}/finalize",
                    state=node_state,
                    attempt=None,
                    lease_state=None,
                    failure_category=failure,
                    activity_reference_digest=activity_reference_digest,
                ),
            )
            previous = projected.get(semantic_node_id)
            if previous is None or precedence[node_state] > precedence[previous[1].state]:
                projected[semantic_node_id] = candidate
        opened = None
        activity_reference_digest = None
    return (
        tuple(graph for graph, _node in projected.values()),
        tuple(node for _graph, node in projected.values()),
    )


def _attempt_terminal_state(
    event: object,
) -> tuple[
    Literal["succeeded", "failed", "stopped"],
    Literal["completed", "failed", "stopped"],
    str | None,
]:
    resolution_kind = getattr(event, "resolution_kind", "")
    if resolution_kind == "committed":
        return "succeeded", "completed", None
    if resolution_kind == "rejected":
        return "stopped", "stopped", "rejected"
    failure = getattr(event, "failure_kind", None) or resolution_kind or "unknown"
    return "failed", "failed", str(failure)


def _terminal_projection(entrypoint: str, snapshot: object | None) -> tuple[str | None, bool]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return None, False
    raw = values.get("terminal")
    if not isinstance(raw, Mapping):
        return None, False
    status = raw.get("status")
    reason = raw.get("reason")
    if not isinstance(status, str) or not isinstance(reason, str) or not reason:
        return None, False
    return reason, entrypoint == "full" and status == "completed" and reason == "achieved"


def _coverage_round(values: Mapping[str, object]) -> int:
    from graph_engine.flow.control import control_table, read_round

    return read_round(control_table(values, "loops"), "coverage")


_EXECUTION_CYCLE_KEY = TASK_ATTEMPT_CONTRACTS["execute"].artifact("cycle").ledger_key
_INSPECTION_KEY = inspect_op.artifact("inspection-outcome").ledger_key
_REPORT_OUTCOME_KEY = report_op.artifact("report-outcome").ledger_key


def _snapshot_values(snapshot: object | None) -> Mapping[str, object] | None:
    values = getattr(snapshot, "values", None)
    return values if isinstance(values, Mapping) else None


def _one_ledger_ref(ledger: object, key: str) -> dict[str, str] | None:
    refs = ledger_refs(ledger, key)
    if not refs:
        return None
    if len(refs) != 1:
        raise ValueError(f"ledger key {key} must have one ref")
    return refs[0]


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _open_status_artifact(
    project: Path,
    ref: Mapping[str, object],
    model: type[_ModelT],
    label: str,
) -> _ModelT:
    try:
        return open_artifact(project, ref, model=model)
    except ArtifactReadError as error:
        path = error.path or ""
        if error.reason == "digest":
            raise ValueError(f"{label} digest drifted: {path}") from error
        if error.reason in {"missing", "path", "symlink"}:
            raise ValueError(f"{label} is missing: {path}") from error
        raise ValueError(f"{label} is invalid: {path}") from error


def _inspection_from_ledger(
    values: Mapping[str, object],
    project_root: Path,
    *,
    label: str,
) -> InspectionOutcomeV1 | None:
    ref = _one_ledger_ref(values.get("artifact_ledger"), _INSPECTION_KEY)
    if ref is None:
        return None
    document = _open_status_artifact(project_root, ref, InspectionDocumentV1, label)
    receipt = ledger_entry_receipt(values.get("artifact_ledger"), _INSPECTION_KEY)
    if receipt is None:
        raise ValueError(f"{label} is missing: {ref['path']}")
    return InspectionOutcomeV1.model_validate(
        {**document.model_dump(mode="json"), "inspection_receipt": receipt}
    )


def _coverage_progress_from_snapshot(
    snapshot: object | None,
    project_root: Path,
) -> Mapping[str, object] | None:
    values = _snapshot_values(snapshot)
    if values is None:
        return None
    try:
        inspection = _inspection_from_ledger(values, project_root, label="terminal checkpoint inspection")
        if inspection is None:
            return None
        if inspection.coverage_epoch != _coverage_round(values):
            return None
        if not inspection.coverage_state:
            return None
        budgets = values.get("budgets")
        budget = budgets.get("coverage_rounds") if isinstance(budgets, Mapping) else None
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError("coverage budget must be a non-negative int")
    except (TypeError, ValueError) as error:
        raise ValueError(f"terminal checkpoint coverage progress is incomplete: {error}") from error
    return {
        "round": inspection.coverage_epoch,
        "maximum_rounds": budget,
        "measured": None,
        "decision": inspection.coverage_state,
    }


def _selected_test_families_from_snapshot(snapshot: object | None) -> tuple[str, ...]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return ()
    raw = values.get("selected_test_families")
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple) or any(not isinstance(item, str) for item in raw):
        raise ValueError("terminal checkpoint selected test families are invalid")
    families = tuple(raw)
    allowed = ("api", "e2e", "fuzz", "performance")
    if len(families) != len(set(families)) or tuple(item for item in allowed if item in families) != families:
        raise ValueError("terminal checkpoint selected test families are not canonical")
    return families


def finalize_achieved(
    project_root: Path,
    change_id: str,
    families: tuple[str, ...],
    *,
    invocation: Mapping[str, object] | StatusV1,
) -> StatusV1:
    project = Path(project_root)
    _require_terminal_full_success(invocation)
    merge_generated(project, change_id, families)
    execution_gate = _require_execution_gate(project, change_id, invocation)
    _require_quality_gate(project, change_id, invocation, execution_gate)
    status = _achieved_status(invocation, change_id)
    change_root = project / "qa"
    _write_canonical_json(change_root / "status.json", status.model_dump(mode="json"))
    return status


def _read_regular_file(path: Path, label: str) -> bytes:
    info = path.lstat()
    _reject_irregular(info, label)
    return path.read_bytes()


def _reject_irregular(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"symlink is not allowed: {label}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"path is not a regular file: {label}")
    if info.st_nlink != 1:
        raise ValueError(f"hard link is not allowed: {label}")


def _require_terminal_full_success(invocation: Mapping[str, object] | StatusV1) -> None:
    payload = invocation.model_dump(mode="json") if isinstance(invocation, StatusV1) else dict(invocation)
    if payload.get("entrypoint") != "full":
        raise ValueError("achieved requires the full entrypoint")
    if payload.get("status") != "completed":
        raise ValueError("achieved requires terminal workflow success")
    if payload.get("pending_interrupt") is not None:
        raise ValueError("achieved requires no pending interrupt")
    reason = payload.get("terminal_reason")
    if isinstance(reason, str) and reason in {"failed", "exhausted", "not_achieved"}:
        raise ValueError(f"achieved rejects {reason} terminal state")
    node_states = payload.get("node_states")
    if isinstance(node_states, tuple | list):
        for item in node_states:
            if not isinstance(item, Mapping):
                continue
            status = item.get("status")
            node_id = item.get("node_id")
            if status in {"failed", "exhausted"} or node_id in {"failed", "exhausted", "not-achieved"}:
                raise ValueError("achieved rejects failed or exhausted state")


def _execution_gate_from_snapshot(
    snapshot: object | None,
    project_root: Path,
) -> ExecutionGateRefV1 | None:
    values = _snapshot_values(snapshot)
    if values is None:
        return None
    ref = _one_ledger_ref(values.get("artifact_ledger"), _EXECUTION_CYCLE_KEY)
    if ref is None:
        return None
    try:
        cycle = _open_status_artifact(
            project_root, ref, ExecutionCycleDocumentV1, "terminal execution checkpoint cycle"
        )
        if cycle.coverage_epoch != _coverage_round(values):
            return None
        evidence, document = _validated_execution_evidence(
            _open_status_artifact(
                project_root,
                cycle.evidence_ref.model_dump(mode="json"),
                ExecutionEvidenceV1,
                "terminal execution checkpoint evidence",
            ).model_dump(mode="json"),
            label="terminal execution checkpoint evidence",
        )
        digest = canonical_digest(cast(JSONValue, document))
        semantic = execution_semantic_node(cycle.evidence_ref.path)
    except ValueError as error:
        raise ValueError(f"terminal execution checkpoint identity is incomplete: {error}") from error
    return ExecutionGateRefV1.model_validate(
        {
            "semantic_node_id": semantic,
            "batch_id": evidence.batch_id,
            "execution_digest": digest,
        }
    )


def _quality_gate_from_snapshot(
    snapshot: object | None,
    change_id: str,
    project_root: Path,
    execution_gate: ExecutionGateRefV1 | None,
) -> QualityGateRefV1 | None:
    values = _snapshot_values(snapshot)
    if values is None:
        return None
    report_ref = _one_ledger_ref(values.get("artifact_ledger"), _REPORT_OUTCOME_KEY)
    if report_ref is None:
        return None
    try:
        report_document = _open_status_artifact(
            project_root, report_ref, ReportOutcomeDocumentV1, "terminal quality checkpoint report"
        )
        report_receipt = ledger_entry_receipt(values.get("artifact_ledger"), _REPORT_OUTCOME_KEY)
        if report_receipt is None:
            raise ValueError(f"terminal quality checkpoint report is missing: {report_ref['path']}")
        inspection = _inspection_from_ledger(
            values, project_root, label="terminal quality checkpoint inspection"
        )
        if inspection is None:
            raise ValueError("terminal quality checkpoint is invalid")
        gate = QualityGateRefV1.model_validate(
            {
                "inspection": inspection,
                "report": ReportOutcomeV1.model_validate(
                    {**report_document.model_dump(mode="json"), "report_receipt": report_receipt}
                ),
            }
        )
    except ValueError as error:
        raise ValueError(f"terminal quality checkpoint is invalid: {error}") from error
    if gate.inspection.coverage_epoch != _coverage_round(values):
        return None
    if gate.inspection.change_id != change_id:
        raise ValueError("terminal quality checkpoint identity drifted")
    if execution_gate is None or execution_gate.batch_id != gate.inspection.batch_id:
        raise ValueError("terminal quality checkpoint batch drifted")
    return gate


def _execution_gate_from_invocation(
    invocation: Mapping[str, object] | StatusV1,
) -> ExecutionGateRefV1:
    raw = invocation.execution_gate if isinstance(invocation, StatusV1) else invocation.get("execution_gate")
    if raw is None:
        raise ValueError("execution gate reference is missing")
    try:
        return raw if isinstance(raw, ExecutionGateRefV1) else ExecutionGateRefV1.model_validate(raw)
    except ValueError as error:
        raise ValueError("execution gate reference is invalid") from error


def _validated_execution_evidence(
    raw: object,
    *,
    label: str,
) -> tuple[ExecutionEvidenceV1, dict[str, object]]:
    try:
        evidence = ExecutionEvidenceV1.model_validate(raw)
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if evidence.executed_at is None:
        raise ValueError(f"{label} is missing executed_at")
    mapping = cast(JSONValue, evidence.mapping.model_dump(mode="json"))
    receipt = cast(JSONValue, evidence.receipt.model_dump(mode="json"))
    if evidence.mapping_digest != canonical_digest(mapping):
        raise ValueError(f"{label} mapping digest drifted")
    if evidence.receipt_digest != canonical_digest(receipt):
        raise ValueError(f"{label} receipt digest drifted")
    counts = {
        "passed": sum(item.status == "passed" for item in evidence.results),
        "failed": sum(item.status == "failed" for item in evidence.results),
        "skipped": sum(item.status == "skipped" for item in evidence.results),
    }
    receipt_total = evidence.receipt.passed + evidence.receipt.failed + evidence.receipt.skipped
    if (
        evidence.receipt.collected < len(evidence.results)
        or receipt_total != evidence.receipt.collected
        or evidence.receipt.passed < counts["passed"]
        or evidence.receipt.failed < counts["failed"]
        or evidence.receipt.skipped < counts["skipped"]
    ):
        raise ValueError(f"{label} receipt counts drifted")
    expected_status = (
        "failed"
        if evidence.receipt.exit_code != 0
        or evidence.receipt.failed != 0
        or any(item.status == "failed" for item in evidence.results)
        else "passed"
    )
    if evidence.status != expected_status:
        raise ValueError(f"{label} status drifted")
    return evidence, cast(dict[str, object], evidence.model_dump(mode="json"))


def _require_execution_gate(
    project: Path,
    change_id: str,
    invocation: Mapping[str, object] | StatusV1,
) -> ExecutionGateRefV1:
    gate = _execution_gate_from_invocation(invocation)
    evidence_path = execution_evidence_path(gate.semantic_node_id)
    payload = _read_json_object(project.joinpath(*evidence_path.split("/")), "execution evidence")
    evidence, document = _validated_execution_evidence(payload, label="execution evidence")
    if evidence.change_id != change_id or evidence.batch_id != gate.batch_id:
        raise ValueError("execution evidence identity drifted")
    if canonical_digest(cast(JSONValue, document)) != gate.execution_digest:
        raise ValueError("execution evidence digest drifted")
    if gate.semantic_node_id == "execution.run":
        initial_path = execution_evidence_path("execution.execute")
        initial_payload = _read_json_object(
            project.joinpath(*initial_path.split("/")),
            "initial execution evidence",
        )
        initial, _document = _validated_execution_evidence(
            initial_payload,
            label="initial execution evidence",
        )
        if initial.change_id != change_id or initial.status != "failed":
            raise ValueError("execution rerun is not preceded by failed initial evidence")
        if (
            initial.mapping != evidence.mapping
            or initial.selected_targets != evidence.selected_targets
            or initial.runner_profile_digest != evidence.runner_profile_digest
        ):
            raise ValueError("execution rerun selection drifted")
    if evidence.status != "passed":
        raise ValueError(f"execution gate failed: {evidence.status!r}")
    return gate


def _quality_gate_from_invocation(
    invocation: Mapping[str, object] | StatusV1,
) -> QualityGateRefV1:
    raw = invocation.quality_gate if isinstance(invocation, StatusV1) else invocation.get("quality_gate")
    if raw is None:
        raise ValueError("quality gate reference is missing")
    try:
        return raw if isinstance(raw, QualityGateRefV1) else QualityGateRefV1.model_validate(raw)
    except ValueError as error:
        raise ValueError("quality gate reference is invalid") from error


def _require_quality_gate(
    project: Path,
    change_id: str,
    invocation: Mapping[str, object] | StatusV1,
    execution_gate: ExecutionGateRefV1,
) -> None:
    reference = _quality_gate_from_invocation(invocation)
    inspection = reference.inspection
    if inspection.change_id != change_id or inspection.batch_id != execution_gate.batch_id:
        raise ValueError("quality inspection identity drifted")
    if inspection.disposition != "satisfied" or inspection.coverage_state != "satisfied":
        raise ValueError("quality gate failed")
    execution_path = execution_evidence_path(execution_gate.semantic_node_id)
    if not any(ref.path == execution_path for ref in inspection.assessment_refs):
        raise ValueError("quality inspection execution reference is missing")
    refs = (
        *inspection.reviewed_case.preparation_refs,
        *inspection.reviewed_case.case_refs,
        inspection.reviewed_case.review_ref,
        inspection.mapping_ref,
        *inspection.assessment_refs,
        *reference.report.report_refs,
    )
    for ref in refs:
        path = project.joinpath(*ref.path.split("/"))
        _reject_symlink_components(project, path)
        try:
            content = _read_regular_file(path, "quality evidence")
        except OSError as error:
            raise ValueError(f"quality evidence is missing: {ref.path}") from error
        if hashlib.sha256(content).hexdigest() != ref.digest:
            raise ValueError(f"quality evidence digest drifted: {ref.path}")


def _reject_symlink_components(root: Path, target: Path) -> None:
    path = root
    for part in target.relative_to(root).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("quality evidence path is a symlink")


def _achieved_status(
    invocation: Mapping[str, object] | StatusV1,
    change_id: str,
) -> StatusV1:
    payload = invocation.model_dump(mode="json") if isinstance(invocation, StatusV1) else dict(invocation)
    payload["change"] = {"change_id": change_id, "state": "achieved"}
    payload["apply"] = {"manifest_digest": None, "file_count": 0}
    payload["publication"] = {"status": "not_ready"}
    return StatusV1.model_validate(payload)


def _read_json_object(path: Path, label: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} is invalid")
    return payload


def _write_canonical_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
