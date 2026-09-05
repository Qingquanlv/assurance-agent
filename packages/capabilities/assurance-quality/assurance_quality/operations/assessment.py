"""Authenticate and materialize the complete deterministic Inspect input set."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import cast

import yaml
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.context import AuthorizedAttemptScope
from graph_engine.attempts.contracts import ExecutedAttemptResult
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_intake.contracts.cases import CaseEntryAuthoring, CaseYamlAuthoring
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    MaterializeAssessmentInputV1,
)
from assurance_quality.contracts.decisions import classify_inspection_disposition
from assurance_quality.contracts.goal_policy import (
    ActiveCoverageScopeV1,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_quality.contracts.metrics import MetricScope, MetricsDocument
from assurance_quality.contracts.pr_metrics import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    JourneyCoverageEvidence,
)
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.coverage import build_coverage_gaps
from assurance_quality.operations.metrics import (
    PrEvidenceBundle,
    RiskInput,
    build_metrics_document,
    resolve_case_risk,
)
from assurance_quality.operations.sufficiency import build_sufficiency_facts
from assurance_quality.operations.trace import TraceCaseInput, TraceOperationInput, project_trace
from assurance_quality.operations.common import json_digest


class AssessmentInputError(ValueError):
    """The assessment sources could not prove one coherent execution cycle."""


_FAMILY_ORDER = ("api", "e2e", "fuzz", "performance")
_GOAL_ORDER = ("constraint_coverage", "auth_matrix_coverage", "journey_coverage")
_BATCH_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _read_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    path = root
    for part in PurePosixPath(ref.path).parts:
        path = path / part
        if path.is_symlink():
            raise AssessmentInputError(f"assessment input must not contain a symlink: {ref.path}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise AssessmentInputError(f"assessment input is missing: {ref.path}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise AssessmentInputError(f"assessment input must be a regular single-link file: {ref.path}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise AssessmentInputError(f"assessment input digest changed: {ref.path}")
    return data


def _load_json(root: Path, ref: EvidenceArtifactRefV1) -> object:
    try:
        return json.loads(_read_ref(root, ref))
    except json.JSONDecodeError as error:
        raise AssessmentInputError(f"assessment input is not JSON: {ref.path}") from error


def _cases(root: Path, request: MaterializeAssessmentInputV1) -> tuple[CaseEntryAuthoring, ...]:
    cases: list[CaseEntryAuthoring] = []
    for ref in request.reviewed_case.case_refs:
        try:
            raw = yaml.safe_load(_read_ref(root, ref))
            if not isinstance(raw, Mapping):
                raise AssessmentInputError(f"reviewed Case inventory is not a mapping: {ref.path}")
            leafs: set[str] = set()
            for section in ("added", "modified"):
                entries = raw.get(section)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, Mapping):
                        continue
                    trace = entry.get("trace")
                    if isinstance(trace, Mapping):
                        leafs.update(str(key) for key in trace)
                    automation = entry.get("automation")
                    performance = automation.get("performance") if isinstance(automation, Mapping) else None
                    scenario = performance.get("scenario") if isinstance(performance, Mapping) else None
                    capability = scenario.get("capability") if isinstance(scenario, Mapping) else None
                    if isinstance(capability, str):
                        leafs.add(capability)
            document = CaseYamlAuthoring.model_validate(
                raw,
                context={"capability_leafs": frozenset(leafs)},
            )
        except (yaml.YAMLError, ValidationError) as error:
            raise AssessmentInputError(f"invalid reviewed Case inventory: {ref.path}: {error}") from error
        cases.extend((*document.added, *document.modified))
    active = tuple(case for case in cases if case.status != "deprecated")
    case_ids = [case.case_id for case in active]
    if not active or len(case_ids) != len(set(case_ids)):
        raise AssessmentInputError("reviewed Case inventory must be non-empty with unique case ids")
    return active


def _policy(
    root: Path, request: MaterializeAssessmentInputV1
) -> tuple[CoverageGoalPolicyV1, SufficiencyPolicyV1]:
    if request.policy_resource_id != "assurance.product.configuration.product-policy":
        raise AssessmentInputError("unknown product policy resource")
    path = root / ".aa" / "policy.yaml"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AssessmentInputError("product policy resource is missing") from error
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise AssessmentInputError("product policy resource must be a regular single-link file")
    if hashlib.sha256(data).hexdigest() != request.policy_sha256:
        raise AssessmentInputError("product policy resource digest changed")
    try:
        raw = yaml.safe_load(data)
        if not isinstance(raw, Mapping):
            raise AssessmentInputError("product policy resource must be a mapping")
        return (
            CoverageGoalPolicyV1.from_product_policy(raw),
            SufficiencyPolicyV1.from_product_policy(raw),
        )
    except (yaml.YAMLError, ValidationError, ValueError) as error:
        if isinstance(error, AssessmentInputError):
            raise
        raise AssessmentInputError(f"policy_error: {error}") from error


def _applicable_goals(cases: tuple[CaseEntryAuthoring, ...]) -> tuple[str, ...]:
    goals: set[str] = set()
    trace_keys = {key for case in cases for key in case.trace}
    if any(".constraints." in key for key in trace_keys):
        goals.add("constraint_coverage")
    if any(key.startswith(("auth.", "auth_matrix.")) for key in trace_keys):
        goals.add("auth_matrix_coverage")
    if any(case.type == "E2E" for case in cases):
        goals.add("journey_coverage")
    return tuple(goal for goal in _GOAL_ORDER if goal in goals)


def _goal_obligations(goal: str, cases: tuple[CaseEntryAuthoring, ...]) -> Mapping[str, frozenset[str]]:
    if goal == "constraint_coverage":
        return {
            key: frozenset(case.case_id for case in cases if key in case.trace)
            for key in sorted({key for case in cases for key in case.trace if ".constraints." in key})
        }
    if goal == "auth_matrix_coverage":
        return {
            key: frozenset(case.case_id for case in cases if key in case.trace)
            for key in sorted(
                {key for case in cases for key in case.trace if key.startswith(("auth.", "auth_matrix."))}
            )
        }
    return {case.case_id: frozenset({case.case_id}) for case in cases if case.type == "E2E"}


def _metrics(
    *,
    cases: tuple[CaseEntryAuthoring, ...],
    projection: TraceProjectionV2,
    policy_digest: str,
    computed_at: datetime,
) -> MetricsDocument:
    trace = projection
    rows = {row.case_id: row for row in trace.rows}

    def scope(goal: str) -> MetricScope | None:
        obligations = _goal_obligations(goal, cases)
        if not obligations:
            return None
        uncovered = tuple(
            key
            for key, case_ids in obligations.items()
            if not any(
                (row := rows.get(case_id)) is not None
                and row.freshest_pass is not None
                and row.freshest_pass.batch_id == trace.authoritative_batch_id
                for case_id in case_ids
            )
        )
        return MetricScope.of(
            total=len(obligations),
            covered=len(obligations) - len(uncovered),
            uncovered=uncovered,
        )

    source_digest = json_digest(trace.model_dump(mode="json"))
    constraint = scope("constraint_coverage")
    auth = scope("auth_matrix_coverage")
    journey = scope("journey_coverage")
    risk = resolve_case_risk(cases)
    preliminary = build_metrics_document(
        PrEvidenceBundle(
            change_id=trace.change_id,
            batch_id=trace.authoritative_batch_id,
            computed_at=computed_at,
            policy_digest=policy_digest,
            risk=RiskInput(
                tier=risk.tier,
                lower_bound=risk.lower_bound,
                declared=risk.declared,
            ),
            constraint=(
                ConstraintCoverageEvidence(
                    schema_version="1",
                    change_id=trace.change_id,
                    batch_id=trace.authoritative_batch_id,
                    declared=constraint,
                    value=constraint.value,
                    source_digest=source_digest,
                )
                if constraint is not None
                else None
            ),
            auth=(
                AuthMatrixEvidence(
                    schema_version="1",
                    change_id=trace.change_id,
                    batch_id=trace.authoritative_batch_id,
                    declared=auth,
                    value=auth.value,
                    source_digest=source_digest,
                )
                if auth is not None
                else None
            ),
            journey=(
                JourneyCoverageEvidence(
                    schema_version="1",
                    change_id=trace.change_id,
                    batch_id=trace.authoritative_batch_id,
                    declared=journey,
                    value=journey.value,
                    source_digest=source_digest,
                )
                if journey is not None
                else None
            ),
        )
    )
    return MetricsDocument.of(
        risk=risk,
        change_id=trace.change_id,
        cadence="pr",
        computed_at=computed_at,
        metrics=preliminary.metrics,
        policy_digest=policy_digest,
        collection_gaps=preliminary.collection_gaps,
        shortboards=preliminary.shortboards,
        floor_ratio=preliminary.floor_ratio,
    )


def _write_document(write_root: Path, path: str, value: BaseModel | JSONValue) -> EvidenceArtifactRefV1:
    payload: JSONValue = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    data = canonical_json_bytes(payload)
    destination = write_root.joinpath(*PurePosixPath(path).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(data).hexdigest())


def materialize_assessment_inputs(
    request: MaterializeAssessmentInputV1,
    *,
    project_root: Path,
    write_root: Path,
) -> AssessmentInputsV1:
    if _BATCH_TOKEN.fullmatch(request.execution.batch_id) is None:
        raise AssessmentInputError("execution batch_id must be a canonical path token")
    for ref in (
        *request.reviewed_case.preparation_refs,
        request.reviewed_case.review_ref,
        *request.generation.source_refs,
        *request.generation.plan_refs,
    ):
        _read_ref(project_root, ref)
    if request.healing_ref is not None:
        healing_prefix = f"qa/changes/{request.reviewed_case.change_id}/healing/"
        if not request.healing_ref.path.startswith(healing_prefix):
            raise AssessmentInputError("healing evidence must belong to the current change")
        _read_ref(project_root, request.healing_ref)
    if request.issue_ref is not None:
        change_prefix = f"qa/changes/{request.reviewed_case.change_id}/inspect/"
        if request.issue_ref.path != "issues/snapshot.json" and not request.issue_ref.path.startswith(
            change_prefix
        ):
            raise AssessmentInputError("issue evidence must be a current-change or canonical snapshot")
        _read_ref(project_root, request.issue_ref)
    cases = _cases(project_root, request)
    case_ids = frozenset(case.case_id for case in cases)
    capability_leafs = frozenset(key for case in cases for key in case.trace)
    try:
        mapping = ClosedMappingV1.model_validate(
            _load_json(project_root, request.generation.mapping_ref),
            context={"case_ids": case_ids, "capability_leafs": capability_leafs},
        )
        evidence = ExecutionEvidenceV1.model_validate(
            _load_json(project_root, request.execution.evidence_ref),
            context={"case_ids": case_ids, "capability_leafs": capability_leafs},
        )
    except ValidationError as error:
        raise AssessmentInputError(f"invalid mapping or execution evidence: {error}") from error
    if evidence.executed_at != request.execution.executed_at:
        raise AssessmentInputError("execution evidence time differs from its committed cycle")
    if evidence.change_id != request.execution.change_id or evidence.batch_id != request.execution.batch_id:
        raise AssessmentInputError("execution evidence identity does not match the execution cycle")
    if evidence.mapping != mapping:
        raise AssessmentInputError("execution evidence mapping differs from the locked generation mapping")
    expected_status = "PASS" if evidence.status == "passed" else "FAIL"
    if request.execution.final_status != expected_status:
        raise AssessmentInputError("execution cycle final_status disagrees with evidence")
    policy, sufficiency_policy = _policy(project_root, request)
    trace_cases = tuple(
        TraceCaseInput(
            case_id=case.case_id,
            module=case.module,
            case_type=case.type,
            automation_required=case.automation.required,
            assertions=tuple(str(item) for item in case.assertions),
            capability=next(iter(sorted(case.trace))),
        )
        for case in cases
    )
    projection = project_trace(
        TraceOperationInput(
            change_id=request.reviewed_case.change_id,
            batch_id=request.execution.batch_id,
            closed_mapping=mapping.selected,
            cases=trace_cases,
            capability_leafs=tuple(sorted(capability_leafs)),
            case_ids=tuple(sorted(case_ids)),
            execution_evidence=evidence,
            executed_at=request.execution_at,
        )
    )
    sufficiency = build_sufficiency_facts(
        projection,
        policy_digest=request.policy_sha256,
        as_of=request.execution_at,
        recency_hours=sufficiency_policy.recency_hours,
    )
    gaps = build_coverage_gaps(
        projection,
        sufficiency,
        change_id=request.reviewed_case.change_id,
        batch_id=request.execution.batch_id,
    ).model_copy(update={"computed_at": request.execution_at})
    metrics = _metrics(
        cases=cases,
        projection=projection,
        policy_digest=request.policy_sha256,
        computed_at=request.execution_at,
    )
    goals = _applicable_goals(cases)
    selected = {family for family in _FAMILY_ORDER if getattr(evidence.selected_targets, family)}
    scope = ActiveCoverageScopeV1.model_validate(
        {
            "change_id": request.reviewed_case.change_id,
            "coverage_epoch": request.reviewed_case.coverage_epoch,
            "required_case_ids": tuple(sorted(case.case_id for case in cases if case.automation.required)),
            "selected_families": tuple(name for name in _FAMILY_ORDER if name in selected),
            "applicable_goals": goals,
            "applicability_refs": tuple(
                sorted(
                    request.reviewed_case.preparation_refs,
                    key=lambda item: (item.path, item.digest),
                )
            ),
            "risk_tier": metrics.risk_tier,
            "policy_digest": request.policy_sha256,
        }
    )
    base = (
        f"qa/changes/{request.reviewed_case.change_id}/inspect/epochs/"
        f"{request.reviewed_case.coverage_epoch}/batches/{request.execution.batch_id}"
    )
    trace_ref = _write_document(write_root, f"{base}/trace.json", projection)
    gaps_ref = _write_document(write_root, f"{base}/coverage-gaps.json", gaps)
    metrics_ref = _write_document(write_root, f"{base}/metrics.json", metrics)
    sufficiency_ref = _write_document(write_root, f"{base}/trace-sufficiency.json", sufficiency)
    return AssessmentInputsV1(
        change_id=request.reviewed_case.change_id,
        coverage_epoch=request.reviewed_case.coverage_epoch,
        batch_id=request.execution.batch_id,
        scope=scope,
        policy=policy,
        trace_ref=trace_ref,
        gaps_ref=gaps_ref,
        metrics_ref=metrics_ref,
        sufficiency_ref=sufficiency_ref,
        execution_ref=request.execution.evidence_ref,
        healing_ref=request.healing_ref,
        issue_ref=request.issue_ref,
    )


class MaterializeAssessmentExecutor:
    async def execute(
        self,
        validated_input: MaterializeAssessmentInputV1,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[AssessmentInputsV1]:
        output = materialize_assessment_inputs(
            validated_input,
            project_root=scope.workspace.project_root,
            write_root=scope.workspace.write_root,
        )
        return ExecutedAttemptResult(output=output)


class MaterializeAssessmentHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            validated = MaterializeAssessmentInputV1.model_validate(request.input)
            output = materialize_assessment_inputs(
                validated,
                project_root=context.project_root,
                write_root=context.write_root,
            )
        except (AssessmentInputError, ValidationError, OSError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


__all__ = [
    "AssessmentInputError",
    "MaterializeAssessmentExecutor",
    "MaterializeAssessmentHandler",
    "classify_inspection_disposition",
    "materialize_assessment_inputs",
]
