"""operation:materialize-trace-projection — reconciled projection + sufficiency facts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.artifacts.models.sufficiency import SufficiencyReportV2
from assurance_agent.artifacts.models.trace import TraceGapCodeV2, TraceProjectionLike
from assurance_agent.artifacts.models.trace_sufficiency import (
    TraceInsufficientCase,
    TraceSufficiencyErrorCode,
    TraceSufficiencyFacts,
)
from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.exceptions import AaError
from assurance_agent.evidence.sufficiency import evaluate_sufficiency
from assurance_agent.evidence.trace import authoritative_batch_instant, fold_trace
from assurance_agent.workflow.core.atomic_io import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace

TRACE_PROJECTION_REL = "inspect/trace-projection.json"
TRACE_SUFFICIENCY_REL = "inspect/trace-sufficiency.json"


def _gap_codes(projection: TraceProjectionLike) -> tuple[TraceGapCodeV2, ...]:
    codes: list[TraceGapCodeV2] = []
    seen: set[str] = set()
    for gap in projection.gaps:
        code = gap.code
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)  # type: ignore[arg-type]
    return tuple(codes)


def _has_open_problems(projection: TraceProjectionLike) -> bool:
    return any(row.open_problem_ids for row in projection.rows)


def _judged(
    projection: TraceProjectionLike, report: SufficiencyReportV2, digest: str, as_of: datetime
) -> TraceSufficiencyFacts:
    return TraceSufficiencyFacts(
        schema_version="1",
        change_id=projection.change_id,
        authoritative_batch_id=projection.authoritative_batch_id,
        policy_digest=digest,
        as_of=as_of,
        integrity=projection.integrity,
        integrity_blocks_routing=projection.integrity == "incomplete",
        sufficient=report.all_sufficient,
        has_open_problems=_has_open_problems(projection),
        error_code=None,
        insufficient_cases=tuple(
            TraceInsufficientCase(case_id=verdict.case_id, reason_codes=verdict.reason_codes)
            for verdict in report.verdicts
            if not verdict.sufficient
        ),
        gap_codes=_gap_codes(projection),
    )


def _unjudged(
    projection: TraceProjectionLike, error_code: TraceSufficiencyErrorCode
) -> TraceSufficiencyFacts:
    return TraceSufficiencyFacts(
        schema_version="1",
        change_id=projection.change_id,
        authoritative_batch_id=projection.authoritative_batch_id,
        policy_digest=None,
        as_of=None,
        integrity=projection.integrity,
        integrity_blocks_routing=projection.integrity == "incomplete",
        sufficient=False,
        has_open_problems=_has_open_problems(projection),
        error_code=error_code,
        insufficient_cases=(),
        gap_codes=_gap_codes(projection),
    )


def _judge(project_root: Path, projection: TraceProjectionLike) -> TraceSufficiencyFacts:
    instant = authoritative_batch_instant(project_root, projection.change_id)
    if instant is None:
        return _unjudged(projection, "evidence_projection_missing")

    try:
        policy: Policy = load_policy(project_root)
    except (AaError, OSError, ValueError):
        return _unjudged(projection, "policy_error")

    try:
        report = evaluate_sufficiency(
            projection,
            policy,
            as_of=instant.as_of,
            require_current_batch=True,
        )
    except KeyError:
        return _unjudged(projection, "policy_error")
    except (TypeError, ValueError):
        return _unjudged(projection, "evidence_projection_missing")

    return _judged(projection, report, policy_digest(policy), instant.as_of)


def materialize_trace_projection(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del task
    try:
        projection = fold_trace(workspace.project_root, context.change_id, phase="reconciled")
    except (AaError, OSError, ValueError) as err:
        return task_failure("invalid_input", f"cannot fold reconciled trace projection: {err}")

    facts = _judge(workspace.project_root, projection)

    try:
        atomic_write_bytes(workspace.change_dir / TRACE_PROJECTION_REL, canonical_json_bytes(projection))
        atomic_write_bytes(workspace.change_dir / TRACE_SUFFICIENCY_REL, canonical_json_bytes(facts))
    except OSError as err:
        return task_failure("internal", f"cannot publish trace projection artifacts: {err}")

    return TaskResult(
        status="succeeded",
        value={
            "phase": projection.phase,
            "integrity": projection.integrity,
            "integrity_blocks_routing": facts.integrity_blocks_routing,
            "sufficient": facts.sufficient,
            "error_code": facts.error_code,
        },
    )


__all__ = [
    "TRACE_PROJECTION_REL",
    "TRACE_SUFFICIENCY_REL",
    "materialize_trace_projection",
]
