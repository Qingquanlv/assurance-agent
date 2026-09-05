"""Publish one executable cycle from the committed selected-family results."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import ValidationError

from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from assurance_generation.contracts.codegen import staged_generated_path
from assurance_generation.contracts.mapping import ClosedMappingEntryV1, ClosedMappingV1
from assurance_generation.contracts.workflow import CompleteGenerationInputV1, GenerationCycleResultV1
from assurance_generation.operations.planning import evidence_ref
from assurance_generation.operations.resolve_inputs import authenticate_reviewed_case
from assurance_intake.contracts import CaseYamlAuthoring
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
    cases = {}
    for ref in reviewed.case_refs:
        document = CaseYamlAuthoring.model_validate(
            yaml.safe_load((project_root / ref.path).read_bytes()),
            context={"capability_leafs": leafs},
        )
        for case in (*document.added, *document.modified):
            if case.case_id in cases:
                raise ValueError("reviewed Case IDs must be unique")
            cases[case.case_id] = case
    sources: dict[str, EvidenceArtifactRefV1] = {}
    plans: dict[str, EvidenceArtifactRefV1] = {}
    entries: list[ClosedMappingEntryV1] = []
    for family in request.families:
        if family.coverage_epoch != request.coverage_epoch or family.mapping.layer != family.family:
            raise ValueError("generation family identity does not match the current cycle")
        targets = {item.target_file for item in family.mapping.entries}
        for file in family.files:
            path = staged_generated_path(request.change_id, family.family, file.repo_path)
            ref = evidence_ref(project_root, path)
            if file.content_sha256 != f"sha256:{ref.digest}":
                raise ValueError(f"committed generated source changed: {path}")
            sources[path] = ref
        test_targets = {file.repo_path for file in family.files if file.role == "test_entry"}
        if test_targets != targets:
            raise ValueError("generated tests must match the reviewed mapping targets")
        for path in family.plan_files:
            if not path.startswith(f"qa/changes/{request.change_id}/plans/"):
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
    relative = f"qa/changes/{request.change_id}/generation/epochs/{request.coverage_epoch}/mapping.json"
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return GenerationCycleResultV1(
        change_id=request.change_id,
        coverage_epoch=request.coverage_epoch,
        reviewed_case=reviewed,
        plan_digest=request.plan_digest,
        plan_ref=request.plan_ref,
        mapping_ref=EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest()),
        source_refs=tuple(sources[key] for key in sorted(sources)),
        plan_refs=tuple(plans[key] for key in sorted(plans)),
    )


class PublishGenerationCycleHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = CompleteGenerationInputV1.model_validate(request.input)
            result = complete_generation_cycle(payload, context.project_root, context.write_root)
            return TaskOutcome.succeeded(result.model_dump(mode="json"))
        except (ValueError, ValidationError, OSError, yaml.YAMLError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
