"""Coverage-repair probe + safety: in-memory metrics adjudication and mechanical gates.

Builds an in-memory ``MetricsDocument`` from the newest complete batch and feeds
the same ``evaluate_metrics_sufficiency`` / project policy the main gate uses.
Never writes ``inspect/metrics.json`` (write-once ownership stays with
``materialize-pr-metrics``).

Safety derives the change set from ``diff_trees(baseline, current)`` only; the
skill's apply-summary is a cross-check, never a judgment input (design v4-2).
"""

from __future__ import annotations

import hashlib
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
    COVERAGE_REPAIR_APPLY_SUMMARY_REL,
    COVERAGE_REPAIR_BASELINE_REL,
    COVERAGE_REPAIR_BRIEF_REL,
    COVERAGE_REPAIR_SAFETY_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    DeferredItem,
    RepairableGapKind,
    RepairItem,
)
from assurance_agent.artifacts.models.metrics import MetricKey
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.change_location import resolve_change
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.execution.tree_hash import (
    diff_trees,
    hash_product_tree,
    hash_test_tree,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.healing.safety import (
    load_product_code_roots,
    _scan_skip_xfail_markers,
)
from assurance_agent.workflow.metrics.case_inputs import CaseArtifactError
from assurance_agent.workflow.metrics.pr_metrics import (
    build_metrics_document,
    select_latest_complete_batch,
)


class CoverageRepairSafetyError(Exception):
    """Missing or unreadable coverage-repair safety inputs."""


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
    if brief.deferred_to_intake:
        lines.append("| kind | reason | locator |")
        lines.append("| --- | --- | --- |")
        for item in brief.deferred_to_intake:
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
        deferred_to_intake=deferred,
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


def declaration_roots(project_root: Path, change_id: str) -> tuple[str, ...]:
    """The single definition of the declaration tree. Both sides must call this."""
    change_rel = resolve_change(project_root, change_id).path.relative_to(project_root).as_posix()
    return ("qa/cases", ".aa", f"{change_rel}/cases")


def mint_attempt_token(
    *,
    change_id: str,
    attempt: int,
    test_tree_sha256: str,
    product_tree_sha256: str,
    declaration_tree_sha256: str,
) -> str:
    """Mint the per-attempt binding token (must include ``attempt``, design v4-5)."""
    preimage = f"{change_id}:{attempt}:{test_tree_sha256}:{product_tree_sha256}:{declaration_tree_sha256}"
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]


def _normalize_repo_rel(raw: str, project_root: Path) -> str:
    """Normalize a reported path to repo-relative POSIX (tree_hash form)."""
    text = raw.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    candidate = Path(text)
    root = project_root.resolve()
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            return text
    return Path(text).as_posix()


def _locator_tokens(locator: CoverageGapLocator) -> tuple[str, ...]:
    return tuple(
        value
        for value in (locator.case_id, locator.constraint_key, locator.cell, locator.cluster_key)
        if value
    )


def _path_explained_by_locators(
    project_root: Path,
    rel: str,
    locators: tuple[CoverageGapLocator, ...],
) -> bool:
    """Whether any briefed locator can explain a mechanically changed test path."""
    rel_norm = rel.replace("\\", "/").lower()
    tokens: list[str] = []
    for locator in locators:
        for token in _locator_tokens(locator):
            tokens.append(token.lower())
        if locator.cluster_key and _normalize_repo_rel(locator.cluster_key, project_root) == rel:
            return True
    if any(token in rel_norm for token in tokens):
        return True
    try:
        text = (project_root / rel).read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return any(token in text for token in tokens)


def _load_baseline(change_dir: Path) -> CoverageRepairBaseline:
    path = Path(change_dir) / COVERAGE_REPAIR_BASELINE_REL
    if not path.is_file():
        raise CoverageRepairSafetyError(
            f"missing {COVERAGE_REPAIR_BASELINE_REL}: cannot compute change set without a baseline"
        )
    try:
        return CoverageRepairBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as err:
        raise CoverageRepairSafetyError(f"unreadable {COVERAGE_REPAIR_BASELINE_REL}: {err}") from err


def _load_apply_summary(change_dir: Path) -> CoverageRepairApplySummary:
    path = Path(change_dir) / COVERAGE_REPAIR_APPLY_SUMMARY_REL
    if not path.is_file():
        raise CoverageRepairSafetyError(
            f"missing {COVERAGE_REPAIR_APPLY_SUMMARY_REL}: repair declared output is required"
        )
    try:
        return CoverageRepairApplySummary.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as err:
        raise CoverageRepairSafetyError(f"unreadable {COVERAGE_REPAIR_APPLY_SUMMARY_REL}: {err}") from err


def _load_brief_locators(change_dir: Path) -> tuple[CoverageGapLocator, ...]:
    path = Path(change_dir) / COVERAGE_REPAIR_BRIEF_REL
    if not path.is_file():
        return ()
    try:
        brief = CoverageRepairBrief.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return ()
    return tuple(item.locator for item in brief.repair_items)


def compute_coverage_repair_safety(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
) -> CoverageRepairSafetyCheck:
    """Derive ``CoverageRepairSafetyCheck`` from baseline × live trees (+ summary cross-check).

    Judgment inputs are mechanical ``diff_trees`` facts only. The apply-summary is
    copied into ``summary_*`` fields and compared; it never shrinks the change set.
    """
    baseline = _load_baseline(change_dir)
    summary = _load_apply_summary(change_dir)

    test_changed = tuple(diff_trees(baseline.test_files_sha256, hash_test_tree(project_root).files))
    product_changed = tuple(
        diff_trees(
            baseline.product_files_sha256,
            hash_product_tree(project_root, load_product_code_roots(project_root)).files,
        )
    )
    declaration_changed = tuple(
        diff_trees(
            baseline.declaration_files_sha256,
            hash_product_tree(project_root, list(declaration_roots(project_root, change_id))).files,
        )
    )

    product_code_modified = bool(product_changed)
    declaration_files_modified = bool(declaration_changed)
    skip_or_xfail_added = _scan_skip_xfail_markers(project_root, list(test_changed))

    locators = _load_brief_locators(change_dir)
    unbriefed = tuple(
        path for path in test_changed if not _path_explained_by_locators(project_root, path, locators)
    )

    mechanical_set = set(test_changed) | set(product_changed) | set(declaration_changed)
    summary_files = tuple(summary.files_modified)
    normalized_summary = {_normalize_repo_rel(path, project_root) for path in summary_files}
    summary_mismatch = normalized_summary != mechanical_set

    stale_summary = not (
        summary.change_id == baseline.change_id
        and summary.attempt == baseline.attempt
        and summary.attempt_token == baseline.attempt_token
    )

    passed = not product_code_modified and not declaration_files_modified
    needs_review = bool(skip_or_xfail_added or unbriefed or summary_mismatch or stale_summary)

    return CoverageRepairSafetyCheck(
        change_id=change_id,
        attempt=baseline.attempt,
        passed=passed,
        needs_review=needs_review,
        test_files_changed=test_changed,
        product_code_modified=product_code_modified,
        product_files_changed=product_changed,
        declaration_files_modified=declaration_files_modified,
        declaration_files_changed=declaration_changed,
        skip_or_xfail_added=skip_or_xfail_added,
        unbriefed_files_modified=unbriefed,
        summary_applied=summary.applied,
        summary_files_modified=summary_files,
        summary_mismatch=summary_mismatch,
        stale_summary=stale_summary,
    )


def compute_coverage_repair_safety_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Compute mechanical coverage-repair safety; write ``coverage-repair/safety-check.json``.

    Change set is derived from ``diff_trees(baseline, current)``; apply-summary is
    only a cross-check (design v4-2).
    """
    del task
    change_id = context.change_id or workspace.change_dir.name
    try:
        check = compute_coverage_repair_safety(
            change_dir=workspace.change_dir,
            project_root=workspace.project_root,
            change_id=change_id,
        )
    except CoverageRepairSafetyError as err:
        return task_failure("invalid_input", str(err))

    out = workspace.change_dir / COVERAGE_REPAIR_SAFETY_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(check))
    return TaskResult(
        status="succeeded",
        value={
            "passed": check.passed,
            "needs_review": check.needs_review,
            "stale_summary": check.stale_summary,
            "summary_mismatch": check.summary_mismatch,
            "attempt": check.attempt,
        },
    )


__all__ = [
    "COVERAGE_REPAIR_BRIEF_MD_REL",
    "CoverageRepairSafetyError",
    "build_repair_brief",
    "compute_coverage_repair_safety",
    "compute_coverage_repair_safety_operation",
    "declaration_roots",
    "mint_attempt_token",
    "probe_coverage_repair_need_operation",
]
