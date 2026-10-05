"""Publish one executable cycle from the committed selected-family results."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.artifacts import stage_json_artifact
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from agent_runtime_contracts.qa_paths import qa_join

from assurance_generation.contracts.codegen import CodegenAuthoringV1, durable_test_path
from assurance_generation.contracts.decisions import complete_generation
from assurance_generation.contracts.families import GENERATION_FAMILIES, LayerName
from assurance_generation.contracts.generated_files import GeneratedFileEntryV1
from assurance_generation.contracts.mapping import ClosedMappingEntryV1, ClosedMappingV1
from assurance_generation.contracts.reviews import ObligationSemanticReviewV1
from assurance_generation.contracts.workflow import (
    CompleteGenerationInputV1,
    GeneratedFamilyV1,
    GENERATION_CYCLE_PATH,
    GenerationCyclePublishedV1,
    GenerationCycleResultV1,
    PublishCycleInputV1,
)
from assurance_generation.operations.planning import evidence_ref
from assurance_generation.operations.resolve_inputs import authenticate_reviewed_case, open_reviewed_case
from assurance_generation.operations.selected_cases import load_selected_cases
from assurance_intake.contracts.explore import PreparedExploreV1
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.domain.obligations import normalize_obligation_drafts
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def complete_generation_cycle(
    request: CompleteGenerationInputV1, project_root: Path, write_root: Path
) -> GenerationCycleResultV1:
    reviewed = authenticate_reviewed_case(
        request.reviewed_case,
        project_root,
        change_id=request.change_id,
        coverage_epoch=request.coverage_epoch,
    )
    families = [item.family for item in request.families]
    if len(families) != len(set(families)) or set(families) != set(request.selected_test_families):
        raise ValueError("generation cycle requires exactly the selected family results")
    leafs = frozenset(request.capability_leafs)
    selected_cases = load_selected_cases(project_root, reviewed)
    cases = {case.case_id: case for case in selected_cases}
    if len(cases) != len(selected_cases):
        raise ValueError("reviewed Case IDs must be unique")
    sources: dict[str, EvidenceArtifactRefV1] = {}
    plans: dict[str, EvidenceArtifactRefV1] = {}
    entries: list[ClosedMappingEntryV1] = []
    method_plans = {}
    semantic_reviews = {}
    for family in request.families:
        if family.coverage_epoch != request.coverage_epoch or family.mapping.layer != family.family:
            raise ValueError("generation family identity does not match the current cycle")
        for method in family.method_plans:
            if method.mrc_id in method_plans:
                raise ValueError(f"obligation has more than one reviewed method: {method.mrc_id}")
            method_plans[method.mrc_id] = method
        for review in family.semantic_reviews:
            key = (review.mrc_id, review.requirement_id)
            if key in semantic_reviews:
                raise ValueError(f"obligation has more than one semantic review: {review.mrc_id}")
            if review.frozen_plan_digest != request.plan_digest or review.plan_ref != request.plan_ref:
                raise ValueError("semantic review identity does not match the frozen plan")
            semantic_reviews[key] = review
        targets = {item.target_file for item in family.mapping.entries}
        for file in family.files:
            path = durable_test_path(file.repo_path)
            ref = evidence_ref(project_root, path)
            if file.content_sha256 != f"sha256:{ref.digest}":
                raise ValueError(f"committed generated source changed: {path}")
            sources[path] = ref
        test_targets = {file.repo_path for file in family.files if file.role == "test_entry"}
        if test_targets != targets:
            raise ValueError("generated tests must match the reviewed mapping targets")
        for path in family.plan_files:
            if not path.startswith(("qa/results/plans/", "qa/results/codegen/")):
                raise ValueError("plan artifacts must belong to the current change")
            plans[path] = evidence_ref(project_root, path)
        for item in family.mapping.entries:
            case = cases.get(item.case_id)
            if case is None or case.type.lower() != family.family:
                raise ValueError("mapping must reference a reviewed Case in the selected family")
            capabilities = sorted(key for key in case.trace if key in leafs)
            if not capabilities:
                raise ValueError("mapped Case must have a declared capability")
            entries.append(
                ClosedMappingEntryV1(
                    test=f"{item.target_file}::{item.symbol.replace('.', '::')}",
                    case_id=item.case_id,
                    capability=capabilities[0],
                    layer=family.family,
                )
            )
    entries.sort(key=lambda entry: (entry.layer, entry.test, entry.case_id))
    mapping = ClosedMappingV1(selected=tuple(entry.test for entry in entries), mappings=tuple(entries))
    data = canonical_json_bytes(mapping.model_dump(mode="json")) + b"\n"
    relative = qa_join(f"generation/epochs/{request.coverage_epoch}/mapping.json")
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    expected_review_keys = {(method.mrc_id, method.requirement_id) for method in method_plans.values()}
    if set(semantic_reviews) != expected_review_keys:
        raise ValueError("semantic reviews must exactly cover the frozen method plans")
    plan = decode_plan((project_root / request.plan_ref.path).read_bytes(), request.plan_ref)
    obligations_bytes = (project_root / plan.quality_goal.obligations_ref.path).read_bytes()
    if hashlib.sha256(obligations_bytes).hexdigest() != plan.quality_goal.obligations_ref.digest:
        raise ValueError("frozen obligation digest changed")
    exploration = load_exploration_document(obligations_bytes)
    obligations = (
        exploration.minimum_required_coverage
        if isinstance(exploration, PreparedExploreV1)
        else normalize_obligation_drafts(exploration.minimum_required_coverage, resolved_quotes={})
    )
    requirements = {
        (obligation.mrc_id, requirement.requirement_id): requirement
        for obligation in obligations
        for requirement in obligation.verification_requirements
    }
    selected_requirements = []
    for method in method_plans.values():
        requirement = requirements.get((method.mrc_id, method.requirement_id))
        if requirement is None:
            raise ValueError(f"method plan references an unknown requirement: {method.mrc_id}")
        selected_requirements.append(requirement)
    method_document = {
        "schema_version": "1",
        "plan_digest": request.plan_digest,
        "plan_ref": request.plan_ref.model_dump(mode="json"),
        "requirements": [
            item.model_dump(mode="json")
            for item in sorted(selected_requirements, key=lambda item: item.requirement_id)
        ],
        "method_plans": [
            item.model_dump(mode="json")
            for item in sorted(method_plans.values(), key=lambda item: item.mrc_id)
        ],
        "semantic_reviews": [
            item.model_dump(mode="json")
            for item in sorted(semantic_reviews.values(), key=lambda item: (item.mrc_id, item.requirement_id))
        ],
    }
    method_data = canonical_json_bytes(cast(JSONValue, method_document)) + b"\n"
    method_relative = qa_join(f"generation/epochs/{request.coverage_epoch}/obligation-methods.json")
    method_path = write_root / method_relative
    method_path.parent.mkdir(parents=True, exist_ok=True)
    method_path.write_bytes(method_data)
    return GenerationCycleResultV1(
        change_id=request.change_id,
        coverage_epoch=request.coverage_epoch,
        reviewed_case=reviewed,
        plan_digest=request.plan_digest,
        plan_ref=request.plan_ref,
        mapping_ref=EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest()),
        source_refs=tuple(sources[key] for key in sorted(sources)),
        plan_refs=tuple(plans[key] for key in sorted(plans)),
        method_plan_ref=EvidenceArtifactRefV1(
            path=method_relative,
            digest=hashlib.sha256(method_data).hexdigest(),
        ),
    )


def _read_authenticated(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    path = root.joinpath(*PurePosixPath(ref.path).parts)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ValueError(f"generation artifact is missing: {ref.path}") from error
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.digest:
        raise ValueError(f"generation artifact digest changed: {ref.path}")
    return data


def _family_result(payload: PublishCycleInputV1, family: LayerName, root: Path) -> GeneratedFamilyV1:
    files_ref = getattr(payload, f"{family}_files")
    summary_ref = getattr(payload, f"{family}_summary")
    review_ref = getattr(payload, f"{family}_review")
    if files_ref is None or summary_ref is None:
        raise ValueError(f"selected family {family} is missing codegen artifacts")
    authored = CodegenAuthoringV1.model_validate(json.loads(_read_authenticated(root, files_ref)))
    if authored.layer != family or authored.change_id != payload.change_id:
        raise ValueError(f"codegen manifest identity does not match {family}")
    _read_authenticated(root, summary_ref)
    files: list[GeneratedFileEntryV1] = []
    for entry in authored.files:
        data = root.joinpath(*PurePosixPath(entry.repo_path).parts).read_bytes()
        files.append(
            GeneratedFileEntryV1(
                repo_path=entry.repo_path,
                disposition=entry.disposition,
                role=entry.role,
                case_ids=list(entry.case_ids),
                content_sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
            )
        )
    reviews: tuple[ObligationSemanticReviewV1, ...] = ()
    if review_ref is not None:
        document = json.loads(_read_authenticated(root, review_ref))
        raw_reviews = document.get("semantic_reviews") if isinstance(document, dict) else None
        reviews = tuple(ObligationSemanticReviewV1.model_validate(item) for item in raw_reviews or ())
    return GeneratedFamilyV1(
        family=family,
        coverage_epoch=payload.coverage_epoch,
        plan_files=tuple(sorted((summary_ref.path, files_ref.path))),
        files=tuple(sorted(files, key=lambda item: item.repo_path)),
        mapping=authored.mapping,
        receipt=ReceiptRef(receipt_id=f"codegen-{family}", receipt_digest=files_ref.digest),
        method_plans=authored.method_plans,
        semantic_reviews=reviews,
    )


class PublishGenerationCycleHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = PublishCycleInputV1.model_validate(request.input)
            reviewed = open_reviewed_case(context.project_root, payload.reviewed_case_ref, ValueError)
            complete_generation(
                {
                    "completed": [{"value": True}] * len(GENERATION_FAMILIES),
                    "selected_families": list(payload.selected_test_families),
                }
            )
            cycle = CompleteGenerationInputV1(
                change_id=payload.change_id,
                coverage_epoch=payload.coverage_epoch,
                reviewed_case=reviewed,
                plan_digest=payload.plan_digest,
                plan_ref=payload.plan_ref,
                selected_test_families=payload.selected_test_families,
                capability_leafs=payload.capability_leafs,
                families=tuple(
                    _family_result(payload, family, context.project_root)
                    for family in payload.selected_test_families
                ),
            )
            result = complete_generation_cycle(cycle, context.project_root, context.write_root)
            stage_json_artifact(context.write_root, GENERATION_CYCLE_PATH, result)
            published = GenerationCyclePublishedV1(generation_result=result)
            return TaskOutcome.succeeded(published.model_dump(mode="json"))
        except (ValueError, ValidationError, OSError, yaml.YAMLError, json.JSONDecodeError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=True)
