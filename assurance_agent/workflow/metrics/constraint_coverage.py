"""``operation:compute-constraint-coverage`` — A2 covered join (§5-A2).

Covered only when marker hit + outcome=passed + fresh (current batch) +
Task 3 ``counts_as_covered_oracle`` (B2 strong). Touched entities with an empty
constraint set emit ``entity_without_constraints``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricScope,
    MetricShortboard,
)
from assurance_agent.artifacts.models.pr_metric_evidence import ConstraintCoverageEvidence
from assurance_agent.knowledge.extract_constraints import constraint_known_keys
from assurance_agent.verification.assertion_class import (
    classify_api_assertions,
    counts_as_covered_oracle,
)
from assurance_agent.verification.property_scan import (
    PropertyMarkerHit,
    scan_property_tests,
)
from assurance_agent.workflow.execution.results import PropertyTestResult, TargetResult
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.batch_io import (
    batch_runs_dir,
    resolve_batch_id,
    write_batch_evidence,
)
from assurance_agent.workflow.metrics.quarantine import (
    QuarantineIntegrityError,
    load_active_quarantine_keys,
)

CONSTRAINT_COVERAGE_REL = "constraint-coverage.json"
DATA_KNOWLEDGE_REL = ".aa/data-knowledge.yaml"


def compute_constraint_coverage(
    *,
    change_id: str,
    batch_id: str,
    known_keys: frozenset[str],
    touched_entities: frozenset[str],
    property_tests: Sequence[PropertyTestResult],
    marker_hits: Sequence[PropertyMarkerHit],
    test_sources: Mapping[str, str],
    unknown_key_gaps: Sequence[MetricCollectionGap] = (),
    quarantine: Sequence[str] = (),
) -> ConstraintCoverageEvidence:
    """Pure A2 join over the closed constraint key set.

    Quarantined keys never count as covered but remain in ``known_keys`` /
    declared denominator (gaming by dropping from denom is forbidden).
    """
    gaps: list[MetricCollectionGap] = list(unknown_key_gaps)
    quarantined = frozenset(quarantine) & known_keys

    for entity in sorted(touched_entities):
        entity_keys = {key for key in known_keys if key.startswith(f"entities.{entity}.constraints.")}
        if not entity_keys:
            gaps.append(
                MetricCollectionGap(
                    code="entity_without_constraints",
                    metric="constraint_coverage",
                    subject=entity,
                    detail=f"touched entity {entity!r} has no declared constraints",
                )
            )

    if not known_keys:
        # Empty declared denominator is a collection gap, not 0/0 = 1.0.
        if not any(gap.code == "entity_without_constraints" for gap in gaps):
            gaps.append(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="constraint_coverage",
                    detail="declared constraint key set is empty",
                )
            )
        return ConstraintCoverageEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            declared=None,
            touched=None,
            value=None,
            collection_gaps=tuple(gaps),
        )

    covered_keys: set[str] = set()
    hits_by_key: dict[str, list[PropertyMarkerHit]] = {}
    for hit in marker_hits:
        for key in hit.constraint_keys:
            hits_by_key.setdefault(key, []).append(hit)

    passed_fresh = [test for test in property_tests if test.outcome == "passed" and test.batch_id == batch_id]

    for key in known_keys:
        if key in quarantined:
            continue
        if key not in hits_by_key:
            continue
        passed_identities = {
            (test.file, _qualified_test_name_from_nodeid(test.nodeid))
            for test in passed_fresh
            if key in test.constraint_keys
        }
        eligible_hits = [
            hit for hit in hits_by_key[key] if (hit.file, hit.pytest_qualified_name) in passed_identities
        ]
        if not eligible_hits:
            continue
        if _key_has_strong_oracle(key, eligible_hits, test_sources):
            covered_keys.add(key)

    uncovered = tuple(sorted(known_keys - covered_keys))
    declared = MetricScope.of(
        total=len(known_keys),
        covered=len(covered_keys),
        uncovered=uncovered,
    )

    touched_keys = frozenset(
        key
        for key in known_keys
        if any(key.startswith(f"entities.{entity}.constraints.") for entity in touched_entities)
    )
    touched_covered = covered_keys & touched_keys
    touched = MetricScope.of(
        total=len(touched_keys),
        covered=len(touched_covered),
        uncovered=tuple(sorted(touched_keys - touched_covered)),
    )

    shortboards = tuple(
        MetricShortboard(
            code="quarantined_excluded",
            metric="constraint_coverage",
            detail=key,
        )
        for key in sorted(quarantined)
    )

    return ConstraintCoverageEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        declared=declared,
        touched=touched,
        value=declared.value,
        collection_gaps=tuple(gaps),
        shortboards=shortboards,
    )


def _key_has_strong_oracle(
    key: str,
    hits: Sequence[PropertyMarkerHit],
    test_sources: Mapping[str, str],
) -> bool:
    del key  # identity is the hit's marker args; oracle is per test function
    for hit in hits:
        source = test_sources.get(hit.file)
        if source is None:
            continue
        classification = classify_api_assertions(
            source,
            function_name=hit.pytest_qualified_name,
        )
        if counts_as_covered_oracle(classification):
            return True
    return False


def _qualified_test_name_from_nodeid(nodeid: str) -> str:
    """Return the class-qualified, parametrization-free pytest test identity."""
    parts = nodeid.split("::")
    qualified = parts[1:] if len(parts) > 1 else parts
    if qualified:
        qualified[-1] = qualified[-1].split("[", 1)[0]
    return "::".join(qualified)


def compute_constraint_coverage_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    change_dir = workspace.change_dir
    project_root = workspace.project_root
    try:
        batch_id = resolve_batch_id(
            change_dir,
            explicit=str(task_with(task).get("batch_id") or "") or None,
        )
    except (OSError, ValueError, FileNotFoundError, yaml.YAMLError) as err:
        return task_failure("invalid_input", f"cannot resolve batch_id: {err}")

    dk_path = project_root / DATA_KNOWLEDGE_REL
    if not dk_path.is_file():
        evidence = ConstraintCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="collection_failed",
                    metric="constraint_coverage",
                    detail="missing .aa/data-knowledge.yaml",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, CONSTRAINT_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    try:
        knowledge = DataKnowledge.model_validate(yaml.safe_load(dk_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as err:
        evidence = ConstraintCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="constraint_coverage",
                    detail=f"cannot load data-knowledge: {err}",
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, CONSTRAINT_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)

    known_keys = constraint_known_keys(knowledge)
    touched_raw = task_with(task).get("touched_entities")
    if isinstance(touched_raw, (list, tuple)):
        touched_entities = frozenset(str(item) for item in touched_raw)
    else:
        touched_entities = frozenset(knowledge.entities)

    quarantine_raw = task_with(task).get("quarantine")
    quarantine_from_task: tuple[str, ...]
    if quarantine_raw is None:
        quarantine_from_task = ()
    elif isinstance(quarantine_raw, (list, tuple)):
        quarantine_from_task = tuple(str(item) for item in quarantine_raw)
    else:
        return task_failure("invalid_input", "with.quarantine must be a list when provided")
    try:
        active_quarantine = load_active_quarantine_keys(change_dir)
    except QuarantineIntegrityError as err:
        evidence = ConstraintCoverageEvidence(
            schema_version="1",
            change_id=context.change_id,
            batch_id=batch_id,
            declared=None,
            collection_gaps=(
                MetricCollectionGap(
                    code="artifact_corrupt",
                    metric="constraint_coverage",
                    detail=str(err),
                ),
            ),
        )
        write_batch_evidence(change_dir, batch_id, CONSTRAINT_COVERAGE_REL, evidence)
        return _ok(context.change_id, batch_id, evidence)
    quarantine = tuple(sorted(set(quarantine_from_task) | set(active_quarantine)))

    test_root = project_root / "tests"
    paths = sorted(test_root.rglob("test_*.py")) if test_root.is_dir() else []
    scan = scan_property_tests(paths, root=project_root, known_keys=known_keys)
    test_sources = _load_sources(paths, root=project_root)
    property_tests = _load_property_tests(change_dir, batch_id)

    evidence = compute_constraint_coverage(
        change_id=context.change_id,
        batch_id=batch_id,
        known_keys=known_keys,
        touched_entities=touched_entities,
        property_tests=property_tests,
        marker_hits=scan.hits,
        test_sources=test_sources,
        unknown_key_gaps=scan.unknown_key_gaps,
        quarantine=quarantine,
    )
    write_batch_evidence(change_dir, batch_id, CONSTRAINT_COVERAGE_REL, evidence)
    return _ok(context.change_id, batch_id, evidence)


def _load_sources(paths: Sequence[Path], *, root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def _load_property_tests(change_dir: Path, batch_id: str) -> list[PropertyTestResult]:
    batch_dir = batch_runs_dir(change_dir, batch_id)
    out: list[PropertyTestResult] = []
    for name in ("api-result.json", "e2e-result.json", "fuzz-result.json"):
        path = batch_dir / name
        if not path.is_file():
            continue
        try:
            payload = TargetResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        out.extend(payload.property_tests)
    return out


def _ok(change_id: str, batch_id: str, evidence: ConstraintCoverageEvidence) -> TaskResult:
    return TaskResult(
        status="succeeded",
        value={
            "change_id": change_id,
            "batch_id": batch_id,
            "written": True,
            "path": f"execution/runs/{batch_id}/{CONSTRAINT_COVERAGE_REL}",
            "value": evidence.value,
            "collection_gaps": len(evidence.collection_gaps),
        },
    )


__all__ = [
    "CONSTRAINT_COVERAGE_REL",
    "compute_constraint_coverage",
    "compute_constraint_coverage_operation",
]
