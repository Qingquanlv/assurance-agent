"""Execution orchestrator: run selected layers, build the gate, publish evidence.

run_change is the public entry consumed by `aa run`. It never fabricates: an
unselected or missing layer becomes a SKIPPED result and the quality gate
degrades accordingly.

Publication order is fixed by one invariant (P0-1): the trace projection folded
here, before the manifest exists, must be byte-identical to the projection
anyone folds later from the published manifest. That requires

1. the batch's result documents on disk *before* the fold (the fold reads them),
2. the per-file test digests computed *before* the fold and published unchanged,
3. one aware ``executed_at``, injected into the fold and written to the manifest,
4. the manifest published *after* the fold, so the fold cannot read a manifest
   that describes a batch it is still describing.

The sufficiency verdict derived from that projection is built here as one
``EvidenceCoverageEvaluation`` and reported through two channels of the gate
document — ``diagnostics["evidence_sufficiency"]`` and
``dimensions.coverage.evidence`` — while being adjudicated by neither: it must
not move ``final_status``, and a fold or policy failure is recorded rather than
raised, because an observation may not fail a run. A failure does append one
warning, so that "not evaluated" is visible to an operator rather than only to a
reader of the diagnostics blob. Routing on that evaluation is the dedicated
trace-sufficiency gate's job, materialized separately.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
import time
from typing import Any

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import ExecutionManifest, QualityGateResult
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.config import AaConfig
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageErrorCode,
    EvidenceCoverageEvaluation,
    evaluate_sufficiency,
)
from assurance_agent.evidence.trace import ExecutionFoldInput, fold_trace
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.execution.evidence import (
    atomic_write_bytes,
    publish_execution_manifest,
    publish_target_results,
)
from assurance_agent.workflow.execution.exec_config import load_coverage_config, load_perf_config
from assurance_agent.workflow.execution.results import CoverageResult
from assurance_agent.workflow.execution.runners import (
    parse_coverage_result,
    run_performance_target,
    run_pytest_target,
)
from assurance_agent.workflow.execution.scope import resolve_test_paths
from assurance_agent.workflow.execution.selection import resolve_selected_targets
from assurance_agent.workflow.execution.tree_hash import hash_product_tree, hash_test_tree
from assurance_agent.workflow.healing.safety import load_product_code_roots
from assurance_agent.workflow.report.quality_gate import build_quality_gate

TRACE_PROJECTION_NAME = "trace-projection.json"

# One warning per failure code, keyed on the code and nothing else. Exception text
# carries absolute paths and library-version wording, and this gate document is
# copied verbatim into `inspect/quality-gate-result.json` and into eval fixtures,
# so anything machine-specific travels with it and makes two runs of the same
# failure incomparable.
#
# The key type closes this table over the error vocabulary: a third
# `EvidenceCoverageErrorCode` cannot be added without a warning to go with it, or
# an operator would see nothing at all where an evaluation silently did not run.
# `test_every_error_code_has_exactly_one_operator_warning` pins the set equality
# that the annotation alone cannot state.
#
# Two things the wording has to be careful about, because an operator reading a
# warning acts on it:
#
# - **Which** gate is unaffected. This batch-scoped observation never moves
#   `QualityGateResult.final_status`, but the change-level
#   `trace-sufficiency-gate` does route on the same facts once the reconciled
#   projection is materialized, so "the gate verdict is unaffected" would read as
#   a claim about both.
# - `evidence_projection_missing` covers two situations, not one: no projection at
#   all, and a projection that was published and then refused by the evaluation
#   (contradictory row facts, a naive timestamp). An operator told only "could not
#   be projected" goes looking for a missing file that is sitting on disk.
_SHADOW_WARNINGS: dict[EvidenceCoverageErrorCode, str] = {
    "policy_error": (
        "EVIDENCE-SUFFICIENCY-NOT-EVALUATED: the evidence sufficiency policy could not be "
        "loaded or applied (error_code=policy_error). The quality gate verdict is unaffected; "
        "the independent trace-sufficiency gate routes on the change-level facts; see "
        "diagnostics.evidence_sufficiency."
    ),
    "evidence_projection_missing": (
        "EVIDENCE-SUFFICIENCY-NOT-EVALUATED: this change's evidence could not be projected, or "
        "the projection was published and then refused (error_code=evidence_projection_missing). "
        "The quality gate verdict is unaffected; the independent trace-sufficiency gate routes on "
        "the change-level facts; see diagnostics.evidence_sufficiency."
    ),
}


@dataclass(frozen=True)
class _ShadowObservation:
    """The typed evaluation plus the two facts that describe this run's channel.

    ``evaluation`` is the verdict-shaped part — the object a consumer acts on,
    and the one serialised into both places the gate reports it. ``projection``
    and ``error_type`` belong to no verdict: they say where the projection was
    written and which exception class stopped the evaluation, which only the
    diagnostics channel carries.
    """

    evaluation: EvidenceCoverageEvaluation
    projection: str | None = None
    error_type: str | None = None

    def diagnostics(self) -> dict[str, Any]:
        document = self.evaluation.to_json_dict()
        document["projection"] = self.projection
        if self.error_type is not None:
            document["error_type"] = self.error_type
        return document


_batch_id_lock = Lock()
_last_batch_tick = 0


def generate_batch_id() -> str:
    """Return a process-monotonic, nanosecond-resolution batch identifier.

    The prior second-resolution label let a fast rerun reuse an existing batch
    directory, so stale result files could be mistaken for current evidence.
    """
    global _last_batch_tick
    with _batch_id_lock:
        tick = max(time.time_ns(), _last_batch_tick + 1)
        _last_batch_tick = tick
    seconds, nanoseconds = divmod(tick, 1_000_000_000)
    prefix = datetime.fromtimestamp(seconds).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{nanoseconds:09d}"


def generate_executed_at() -> datetime:
    """The batch's authoritative instant, offset-aware.

    Separate from ``generate_batch_id`` because the batch id is a filesystem-safe
    label with no zone, while this is the value recency judgements compare
    against an aware ``as_of``.
    """
    return datetime.now(UTC)


def _strip(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


def _test_dir(config: AaConfig, attr: str, default: str) -> str:
    tests = getattr(config, "tests", None)
    value = getattr(tests, attr, None) if tests is not None else None
    return _strip(value) if isinstance(value, str) else default


def run_change(
    project_root: Path,
    change_dir: Path,
    config: AaConfig,
    *,
    batch_id: str | None = None,
) -> ExecutionManifest:
    change_id = change_dir.name
    batch_id = batch_id or generate_batch_id()
    execution_dir = change_dir / "execution"
    batch_dir = execution_dir / "runs" / batch_id

    selected = resolve_selected_targets(change_dir)
    cov_config = load_coverage_config(config)
    perf_config = load_perf_config(config)

    cov_package = cov_config.target_package if cov_config.enabled else None
    api = (
        run_pytest_target(
            project_root=project_root,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            target="api",
            test_dir=_test_dir(config, "api", "tests/api"),
            test_paths=resolve_test_paths(change_dir, "api"),
            cov_package=cov_package,
        )
        if selected.api
        else None
    )
    e2e = (
        run_pytest_target(
            project_root=project_root,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            target="e2e",
            test_dir=_test_dir(config, "e2e", "tests/e2e"),
            test_paths=resolve_test_paths(change_dir, "e2e"),
        )
        if selected.e2e
        else None
    )
    fuzz = (
        run_pytest_target(
            project_root=project_root,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            target="fuzz",
            test_dir=_test_dir(config, "fuzz", "tests/fuzz"),
            test_paths=resolve_test_paths(change_dir, "fuzz"),
        )
        if selected.fuzz
        else None
    )

    if selected.api:
        coverage = parse_coverage_result(
            change_id=change_id,
            batch_id=batch_id,
            batch_dir=batch_dir,
            threshold=cov_config.threshold,
        )
    else:
        coverage = CoverageResult(
            change_id=change_id,
            batch_id=batch_id,
            available=False,
            line_coverage=0.0,
            branch_coverage=0.0,
            threshold=cov_config.threshold,
            status="SKIPPED",
            skip_reason="api_unselected",
        )

    performance = (
        run_performance_target(
            project_root=project_root,
            change_dir=change_dir,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            perf_config=perf_config,
            test_paths=resolve_test_paths(change_dir, "performance"),
        )
        if selected.performance
        else None
    )

    # Phase 1 of publication: the fold below reads these documents off disk, so
    # they must be complete before it runs — and written only here, so no reader
    # can observe two byte states for one batch artifact.
    result_files = publish_target_results(
        execution_dir=execution_dir,
        batch_id=batch_id,
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        coverage=coverage,
        performance=performance,
    )

    test_tree = hash_test_tree(project_root)
    product_tree = hash_product_tree(project_root, load_product_code_roots(project_root))
    executed_at = generate_executed_at()
    current = ExecutionFoldInput(
        batch_id=batch_id,
        executed_at=executed_at,
        selected_targets=selected,
        test_files_sha256=test_tree.files,
    )
    shadow = _shadow_evidence_sufficiency(project_root, change_id, batch_dir, current)

    quality_gate = build_quality_gate(
        change_id=change_id,
        batch_id=batch_id,
        api=api,
        e2e=e2e,
        coverage=coverage,
        coverage_gate_mode=cov_config.gate_mode,
        fuzz=fuzz,
        performance=performance,
        evidence_coverage=shadow.evaluation,
    )
    # Attached after the verdict is decided, never passed into the decision.
    quality_gate = quality_gate.model_copy(update=_shadow_update(quality_gate, shadow))
    summary = _build_summary(change_id, batch_id, api, e2e, fuzz, coverage, performance, quality_gate)

    return publish_execution_manifest(
        execution_dir=execution_dir,
        change_id=change_id,
        batch_id=batch_id,
        selected_targets=selected,
        result_files=result_files,
        quality_gate=quality_gate,
        summary=summary,
        executed_at=executed_at,
        tests_tree_sha256=test_tree.aggregate,
        test_files_sha256=test_tree.files,
        product_tree_sha256=product_tree.aggregate,
    )


def _shadow_update(gate: QualityGateResult, shadow: _ShadowObservation) -> dict[str, Any]:
    """The only two gate fields a shadow observation may touch.

    Diagnostics, plus — when the evaluation did not run — one warning. The
    warning exists because diagnostics are addressed to machines: an operator
    reading the gate would otherwise have no way to tell a change whose evidence
    was judged sufficient from one where nothing was judged at all. It is
    appended to whatever the gate already warned about, and it moves no status.

    Informational, and specifically *not* a reason for ``PASS_WITH_WARNINGS``:
    that status means a dimension degraded, whereas this says a dimension was
    never judged. Nothing may infer a verdict from the presence of a warning —
    ``warnings`` is free text that consumers display, and ``final_status`` is the
    only verdict.
    """
    update: dict[str, Any] = {"diagnostics": {"evidence_sufficiency": shadow.diagnostics()}}
    code = shadow.evaluation.error_code
    warning = _SHADOW_WARNINGS.get(code) if code is not None else None
    if warning is not None:
        update["warnings"] = [*(gate.warnings or []), warning]
    return update


def _shadow_evidence_sufficiency(
    project_root: Path,
    change_id: str,
    batch_dir: Path,
    current: ExecutionFoldInput,
) -> _ShadowObservation:
    """Fold the batch, publish the projection, and judge it — for the record only.

    Every failure is recorded rather than raised. A change whose evidence cannot
    be projected, and a policy that cannot be loaded or applied, are both real
    problems, but neither is a reason for a test run to fail: this whole path is
    an observation. The error codes are the vocabulary a consumer dispatches on,
    so they are stable strings, not free text — and they have to name *which
    input was unusable*, not which line happened to raise, or an operator reading
    ``policy_error`` goes and audits a policy that was fine.

    Three guarded stages, each attributing its failures to the input it read:

    1. **the fold** — no projection at all, ``evidence_projection_missing``;
    2. **the policy load** — ``policy_error``;
    3. **the evaluation** — split by what the exception says about its two
       inputs. ``KeyError`` is the policy failing to declare what a case type
       requires, so ``policy_error``; ``ValueError``/``TypeError`` come from the
       projection's own facts contradicting each other or lacking a zone
       (``_reject_contradictory_facts``, ``_require_aware``), so
       ``evidence_projection_missing`` — the projection exists and is published,
       and its recorded path is where a reviewer reads the bytes that were
       refused. The policy-caused ``ValueError`` in ``_required_kinds`` (an
       evidence kind outside the vocabulary) is unreachable through
       ``load_policy``, whose literals reject it first, so it does not need a
       branch of its own here;
    4. **the evaluation's own construction** — guarded rather than left to
       propagate, because ``EvidenceCoverageEvaluation`` validates ``action``
       fail-closed and a policy that bypassed pydantic's literal raises there.
       Outside a guard that exception would fail a whole test run over an
       observation. Only the action can be rejected, and only a policy can carry
       a bad one, so it is ``policy_error``.

    The success path pairs the report with ``on_insufficient`` read from the same
    policy that produced it — copied, never interpreted, because concluding
    anything from it here would be the routing this module must not do.

    Failure diagnostics record the exception's *type*, never its message: the
    messages here name the absolute path of whatever could not be loaded, and
    two runs of the same failure have to be comparable byte for byte to be worth
    recording at all.
    """

    def failed(
        code: EvidenceCoverageErrorCode, exc: BaseException, projection: str | None
    ) -> _ShadowObservation:
        return _ShadowObservation(
            evaluation=EvidenceCoverageEvaluation.failed(code),
            projection=projection,
            error_type=type(exc).__name__,
        )

    try:
        projection = fold_trace(project_root, change_id, phase="execution", current=current)
        atomic_write_bytes(batch_dir / TRACE_PROJECTION_NAME, canonical_json_bytes(projection))
    except (AaError, OSError, ValueError) as exc:
        return failed("evidence_projection_missing", exc, None)

    projection_rel = f"runs/{current.batch_id}/{TRACE_PROJECTION_NAME}"
    try:
        policy = load_policy(project_root)
    except (AaError, OSError, ValueError) as exc:
        return failed("policy_error", exc, projection_rel)

    try:
        report = evaluate_sufficiency(projection, policy, as_of=current.executed_at)
    except KeyError as exc:
        return failed("policy_error", exc, projection_rel)
    except (TypeError, ValueError) as exc:
        return failed("evidence_projection_missing", exc, projection_rel)
    except (AaError, OSError) as exc:
        # Neither input is implicated: the evaluation reads no disk and raises no
        # domain error today. Retained as the net that keeps an observation from
        # failing a run, and attributed to the policy channel because that is the
        # code whose warning says "could not be loaded *or applied*".
        return failed("policy_error", exc, projection_rel)

    # A separate guard, so its one classification needs no reasoning about which
    # of two calls raised: only the action can be rejected here, and only a
    # policy can carry a bad one.
    try:
        evaluation = EvidenceCoverageEvaluation.evaluated(
            report=report, action=policy.evidence_sufficiency.on_insufficient
        )
    except ValueError as exc:
        return failed("policy_error", exc, projection_rel)
    return _ShadowObservation(evaluation=evaluation, projection=projection_rel)


def _build_summary(change_id, batch_id, api, e2e, fuzz, coverage, performance, gate) -> str:  # noqa: ANN001
    def line(label: str, r) -> str:  # noqa: ANN001
        if r is None:
            return f"- {label}: unselected"
        return f"- {label}: {r.status} total={r.total} passed={r.passed} failed={r.failed}"

    parts = [
        f"# Execution Summary — {change_id}",
        "",
        f"- Batch: {batch_id}",
        f"- Final Status: {gate.final_status}",
        "",
        line("API", api),
        line("E2E", e2e),
        line("Fuzz", fuzz),
        f"- Coverage: {coverage.status} line={coverage.line_coverage}%"
        if coverage
        else "- Coverage: unselected",
        f"- Performance: {performance.status}" if performance else "- Performance: unselected",
        "",
    ]
    return "\n".join(parts)
