"""Apply an evidence policy to a trace projection (spec v3 §8/§13).

``fold_trace`` deliberately produces no judgement: it reads documents and never
a clock or a policy, so the same sources always fold to the same bytes. Every
question of the form "is this evidence *good enough, now*" lives here instead,
and takes its "now" as an explicit ``as_of`` argument. Two call sites need that
split: the runner evaluates at the batch's ``executed_at`` (Task 8/9) while
``aa verify`` evaluates at wall-clock time (Task 11). Sharing one function keeps
the two channels from drifting into two definitions of sufficiency.

The evaluation is a pure function of ``(projection, policy, as_of)``:

- **Recency** is one cutoff, ``as_of - recency_hours``, compared with ``>=`` so
  a timestamp exactly on the boundary is still fresh. All comparisons are
  aware-to-aware; a naive ``as_of`` is a ``TypeError`` rather than a guess about
  which zone the caller meant, and a naive timestamp inside the projection is
  the same error, named by case.
- **Kinds** are conjunctive: a row is sufficient when every kind its case type
  requires holds. ``covered``/``fuzz_run``/``perf_run`` read projection facts;
  ``execution_recent`` and ``pass_status`` are derived here from
  ``latest_execution`` and ``freshest_pass`` *independently* — a batch that ran
  the case and failed is recent evidence but not a pass.
- **Rows that require no automation are exempt** from all of it. Spec §10 says a
  case with ``automation.required=false`` and no mapped test is a row state, not
  a gap; judging it against ``covered``/``execution_recent`` would contradict
  that and route every manual case to ``on_insufficient`` (``require_human`` by
  default). The exemption keys on ``automation_required`` — the fact that
  carries the decision — never on ``coverage_state``, which is a consequence.
- **Stability** matters because these codes end up in gate output and in verify's
  gap list: ``missing_kinds`` is emitted in the policy vocabulary's own order
  (never the order an operator happened to type), and ``reason_codes`` is
  index-aligned with it, one code per missing kind.

What this module does *not* do: it never *routes* on ``gaps`` or ``integrity``.
A projection that failed to read its inputs is a fail-closed verdict for the
consumer (spec §13 orders blocking gaps *before* sufficiency), and softening
that here would let ``on_insufficient: warn`` excuse a missing input. The report
does carry ``integrity`` verbatim, so a consumer holding only the report can see
what it must judge first instead of reaching back to the projection.

``EvidenceCoverageEvaluation`` is the shape that verdict travels in. It lives
here, beside the report, because it is the report *plus the policy it was judged
under* — the smallest thing a consumer can act on — and because pairing them at
the producer is what stops two consumers from inventing two pairings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict

from assurance_agent.artifacts.models.policy import (
    EvidenceKind,
    EvidenceSufficiency,
    PlanCheckAction,
    Policy,
)
from assurance_agent.artifacts.models.trace import TraceExecution, TraceProjection, TraceRow

ExecutionState = Literal["never_run", "stale", "fresh"]

# Spelled out rather than imported, because `TraceProjection` declares it inline;
# `test_the_report_integrity_vocabulary_tracks_the_projection` pins the two together.
TraceIntegrity = Literal["complete", "complete_with_gaps", "incomplete"]

# One code per (missing kind × the fact that made it missing). The temporal kinds
# split because "never happened" and "happened too long ago" call for different
# actions — run the suite vs. re-run it — and the atemporal ones do not split
# because there is only one way to lack them.
SufficiencyReasonCode = Literal[
    "not_covered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "never_passed",
    "pass_stale",
]

# The canonical order of `missing_kinds` / `reason_codes`, taken from the policy
# vocabulary itself so a new kind cannot be ordered inconsistently by two
# modules. Policies may list kinds in any order; the report never varies with it.
_KIND_ORDER: tuple[EvidenceKind, ...] = get_args(EvidenceKind)


class RowVerdict(BaseModel):
    """One case's sufficiency, with the reason it fell short."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    sufficient: bool
    missing_kinds: tuple[EvidenceKind, ...]
    reason_codes: tuple[SufficiencyReasonCode, ...]
    execution_state: ExecutionState


class SufficiencyReport(BaseModel):
    """Per-case verdicts plus the inputs that decided them.

    ``as_of`` and ``recency_hours`` are echoed because a stored report — Task 8
    writes one into quality-gate diagnostics — is otherwise uninterpretable: the
    same rows are sufficient or not depending on a cutoff nothing else records.
    ``integrity`` is copied from the projection for a sharper reason: it is what
    a consumer must judge *before* these verdicts (spec §13), and a report that
    omitted it would let the gate route on ``sufficient`` alone without ever
    noticing the projection could not read its inputs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    as_of: AwareDatetime
    recency_hours: int
    integrity: TraceIntegrity
    rows: tuple[RowVerdict, ...]

    @property
    def sufficient(self) -> bool:
        """Whether every row cleared its policy — and nothing more.

        Deliberately blind to ``integrity``: routing is the consumer's decision,
        and folding it in here would hide one verdict inside another. A report
        with no rows is therefore vacuously sufficient, which is exactly why
        ``integrity`` travels alongside — zero rows means the fold found no
        readable case document, i.e. ``integrity="incomplete"``.
        """
        return all(row.sufficient for row in self.rows)

    @property
    def insufficient_rows(self) -> tuple[RowVerdict, ...]:
        """The gap list, in report order. Named for the rows, not as the negation
        of ``sufficient``, so the two are not mistaken for a bool pair."""
        return tuple(row for row in self.rows if not row.sufficient)

    @property
    def integrity_blocks_routing(self) -> bool:
        """Whether ``sufficient`` must not be believed — spec §13's ordering, as a fact.

        **Consumers MUST branch on this before reading ``sufficient``.** Carrying
        ``integrity`` made the obligation visible; it did not make it checkable,
        because every consumer still had to know which of the three levels forbids
        believing the row verdicts, and one that got it wrong looked identical to
        one that got it right. Naming the comparison once leaves a single place to
        be wrong, and forces a new integrity level to be decided here rather than
        defaulting into "does not block".

        ``incomplete`` blocks and the other two do not: it is the level where an
        input could not be read at all, so ``rows`` is not the change's row set
        and no verdict over it means anything — most sharply when the fold read
        nothing and ``rows=()`` makes ``sufficient`` vacuously ``True``.
        ``complete_with_gaps`` is the opposite situation: every input was read and
        the absences are *known*, which is exactly what ``sufficient`` already
        judges, so blocking on it would double-count the same fact.

        Deliberately a fact and not a disposition. It says the verdicts are
        unusable, never what to do about that; mapping it to
        ``pass``/``needs_human``/``stop`` is the independent trace-sufficiency
        gate's job (Task 11). Derived rather than stored, so no copy of it can
        contradict the ``integrity`` it came from.
        """
        return self.integrity == "incomplete"


# The two ways adjudication can fail to happen at all, as opposed to concluding
# that evidence is thin. Stable strings rather than free text because they are
# the vocabulary a consumer dispatches on.
EvidenceCoverageErrorCode = Literal["evidence_projection_missing", "policy_error"]

_ACTIONS: frozenset[str] = frozenset(get_args(PlanCheckAction))
_ERROR_CODES: frozenset[str] = frozenset(get_args(EvidenceCoverageErrorCode))


@dataclass(frozen=True)
class EvidenceCoverageEvaluation:
    """What one change's evidence amounts to, as a two-state discriminated union.

    This is the whole interface between whoever *produces* an evidence judgement
    (the runner, at the batch's ``executed_at``) and whoever *acts* on one. A
    bare ``SufficiencyReport | None`` was the alternative, and it is not enough:
    ``None`` conflates "the fold could not read its inputs" with "the policy
    could not be applied", and it carries no record of which policy produced the
    verdicts, so a stored report cannot be interpreted later.

    Exactly two states are constructible:

    - **evaluated** — ``report`` and ``action``, no ``error_code``;
    - **failed** — ``error_code`` alone.

    ``action`` is ``policy.evidence_sufficiency.on_insufficient`` copied
    verbatim, not a verdict derived from it. Producing a disposition here would
    put one routing table in the producer and a second one in the consumer, and
    the two would drift; stating the policy instead leaves exactly one.

    Two obligations from Task 7 survive into this object rather than being
    re-derived by each consumer:

    - **Integrity is judged first.** A consumer **MUST** branch on
      ``report.integrity_blocks_routing`` before reading ``report.sufficient``: a
      projection that could not read its inputs yields ``rows=()``, which is
      *vacuously* sufficient, so a consumer routing on ``sufficient`` alone
      passes the one case it most needs to stop on. The predicate carries that
      ordering mechanically rather than leaving each consumer to rediscover which
      integrity level forbids believing the verdicts. Incomplete integrity is
      deliberately **not** an ``error_code``: the evaluation did run, on partial
      inputs, and saying otherwise would lose the verdicts it did reach.
    - **Manual cases are exempt, not gaps.** A case with
      ``automation.required=false`` is a row state (spec §10), and
      ``evaluate_sufficiency`` never marks it insufficient. So an all-manual
      change reaches a consumer as a *sufficient* report even under the default
      ``require_human``; nothing downstream may infer a gap from the presence of
      an ``action``, which says what to do *if* a row falls short.

    What this object does not have is a gate status. Under the accepted M1
    architecture case sufficiency does not move
    ``QualityGateResult.final_status`` — that field states what the execution
    found — and the ``report × action/error → disposition`` mapping belongs to
    the dedicated ``trace-sufficiency-gate`` materialized in Task 11. Until then
    the evaluation is reported and not routed: ``build_quality_gate`` attaches
    its dump to ``dimensions.coverage.evidence`` and reads nothing back.
    """

    report: SufficiencyReport | None
    action: PlanCheckAction | None
    error_code: EvidenceCoverageErrorCode | None

    def __post_init__(self) -> None:
        if self.error_code is None:
            if self.report is None:
                raise ValueError("an evaluated state requires a report (or an error_code saying why not)")
            if self.action is None:
                raise ValueError("an evaluated state requires the policy action it was judged under")
        else:
            if self.error_code not in _ERROR_CODES:
                raise ValueError(
                    f"unknown error_code {self.error_code!r}; expected one of {sorted(_ERROR_CODES)}"
                )
            if self.report is not None:
                raise ValueError(f"a {self.error_code} evaluation cannot also carry a report")
            if self.action is not None:
                raise ValueError(f"a {self.error_code} evaluation cannot also carry an action")
        # Checked after the state split so the message names the field rather
        # than the mixture: an out-of-vocabulary action is a typo at the call
        # site, not a confused state. Unknown values must raise rather than be
        # stored, because a consumer's routing table would meet them as an
        # unmatched branch — a failure that routes as a success.
        if self.action is not None and self.action not in _ACTIONS:
            raise ValueError(f"unknown action {self.action!r}; expected one of {sorted(_ACTIONS)}")

    @classmethod
    def evaluated(cls, *, report: SufficiencyReport, action: PlanCheckAction) -> EvidenceCoverageEvaluation:
        """The success state, named so a call site cannot forget ``action``."""
        return cls(report=report, action=action, error_code=None)

    @classmethod
    def failed(cls, error_code: EvidenceCoverageErrorCode) -> EvidenceCoverageEvaluation:
        """The state where nothing was judged, and the code saying why."""
        return cls(report=None, action=None, error_code=error_code)

    def to_json_dict(self) -> dict[str, Any]:
        """The one serialisation, shared by every channel that reports this.

        All three keys are always present: a persisted document that sometimes
        omits a key and sometimes nulls it forces every reader to ask the same
        question two ways. The report is dumped by its own model rather than
        summarised here, so this cannot drift from what ``aa verify`` reads.
        """
        return {
            "report": None if self.report is None else self.report.model_dump(mode="json"),
            "action": self.action,
            "error_code": self.error_code,
        }


def _require_aware(value: datetime, label: str) -> datetime:
    if value.utcoffset() is None:
        raise TypeError(f"{label} must be timezone-aware, got {value!r}")
    return value


def _is_recent(execution: TraceExecution | None, cutoff: datetime, label: str) -> bool | None:
    """``None`` when the execution does not exist, else whether it beat the cutoff."""
    if execution is None:
        return None
    return _require_aware(execution.ts, label) >= cutoff


@dataclass(frozen=True)
class _RowFacts:
    """Every temporal fact about one row, resolved before any policy is read.

    Both timestamps are validated here rather than inside the kind that happens
    to need them: whether a projection carries a naive value must not depend on
    what the policy asks for, or a naive ``freshest_pass`` would pass unnoticed
    under the default policy and only surface when someone adds ``pass_status``.
    """

    execution_state: ExecutionState
    pass_is_recent: bool | None

    @classmethod
    def of(cls, row: TraceRow, cutoff: datetime) -> _RowFacts:
        latest = _is_recent(row.latest_execution, cutoff, f"{row.case_id}.latest_execution.ts")
        return cls(
            execution_state="never_run" if latest is None else ("fresh" if latest else "stale"),
            pass_is_recent=_is_recent(row.freshest_pass, cutoff, f"{row.case_id}.freshest_pass.ts"),
        )


def _reject_contradictory_facts(row: TraceRow) -> None:
    """Refuse a row whose two automation facts disagree.

    ``fold_trace`` derives ``coverage_state`` *from* ``automation_required``: an
    unautomated case is ``not_required`` and an automation-required one without a
    mapped test is ``uncovered``. Either mixture below therefore cannot come from
    a real fold, and picking one fact to believe would silently decide whether
    the row is exempt — the one judgement this module must never guess.
    """
    if row.automation_required and row.coverage_state == "not_required":
        raise ValueError(f"{row.case_id}: coverage_state='not_required' contradicts automation_required=True")
    if not row.automation_required and row.coverage_state == "uncovered":
        raise ValueError(f"{row.case_id}: coverage_state='uncovered' contradicts automation_required=False")


def _missing_reason(kind: EvidenceKind, row: TraceRow, facts: _RowFacts) -> SufficiencyReasonCode | None:
    """The reason ``kind`` is missing from ``row``, or ``None`` when it holds.

    Satisfaction and reason are one decision, so they are computed once: a row
    can never report a missing kind without a code, or a code without the kind.
    """
    match kind:
        case "covered":
            # Only rows that must be automated reach this: `not_required` is
            # exempt earlier, and `_reject_contradictory_facts` rules out the
            # mixtures where that would not hold.
            return None if row.coverage_state == "covered" else "not_covered"
        case "execution_recent":
            if facts.execution_state == "fresh":
                return None
            return "never_run" if facts.execution_state == "never_run" else "execution_stale"
        case "fuzz_run":
            return None if "fuzz_run" in row.atemporal_kinds_present else "fuzz_run_missing"
        case "perf_run":
            return None if "perf_run" in row.atemporal_kinds_present else "perf_run_missing"
        case "pass_status":
            if facts.pass_is_recent is None:
                return "never_passed"
            return None if facts.pass_is_recent else "pass_stale"
    # Unreachable while every `EvidenceKind` literal has a case above. It exists
    # for the literal added *without* one: such a kind survives `_required_kinds`
    # (it is in the vocabulary) and would otherwise count as satisfied.
    # `test_every_policy_kind_has_a_rule` is what makes that addition red here.
    raise ValueError(  # pragma: no cover - unreachable, see above
        f"evidence kind {kind!r} is accepted by policy but has no sufficiency rule"
    )


def _required_kinds(evidence: EvidenceSufficiency, row: TraceRow) -> tuple[EvidenceKind, ...]:
    """This row's required kinds, deduplicated and canonically ordered.

    A kind outside the policy vocabulary raises instead of being skipped:
    dropping it would make the row *more* likely to pass, which is exactly the
    vacuous success the closed ``EvidenceKind`` literal exists to prevent. A
    case type the policy never declared is a ``KeyError`` for the same reason —
    Task 6's validator forbids it, and a bypassed policy must not fail open.
    """
    try:
        declared = evidence.required_kinds[row.case_type]
    except KeyError:
        raise KeyError(
            f"policy.evidence_sufficiency.required_kinds has no entry for case type "
            f"{row.case_type!r} (required by case {row.case_id})"
        ) from None
    unknown = sorted(set(declared) - set(_KIND_ORDER))
    if unknown:
        raise ValueError(f"unknown evidence kinds in policy for {row.case_type}: {', '.join(unknown)}")
    return tuple(kind for kind in _KIND_ORDER if kind in declared)


def _row_verdict(row: TraceRow, evidence: EvidenceSufficiency, cutoff: datetime) -> RowVerdict:
    _reject_contradictory_facts(row)
    facts = _RowFacts.of(row, cutoff)
    # Resolved before the exemption, never inside it: a policy that cannot state
    # what this row's case type requires is broken regardless of whether this row
    # happens to need the answer. Asking only for automated rows would let a
    # malformed policy pass unnoticed on any all-manual change, and then fail on
    # the first change that automates something.
    required = _required_kinds(evidence, row)
    missing: dict[EvidenceKind, SufficiencyReasonCode] = (
        {}
        if not row.automation_required
        else {kind: reason for kind in required if (reason := _missing_reason(kind, row, facts)) is not None}
    )
    return RowVerdict(
        case_id=row.case_id,
        sufficient=not missing,
        missing_kinds=tuple(missing),
        reason_codes=tuple(missing.values()),
        execution_state=facts.execution_state,
    )


def evaluate_sufficiency(
    projection: TraceProjection, policy: Policy, *, as_of: datetime
) -> SufficiencyReport:
    """Judge every projected row against ``policy`` at the instant ``as_of``.

    Verdicts follow ``projection.rows`` one for one and in order — including the
    rows exempt from automation, which stay countable and auditable — so a report
    can be zipped with the projection it came from; the fold already emits those
    rows case_id-sorted and unique.
    """
    _require_aware(as_of, "as_of")
    evidence = policy.evidence_sufficiency
    cutoff = as_of - timedelta(hours=evidence.recency_hours)
    return SufficiencyReport(
        change_id=projection.change_id,
        as_of=as_of,
        recency_hours=evidence.recency_hours,
        integrity=projection.integrity,
        rows=tuple(_row_verdict(row, evidence, cutoff) for row in projection.rows),
    )


def build_evidence_coverage_evaluation(
    projection: TraceProjection | None,
    policy: Policy | None,
    *,
    as_of: datetime,
) -> EvidenceCoverageEvaluation:
    """Produce an ``EvidenceCoverageEvaluation`` from optional fold/policy inputs.

    Callers that already hold a live projection and policy (the runner) can build
    the evaluated state directly; this helper is the shared null-guard used by
    verify/CLI paths that may lack either input.
    """
    if projection is None:
        return EvidenceCoverageEvaluation.failed("evidence_projection_missing")
    if policy is None:
        return EvidenceCoverageEvaluation.failed("policy_error")
    report = evaluate_sufficiency(projection, policy, as_of=as_of)
    return EvidenceCoverageEvaluation.evaluated(
        report=report,
        action=policy.evidence_sufficiency.on_insufficient,
    )


# Compatibility alias for callers that accept either wire shape.
SufficiencyReportLike = SufficiencyReport


__all__ = [
    "EvidenceCoverageErrorCode",
    "EvidenceCoverageEvaluation",
    "ExecutionState",
    "RowVerdict",
    "SufficiencyReasonCode",
    "SufficiencyReport",
    "SufficiencyReportLike",
    "TraceIntegrity",
    "build_evidence_coverage_evaluation",
    "evaluate_sufficiency",
]
