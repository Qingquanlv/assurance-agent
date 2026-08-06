"""Coverage-repair probe: in-memory metrics adjudication + typed brief.

Builds an in-memory ``MetricsDocument`` from the newest complete batch and feeds
the same ``evaluate_metrics_sufficiency`` / project policy the main gate uses.
Never writes ``inspect/metrics.json`` (write-once ownership stays with
``materialize-pr-metrics``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.coverage_gaps import (
    COVERAGE_GAPS_REL,
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_BRIEF_REL,
    CoverageRepairBrief,
    DeferredItem,
    RepairableGapKind,
    RepairItem,
)
from assurance_agent.artifacts.models.metrics import MetricKey
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.case_inputs import CaseArtifactError
from assurance_agent.workflow.metrics.pr_metrics import (
    build_metrics_document,
    select_latest_complete_batch,
)

COVERAGE_REPAIR_BRIEF_MD_REL = "coverage-repair/brief.md"

_REPAIRABLE_KINDS: frozenset[str] = frozenset(get_args(RepairableGapKind))

# Which metric shortfall each repairable gap kind serves (design §6.2 / RepairItem).
_SERVING_METRIC: dict[str, MetricKey] = {
    "uncovered_required_case": "constraint_coverage",
    "stale_required_case": "constraint_coverage",
    "constraint_without_property": "constraint_coverage",
    "matrix_cell_unasserted": "auth_matrix_coverage",
}

_HINTS: dict[str, str] = {
    "uncovered_required_case": "Add or bind a test for the required case",
    "stale_required_case": "Refresh execution evidence for the required case",
    "constraint_without_property": "Add a property assertion for the constraint key",
    "matrix_cell_unasserted": "Add a parameterized auth-matrix assertion for the cell",
}


def _load_coverage_gaps(change_dir: Path) -> tuple[CoverageGap, ...]:
    """Best-effort read of ``inspect/coverage-gaps.json``; missing/invalid → empty."""
    path = Path(change_dir) / COVERAGE_GAPS_REL
    if not path.is_file():
        return ()
    try:
        document = CoverageGapsDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return ()
    return document.gaps


def _locator_label(locator: CoverageGapLocator) -> str:
    if locator.case_id:
        return f"case_id={locator.case_id}"
    if locator.constraint_key:
        return f"constraint_key={locator.constraint_key}"
    if locator.cell:
        return f"cell={locator.cell}"
    if locator.cluster_key:
        return f"cluster_key={locator.cluster_key}"
    return "—"


def _partition_gaps(
    gaps: tuple[CoverageGap, ...],
) -> tuple[tuple[RepairItem, ...], tuple[DeferredItem, ...]]:
    repair: list[RepairItem] = []
    deferred: list[DeferredItem] = []
    for gap in gaps:
        if gap.layer == "declaration":
            deferred.append(DeferredItem(kind=gap.kind, locator=gap.locator, reason="declaration_layer"))
            continue
        if gap.kind == "unmapped_test_cluster":
            deferred.append(DeferredItem(kind=gap.kind, locator=gap.locator, reason="unmapped_cluster"))
            continue
        if gap.kind not in _REPAIRABLE_KINDS:
            deferred.append(DeferredItem(kind=gap.kind, locator=gap.locator, reason="not_repairable_metric"))
            continue
        metric = _SERVING_METRIC[gap.kind]
        if metric == "adversarial_clean":
            # Promotion-track boolean; never a coverage-repair delivery (design §6.1).
            continue
        repair.append(
            RepairItem(
                kind=gap.kind,  # type: ignore[arg-type]
                locator=gap.locator,
                metric=metric,
                hint=_HINTS.get(gap.kind, ""),
            )
        )
    return tuple(repair), tuple(deferred)


def _render_brief_md(brief: CoverageRepairBrief) -> str:
    lines = [
        f"# Coverage repair brief — {brief.change_id}",
        "",
        f"- batch_id: `{brief.batch_id}`",
        f"- probe_verdict: `{brief.probe_verdict}`",
        f"- eligible: `{brief.eligible}`",
        "",
        "## Shortboards",
        "",
    ]
    if brief.shortboards:
        lines.append("| metric | code | detail |")
        lines.append("| --- | --- | --- |")
        for board in brief.shortboards:
            detail = (board.detail or "").replace("|", "\\|")
            lines.append(f"| {board.metric} | {board.code} | {detail} |")
    else:
        lines.append("_none_")
    lines.extend(["", "## Repair items", ""])
    if brief.repair_items:
        lines.append("| metric | shortboard code | locator | hint |")
        lines.append("| --- | --- | --- | --- |")
        shortboard_by_metric = {s.metric: s.code for s in brief.shortboards}
        for item in brief.repair_items:
            code = shortboard_by_metric.get(item.metric, "—")
            locator = _locator_label(item.locator).replace("|", "\\|")
            hint = item.hint.replace("|", "\\|")
            lines.append(f"| {item.metric} | {code} | {locator} | {hint} |")
    else:
        lines.append("_none_")
    lines.extend(["", "## Deferred to intake", ""])
    if brief.deferred_to_meetup:
        lines.append("| kind | reason | locator |")
        lines.append("| --- | --- | --- |")
        for item in brief.deferred_to_meetup:
            locator = _locator_label(item.locator).replace("|", "\\|")
            lines.append(f"| {item.kind} | {item.reason} | {locator} |")
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines)


def build_repair_brief(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
    computed_at: datetime,
) -> CoverageRepairBrief:
    """Adjudicate the newest complete batch and partition coverage gaps into a brief.

    Writes nothing. Uses ``load_policy(project_root)`` so a project
    ``.aa/policy.yaml`` override stays aligned with the main gate.
    """
    batch_id = select_latest_complete_batch(change_dir)
    if batch_id is None:
        return CoverageRepairBrief(
            change_id=change_id,
            batch_id=None,
            probe_verdict="reject",
            eligible=False,
            computed_at=computed_at,
        )

    document = build_metrics_document(
        change_dir=change_dir,
        project_root=project_root,
        change_id=change_id,
        computed_at=computed_at,
        batch_id=batch_id,
    )
    decision = evaluate_metrics_sufficiency(
        document,
        load_policy(project_root).evidence_sufficiency,
    )

    if decision.verdict != "needs_human":
        return CoverageRepairBrief(
            change_id=change_id,
            batch_id=batch_id,
            probe_verdict=decision.verdict,
            eligible=False,
            shortboards=decision.shortboards,
            computed_at=computed_at,
        )

    gaps = _load_coverage_gaps(change_dir)
    repair_items, deferred = _partition_gaps(gaps)
    return CoverageRepairBrief(
        change_id=change_id,
        batch_id=batch_id,
        probe_verdict=decision.verdict,
        eligible=bool(repair_items),
        shortboards=decision.shortboards,
        repair_items=repair_items,
        deferred_to_meetup=deferred,
        computed_at=computed_at,
    )


def probe_coverage_repair_need_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Probe coverage-repair eligibility; write ``coverage-repair/brief.{json,md}``."""
    del task  # no with-fields required
    change_id = context.change_id or workspace.change_dir.name
    try:
        brief = build_repair_brief(
            change_dir=workspace.change_dir,
            project_root=workspace.project_root,
            change_id=change_id,
            computed_at=datetime.now(tz=UTC),
        )
    except CaseArtifactError as err:
        return task_failure("invalid_input", str(err))

    out_json = workspace.change_dir / COVERAGE_REPAIR_BRIEF_REL
    out_json.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out_json, canonical_json_bytes(brief))
    out_md = workspace.change_dir / COVERAGE_REPAIR_BRIEF_MD_REL
    out_md.write_text(_render_brief_md(brief), encoding="utf-8")
    return TaskResult(
        status="succeeded",
        value={
            "eligible": brief.eligible,
            "probe_verdict": brief.probe_verdict,
            "batch_id": brief.batch_id,
            "repair_item_count": len(brief.repair_items),
        },
    )


__all__ = [
    "COVERAGE_REPAIR_BRIEF_MD_REL",
    "build_repair_brief",
    "probe_coverage_repair_need_operation",
]
