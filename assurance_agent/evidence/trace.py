"""Fold on-disk execution facts into a policy-free TraceProjection (execution phase)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.inspect import FailureAnalysis
from assurance_agent.artifacts.models.issues import ChangeIssueSnapshot, Problem, ProblemProjection
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceFailure,
    TraceGap,
    TraceProjection,
    TraceRow,
    TraceSource,
    TraceTestRef,
    UnmappedTest,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.evidence.case_doc import EvidenceCaseEntry, load_case_entries
from assurance_agent.evidence.tree_scan import TreeScanResult, scan_test_tree

_TREE_DIGEST_SOURCE = "tests/#tree-digest"
_DEGRADED_ONLY_GAP = "mapped_test_missing_from_tree"

_FOLD_VIEW_SOURCE = "execution/execution-manifest.yaml#fold-view"
_MANIFEST_PATH = "execution/execution-manifest.yaml"
_FAILURE_ANALYSIS_PATH = "inspect/failure-analysis.json"
_ISSUES_SNAPSHOT_PATH = "issues/snapshot.json"
_PROBLEMS_PATH = "qa/issues/problems.json"
_MERGE_INTO_PREFIX = "merged_into:"
_CLOSED_PROBLEM_STATUSES = frozenset({"resolved", "not_an_issue", "accepted_risk"})
_PYTEST_TARGETS = ("api", "e2e", "fuzz")
_CASE_TYPE_TO_TARGET: dict[str, Literal["api", "e2e", "fuzz", "performance"]] = {
    "API": "api",
    "E2E": "e2e",
    "Fuzz": "fuzz",
    "Performance": "performance",
}
_PERF_VERDICT_TO_STATUS: dict[str, Literal["passed", "failed", "skipped"]] = {
    "PASS": "passed",
    "FAIL": "failed",
    "SKIPPED": "skipped",
}


def canonical_json_bytes(obj: object) -> bytes:
    """Deterministic JSON bytes for digests and tests (sorted keys, compact)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    ).encode("utf-8")


def _canonical_json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, SelectedTargets):
        return value.model_dump()
    raise TypeError(f"unsupported type for canonical JSON: {type(value)!r}")


@dataclass(frozen=True)
class ExecutionFoldInput:
    """Runner-injected current-batch facts before top-level manifest is published."""

    batch_id: str
    executed_at: datetime
    selected_targets: SelectedTargets
    test_files_sha256: Mapping[str, str]


class ResultTestRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    case_id: str
    status: Literal["passed", "failed", "skipped"]
    file: str
    test_name: str


class ResultDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    change_id: str
    batch_id: str
    target: Literal["api", "e2e", "fuzz"]
    cases: list[ResultTestRow]
    unmapped_tests: list[UnmappedTest]


class PerformanceScenarioRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    capability: str
    verdict: Literal["PASS", "FAIL", "SKIPPED"]


class PerformanceResultDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    change_id: str
    batch_id: str
    kind: Literal["performance"]
    scenarios: list[PerformanceScenarioRow]


@dataclass(frozen=True)
class _BatchExecution:
    batch_id: str
    ts: datetime
    ts_source: Literal["executed_at", "batch_id_legacy_utc"]
    pytest: dict[str, ResultDocument]
    performance: PerformanceResultDocument | None


@dataclass(frozen=True)
class _CaseExecution:
    batch_id: str
    target: Literal["api", "e2e", "fuzz", "performance"]
    status: Literal["passed", "failed", "skipped"]
    ts: datetime
    ts_source: Literal["executed_at", "batch_id_legacy_utc"]


@dataclass(frozen=True)
class _CurrentBatchView:
    batch_id: str
    executed_at: datetime | None
    selected_targets: SelectedTargets
    test_files_sha256: dict[str, str]
    manifest_exists: bool


def fold_trace(
    project_root: Path,
    change_id: str,
    *,
    phase: Literal["execution", "reconciled"] = "execution",
    current: ExecutionFoldInput | None = None,
) -> TraceProjection:
    change_dir = resolve_change(project_root, change_id).path
    entries, case_gaps = load_case_entries(change_dir)
    gaps: list[TraceGap] = list(case_gaps)
    sources: list[TraceSource] = _case_sources(change_dir)

    current_view = _resolve_current_batch(change_dir, change_id, current, gaps, sources)
    batches, batch_gaps = _load_batch_executions(change_dir, change_id, current_view, gaps, sources)
    gaps.extend(batch_gaps)

    fold_digest = _record_fold_view_source(current_view, change_id, sources)
    if fold_digest is None and current_view.manifest_exists:
        gaps.append(
            TraceGap(
                code="batch_id_unparseable",
                source=_FOLD_VIEW_SOURCE,
                batch_id=current_view.batch_id,
                detail="cannot derive executed_at for fold-view digest",
            )
        )

    tree = scan_test_tree(project_root)
    sources.append(TraceSource(path=_TREE_DIGEST_SOURCE, exists=True, sha256=tree.tree_digest))
    _compare_test_files_sha256(current_view, tree, gaps)

    executions_by_case = _index_executions(entries, batches)
    rows = tuple(
        _build_row(
            entry,
            executions_by_case.get(entry.case_id, ()),
            current_view,
            tree,
            gaps,
        )
        for entry in sorted(entries, key=lambda item: item.case_id)
    )
    unmapped = _collect_unmapped_tests(batches, current_view.batch_id)
    integrity = _derive_integrity(gaps, rows)
    execution = TraceProjection(
        change_id=change_id,
        phase="execution",
        authoritative_batch_id=current_view.batch_id,
        sources=tuple(sorted(sources, key=lambda item: item.path)),
        rows=rows,
        unmapped_tests=unmapped,
        gaps=tuple(sorted(gaps, key=_gap_sort_key)),
        integrity=integrity,
    )
    if phase == "execution":
        return execution
    return _enrich_reconciled(project_root, change_dir, execution)


def _compare_test_files_sha256(
    current: _CurrentBatchView,
    tree: TreeScanResult,
    gaps: list[TraceGap],
) -> None:
    if not current.manifest_exists:
        # Disk/injection view absent → Task 3 already emitted manifest_missing.
        return
    if dict(tree.file_sha256) != dict(current.test_files_sha256):
        gaps.append(
            TraceGap(
                code="tests_tree_digest_mismatch",
                source=_TREE_DIGEST_SOURCE,
                batch_id=current.batch_id or None,
                detail="per-file test_files_sha256 differs from current tree scan",
            )
        )


def _derive_integrity(
    gaps: list[TraceGap],
    rows: tuple[TraceRow, ...],
) -> Literal["complete", "degraded", "incomplete"]:
    if not rows:
        return "incomplete"
    if not gaps:
        return "complete"
    codes = {gap.code for gap in gaps}
    if codes == {_DEGRADED_ONLY_GAP}:
        return "degraded"
    return "incomplete"


def _gap_sort_key(gap: TraceGap) -> tuple[str, str, str, str, str]:
    return (
        gap.code,
        gap.source,
        gap.batch_id or "",
        gap.target or "",
        gap.detail,
    )


def _resolve_current_batch(
    change_dir: Path,
    change_id: str,
    current: ExecutionFoldInput | None,
    gaps: list[TraceGap],
    sources: list[TraceSource],
) -> _CurrentBatchView:
    manifest_path = change_dir / _MANIFEST_PATH
    if current is not None:
        if current.executed_at.tzinfo is None:
            raise TypeError("executed_at must be timezone-aware")
        if manifest_path.is_file():
            sources.append(_file_source(_MANIFEST_PATH, manifest_path))
        return _CurrentBatchView(
            batch_id=current.batch_id,
            executed_at=current.executed_at,
            selected_targets=current.selected_targets,
            test_files_sha256=dict(current.test_files_sha256),
            manifest_exists=True,
        )

    sources.append(_file_source(_MANIFEST_PATH, manifest_path))
    if not manifest_path.is_file():
        gaps.append(TraceGap(code="manifest_missing", source=_MANIFEST_PATH))
        return _CurrentBatchView(
            batch_id="",
            executed_at=None,
            selected_targets=SelectedTargets(
                api=False,
                e2e=False,
                fuzz=False,
                performance=False,
            ),
            test_files_sha256={},
            manifest_exists=False,
        )

    raw = _read_yaml_mapping(manifest_path)
    if raw is None:
        gaps.append(TraceGap(code="manifest_missing", source=_MANIFEST_PATH, detail="invalid YAML mapping"))
        return _CurrentBatchView(
            batch_id="",
            executed_at=None,
            selected_targets=SelectedTargets(
                api=False,
                e2e=False,
                fuzz=False,
                performance=False,
            ),
            test_files_sha256={},
            manifest_exists=False,
        )

    batch_id = raw.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        gaps.append(TraceGap(code="manifest_missing", source=_MANIFEST_PATH, detail="missing batch_id"))
        return _CurrentBatchView(
            batch_id="",
            executed_at=None,
            selected_targets=SelectedTargets(
                api=False,
                e2e=False,
                fuzz=False,
                performance=False,
            ),
            test_files_sha256={},
            manifest_exists=False,
        )

    selected = _parse_selected_targets(raw.get("selected_targets"), _MANIFEST_PATH, gaps)
    test_files = _parse_test_files_sha256(raw.get("test_files_sha256"))
    executed_at = _parse_executed_at(raw.get("executed_at"))
    if raw.get("change_id") != change_id:
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=_MANIFEST_PATH,
                batch_id=batch_id,
                detail="manifest change_id mismatch",
            )
        )
    return _CurrentBatchView(
        batch_id=batch_id,
        executed_at=executed_at,
        selected_targets=selected,
        test_files_sha256=test_files,
        manifest_exists=True,
    )


def _record_fold_view_source(
    current: _CurrentBatchView,
    change_id: str,
    sources: list[TraceSource],
) -> str | None:
    if not current.manifest_exists or not current.batch_id:
        sources.append(TraceSource(path=_FOLD_VIEW_SOURCE, exists=False, sha256=None))
        return None

    ts, _ = _resolve_batch_ts(current.batch_id, current.executed_at, _FOLD_VIEW_SOURCE, [])
    if ts is None:
        sources.append(TraceSource(path=_FOLD_VIEW_SOURCE, exists=False, sha256=None))
        return None

    payload = {
        "batch_id": current.batch_id,
        "change_id": change_id,
        "executed_at": ts,
        "selected_targets": current.selected_targets,
        "test_files_sha256": dict(sorted(current.test_files_sha256.items())),
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    sources.append(TraceSource(path=_FOLD_VIEW_SOURCE, exists=True, sha256=digest))
    return digest


def _load_batch_executions(
    change_dir: Path,
    change_id: str,
    current: _CurrentBatchView,
    gaps: list[TraceGap],
    sources: list[TraceSource],
) -> tuple[list[_BatchExecution], list[TraceGap]]:
    runs_root = change_dir / "execution" / "runs"
    if not runs_root.is_dir():
        return [], []

    out: list[_BatchExecution] = []
    extra_gaps: list[TraceGap] = []
    for batch_dir in sorted(runs_root.iterdir()):
        if not batch_dir.is_dir():
            continue
        batch_id = batch_dir.name
        manifest_path = batch_dir / "execution-manifest.yaml"
        executed_at = None
        if manifest_path.is_file():
            sources.append(_file_source(f"execution/runs/{batch_id}/execution-manifest.yaml", manifest_path))
            raw = _read_yaml_mapping(manifest_path)
            if raw is not None:
                executed_at = _parse_executed_at(raw.get("executed_at"))
        if executed_at is None and batch_id == current.batch_id:
            executed_at = current.executed_at
        ts, ts_source = _resolve_batch_ts(
            batch_id,
            executed_at,
            f"execution/runs/{batch_id}",
            extra_gaps,
        )
        if ts is None:
            continue

        pytest_docs: dict[str, ResultDocument] = {}
        for target in _PYTEST_TARGETS:
            rel = f"execution/runs/{batch_id}/{target}-result.json"
            path = batch_dir / f"{target}-result.json"
            if not path.is_file():
                continue
            sources.append(_file_source(rel, path))
            doc = _load_pytest_result(path, change_id, batch_id, target, rel, extra_gaps)
            if doc is not None:
                pytest_docs[target] = doc

        perf_doc: PerformanceResultDocument | None = None
        perf_rel = f"execution/runs/{batch_id}/performance-result.json"
        perf_path = batch_dir / "performance-result.json"
        if perf_path.is_file():
            sources.append(_file_source(perf_rel, perf_path))
            perf_doc = _load_performance_result(perf_path, change_id, batch_id, perf_rel, extra_gaps)

        out.append(
            _BatchExecution(
                batch_id=batch_id,
                ts=ts,
                ts_source=ts_source,
                pytest=pytest_docs,
                performance=perf_doc,
            )
        )
    return out, extra_gaps


def _load_pytest_result(
    path: Path,
    change_id: str,
    batch_id: str,
    target: str,
    source: str,
    gaps: list[TraceGap],
) -> ResultDocument | None:
    try:
        doc = ResultDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as err:
        gaps.append(
            TraceGap(
                code="result_corrupt",
                source=source,
                batch_id=batch_id,
                target=target,
                detail=str(err),
            )
        )
        return None

    if doc.change_id != change_id:
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=source,
                batch_id=batch_id,
                target=target,
                detail="change_id mismatch",
            )
        )
        return None
    if doc.batch_id != batch_id:
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=source,
                batch_id=batch_id,
                target=target,
                detail="batch_id mismatch",
            )
        )
        return None
    if doc.target != target:
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=source,
                batch_id=batch_id,
                target=target,
                detail="target mismatch",
            )
        )
        return None
    return doc


def _load_performance_result(
    path: Path,
    change_id: str,
    batch_id: str,
    source: str,
    gaps: list[TraceGap],
) -> PerformanceResultDocument | None:
    try:
        doc = PerformanceResultDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as err:
        gaps.append(
            TraceGap(
                code="result_corrupt",
                source=source,
                batch_id=batch_id,
                target="performance",
                detail=str(err),
            )
        )
        return None

    if doc.change_id != change_id:
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=source,
                batch_id=batch_id,
                target="performance",
                detail="change_id mismatch",
            )
        )
        return None
    if doc.batch_id != batch_id:
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=source,
                batch_id=batch_id,
                target="performance",
                detail="batch_id mismatch",
            )
        )
        return None
    if doc.kind != "performance":
        gaps.append(
            TraceGap(
                code="result_identity_mismatch",
                source=source,
                batch_id=batch_id,
                target="performance",
                detail="kind mismatch",
            )
        )
        return None
    return doc


def _index_executions(
    entries: list[EvidenceCaseEntry],
    batches: list[_BatchExecution],
) -> dict[str, tuple[_CaseExecution, ...]]:
    by_case: dict[str, list[_CaseExecution]] = {}
    perf_entries = [entry for entry in entries if entry.type == "Performance"]
    other_entries = [entry for entry in entries if entry.type != "Performance"]

    for batch in batches:
        for entry in other_entries:
            target = _CASE_TYPE_TO_TARGET[entry.type]
            doc = batch.pytest.get(target)
            if doc is None:
                continue
            for row in doc.cases:
                if row.case_id != entry.case_id:
                    continue
                by_case.setdefault(entry.case_id, []).append(
                    _CaseExecution(
                        batch_id=batch.batch_id,
                        target=target,
                        status=row.status,
                        ts=batch.ts,
                        ts_source=batch.ts_source,
                    )
                )

        if batch.performance is None:
            continue
        for entry in perf_entries:
            if entry.perf_capability is None:
                continue
            for scenario in batch.performance.scenarios:
                if scenario.capability != entry.perf_capability:
                    continue
                by_case.setdefault(entry.case_id, []).append(
                    _CaseExecution(
                        batch_id=batch.batch_id,
                        target="performance",
                        status=_PERF_VERDICT_TO_STATUS[scenario.verdict],
                        ts=batch.ts,
                        ts_source=batch.ts_source,
                    )
                )
    return {case_id: tuple(items) for case_id, items in by_case.items()}


def _build_row(
    entry: EvidenceCaseEntry,
    executions: tuple[_CaseExecution, ...],
    current: _CurrentBatchView,
    tree: TreeScanResult,
    gaps: list[TraceGap],
) -> TraceRow:
    target = _CASE_TYPE_TO_TARGET[entry.type]
    latest = _pick_latest(executions)
    freshest_pass = _pick_freshest_pass(executions)
    presence = _presence_in_current_batch(entry, executions, current, target)
    covering, coverage_state = _coverage_for_entry(entry, tree)
    if entry.automation_required and latest is not None:
        _maybe_mapped_test_missing(entry, latest, tree, gaps)
    atemporal = _atemporal_kinds(entry, latest, coverage_state)
    return TraceRow(
        case_id=entry.case_id,
        module=entry.module,
        case_type=entry.type,
        automation_required=entry.automation_required,
        assertions=entry.assertions,
        covering_tests=covering,
        coverage_state=coverage_state,
        latest_execution=_to_trace_execution(latest),
        freshest_pass=_to_trace_execution(freshest_pass),
        presence_in_current_batch=presence,
        atemporal_kinds_present=atemporal,
    )


def _coverage_for_entry(
    entry: EvidenceCaseEntry,
    tree: TreeScanResult,
) -> tuple[tuple[TraceTestRef, ...], Literal["covered", "uncovered", "not_required"]]:
    if not entry.automation_required:
        return (), "not_required"
    if entry.type == "Performance":
        if entry.perf_capability and entry.perf_capability in tree.perf_capabilities:
            return (), "covered"
        return (), "uncovered"
    covering = tree.case_refs.get(entry.case_id, ())
    if covering:
        return covering, "covered"
    return (), "uncovered"


def _maybe_mapped_test_missing(
    entry: EvidenceCaseEntry,
    latest: _CaseExecution,
    tree: TreeScanResult,
    gaps: list[TraceGap],
) -> None:
    """Emit gap when latest mapped pytest test disappeared from the current tree."""
    if entry.type == "Performance":
        return
    if entry.case_id in tree.case_ids:
        return
    gaps.append(
        TraceGap(
            code="mapped_test_missing_from_tree",
            source=_TREE_DIGEST_SOURCE,
            batch_id=latest.batch_id,
            target=latest.target,
            detail=f"case {entry.case_id} mapped in {latest.batch_id} missing from current tests tree",
        )
    )


def _pick_latest(executions: tuple[_CaseExecution, ...]) -> _CaseExecution | None:
    if not executions:
        return None
    return max(executions, key=lambda item: item.batch_id)


def _pick_freshest_pass(executions: tuple[_CaseExecution, ...]) -> _CaseExecution | None:
    passed = [item for item in executions if item.status == "passed"]
    if not passed:
        return None
    return max(passed, key=lambda item: item.batch_id)


def _to_trace_execution(item: _CaseExecution | None) -> TraceExecution | None:
    if item is None:
        return None
    return TraceExecution(
        batch_id=item.batch_id,
        target=item.target,
        status=item.status,
        ts=item.ts,
        ts_source=item.ts_source,
    )


def _presence_in_current_batch(
    entry: EvidenceCaseEntry,
    executions: tuple[_CaseExecution, ...],
    current: _CurrentBatchView,
    target: Literal["api", "e2e", "fuzz", "performance"],
) -> Literal["executed", "not_in_current_batch", "target_not_selected"]:
    selected = _target_selected(current.selected_targets, target)
    if not selected:
        return "target_not_selected"
    if not current.batch_id:
        return "not_in_current_batch"
    current_execs = [item for item in executions if item.batch_id == current.batch_id]
    if current_execs:
        return "executed"
    return "not_in_current_batch"


def _target_selected(selected: SelectedTargets, target: str) -> bool:
    return {
        "api": selected.api,
        "e2e": selected.e2e,
        "fuzz": selected.fuzz,
        "performance": selected.performance,
    }[target]


def _atemporal_kinds(
    entry: EvidenceCaseEntry,
    latest: _CaseExecution | None,
    coverage_state: Literal["covered", "uncovered", "not_required"],
) -> tuple[str, ...]:
    kinds: list[str] = []
    if coverage_state == "covered":
        kinds.append("covered")
    if entry.type == "Fuzz" and latest is not None and latest.target == "fuzz":
        if latest.status in ("passed", "failed"):
            kinds.append("fuzz_run")
    if entry.type == "Performance" and latest is not None and latest.target == "performance":
        if latest.status in ("passed", "failed"):
            kinds.append("perf_run")
    return tuple(kinds)


def _collect_unmapped_tests(
    batches: list[_BatchExecution],
    current_batch_id: str,
) -> tuple[UnmappedTest, ...]:
    if not current_batch_id:
        return ()
    items: list[UnmappedTest] = []
    for batch in batches:
        if batch.batch_id != current_batch_id:
            continue
        for doc in batch.pytest.values():
            items.extend(doc.unmapped_tests)
    return tuple(sorted(items, key=lambda item: (item.file, item.test_name)))


def _resolve_batch_ts(
    batch_id: str,
    executed_at: datetime | None,
    source: str,
    gaps: list[TraceGap],
) -> tuple[datetime | None, Literal["executed_at", "batch_id_legacy_utc"]]:
    if executed_at is not None:
        if executed_at.tzinfo is None:
            raise TypeError("executed_at must be timezone-aware")
        return executed_at, "executed_at"
    legacy = _parse_legacy_batch_ts(batch_id)
    if legacy is None:
        gaps.append(
            TraceGap(
                code="batch_id_unparseable",
                source=source,
                batch_id=batch_id,
            )
        )
        return None, "batch_id_legacy_utc"
    return legacy, "batch_id_legacy_utc"


def _parse_legacy_batch_ts(batch_id: str) -> datetime | None:
    try:
        naive = datetime.strptime(batch_id, "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=UTC)


def _parse_executed_at(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _parse_selected_targets(
    value: object,
    source: str,
    gaps: list[TraceGap],
) -> SelectedTargets:
    if not isinstance(value, dict):
        gaps.append(TraceGap(code="manifest_missing", source=source, detail="selected_targets invalid"))
        return SelectedTargets(api=False, e2e=False, fuzz=False, performance=False)
    try:
        return SelectedTargets.model_validate(value)
    except ValidationError:
        gaps.append(TraceGap(code="manifest_missing", source=source, detail="selected_targets invalid"))
        return SelectedTargets(api=False, e2e=False, fuzz=False, performance=False)


def _parse_test_files_sha256(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, digest in value.items():
        if isinstance(key, str) and isinstance(digest, str):
            out[key] = digest
    return out


def _read_yaml_mapping(path: Path) -> dict[str, object] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _case_sources(change_dir: Path) -> list[TraceSource]:
    cases_root = change_dir / "cases"
    if not cases_root.is_dir():
        return []
    out: list[TraceSource] = []
    for path in sorted(cases_root.glob("**/case.yaml")):
        rel = path.relative_to(change_dir).as_posix()
        out.append(_file_source(rel, path))
    return out


def _file_source(rel: str, path: Path) -> TraceSource:
    if not path.is_file():
        return TraceSource(path=rel, exists=False, sha256=None)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return TraceSource(path=rel, exists=True, sha256=digest)


def _enrich_reconciled(
    project_root: Path,
    change_dir: Path,
    execution: TraceProjection,
) -> TraceProjection:
    gaps = list(execution.gaps)
    sources = list(execution.sources)
    failure_analysis = _load_failure_analysis(change_dir, gaps, sources)
    issues_snapshot = _load_issues_snapshot(change_dir, gaps, sources)
    problems_projection = _load_problems_projection(project_root, gaps, sources)
    failures_by_case = _index_failures_by_case(failure_analysis)
    problems_by_id = (
        {problem.problem_id: problem for problem in problems_projection.problems}
        if problems_projection is not None
        else {}
    )
    open_by_case = _index_open_problems_by_case(issues_snapshot, problems_by_id, gaps)
    rows = tuple(
        row.model_copy(
            update={
                "failures": failures_by_case.get(row.case_id, ()),
                "open_problem_ids": open_by_case.get(row.case_id, ()),
            }
        )
        for row in execution.rows
    )
    integrity = _derive_integrity(gaps, rows)
    return TraceProjection(
        change_id=execution.change_id,
        phase="reconciled",
        authoritative_batch_id=execution.authoritative_batch_id,
        sources=tuple(sorted(sources, key=lambda item: item.path)),
        rows=rows,
        unmapped_tests=execution.unmapped_tests,
        gaps=tuple(sorted(gaps, key=_gap_sort_key)),
        integrity=integrity,
    )


def _load_failure_analysis(
    change_dir: Path,
    gaps: list[TraceGap],
    sources: list[TraceSource],
) -> FailureAnalysis | None:
    path = change_dir / _FAILURE_ANALYSIS_PATH
    sources.append(_file_source(_FAILURE_ANALYSIS_PATH, path))
    if not path.is_file():
        gaps.append(TraceGap(code="failure_analysis_missing", source=_FAILURE_ANALYSIS_PATH))
        return None
    try:
        return FailureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as err:
        gaps.append(
            TraceGap(
                code="failure_analysis_missing",
                source=_FAILURE_ANALYSIS_PATH,
                detail=str(err),
            )
        )
        return None


def _load_issues_snapshot(
    change_dir: Path,
    gaps: list[TraceGap],
    sources: list[TraceSource],
) -> ChangeIssueSnapshot | None:
    path = change_dir / _ISSUES_SNAPSHOT_PATH
    sources.append(_file_source(_ISSUES_SNAPSHOT_PATH, path))
    if not path.is_file():
        gaps.append(TraceGap(code="issues_snapshot_missing", source=_ISSUES_SNAPSHOT_PATH))
        return None
    try:
        return ChangeIssueSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as err:
        gaps.append(
            TraceGap(
                code="issues_snapshot_missing",
                source=_ISSUES_SNAPSHOT_PATH,
                detail=str(err),
            )
        )
        return None


def _load_problems_projection(
    project_root: Path,
    gaps: list[TraceGap],
    sources: list[TraceSource],
) -> ProblemProjection | None:
    path = project_root / _PROBLEMS_PATH
    sources.append(_file_source(_PROBLEMS_PATH, path))
    if not path.is_file():
        gaps.append(TraceGap(code="problems_snapshot_missing", source=_PROBLEMS_PATH))
        return None
    try:
        return ProblemProjection.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as err:
        gaps.append(
            TraceGap(
                code="problems_snapshot_missing",
                source=_PROBLEMS_PATH,
                detail=str(err),
            )
        )
        return None


def _index_failures_by_case(
    failure_analysis: FailureAnalysis | None,
) -> dict[str, tuple[TraceFailure, ...]]:
    if failure_analysis is None:
        return {}
    grouped: dict[str, list[TraceFailure]] = {}
    for entry in failure_analysis.failures:
        grouped.setdefault(entry.case_id, []).append(
            TraceFailure(category=entry.category, severity=entry.severity)
        )
    return {case_id: tuple(items) for case_id, items in grouped.items()}


def _index_open_problems_by_case(
    issues_snapshot: ChangeIssueSnapshot | None,
    problems_by_id: dict[str, Problem],
    gaps: list[TraceGap],
) -> dict[str, tuple[str, ...]]:
    if issues_snapshot is None or not problems_by_id:
        return {}

    observation_case: dict[str, str] = {}
    for observation in issues_snapshot.observations:
        if observation.case_id is not None:
            observation_case[observation.observation_id] = observation.case_id

    case_problem_ids: dict[str, list[str]] = {}
    for occurrence in issues_snapshot.occurrences:
        for observation_id in occurrence.observation_ids:
            case_id = observation_case.get(observation_id)
            if case_id is None:
                continue
            case_problem_ids.setdefault(case_id, []).append(occurrence.problem_id)

    out: dict[str, tuple[str, ...]] = {}
    for case_id, problem_ids in case_problem_ids.items():
        open_ids = _collect_open_problem_ids(problem_ids, problems_by_id, gaps)
        if open_ids:
            out[case_id] = open_ids
    return out


def _collect_open_problem_ids(
    problem_ids: list[str],
    problems_by_id: dict[str, Problem],
    gaps: list[TraceGap],
) -> tuple[str, ...]:
    seen_fingerprints: set[str] = set()
    open_ids: list[str] = []
    for problem_id in sorted(problem_ids):
        canonical_id = _resolve_canonical_problem_id(problem_id, problems_by_id, gaps)
        if canonical_id is None:
            continue
        problem = problems_by_id[canonical_id]
        if problem.status in _CLOSED_PROBLEM_STATUSES:
            continue
        if problem.assessment.classification != "product_bug":
            continue
        fingerprint = problem.fingerprint.digest
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        open_ids.append(canonical_id)
    return tuple(sorted(open_ids))


def _resolve_canonical_problem_id(
    problem_id: str,
    problems_by_id: dict[str, Problem],
    gaps: list[TraceGap],
) -> str | None:
    visited: set[str] = set()
    current = problem_id
    while True:
        if current in visited:
            gaps.append(
                TraceGap(
                    code="problem_alias_invalid",
                    source=_PROBLEMS_PATH,
                    detail=f"merge alias cycle at {current}",
                )
            )
            return None
        visited.add(current)
        problem = problems_by_id.get(current)
        if problem is None:
            gaps.append(
                TraceGap(
                    code="problem_alias_invalid",
                    source=_PROBLEMS_PATH,
                    detail=f"merge alias target missing: {current}",
                )
            )
            return None
        resolution = problem.resolution
        if resolution is not None and resolution.disposition.startswith(_MERGE_INTO_PREFIX):
            target_id = resolution.disposition[len(_MERGE_INTO_PREFIX) :]
            if not target_id:
                gaps.append(
                    TraceGap(
                        code="problem_alias_invalid",
                        source=_PROBLEMS_PATH,
                        detail=f"empty merge alias target from {current}",
                    )
                )
                return None
            current = target_id
            continue
        return current
