"""``operation:materialize-trace-projection`` — publish the reconciled projection
and the facts the independent trace gate routes on.

The node runs at the end of ``inspect-with-issues``, after issue reconciliation —
the earliest point a *reconciled* projection exists for the change as a whole,
since the execution-phase fold the runner performs is per-batch and predates the
Problem join, so it cannot answer "does this case reach an open product bug".

It publishes and stops there. The ``trace-sufficiency-gate`` that routes on these
facts lives one level up, in ``assurance`` after ``healing``, because this node
runs once per authoritative batch — every healing rerun included — and a verdict
reached per batch would decide the run while the fixers were still working. Each
rerun overwrites both documents, so what the gate reads describes the last batch
this node saw.

That is only the same thing as "the tests on disk" because healing's post-fixer
aborts do not return to the gate: ``safety`` and ``safety-interrupt`` route to
``STOP``, since at that point the fixers have edited tests that no rerun has
executed. Re-running this node on such a path would not help — staleness is
measured against the batch's own ``executed_at``, so a refold over an unchanged
manifest republishes an unchanged document — which is why the schema stops the
run instead.

Three properties are load-bearing, and each of them exists to remove a way the
verdict could differ between two runs over the same bytes:

- **The judging instant comes from the batch, never a clock.**
  ``authoritative_batch_instant`` resolves it from the manifest's ``executed_at``
  (or the legacy batch-id instant), so replaying one change is idempotent. A
  change with no orderable batch is *not* judged at ``now`` as a fallback — it is
  recorded as ``evidence_projection_missing``, because a fallback clock is
  exactly the nondeterminism the field exists to prevent.
- **Failures are written, not raised.** A policy that cannot be loaded or applied
  is a fact the gate must see and route on (``needs_human_review``); failing the
  task instead would deny it the chance, and the workflow would stop with no
  document explaining why. Only an unresolvable change fails the task, because
  then there is no change directory to publish anything into.
- **Facts, never a route.** This operation does not decide
  ``pass``/``stop``/``needs_human_review``, and does not touch
  ``inspect/quality-gate-result.json`` or the execution manifest, whose
  ``final_status`` states what the *execution* found. Case sufficiency is a
  second, independent judgement (spec M1); mapping the facts onto a disposition
  is the gate's job, so there is one routing table rather than two that drift.

Both documents are published through ``atomic_write_bytes``, which is atomic per
file rather than across the pair. Ordering is what carries the rest: the projection
is written first, so a reader that finds the facts always finds the projection they
summarise, never the reverse.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.artifacts.models.trace import TraceGapCode, TraceProjection
from assurance_agent.artifacts.models.trace_sufficiency import (
    TraceInsufficientCase,
    TraceSufficiencyErrorCode,
    TraceSufficiencyFacts,
)
from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.exceptions import AaError
from assurance_agent.evidence.sufficiency import SufficiencyReport, evaluate_sufficiency
from assurance_agent.evidence.trace import authoritative_batch_instant, fold_trace
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace

TRACE_PROJECTION_REL = "inspect/trace-projection.json"
TRACE_SUFFICIENCY_REL = "inspect/trace-sufficiency.json"


def _judged(
    projection: TraceProjection, report: SufficiencyReport, digest: str, as_of: datetime
) -> TraceSufficiencyFacts:
    """The facts of a completed judgement.

    ``insufficient_cases`` keeps each case's reasons attached to the case rather
    than flattening them into one document-level set, because an operator acting
    on this needs "which case needs what" and a flattened set cannot answer it.
    ``sufficient`` is read from the report and not recomputed here, so the gate
    and ``aa verify`` cannot disagree about the same rows.

    ``policy.evidence_sufficiency.on_insufficient`` is deliberately *not* copied
    in: the gate reads the policy itself, so a stale copy here could tell one
    story while the policy in force told another. ``policy_digest`` records
    *which* policy produced these verdicts, which is the part the policy cannot
    tell you after the fact.
    """
    return TraceSufficiencyFacts(
        schema_version="1",
        change_id=projection.change_id,
        authoritative_batch_id=projection.authoritative_batch_id,
        policy_digest=digest,
        as_of=as_of,
        integrity=projection.integrity,
        integrity_blocks_routing=report.integrity_blocks_routing,
        sufficient=report.sufficient,
        has_open_problems=_has_open_problems(projection),
        error_code=None,
        insufficient_cases=tuple(
            TraceInsufficientCase(case_id=row.case_id, reason_codes=row.reason_codes)
            for row in report.insufficient_rows
        ),
        gap_codes=_gap_codes(projection),
    )


def _unjudged(projection: TraceProjection, error_code: TraceSufficiencyErrorCode) -> TraceSufficiencyFacts:
    """The facts of a judgement that never happened.

    ``sufficient=False`` is not a verdict of "insufficient" — ``error_code`` is
    what says no verdict was reached — but the field must hold *some* value, and
    ``False`` is the one a consumer that forgot to branch on ``error_code`` fails
    closed on. The projection's own facts (``integrity``, ``gap_codes``,
    ``has_open_problems``) survive, because they were read, not judged.
    """
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


def _has_open_problems(projection: TraceProjection) -> bool:
    """Whether any case reaches an open product bug (spec §9.5's routing subset).

    Reduced to one bit because that is all a gate can branch on; the per-case
    ``open_problem_ids`` stay in the projection for anyone who needs the detail.
    """
    return any(row.open_problem_ids for row in projection.rows)


def _gap_codes(projection: TraceProjection) -> tuple[TraceGapCode, ...]:
    """The distinct gap codes, sorted, so two runs over one projection agree."""
    return tuple(sorted({gap.code for gap in projection.gaps}))


def _judge(project_root: Path, projection: TraceProjection) -> TraceSufficiencyFacts:
    """Evaluate the projection under the change's policy, recording every failure.

    The error classification mirrors ``_shadow_evidence_sufficiency`` in the
    runner, and for the same reason: the code must name *which input was
    unusable*, or an operator reading ``policy_error`` audits a policy that was
    fine.

    - **no orderable batch instant** — the projection names no batch that can be
      placed in time, so there is no cutoff to judge against:
      ``evidence_projection_missing``;
    - **the policy load** — ``policy_error``;
    - **the evaluation** — ``KeyError`` is the policy failing to declare what a
      case type requires (``policy_error``); ``ValueError``/``TypeError`` come
      from the projection's own facts contradicting each other or lacking a zone
      (``evidence_projection_missing``). The published projection is where a
      reviewer reads the bytes that were refused.

    Composing the facts is deliberately *not* guarded. Every invariant the model
    enforces is already satisfied by construction — the report copies the
    projection's integrity level verbatim, ``sufficient`` and the insufficient rows
    are two readings of one tuple, and both the digest and the instant are in hand
    by this point — so a refusal here would mean this function contradicted itself.
    Neither error code describes that: both name an *input* that was unusable, and
    reporting a producer bug as ``policy_error`` would send an operator to audit a
    policy that was fine.
    """
    instant = authoritative_batch_instant(project_root, projection.change_id)
    if instant is None:
        return _unjudged(projection, "evidence_projection_missing")

    try:
        policy: Policy = load_policy(project_root)
    except (AaError, OSError, ValueError):
        return _unjudged(projection, "policy_error")

    try:
        report = evaluate_sufficiency(projection, policy, as_of=instant.as_of)
    except KeyError:
        return _unjudged(projection, "policy_error")
    except (TypeError, ValueError):
        return _unjudged(projection, "evidence_projection_missing")

    return _judged(projection, report, policy_digest(policy), instant.as_of)


def materialize_trace_projection(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Publish ``inspect/trace-projection.json`` and ``inspect/trace-sufficiency.json``.

    A change whose evidence is absent is a *success*: the fold reports what it
    could not read as gaps and ``integrity="incomplete"``, which is a fact the
    gate must route on, not an error that hides it. Only an unresolvable change
    fails, because a projection that cannot be attributed to a change directory
    has nowhere to be written and nothing to be about.
    """
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
            "change_id": facts.change_id,
            "authoritative_batch_id": facts.authoritative_batch_id,
            "integrity": facts.integrity,
            "integrity_blocks_routing": facts.integrity_blocks_routing,
            "sufficient": facts.sufficient,
            "has_open_problems": facts.has_open_problems,
            "error_code": facts.error_code,
            "insufficient_case_ids": [case.case_id for case in facts.insufficient_cases],
        },
    )


__all__ = [
    "TRACE_PROJECTION_REL",
    "TRACE_SUFFICIENCY_REL",
    "materialize_trace_projection",
]
