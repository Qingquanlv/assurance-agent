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
    COVERAGE_REPAIR_STATUS_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    CoverageRepairStatus,
    CoverageRepairStatusValue,
    DeferredItem,
    RepairableGapKind,
    RepairItem,
)
from assurance_agent.artifacts.models.metrics import MetricKey
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.change_location import resolve_change
from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.execution.scope import resolve_test_paths
from assurance_agent.workflow.execution.tree_hash import (
    diff_trees,
    hash_product_tree,
    hash_test_tree,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
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
    "uncovered_required_case": "journey_coverage",
    "stale_required_case": "journey_coverage",
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
    active_shortboards: frozenset[MetricKey],
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
        if metric not in active_shortboards:
            deferred.append(DeferredItem(kind=gap.kind, locator=gap.locator, reason="not_serving_shortboard"))
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


def _allowed_test_files(change_dir: Path) -> tuple[str, ...]:
    """Return exactly the codegen-plan paths that ``run_change`` will execute."""
    paths: set[str] = set()
    for target in ("api", "e2e", "fuzz", "performance"):
        resolved = resolve_test_paths(change_dir, target)
        if resolved is not None:
            paths.update(resolved)
    return tuple(sorted(paths))


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
    lines.extend(["", "## Allowed test files", ""])
    lines.extend(f"- `{path}`" for path in brief.allowed_test_files)
    if not brief.allowed_test_files:
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
    active_shortboards: frozenset[MetricKey] = frozenset(board.metric for board in decision.shortboards)
    repair_items, deferred = _partition_gaps(gaps, active_shortboards)
    allowed_test_files = _allowed_test_files(change_dir)
    if repair_items and not allowed_test_files:
        deferred = deferred + tuple(
            DeferredItem(kind=item.kind, locator=item.locator, reason="no_test_scope")
            for item in repair_items
        )
        repair_items = ()
    return CoverageRepairBrief(
        change_id=change_id,
        batch_id=batch_id,
        probe_verdict=decision.verdict,
        eligible=bool(repair_items and allowed_test_files),
        allowed_test_files=allowed_test_files,
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


def _load_brief(change_dir: Path) -> CoverageRepairBrief | None:
    path = Path(change_dir) / COVERAGE_REPAIR_BRIEF_REL
    if not path.is_file():
        return None
    try:
        brief = CoverageRepairBrief.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None
    return brief


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

    brief = _load_brief(change_dir)
    allowed = set(brief.allowed_test_files) if brief is not None else set()
    unbriefed = tuple(path for path in test_changed if path not in allowed)

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


_ALLOWED_COVERAGE_REPAIR_STATUSES: frozenset[str] = frozenset(get_args(CoverageRepairStatusValue))


def _load_prior_status(change_dir: Path) -> CoverageRepairStatus | None:
    path = Path(change_dir) / COVERAGE_REPAIR_STATUS_REL
    if not path.is_file():
        return None
    try:
        return CoverageRepairStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None


def _load_brief_deferred(change_dir: Path) -> tuple[DeferredItem, ...]:
    """Best-effort deferred list from brief.json (missing/invalid → empty)."""
    path = Path(change_dir) / COVERAGE_REPAIR_BRIEF_REL
    if not path.is_file():
        return ()
    try:
        brief = CoverageRepairBrief.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return ()
    return brief.deferred_to_intake


def allocate_coverage_repair_attempt_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Consume one coverage-repair budget unit and freeze ``entry-baseline.json``.

    Fails with ``invalid_input`` (writing neither status nor baseline) when the
    brief is missing or ``eligible`` is false. Re-freezes the three-tree baseline
    on every attempt so attempt N's safety measures only N's edits (design v4-2).
    """
    del task
    change_id = context.change_id or workspace.change_dir.name
    brief_path = workspace.change_dir / COVERAGE_REPAIR_BRIEF_REL
    if not brief_path.is_file():
        return task_failure(
            "invalid_input",
            f"missing {COVERAGE_REPAIR_BRIEF_REL}: cannot allocate without a brief",
        )
    try:
        brief = CoverageRepairBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as err:
        return task_failure("invalid_input", f"unreadable {COVERAGE_REPAIR_BRIEF_REL}: {err}")
    if not brief.eligible:
        return task_failure(
            "invalid_input",
            "cannot allocate coverage-repair attempt for an ineligible brief",
        )

    prior = _load_prior_status(workspace.change_dir)
    attempts_used = (prior.attempts_used if prior is not None else 0) + 1
    deferred = brief.deferred_to_intake if prior is None else prior.deferred_to_intake

    project_root = workspace.project_root
    test_tree = hash_test_tree(project_root)
    product_tree = hash_product_tree(project_root, load_product_code_roots(project_root))
    declaration_tree = hash_product_tree(project_root, list(declaration_roots(project_root, change_id)))
    attempt_token = mint_attempt_token(
        change_id=change_id,
        attempt=attempts_used,
        test_tree_sha256=test_tree.aggregate,
        product_tree_sha256=product_tree.aggregate,
        declaration_tree_sha256=declaration_tree.aggregate,
    )
    baseline = CoverageRepairBaseline(
        change_id=change_id,
        attempt=attempts_used,
        attempt_token=attempt_token,
        test_tree_sha256=test_tree.aggregate,
        test_files_sha256=dict(test_tree.files),
        product_tree_sha256=product_tree.aggregate,
        product_files_sha256=dict(product_tree.files),
        declaration_tree_sha256=declaration_tree.aggregate,
        declaration_files_sha256=dict(declaration_tree.files),
    )
    status = CoverageRepairStatus(
        change_id=change_id,
        status="in_progress",
        attempts_used=attempts_used,
        last_batch_id=brief.batch_id,
        deferred_to_intake=deferred,
    )

    status_path = workspace.change_dir / COVERAGE_REPAIR_STATUS_REL
    baseline_path = workspace.change_dir / COVERAGE_REPAIR_BASELINE_REL
    status_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(status_path, canonical_json_bytes(status))
    atomic_write_bytes(baseline_path, canonical_json_bytes(baseline))
    return TaskResult(
        status="succeeded",
        value={
            "attempts_used": attempts_used,
            "last_batch_id": brief.batch_id,
            "attempt_token": attempt_token,
        },
    )


def record_coverage_repair_status_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Write a terminal (or otherwise declared) coverage-repair status from ``with.status``."""
    raw_status = task_with(task).get("status")
    if not isinstance(raw_status, str) or raw_status not in _ALLOWED_COVERAGE_REPAIR_STATUSES:
        allowed = ", ".join(sorted(_ALLOWED_COVERAGE_REPAIR_STATUSES))
        return task_failure(
            "invalid_input",
            f"operation:record-coverage-repair-status requires with.status in: {allowed}",
        )

    change_id = context.change_id or workspace.change_dir.name
    prior = _load_prior_status(workspace.change_dir)
    # Prefer prior (allocate already froze deferred); else copy from brief so
    # not_eligible paths that never allocate still surface deferred for retro.
    deferred = prior.deferred_to_intake if prior is not None else _load_brief_deferred(workspace.change_dir)
    status = CoverageRepairStatus(
        change_id=change_id,
        status=raw_status,  # type: ignore[arg-type]
        attempts_used=prior.attempts_used if prior is not None else 0,
        last_batch_id=prior.last_batch_id if prior is not None else None,
        deferred_to_intake=deferred,
    )
    out = workspace.change_dir / COVERAGE_REPAIR_STATUS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(status))
    return TaskResult(status="succeeded", value={"coverage_repair_status": raw_status})


__all__ = [
    "COVERAGE_REPAIR_BRIEF_MD_REL",
    "CoverageRepairSafetyError",
    "allocate_coverage_repair_attempt_operation",
    "build_repair_brief",
    "compute_coverage_repair_safety",
    "compute_coverage_repair_safety_operation",
    "declaration_roots",
    "mint_attempt_token",
    "probe_coverage_repair_need_operation",
    "record_coverage_repair_status_operation",
]
