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
from assurance_generation.contracts.families import LayerName
from assurance_intake.contracts.cases import (
    CaseEntryAuthoring,
    CaseYamlAuthoring,
    MinimumCoverageMatrixAuthoring,
)
from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.quality_goals import (
    CoverageGoal,
    MrcCategory,
    MrcLayer,
    PreparedObligationV1,
    journey_keys_from_document,
    normalize_goal_obligations,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan, decode_plan
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    MaterializeAssessmentInputV1,
)
from assurance_quality.contracts.decisions import classify_inspection_disposition
from assurance_quality.contracts.coverage import MinimumCoverageMatrixRow
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
from assurance_quality.operations.coverage import (
    MinimumCoverageInput,
    build_coverage_gaps,
    join_minimum_coverage,
)
from assurance_quality.operations.metrics import (
    PrEvidenceBundle,
    RiskInput,
    build_metrics_document,
    resolve_case_risk,
)
from assurance_quality.operations.sufficiency import build_sufficiency_facts
from assurance_quality.operations.trace import TraceCaseInput, TraceOperationInput, project_trace
from assurance_quality.operations.common import json_digest
from assurance_quality.operations.goal_scope import goal_case_map, has_layer_evidence, obligation_goal


class AssessmentInputError(ValueError):
    """The assessment sources could not prove one coherent execution cycle."""


_FAMILY_ORDER = ("api", "e2e", "fuzz", "performance")
_GOAL_ORDER = ("constraint_coverage", "auth_matrix_coverage", "journey_coverage")
_BATCH_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_GOAL_RESOURCE_PATHS = {
    "assurance.product.configuration.capability-catalog": ".aa/capability-catalog.json",
    "assurance.product.configuration.data-knowledge": ".aa/data-knowledge.yaml",
}


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


def _cases(
    root: Path,
    request: MaterializeAssessmentInputV1,
    *,
    capability_leafs: frozenset[str],
) -> tuple[CaseEntryAuthoring, ...]:
    cases: list[CaseEntryAuthoring] = []
    for ref in request.reviewed_case.case_refs:
        try:
            raw = yaml.safe_load(_read_ref(root, ref))
            if not isinstance(raw, Mapping):
                raise AssessmentInputError(f"reviewed Case inventory is not a mapping: {ref.path}")
            document = CaseYamlAuthoring.model_validate(
                raw,
                context={"capability_leafs": capability_leafs},
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


def _prepared_obligations(
    root: Path,
    request: MaterializeAssessmentInputV1,
    *,
    plan: ResolvedAssurancePlan,
) -> tuple[tuple[PreparedObligationV1, ...], frozenset[str], frozenset[str]]:
    quality_goal = plan.quality_goal
    try:
        advisory = ExploreAdvisoryV1.model_validate(json.loads(_read_ref(root, quality_goal.obligations_ref)))
    except (json.JSONDecodeError, ValidationError) as error:
        raise AssessmentInputError(f"invalid prepared goal obligations: {error}") from error
    if advisory.change_id != request.reviewed_case.change_id:
        raise AssessmentInputError("prepared goal obligations belong to another change")

    source_bytes: dict[str, bytes] = {}
    for resource_id, digest in quality_goal.source_resource_digests:
        try:
            path = _GOAL_RESOURCE_PATHS[resource_id]
        except KeyError as error:
            raise AssessmentInputError(f"unsupported prepared goal source: {resource_id}") from error
        source_bytes[resource_id] = _read_ref(
            root,
            EvidenceArtifactRefV1(path=path, digest=digest),
        )
    if set(source_bytes) != set(_GOAL_RESOURCE_PATHS):
        raise AssessmentInputError("prepared goals require catalog and data knowledge sources")
    try:
        catalog = json.loads(source_bytes["assurance.product.configuration.capability-catalog"])
        knowledge = yaml.safe_load(source_bytes["assurance.product.configuration.data-knowledge"])
        if not isinstance(catalog, Mapping) or not isinstance(knowledge, Mapping):
            raise AssessmentInputError("prepared goal sources must be mappings")
        raw_leafs = catalog.get("typed_leafs")
        if not isinstance(raw_leafs, list) or any(not isinstance(item, str) for item in raw_leafs):
            raise AssessmentInputError("capability catalog typed_leafs are invalid")
        capability_leafs = frozenset(raw_leafs)
        journey_keys = frozenset(journey_keys_from_document(knowledge))
        obligations = normalize_goal_obligations(
            advisory,
            capability_leafs=capability_leafs,
            journey_keys=journey_keys,
        )
    except (json.JSONDecodeError, yaml.YAMLError, ValidationError, ValueError) as error:
        if isinstance(error, AssessmentInputError):
            raise
        raise AssessmentInputError(f"invalid prepared goal sources: {error}") from error
    return obligations, capability_leafs, journey_keys


def _reviewed_obligations(
    root: Path,
    request: MaterializeAssessmentInputV1,
    cases: tuple[CaseEntryAuthoring, ...],
    *,
    baseline: tuple[PreparedObligationV1, ...],
    capability_leafs: frozenset[str],
    journey_keys: frozenset[str],
) -> tuple[MinimumCoverageMatrixRow, ...]:
    matrix_path = f"qa/changes/{request.reviewed_case.change_id}/trace/minimum-coverage-matrix.json"
    matrix_ref = next(
        (ref for ref in request.reviewed_case.preparation_refs if ref.path == matrix_path),
        None,
    )
    if matrix_ref is None:
        raise AssessmentInputError("Reviewed Case is missing its minimum coverage matrix")
    try:
        matrix = MinimumCoverageMatrixAuthoring.model_validate(json.loads(_read_ref(root, matrix_ref)))
    except (json.JSONDecodeError, ValidationError) as error:
        raise AssessmentInputError(f"invalid reviewed minimum coverage matrix: {error}") from error
    case_by_id = {case.case_id: case for case in cases}
    prepared = {row.key: row for row in baseline}
    result = {row.key: MinimumCoverageMatrixRow(**row.model_dump()) for row in baseline}
    journey_cases: set[str] = set()
    for row in matrix.root:
        unknown = sorted(set(row.covered_by_cases) - set(case_by_id))
        if unknown:
            raise AssessmentInputError(f"minimum coverage matrix references unknown active cases: {unknown}")
        original = prepared.get(row.key)
        category: MrcCategory = (
            original.category
            if original is not None
            else row.category
            or (
                "e2e"
                if row.layer == "e2e" or any(case_by_id[cid].type == "E2E" for cid in row.covered_by_cases)
                else "api"
            )
        )
        layer = (
            original.layer
            if original is not None
            else row.layer or ("e2e" if category in {"e2e", "e2e_if_enabled"} else "api")
        )
        if original is not None and (
            row.category not in {None, original.category} or row.layer not in {None, original.layer}
        ):
            raise AssessmentInputError(f"reviewed MRC scope conflicts with prepared obligation: {row.key}")
        goal = obligation_goal(key=row.key, category=category)
        if goal == "journey_coverage" and row.key not in journey_keys:
            raise AssessmentInputError(f"unknown reviewed journey key: {row.key}")
        if (
            category in {"negative", "data_integrity"}
            or goal in {"constraint_coverage", "auth_matrix_coverage"}
        ) and row.key not in capability_leafs:
            raise AssessmentInputError(f"unknown reviewed closed MRC key: {row.key}")
        related_cases: list[str] = []
        for case_id in sorted(set(row.covered_by_cases)):
            case = case_by_id[case_id]
            if layer in {"api", "e2e"} and case.type.lower() != layer:
                raise AssessmentInputError(f"reviewed MRC {row.key} requires {layer} Case evidence")
            if goal == "journey_coverage":
                if case.type != "E2E":
                    continue
                journey_cases.add(case_id)
            elif row.key in capability_leafs and row.key not in case.trace:
                continue
            related_cases.append(case_id)
        result[row.key] = MinimumCoverageMatrixRow(
            mrc_id=original.mrc_id if original is not None else row.mrc_id,
            key=row.key,
            category=category,
            required=row.required or (original.required if original is not None else False),
            layer=layer,
            covered_by_cases=related_cases,
        )
    unmapped_journeys = sorted(
        case.case_id for case in cases if case.type == "E2E" and case.case_id not in journey_cases
    )
    if unmapped_journeys:
        raise AssessmentInputError(f"reviewed E2E Cases have no valid journey mapping: {unmapped_journeys}")
    ids = [row.mrc_id for row in result.values()]
    if len(ids) != len(set(ids)):
        raise AssessmentInputError("reviewed MRC additions reuse a prepared obligation id")
    for key, obligation in result.items():
        if obligation_goal(key=key, category=obligation.category) in {
            "constraint_coverage",
            "auth_matrix_coverage",
        }:
            result[key] = obligation.model_copy(
                update={
                    "covered_by_cases": sorted(
                        case.case_id
                        for case in cases
                        if key in case.trace
                        and (
                            case.type.lower() == obligation.layer
                            or (obligation.layer == "both" and case.type in {"API", "E2E"})
                        )
                    )
                }
            )
    return tuple(sorted(result.values(), key=lambda row: (row.mrc_id, row.key)))


def _metrics(
    *,
    cases: tuple[CaseEntryAuthoring, ...],
    projection: TraceProjectionV2,
    policy_digest: str,
    computed_at: datetime,
    baseline: tuple[PreparedObligationV1, ...],
    reviewed: Mapping[CoverageGoal, Mapping[str, tuple[str, ...]]],
    required_layers: Mapping[str, MrcLayer],
) -> MetricsDocument:
    trace = projection
    passed = {
        row.case_id
        for row in trace.rows
        if row.freshest_pass is not None and row.freshest_pass.batch_id == trace.authoritative_batch_id
    }
    case_layers = {case.case_id: case.type.lower() for case in cases}

    def covered(key: str, case_ids: frozenset[str]) -> bool:
        evidence = case_ids & passed
        layer = required_layers.get(key)
        return bool(evidence) if layer is None else has_layer_evidence(layer, evidence, case_layers)

    def scope(goal: CoverageGoal) -> MetricScope | None:
        obligations = goal_case_map(goal, baseline=baseline, reviewed=reviewed[goal])
        if not obligations:
            return None
        uncovered = tuple(key for key, case_ids in obligations.items() if not covered(key, case_ids))
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
    try:
        plan = decode_plan(_read_ref(project_root, request.plan_ref), request.plan_ref)
    except (ValidationError, ValueError) as error:
        raise AssessmentInputError(f"invalid frozen assurance plan: {error}") from error
    if plan.plan_digest != request.plan_digest:
        raise AssessmentInputError("assessment plan digest does not match frozen plan")
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
    baseline, goal_leafs, journey_keys = _prepared_obligations(project_root, request, plan=plan)
    cases = _cases(project_root, request, capability_leafs=goal_leafs)
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
    if plan.policy_resource_id != request.policy_resource_id:
        raise AssessmentInputError("assessment policy resource differs from frozen plan")
    if plan.policy_digest != request.policy_sha256:
        raise AssessmentInputError("assessment policy digest differs from frozen plan")
    if plan.quality_goal.coverage_policy != policy:
        raise AssessmentInputError("current coverage policy differs from frozen plan")
    if plan.quality_goal.sufficiency_policy != sufficiency_policy:
        raise AssessmentInputError("current sufficiency policy differs from frozen plan")
    obligations = _reviewed_obligations(
        project_root,
        request,
        cases,
        baseline=baseline,
        capability_leafs=goal_leafs,
        journey_keys=journey_keys,
    )
    reviewed_goal_maps: dict[CoverageGoal, dict[str, tuple[str, ...]]] = {
        "constraint_coverage": {},
        "auth_matrix_coverage": {},
        "journey_coverage": {},
    }
    trace_keys = {key for case in cases for key in case.trace}
    for obligation in obligations:
        goal = obligation_goal(key=obligation.key, category=obligation.category)
        if goal is not None and (
            obligation.required or obligation.covered_by_cases or obligation.key in trace_keys
        ):
            reviewed_goal_maps[goal][obligation.key] = tuple(obligation.covered_by_cases)
    mrc_keys = {obligation.key for obligation in obligations}
    for case in cases:
        for key in case.trace:
            if key in mrc_keys:
                continue
            goal = obligation_goal(key=key, category="api")
            if goal is not None:
                mapped = reviewed_goal_maps[goal].get(key, ())
                reviewed_goal_maps[goal][key] = tuple(sorted({*mapped, case.case_id}))
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
    minimum_coverage = join_minimum_coverage(
        MinimumCoverageInput(
            change_id=request.reviewed_case.change_id,
            items=obligations,
            executed_case_ids=tuple(
                row.case_id
                for row in projection.rows
                if row.latest_execution is not None
                and row.latest_execution.batch_id == request.execution.batch_id
                and row.latest_execution.status in {"passed", "failed"}
            ),
            failed_case_ids=tuple(
                row.case_id
                for row in projection.rows
                if row.latest_execution is not None and row.latest_execution.status == "failed"
            ),
            case_layers={case.case_id: cast(LayerName, case.type.lower()) for case in cases},
        )
    )
    # Numeric obligations retain their authenticated coverage floors. MRCs
    # without a numeric goal still require execution evidence through the
    # existing sufficiency route.
    if any(
        item.required
        and obligation_goal(key=item.key, category=item.category) is None
        and item.status != "covered"
        for item in minimum_coverage.items
    ):
        sufficiency = sufficiency.model_copy(update={"sufficient": False})
    gaps = build_coverage_gaps(
        projection,
        sufficiency,
        change_id=request.reviewed_case.change_id,
        batch_id=request.execution.batch_id,
    ).model_copy(update={"computed_at": request.execution_at, "minimum_coverage": minimum_coverage})
    metrics = _metrics(
        cases=cases,
        projection=projection,
        policy_digest=request.policy_sha256,
        computed_at=request.execution_at,
        baseline=baseline,
        reviewed=reviewed_goal_maps,
        required_layers={obligation.key: obligation.layer for obligation in obligations},
    )
    goals = tuple(
        goal
        for goal in _GOAL_ORDER
        if goal_case_map(
            cast(CoverageGoal, goal),
            baseline=baseline,
            reviewed=reviewed_goal_maps[cast(CoverageGoal, goal)],
        )
    )
    selected = {family for family in _FAMILY_ORDER if getattr(evidence.selected_targets, family)}
    applicability_refs = (
        *request.reviewed_case.preparation_refs,
        plan.quality_goal.obligations_ref,
        *(
            EvidenceArtifactRefV1(path=_GOAL_RESOURCE_PATHS[resource_id], digest=digest)
            for resource_id, digest in plan.quality_goal.source_resource_digests
        ),
    )
    applicability_by_identity = {(ref.path, ref.digest): ref for ref in applicability_refs}
    scope = ActiveCoverageScopeV1.model_validate(
        {
            "change_id": request.reviewed_case.change_id,
            "coverage_epoch": request.reviewed_case.coverage_epoch,
            "required_case_ids": tuple(sorted(case.case_id for case in cases if case.automation.required)),
            "selected_families": tuple(name for name in _FAMILY_ORDER if name in selected),
            "applicable_goals": goals,
            "applicability_refs": tuple(
                sorted(
                    applicability_by_identity.values(),
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
        plan_digest=request.plan_digest,
        plan_ref=request.plan_ref,
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
