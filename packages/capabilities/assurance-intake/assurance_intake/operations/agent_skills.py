"""Deterministic intake prepare handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import (
    AgentBindingDataV1,
    CaseDesignInputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    IntakeInputV1,
    ReviewRepairActionV1,
    ReviewRepairContractV1,
)
from assurance_intake.contracts.cases import CaseEntryAuthoring, CaseYamlAuthoring
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan, decode_plan
from assurance_intake.contracts.verification import (
    AssertionSourcesV1,
    BusinessAssertionV1,
    validate_assertion_provenance,
)
from assurance_intake.contracts.explore import ExploreAdvisoryV1, build_explore_context
from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.resource_loader import resource_bytes, resource_text

INTAKE_SKILL = "skills/aa-intake/SKILL.md"
INTAKE_PERSONA = "personas/intake-host.md"
EXPLORE_SKILL = "skills/aa-explore/SKILL.md"
EXPLORE_PERSONA = "personas/explorer.md"
CASE_DESIGN_SKILL = "skills/aa-case-design/SKILL.md"
CASE_DESIGN_REPAIR_SKILL = "skills/aa-case-repair/SKILL.md"
CASE_DESIGN_PERSONA = "personas/doc-author.md"
CASE_REVIEW_SKILL = "skills/aa-case-reviewer/SKILL.md"
CASE_REVIEW_PERSONA = "personas/reviewer.md"

INTAKE_RESULT_ID = "assurance.intake.result.intake.v1"
EXPLORE_RESULT_ID = "assurance.intake.result.explore.v1"
CASE_DESIGN_RESULT_ID = "assurance.intake.result.case-design.v1"
CASE_REVIEW_RESULT_ID = "assurance.intake.result.case-review.v1"

_RESULT_FILES: Mapping[str, str] = {
    INTAKE_RESULT_ID: "result-contracts/intake.v1.schema.json",
    EXPLORE_RESULT_ID: "result-contracts/explore.v1.schema.json",
    CASE_DESIGN_RESULT_ID: "result-contracts/case-design.v1.schema.json",
    CASE_REVIEW_RESULT_ID: "result-contracts/case-review.v1.schema.json",
}
_BOUNDED_PROFILES: Mapping[str, str] = {
    "aa-archiver": "assurance-v1-archiver",
    "aa-doc-author": "assurance-v1-doc-author",
    "aa-executor": "assurance-v1-executor",
    "aa-explorer": "assurance-v1-explorer",
    "aa-reporter": "assurance-v1-reporter",
    "aa-reviewer": "assurance-v1-reviewer",
    "aa-test-author": "assurance-v1-test-author",
}


def intake_outputs(change_id: str) -> tuple[str, ...]:
    return tuple(sorted((f"qa/changes/{change_id}/.qa.yaml", f"qa/changes/{change_id}/requirement.md")))


def explore_outputs(change_id: str) -> tuple[str, ...]:
    return (f"qa/changes/{change_id}/explore/exploration.json",)


def case_design_outputs(
    change_id: str,
    case_delta_paths: tuple[str, ...],
    assertion_source_paths: tuple[str, ...] = (),
) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                *case_delta_paths,
                *assertion_source_paths,
                f"qa/changes/{change_id}/.qa.yaml",
                f"qa/changes/{change_id}/proposal.md",
                f"qa/changes/{change_id}/trace/minimum-coverage-matrix.json",
            )
        )
    )


def case_review_outputs(change_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                f"qa/changes/{change_id}/review/case-review.json",
                f"qa/changes/{change_id}/review/case-review-summary.md",
            )
        )
    )


def case_review_inputs(
    change_id: str,
    case_delta_paths: tuple[str, ...],
    assertion_source_paths: tuple[str, ...] = (),
) -> tuple[str, ...]:
    change_root = f"qa/changes/{change_id}"
    return tuple(
        sorted(
            (
                f"{change_root}/.qa.yaml",
                *case_delta_paths,
                *assertion_source_paths,
                f"{change_root}/proposal.md",
                f"{change_root}/requirement.md",
                f"{change_root}/trace/minimum-coverage-matrix.json",
            )
        )
    )


def _require_regular_project_input(project_root: Path, relative: str) -> None:
    root = project_root.resolve()
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise InputError(f"case-review input must not contain a symlink: {relative}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise InputError(f"missing case-review input: {relative}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise InputError(f"case-review input must be a regular single-link file: {relative}")


def _authenticate_evidence_refs(
    project_root: Path,
    refs: tuple[EvidenceArtifactRefV1, ...],
) -> None:
    for ref in refs:
        _require_regular_project_input(project_root, ref.path)
        path = project_root.joinpath(*ref.path.split("/"))
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref.digest:
            raise InputError(f"evidence digest changed after it was committed: {ref.path}")


def _authenticate_plan(
    project_root: Path,
    *,
    change_id: str,
    plan_digest: str,
    plan_ref: EvidenceArtifactRefV1,
) -> ResolvedAssurancePlan:
    _authenticate_evidence_refs(project_root, (plan_ref,))
    try:
        plan = decode_plan(
            project_root.joinpath(*plan_ref.path.split("/")).read_bytes(),
            plan_ref,
        )
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.change_id != change_id or plan.plan_digest != plan_digest:
        raise InputError("frozen assurance plan does not match case input")
    return plan


def _require_verified_sidecars(
    plan: ResolvedAssurancePlan,
    *,
    case_paths: tuple[str, ...],
    source_paths: tuple[str, ...],
) -> None:
    if plan.verification_policy is not None and len(source_paths) != len(case_paths):
        raise InputError("verified plan requires exactly one assertion source sidecar per case")


def _require_verified_requirement_ref(
    plan: ResolvedAssurancePlan,
    *,
    change_id: str,
    refs: tuple[EvidenceArtifactRefV1, ...],
) -> None:
    if plan.verification_policy is None:
        return
    requirement_path = f"qa/changes/{change_id}/requirement.md"
    if sum(ref.path == requirement_path for ref in refs) != 1:
        raise InputError("verified plan requires one authenticated requirement ref")


def _validate_review_verification_inputs(
    project_root: Path,
    *,
    plan: ResolvedAssurancePlan,
    business: CaseReviewInputV1,
) -> None:
    policy = plan.verification_policy
    if policy is None:
        return
    requirement_path = f"qa/changes/{business.change_id}/requirement.md"
    requirement_ref = next(ref for ref in business.preparation_refs if ref.path == requirement_path)
    review_prefix = f"qa/changes/{business.change_id}/review/"
    review_history_marker = f"qa/changes/{business.change_id}/cases/reviews/"
    authority_refs = tuple(
        ref
        for ref in business.preparation_refs
        if ref.path == requirement_path
        or ref.path.startswith(review_prefix)
        or ref.path.startswith(review_history_marker)
    )
    entries: dict[str, CaseEntryAuthoring] = {}
    try:
        for relative in business.case_delta_paths:
            raw = yaml.safe_load(project_root.joinpath(*relative.split("/")).read_bytes())
            document = CaseYamlAuthoring.model_validate(
                raw,
                context={"capability_leafs": frozenset(business.capability_leafs)},
            )
            for entry in (*document.added, *document.modified):
                if entry.case_id in entries:
                    raise ValueError(f"duplicate authored case: {entry.case_id}")
                entries[entry.case_id] = entry
        seen: set[str] = set()
        revision = policy.validation_profile.rsplit(".v", maxsplit=1)[1]
        for relative in business.assertion_source_paths:
            sources = AssertionSourcesV1.model_validate_json(
                project_root.joinpath(*relative.split("/")).read_bytes()
            )
            entry = entries.get(sources.case_id)
            if entry is None:
                raise ValueError(
                    f"assertion-sources.json references unknown authored case: {sources.case_id}"
                )
            seen.add(sources.case_id)
            assertions = tuple(BusinessAssertionV1.model_validate(item) for item in entry.assertions)
            validate_assertion_provenance(
                case_id=sources.case_id,
                revision=revision,
                spec_digest=plan.requirement_digest,
                assertions=assertions,
                sources=sources,
                requirement_ref=requirement_ref,
                authority_refs=authority_refs,
            )
        if seen != set(entries):
            missing = sorted(set(entries) - seen)
            raise ValueError(f"typed assertion sources are missing for authored cases: {missing}")
    except (OSError, yaml.YAMLError, ValidationError, TypeError, ValueError) as error:
        raise InputError(f"invalid typed assertion provenance: {error}") from error


def _review_repair_contract(
    project_root: Path,
    *,
    business: CaseDesignInputV1,
) -> ReviewRepairContractV1 | None:
    review_relative = f"qa/changes/{business.change_id}/review/case-review.json"
    review_path = project_root.joinpath(*review_relative.split("/"))
    if not review_path.exists() and not review_path.is_symlink():
        return None
    _require_regular_project_input(project_root, review_relative)
    review_bytes = review_path.read_bytes()
    try:
        review = CaseReviewResultV1.model_validate_json(review_bytes)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid case-review.json for repair: {error}") from error
    if review.change_id != business.change_id:
        raise InputError("case-review.json change_id does not match case-design change_id")
    if review.public_outcome != "needs_fix":
        return None

    findings = {finding.id: finding for finding in review.findings}
    actions: list[ReviewRepairActionV1] = []
    for raw_plan in review.auto_fix_plan:
        if not isinstance(raw_plan, Mapping):
            raise InputError("case-review auto_fix_plan items must be mappings")
        finding_id = raw_plan.get("finding_id")
        artifact = raw_plan.get("artifact")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise InputError("case-review repair finding_id must reference an existing finding")
        finding = findings[finding_id]
        try:
            case_id = normalized_auto_fix_case_id(raw_plan, finding.locator.case_id)
            edits = normalized_auto_fix_edits(raw_plan)
        except ValueError as error:
            raise InputError(str(error)) from error
        if finding.severity in {"critical", "blocking"}:
            raise InputError("critical or blocking case-review findings cannot be auto-fixed")
        if not isinstance(artifact, str) or artifact != finding.locator.artifact:
            raise InputError("case-review repair artifact must match its finding locator")
        if case_id != finding.locator.case_id:
            raise InputError("case-review repair case_id must match its finding locator")
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise InputError("automatic case repair requires an exact locator key")
        allowed_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        try:
            actions.append(
                ReviewRepairActionV1(
                    finding_id=finding_id,
                    artifact=artifact,
                    case_id=case_id,
                    allowed_paths=allowed_paths,
                    instructions=edits,
                )
            )
        except ValidationError as error:
            raise InputError(f"invalid case-review repair action: {error}") from error
    if not actions:
        raise InputError("needs_fix case-review must provide at least one bounded repair action")

    outputs = case_design_outputs(
        business.change_id,
        business.case_delta_paths,
        business.assertion_source_paths,
    )
    allowed = set(outputs)
    for action in actions:
        if action.artifact not in allowed:
            raise InputError(
                f"case-review repair artifact is outside the locked case-design write set: {action.artifact}"
            )
    baseline_file_digests: dict[str, str] = {}
    baseline_case_documents: dict[str, object] = {}
    for relative in outputs:
        _require_regular_project_input(project_root, relative)
        data = project_root.joinpath(*relative.split("/")).read_bytes()
        baseline_file_digests[relative] = hashlib.sha256(data).hexdigest()
        if relative.endswith("/case.yaml"):
            try:
                baseline_case_documents[relative] = yaml.safe_load(data)
            except yaml.YAMLError as error:
                raise InputError(f"invalid baseline case.yaml {relative}: {error}") from error
    try:
        return ReviewRepairContractV1(
            review_path=review_relative,
            review_sha256=hashlib.sha256(review_bytes).hexdigest(),
            baseline_file_digests=baseline_file_digests,
            baseline_case_documents=baseline_case_documents,
            actions=tuple(actions),
        )
    except ValidationError as error:
        raise InputError(f"invalid deterministic case-review repair contract: {error}") from error


def _logical_write_root(context: TaskContext) -> str:
    try:
        relative = context.write_root.resolve().relative_to(context.project_root.resolve()).as_posix()
    except ValueError:
        relative = "qa/changes/_attempt/.staging/write"
    if relative in {".", ""}:
        return ".staging/write"
    return relative


def agent_workspace(
    context: TaskContext,
    *,
    allowed_outputs: tuple[str, ...],
    agent_profile: str,
    scope_id: str,
) -> AgentWorkspaceV1:
    write_root = _logical_write_root(context)
    payload = {
        "schema_version": "1",
        "agent_profile": _BOUNDED_PROFILES.get(agent_profile, agent_profile),
        "scope_id": scope_id,
        "write_root": write_root,
        "allowed_outputs": tuple(sorted(set(allowed_outputs))),
        "read_roots": (),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


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


def validate_input(model: type[Any], data: object) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(
    *,
    skill_path: str,
    persona_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(business.model_dump(mode="json")),
        ),
        result_contract=result_contract(result_schema_id),
        execution=binding.execution,
        workspace=agent_workspace(
            context,
            allowed_outputs=allowed_outputs,
            agent_profile=binding.agent_profile,
            scope_id=business.change_id,
        ),
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=False)


class IntakePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(IntakeInputV1, request.input)
            binding = validate_binding(request.binding_data)
            return prepare_outcome(
                skill_path=INTAKE_SKILL,
                persona_path=INTAKE_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=INTAKE_RESULT_ID,
                context=context,
                allowed_outputs=intake_outputs(business.change_id),
            )
        except InputError as error:
            return failed_input(error)


class ExplorePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(ExploreInputV1, request.input)
            binding = validate_binding(request.binding_data)
            document = build_explore_context(
                context.project_root,
                change_id=business.change_id,
            )
            relative = f"qa/changes/{business.change_id}/explore/context.json"
            path = context.write_root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json_bytes(document.model_dump(mode="json")) + b"\n")
            return prepare_outcome(
                skill_path=EXPLORE_SKILL,
                persona_path=EXPLORE_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=EXPLORE_RESULT_ID,
                context=context,
                allowed_outputs=explore_outputs(business.change_id),
            )
        except InputError as error:
            return failed_input(error)


class CaseDesignPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = CaseDesignInputV1.model_validate(request.input)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            plan = _authenticate_plan(
                context.project_root,
                change_id=business.change_id,
                plan_digest=business.plan_digest,
                plan_ref=business.plan_ref,
            )
            if business.selected_test_families != plan.selected_test_families:
                raise InputError("case selected families do not match frozen assurance plan")
            _require_verified_sidecars(
                plan,
                case_paths=business.case_delta_paths,
                source_paths=business.assertion_source_paths,
            )
            _authenticate_evidence_refs(context.project_root, business.preparation_refs)
            _require_verified_requirement_ref(
                plan,
                change_id=business.change_id,
                refs=business.preparation_refs,
            )
            if business.case_rework_context is not None:
                rework = business.case_rework_context
                _authenticate_evidence_refs(context.project_root, rework.assessment_refs)
                _authenticate_evidence_refs(
                    context.project_root,
                    (*rework.previous_case.preparation_refs, *rework.previous_case.case_refs),
                )
            exploration_relative = f"qa/changes/{business.change_id}/explore/exploration.json"
            exploration_path = context.project_root.joinpath(*exploration_relative.split("/"))
            exploration = None
            if exploration_path.exists() or exploration_path.is_symlink():
                if not exploration_path.is_file() or exploration_path.is_symlink():
                    raise InputError("exploration.json must be a regular file")
                try:
                    exploration = ExploreAdvisoryV1.model_validate_json(exploration_path.read_bytes())
                except (OSError, ValidationError, ValueError) as error:
                    raise InputError(f"invalid exploration.json: {error}") from error
                if exploration.change_id != business.change_id:
                    raise InputError("exploration.json change_id does not match case-design change_id")
                if exploration.context_ref != "explore/context.json":
                    raise InputError("exploration.json context_ref must be explore/context.json")
            business = business.model_copy(update={"exploration": exploration})
            review_repair = business.review_repair or _review_repair_contract(
                context.project_root,
                business=business,
            )
            business = business.model_copy(update={"review_repair": review_repair})
            return prepare_outcome(
                skill_path=(CASE_DESIGN_REPAIR_SKILL if review_repair is not None else CASE_DESIGN_SKILL),
                persona_path=CASE_DESIGN_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=CASE_DESIGN_RESULT_ID,
                context=context,
                allowed_outputs=case_design_outputs(
                    business.change_id,
                    business.case_delta_paths,
                    business.assertion_source_paths,
                ),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)


class CaseReviewPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(CaseReviewInputV1, request.input)
            binding = validate_binding(request.binding_data)
            plan = _authenticate_plan(
                context.project_root,
                change_id=business.change_id,
                plan_digest=business.plan_digest,
                plan_ref=business.plan_ref,
            )
            _require_verified_sidecars(
                plan,
                case_paths=business.case_delta_paths,
                source_paths=business.assertion_source_paths,
            )
            _authenticate_evidence_refs(context.project_root, business.preparation_refs)
            _require_verified_requirement_ref(
                plan,
                change_id=business.change_id,
                refs=business.preparation_refs,
            )
            _authenticate_evidence_refs(context.project_root, business.case_refs)
            if business.case_refs and {item.path for item in business.case_refs} != set(
                business.case_delta_paths
            ):
                raise InputError("case_refs must bind every locked case_delta_path exactly once")
            review_inputs = case_review_inputs(
                business.change_id,
                business.case_delta_paths,
                business.assertion_source_paths,
            )
            for relative in review_inputs:
                _require_regular_project_input(context.project_root, relative)
            _validate_review_verification_inputs(
                context.project_root,
                plan=plan,
                business=business,
            )
            business = CaseReviewInputV1.model_validate(
                {**business.model_dump(mode="json"), "review_input_paths": review_inputs}
            )
            return prepare_outcome(
                skill_path=CASE_REVIEW_SKILL,
                persona_path=CASE_REVIEW_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=CASE_REVIEW_RESULT_ID,
                context=context,
                allowed_outputs=case_review_outputs(business.change_id),
            )
        except InputError as error:
            return failed_input(error)
