"""Publish one executable cycle from the committed selected-family results."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from agent_runtime_contracts.qa_paths import qa_join

from assurance_generation.contracts.codegen import durable_test_path
from assurance_generation.contracts.mapping import ClosedMappingEntryV1, ClosedMappingV1
from assurance_generation.contracts.workflow import CompleteGenerationInputV1, GenerationCycleResultV1
from assurance_generation.operations.planning import evidence_ref
from assurance_generation.operations.resolve_inputs import authenticate_reviewed_case
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


class PublishGenerationCycleHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = CompleteGenerationInputV1.model_validate(request.input)
            result = complete_generation_cycle(payload, context.project_root, context.write_root)
            return TaskOutcome.succeeded(result.model_dump(mode="json"))
        except (ValueError, ValidationError, OSError, yaml.YAMLError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=True)
