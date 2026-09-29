"""Provider-neutral quality prepare/finalize handlers."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
    InstructionPart,
    ResultContract,
    prompt_model_json,
    with_validation_retry,
)
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    FactBaselineResultV1,
    FinalizedIssueAnalysisV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    QualitySkillInputV1,
    ReportResultV1,
)
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import (
    AssessmentFinalizeInputV1,
    AssessmentSkillInputV1,
    FactBaselineFinalizeInputV1,
    FactBaselineSkillInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    FinalizedReportV1,
    ReportSkillInputV1,
    ReportFinalizeInputV1,
)
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.contracts.issues import (
    IssueCandidateDocument,
    IssueEvidenceManifest,
    ObservationDocument,
)
from assurance_quality.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_input,
)
from assurance_quality.operations.identity import (
    candidate_document_digest,
    problem_fingerprint,
    problem_id,
)
from assurance_quality.operations.inspect import build_failure_classification_facts
from assurance_quality.resource_loader import resource_bytes, resource_text

FACT_BASELINE_SKILL = "skills/aa-fact-baseline/SKILL.md"
INSPECT_SKILL = "skills/aa-inspect/SKILL.md"
ISSUE_ANALYSIS_SKILL = "skills/aa-issue-analyzer/SKILL.md"
ISSUE_TRIAGE_SKILL = "skills/aa-issue-triage-advisor/SKILL.md"
REPORT_SKILL = "skills/aa-report-generator/SKILL.md"

FACT_BASELINE_RESULT_ID = "assurance.quality.result.fact-baseline.v1"
INSPECTION_RESULT_ID = "assurance.quality.result.inspection.v1"
ISSUE_ANALYSIS_RESULT_ID = "assurance.quality.result.issue-analysis.v1"
ISSUE_TRIAGE_RESULT_ID = "assurance.quality.result.issue-triage.v1"
REPORT_RESULT_ID = "assurance.quality.result.report.v1"

_RESULT_FILES: dict[str, str] = {
    FACT_BASELINE_RESULT_ID: "result-contracts/fact-baseline.v1.schema.json",
    INSPECTION_RESULT_ID: "result-contracts/inspection.v1.schema.json",
    ISSUE_ANALYSIS_RESULT_ID: "result-contracts/issue-analysis.v1.schema.json",
    ISSUE_TRIAGE_RESULT_ID: "result-contracts/issue-triage.v1.schema.json",
    REPORT_RESULT_ID: "result-contracts/report.v1.schema.json",
}

_BOUNDED_PROFILES = {
    "aa-archiver": "assurance-v1-archiver",
    "aa-doc-author": "assurance-v1-doc-author",
    "aa-executor": "assurance-v1-executor",
    "aa-explorer": "assurance-v1-explorer",
    "aa-reporter": "assurance-v1-reporter",
    "aa-reviewer": "assurance-v1-reviewer",
    "aa-test-author": "assurance-v1-test-author",
}
_QUALITY_OUTPUTS = {
    FACT_BASELINE_RESULT_ID: lambda change_id: ("qa/results/facts/fact-baseline.json",),
    INSPECTION_RESULT_ID: lambda change_id: ("qa/results/inspect/inspection.json",),
    ISSUE_ANALYSIS_RESULT_ID: lambda change_id: ("qa/results/inspect/issue-analysis.json",),
    ISSUE_TRIAGE_RESULT_ID: lambda change_id: ("qa/results/inspect/issue-triage.json",),
    REPORT_RESULT_ID: lambda change_id: ("qa/results/report/report.md",),
}


def agent_workspace(
    context: TaskContext,
    *,
    allowed_outputs: tuple[str, ...],
    agent_profile: str,
    scope_id: str,
) -> AgentWorkspaceV1:
    try:
        write_root = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        write_root = "qa/.staging/write"
    if write_root in {".", ""}:
        write_root = ".staging/write"
    payload = {
        "schema_version": "1",
        "agent_profile": _BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "scope_id": scope_id,
        "write_root": write_root,
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
        "read_roots": (),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


_TRIAGE_ACTIONS = frozenset(
    {
        "confirm_assessment",
        "mark_not_an_issue",
        "accept_risk",
        "start_work",
        "reopen",
        "merge",
        "stop",
    }
)


def result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        delivery_mode="assistant_json_local_v1",
        schema_document=payload,
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=with_validation_retry(
            (
                InstructionPart.text("text/plain", resource_text(skill_path)),
                InstructionPart.from_json(prompt_model_json(business)),
            ),
            getattr(business, "validation_error", None),
        ),
        result_contract=result_contract(result_schema_id),
        execution=binding.execution,
        workspace=agent_workspace(
            context,
            allowed_outputs=_QUALITY_OUTPUTS[result_schema_id](business.change_id),
            agent_profile=binding.agent_profile,
            scope_id=business.change_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.result_payload)


def _canonical_file(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in posix.parts):
        raise OutputError(f"evidence path must be canonical and relative: {relative}")
    path = root.joinpath(*posix.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise OutputError(f"evidence file is missing: {relative}") from error
    if resolved != path or path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise OutputError(f"evidence file must be a regular single-link file: {relative}")
    return path


def _authenticate_ref(root: Path, ref: object) -> bytes:
    artifact = EvidenceArtifactRefV1.model_validate(ref)
    data = _canonical_file(root, artifact.path).read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.digest:
        raise OutputError(f"evidence digest changed: {artifact.path}")
    return data


def _authenticate_fact_baseline_input(business: FactBaselineSkillInputV1, root: Path) -> None:
    refs = (
        *business.reviewed_case.preparation_refs,
        *business.reviewed_case.case_refs,
        business.reviewed_case.review_ref,
        business.plan_ref,
    )
    for ref in refs:
        _authenticate_ref(root, ref)


def _authenticate_assessment_input(business: AssessmentSkillInputV1, root: Path) -> None:
    refs = (
        *business.reviewed_case.preparation_refs,
        *business.reviewed_case.case_refs,
        business.reviewed_case.review_ref,
        business.mapping_ref,
        business.assessment.trace_ref,
        business.assessment.gaps_ref,
        business.assessment.metrics_ref,
        business.assessment.sufficiency_ref,
        business.assessment.execution_ref,
    )
    for ref in refs:
        _authenticate_ref(root, ref)
    for optional in (business.assessment.healing_ref, business.assessment.issue_ref):
        if optional is not None:
            _authenticate_ref(root, optional)
    if business.fact_baseline_ref is not None:
        _authenticate_ref(root, business.fact_baseline_ref)
    policy = _canonical_file(root, ".aa/policy.yaml").read_bytes()
    if hashlib.sha256(policy).hexdigest() != business.assessment.scope.policy_digest:
        raise OutputError("product policy digest changed after assessment materialization")


def _authenticate_report_input(business: ReportSkillInputV1, root: Path) -> None:
    refs = (
        *business.inspection.reviewed_case.preparation_refs,
        *business.inspection.reviewed_case.case_refs,
        business.inspection.reviewed_case.review_ref,
        business.inspection.mapping_ref,
        *business.generation.source_refs,
        *business.generation.plan_refs,
        *business.inspection.assessment_refs,
    )
    for ref in refs:
        _authenticate_ref(root, ref)
    if business.issue_analysis_ref is not None:
        _authenticate_ref(root, business.issue_analysis_ref)
    policy = _canonical_file(root, ".aa/policy.yaml").read_bytes()
    if hashlib.sha256(policy).hexdigest() != business.assessment.scope.policy_digest:
        raise OutputError("product policy digest changed after inspection")


def _authenticate_issue_analysis_input(business: QualitySkillInputV1, root: Path) -> None:
    expected_prefix = "qa/results/inspect/"

    def unique_path(name: str) -> str:
        matches = [
            path
            for path in business.artifact_paths
            if path.startswith(expected_prefix) and PurePosixPath(path).name == name
        ]
        if len(matches) != 1:
            raise OutputError(f"issue analysis requires exactly one current {name}")
        return matches[0]

    observations_path = unique_path("observations.json")
    manifest_path = unique_path("issue-evidence-manifest.json")
    try:
        observations = ObservationDocument.model_validate_json(
            _canonical_file(root, observations_path).read_bytes()
        )
        manifest = IssueEvidenceManifest.model_validate_json(
            _canonical_file(root, manifest_path).read_bytes()
        )
    except ValidationError as error:
        raise OutputError(str(error)) from error
    identity = (business.change_id, business.batch_id)
    if identity != (observations.change_id, observations.batch_id) or identity != (
        manifest.change_id,
        manifest.batch_id,
    ):
        raise OutputError("issue analysis evidence belongs to another execution batch")
    entries = [entry.model_dump(mode="json") for entry in manifest.entries]
    expected_bundle = f"sha256:{canonical_digest(cast(JSONValue, entries))}"
    if manifest.digest != expected_bundle or business.evidence_bundle_digest != expected_bundle:
        raise OutputError("issue analysis evidence bundle digest changed")
    observation_ids = tuple(sorted(item.observation_id for item in observations.observations))
    if len(observation_ids) != len(set(observation_ids)):
        raise OutputError("issue analysis observations must have unique ids")
    if observation_ids != business.owned_evidence_ids:
        raise OutputError("issue analysis owned evidence does not match observations")
    manifest_paths = {entry.path for entry in manifest.entries}
    if observations_path not in manifest_paths:
        raise OutputError("issue analysis observations must be bound by the evidence manifest")
    if len(manifest_paths) != len(manifest.entries):
        raise OutputError("issue analysis evidence manifest paths must be unique")
    if manifest_paths != set(business.artifact_paths) - {manifest_path}:
        raise OutputError("issue analysis artifacts must match the authenticated manifest")
    for observation in observations.observations:
        if (observation.change_id, observation.batch_id) != identity:
            raise OutputError("issue analysis observation belongs to another execution batch")
        if observation.source.artifact not in manifest_paths:
            raise OutputError("issue analysis observation source is not authenticated")
        if not set(observation.evidence_refs).issubset(manifest_paths):
            raise OutputError("issue analysis observation evidence is not authenticated")
    for entry in manifest.entries:
        actual = hashlib.sha256(_canonical_file(root, entry.path).read_bytes()).hexdigest()
        if entry.digest != f"sha256:{actual}":
            raise OutputError(f"issue analysis evidence digest changed: {entry.path}")


def _load_json_ref(root: Path, ref: object, model: type[Any]) -> Any:
    try:
        return model.model_validate_json(_authenticate_ref(root, ref))
    except ValidationError as error:
        raise OutputError(str(error)) from error


def _staged_agent_document(
    *,
    context: TaskContext,
    relative: str,
    result: object,
    model: type[Any],
) -> tuple[Any, EvidenceArtifactRefV1]:
    path = _canonical_file(context.write_root, relative)
    data = path.read_bytes()
    try:
        staged = model.model_validate_json(data)
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if staged != result:
        raise OutputError(f"staged agent document differs from the structured result: {relative}")
    return staged, EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _prepare(
    skill: str,
    result_id: str,
    request: TaskRequest,
    context: TaskContext,
    *,
    input_model: type[Any] = QualitySkillInputV1,
) -> TaskOutcome:
    business = validate_input(input_model, request.input)
    if isinstance(business, FactBaselineSkillInputV1):
        _authenticate_fact_baseline_input(business, context.project_root)
    elif isinstance(business, AssessmentSkillInputV1):
        _authenticate_assessment_input(business, context.project_root)
    binding = validate_binding(request.binding_data)
    return prepare_outcome(
        skill_path=skill,
        business=business,
        binding=binding,
        result_schema_id=result_id,
        context=context,
    )


class FactBaselinePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(
                FACT_BASELINE_SKILL,
                FACT_BASELINE_RESULT_ID,
                request,
                context,
                input_model=FactBaselineSkillInputV1,
            )
        except (InputError, OutputError) as error:
            return failed_input(error)


class InspectPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(
                INSPECT_SKILL,
                INSPECTION_RESULT_ID,
                request,
                context,
                input_model=AssessmentSkillInputV1,
            )
        except (InputError, OutputError) as error:
            return failed_input(error)


class IssueAnalysisPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(QualitySkillInputV1, request.input)
            _authenticate_issue_analysis_input(business, context.project_root)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=ISSUE_ANALYSIS_SKILL,
                business=business,
                binding=binding,
                result_schema_id=ISSUE_ANALYSIS_RESULT_ID,
                context=context,
            )
        except (InputError, OutputError) as error:
            return failed_input(error)


class IssueTriagePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(ISSUE_TRIAGE_SKILL, ISSUE_TRIAGE_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class ReportPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(ReportSkillInputV1, request.input)
            _authenticate_report_input(business, context.project_root)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=REPORT_SKILL,
                business=business,
                binding=binding,
                result_schema_id=REPORT_RESULT_ID,
                context=context,
            )
        except (InputError, OutputError) as error:
            return failed_input(error)


class FactBaselineFinalizeHandler:
    input_model = FactBaselineFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(FactBaselineFinalizeInputV1, request.input)
            agent_run = business.agent_result
            try:
                document = FactBaselineResultV1.model_validate(thaw_json(agent_run.result_payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            _authenticate_fact_baseline_input(business, context.project_root)
            if document.change_id != business.change_id:
                raise OutputError("fact baseline change_id does not match the locked Reviewed Case")
            relative = "qa/results/facts/fact-baseline.json"
            _, baseline_ref = _staged_agent_document(
                context=context,
                relative=relative,
                result=document,
                model=FactBaselineResultV1,
            )
            finalized = FinalizedFactBaselineV1(
                agent_result=document,
                reviewed_case=business.reviewed_case,
                fact_baseline_ref=baseline_ref,
            )
            return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class InspectFinalizeHandler:
    input_model = AssessmentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(AssessmentFinalizeInputV1, request.input)
            agent_run = business.agent_result
            try:
                document = InspectionResultV1.model_validate(thaw_json(agent_run.result_payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            _authenticate_assessment_input(business, context.project_root)
            if business.fact_baseline_ref is None:
                raise InputError("Inspect requires an authenticated fact baseline")
            relative = "qa/results/inspect/inspection.json"
            _staged_agent_document(
                context=context,
                relative=relative,
                result=document,
                model=InspectionResultV1,
            )
            expected = {
                "execution_digest": business.assessment.execution_ref.digest,
                "healing_digest": (
                    business.assessment.healing_ref.digest
                    if business.assessment.healing_ref is not None
                    else None
                ),
                "trace_digest": business.assessment.trace_ref.digest,
                "coverage_digest": business.assessment.gaps_ref.digest,
                "metrics_digest": business.assessment.metrics_ref.digest,
            }
            actual = {
                "execution_digest": document.execution_digest,
                "healing_digest": document.healing_digest,
                "trace_digest": document.trace_digest,
                "coverage_digest": document.coverage_digest,
                "metrics_digest": document.metrics_digest,
            }
            for key, locked in expected.items():
                if actual[key] != locked:
                    raise OutputError(f"inspection {key} is not closed against the locked projection")
            if document.change_id != business.change_id or document.batch_id != business.batch_id:
                raise OutputError("inspection identity does not match the locked change")
            metrics = _load_json_ref(context.project_root, business.assessment.metrics_ref, MetricsDocument)
            sufficiency = _load_json_ref(
                context.project_root,
                business.assessment.sufficiency_ref,
                TraceSufficiencyFacts,
            )
            execution = _load_json_ref(
                context.project_root,
                business.assessment.execution_ref,
                ExecutionEvidenceV1,
            )
            failure_facts, reason_codes = build_failure_classification_facts(
                execution,
                metrics,
            )
            has_failures = any(item.status == "failed" for item in execution.results)
            if not document.classification_performed:
                raise OutputError("inspection did not complete authenticated classification")
            expected_status = "analyzed" if has_failures else "no_failures"
            if document.status != expected_status:
                raise OutputError(
                    f"inspection status must be {expected_status} for the authenticated execution"
                )
            finalized = FinalizedInspectionV1(
                agent_result=document,
                assessment=business.assessment,
                reviewed_case=business.reviewed_case,
                mapping_ref=business.mapping_ref,
                metrics=metrics,
                sufficiency=sufficiency,
                failure_facts=failure_facts,
                fact_baseline_ref=business.fact_baseline_ref,
                reason_codes=reason_codes,
            )
            return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class IssueAnalysisFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = IssueAnalysisResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.change_id != payload.change_id or document.batch_id != payload.batch_id:
                raise OutputError("issue analysis identity does not match the locked change")
            if not payload.evidence_bundle_digest:
                raise InputError("evidence_bundle_digest must authenticate issue analysis")
            if document.evidence_bundle_digest != payload.evidence_bundle_digest:
                raise OutputError("issue analysis evidence bundle is not authenticated")
            owned = frozenset(payload.owned_evidence_ids)
            try:
                document.require_complete_coverage(owned)
            except ValueError as error:
                raise OutputError(str(error)) from error
            for candidate in document.candidates:
                try:
                    fingerprint = problem_fingerprint(
                        affected_surface=candidate.affected_surface,
                        fingerprint_inputs=candidate.fingerprint_inputs,
                    )
                except ValueError as error:
                    raise OutputError(f"issue candidate {candidate.candidate_id}: {error}") from error
                expected_id = problem_id(fingerprint)
                if candidate.possible_problem_ids and expected_id not in candidate.possible_problem_ids:
                    raise OutputError(f"issue candidate possible_problem_ids does not contain {expected_id}")
            _authenticate_issue_analysis_input(payload, context.project_root)
            candidates_doc = IssueCandidateDocument(
                schema_version="1.0",
                change_id=document.change_id,
                batch_id=document.batch_id,
                evidence_bundle_digest=document.evidence_bundle_digest,
                candidates=list(document.candidates),
            )
            relative = "qa/results/inspect/issue-analysis.json"
            _, issue_analysis_ref = _staged_agent_document(
                context=context,
                relative=relative,
                result=document,
                model=IssueAnalysisResultV1,
            )
            finalized = FinalizedIssueAnalysisV1(
                agent_result=document,
                candidate_digest=(
                    candidate_document_digest(candidates_doc) if document.status == "completed" else None
                ),
                issue_analysis_ref=issue_analysis_ref,
            )
            return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class IssueTriageFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = IssueTriageResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.recommended_action not in _TRIAGE_ACTIONS:
                raise OutputError(
                    f"issue triage recommended_action is not declared: {document.recommended_action}"
                )
            locked = payload.locked_evidence_digests
            if not locked:
                raise InputError("locked_evidence_digests must authenticate triage evidence")
            if dict(document.evidence_digests) != dict(locked):
                raise OutputError("issue triage evidence_digests must equal locked_evidence_digests")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ReportFinalizeHandler:
    input_model = ReportFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(ReportFinalizeInputV1, request.input)
            agent_run = business.agent_result
            try:
                document = ReportResultV1.model_validate(thaw_json(agent_run.result_payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            _authenticate_report_input(business, context.project_root)
            expected = {
                "case_digest": business.case_digest,
                "plan_digest": business.plan_digest,
                "mapping_digest": business.mapping_digest,
                "execution_digest": business.execution_digest,
                "healing_digest": business.healing_digest,
                "trace_digest": business.trace_digest,
                "coverage_digest": business.coverage_digest,
                "issue_digest": business.issue_digest,
                "metrics_digest": business.metrics_digest,
            }
            actual = {
                "case_digest": document.case_digest,
                "plan_digest": document.plan_digest,
                "mapping_digest": document.mapping_digest,
                "execution_digest": document.execution_digest,
                "healing_digest": document.healing_digest,
                "trace_digest": document.trace_digest,
                "coverage_digest": document.coverage_digest,
                "issue_digest": document.issue_digest,
                "metrics_digest": document.metrics_digest,
            }
            for key, locked in expected.items():
                if actual[key] != locked:
                    raise OutputError(f"report {key} is not closed against the locked projection")
            if document.change_id != business.change_id or document.batch_id != business.batch_id:
                raise OutputError("report identity does not match the locked change")
            if document.purpose != business.purpose:
                raise OutputError("report purpose does not match the locked request")
            allowed = _QUALITY_OUTPUTS[REPORT_RESULT_ID](business.change_id)
            if document.report_files != allowed:
                raise OutputError("report files must exactly match the declared report write set")
            staged = {
                path.relative_to(context.write_root).as_posix()
                for path in context.write_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            if staged != set(document.report_files):
                raise OutputError("report result does not match the actual candidate write set")
            refs = tuple(
                EvidenceArtifactRefV1(
                    path=relative,
                    digest=hashlib.sha256(
                        _canonical_file(context.write_root, relative).read_bytes()
                    ).hexdigest(),
                )
                for relative in document.report_files
            )
            finalized = FinalizedReportV1(
                change_id=business.change_id,
                coverage_epoch=business.coverage_epoch,
                batch_id=business.batch_id,
                purpose=business.purpose,
                plan_digest=business.plan_digest,
                plan_ref=business.plan_ref,
                inspection_receipt=business.inspection.inspection_receipt,
                report_refs=refs,
            )
            return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
