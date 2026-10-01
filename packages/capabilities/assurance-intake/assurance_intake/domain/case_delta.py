"""Case-delta evidence binding and output validation shared by case design and case repair."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TypeVar

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from agent_runtime_contracts.ops import ArtifactListResultV1, InputError, OutputError
from graph_engine.frozen_json import FrozenJSONValue, thaw_json

from assurance_intake.contracts.agent import SkillInputV1, canonical_relative_paths
from assurance_intake.contracts.cases import QaYaml, require_impact_row_coverage
from assurance_intake.contracts.common import SHA256_PATTERN, TestFamily, validate_family_tuple
from assurance_intake.contracts.explore import EXPLORATION_PATH, ExploreAdvisoryV1, PreparedExploreV1
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.artifacts import (
    file_digest,
    leafs,
    read_regular_bytes,
    workspace_file,
)
from assurance_intake.domain.case_checks import (
    authenticated_journey_keys,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    read_minimum_coverage_matrix,
    reject_endpoint_literals,
    require_frozen_unresolved_rows,
    require_selected_test_families,
)
from assurance_intake.domain.case_modules import infer_case_delta_paths
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.domain.prepare_evidence import authenticate_evidence_refs

MARKER_PATH = "qa/.qa.yaml"
PROPOSAL_PATH = "qa/proposal.md"
MATRIX_PATH = "qa/results/trace/minimum-coverage-matrix.json"


def validate_case_delta_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[:2] != ("qa", "cases") or parts[-1] != "case.yaml":
            raise ValueError("case_delta_paths must be exact current-change cases/<module>/case.yaml paths")
    return paths


def case_delta_outputs(case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted((*case_delta_paths, MARKER_PATH, PROPOSAL_PATH, MATRIX_PATH)))


class CaseDeltaInputV1(SkillInputV1):
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    plan_ref: EvidenceArtifactRefV1
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    selected_test_families: tuple[TestFamily, ...] = ()
    case_delta_paths: tuple[str, ...] = ()
    exploration: PreparedExploreV1 | ExploreAdvisoryV1 | None = None
    impact_inventory: ChangeImpactInventoryV1 | None = None
    ui_exploration_ref: EvidenceArtifactRefV1 | None = None
    api_discovery_ref: EvidenceArtifactRefV1 | None = None
    ui_exploration: FrozenJSONValue | None = None
    api_discovery: FrozenJSONValue | None = None

    @field_validator("selected_test_families")
    @classmethod
    def _selected_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return validate_family_tuple(value)

    @field_validator("case_delta_paths")
    @classmethod
    def _case_delta_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = canonical_relative_paths(value)
        if canonical != value:
            raise ValueError("case_delta_paths must be sorted and unique")
        return canonical

    @model_validator(mode="after")
    def _case_delta_paths_match_change(self) -> CaseDeltaInputV1:
        validate_case_delta_paths(self.case_delta_paths)
        return self


CaseDeltaT = TypeVar("CaseDeltaT", bound=CaseDeltaInputV1)


def _committed_inventory(
    project_root: Path, plan: ResolvedAssurancePlan, change_id: str
) -> ChangeImpactInventoryV1:
    authenticate_evidence_refs(project_root, (plan.impact_inventory_ref,))
    try:
        inventory = ChangeImpactInventoryV1.model_validate_json(
            project_root.joinpath(*plan.impact_inventory_ref.path.split("/")).read_bytes()
        )
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid impact-inventory.json: {error}") from error
    if inventory.change_id != change_id:
        raise InputError("impact-inventory.json change_id does not match case-design change_id")
    return inventory


def _committed_exploration(project_root: Path, change_id: str) -> object | None:
    path = project_root.joinpath(*EXPLORATION_PATH.split("/"))
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise InputError("exploration.json must be a regular file")
    try:
        exploration = load_exploration_document(path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid exploration.json: {error}") from error
    if exploration.change_id != change_id:
        raise InputError("exploration.json change_id does not match case-design change_id")
    if exploration.context_ref != "explore/context.json":
        raise InputError("exploration.json context_ref must be explore/context.json")
    return exploration


def _surface_documents(project_root: Path, business: CaseDeltaInputV1) -> dict[str, object]:
    # Lazy: quality.contracts.surface must not load at intake import time
    # (generation → intake → quality → execution → generation cycle).
    from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument

    updates: dict[str, object] = {}
    if business.ui_exploration_ref is not None:
        authenticate_evidence_refs(project_root, (business.ui_exploration_ref,))
        try:
            ui_exploration = UiExplorationDocument.model_validate_json(
                project_root.joinpath(*business.ui_exploration_ref.path.split("/")).read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid ui-exploration.json: {error}") from error
        if ui_exploration.change_id != business.change_id:
            raise InputError("ui-exploration.json change_id does not match case-design change_id")
        updates["ui_exploration"] = ui_exploration.model_dump(mode="json")
    if business.api_discovery_ref is not None:
        authenticate_evidence_refs(project_root, (business.api_discovery_ref,))
        try:
            api_discovery = ApiDiscoveryDocument.model_validate_json(
                project_root.joinpath(*business.api_discovery_ref.path.split("/")).read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid api-discovery.json: {error}") from error
        if api_discovery.change_id != business.change_id:
            raise InputError("api-discovery.json change_id does not match case-design change_id")
        updates["api_discovery"] = api_discovery.model_dump(mode="json")
    return updates


def bind_case_delta_evidence(
    project_root: Path, business: CaseDeltaT, plan: ResolvedAssurancePlan
) -> CaseDeltaT:
    """Authenticate committed evidence and attach it to the business input before the run."""
    if business.selected_test_families != plan.selected_test_families:
        raise InputError("case selected families do not match frozen assurance plan")
    authenticate_evidence_refs(project_root, business.preparation_refs)
    inventory = _committed_inventory(project_root, plan, business.change_id)
    exploration = _committed_exploration(project_root, business.change_id)
    business = business.model_copy(update={"exploration": exploration, "impact_inventory": inventory})
    surface = _surface_documents(project_root, business)
    if surface:
        business = business.model_copy(update=surface)
    try:
        inferred = infer_case_delta_paths(inventory)
    except ValueError:
        inferred = ()
    if inferred:
        business = business.model_copy(update={"case_delta_paths": inferred})
    elif not business.case_delta_paths:
        raise InputError("impact inventory does not imply any case module")
    return business


def finalize_plan(project_root: Path, business: CaseDeltaInputV1) -> ResolvedAssurancePlan:
    try:
        plan_path = workspace_file(project_root, business.plan_ref.path)
        plan = decode_plan(plan_path.read_bytes(), business.plan_ref)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.change_id != business.change_id or plan.plan_digest != business.plan_digest:
        raise InputError("frozen assurance plan does not match case input")
    if plan.selected_test_families != business.selected_test_families:
        raise InputError("case selected families do not match frozen assurance plan")
    return plan


def finalize_inventory(
    project_root: Path, plan: ResolvedAssurancePlan, change_id: str
) -> ChangeImpactInventoryV1:
    try:
        inventory_path = workspace_file(project_root, plan.impact_inventory_ref.path)
        inventory_bytes = inventory_path.read_bytes()
        if hashlib.sha256(inventory_bytes).hexdigest() != plan.impact_inventory_ref.digest:
            raise InputError("impact inventory digest changed after it was committed")
        inventory = ChangeImpactInventoryV1.model_validate_json(inventory_bytes)
    except InputError:
        raise
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid impact-inventory.json: {error}") from error
    if inventory.change_id != change_id:
        raise InputError("impact inventory does not belong to the case-design change")
    return inventory


def require_receipt_paths(
    receipt: ArtifactListResultV1,
    business: CaseDeltaInputV1,
    inventory: ChangeImpactInventoryV1,
) -> None:
    for relative in receipt.output_files:
        if not relative.startswith("qa/"):
            raise OutputError("case-design receipt may contain only current change outputs")
    missing = sorted({MARKER_PATH, PROPOSAL_PATH, MATRIX_PATH}.difference(receipt.output_files))
    if missing:
        raise OutputError("case-design receipt is missing required output files: " + ", ".join(missing))
    try:
        inferred = infer_case_delta_paths(inventory)
    except ValueError:
        inferred = ()
    if inferred:
        expected_cases = set(inferred)
    elif business.case_delta_paths:
        expected_cases = set(business.case_delta_paths)
    else:
        raise InputError("impact inventory does not imply any case module")
    declared_cases = {
        relative
        for relative in receipt.output_files
        if relative.startswith("qa/cases/") and relative.endswith("/case.yaml")
    }
    if declared_cases != expected_cases:
        missing_cases = sorted(expected_cases - declared_cases)
        unexpected_cases = sorted(declared_cases - expected_cases)
        raise OutputError(
            "case-design receipt case paths do not match locked case_delta_paths; "
            f"missing={missing_cases}, unexpected={unexpected_cases}"
        )


def validate_case_delta(
    *,
    project_root: Path,
    write_root: Path,
    business: CaseDeltaInputV1,
    plan: ResolvedAssurancePlan,
    inventory: ChangeImpactInventoryV1,
    receipt: ArtifactListResultV1,
    artifacts: list[dict[str, str]],
    images: Mapping[str, bytes] | None,
) -> None:
    """Check the change marker, authored delta, surface, and coverage matrix the run produced."""
    try:
        qa_bytes = (
            images[MARKER_PATH]
            if images is not None
            else read_regular_bytes(write_root, MARKER_PATH, kind="change document")
        )
        qa_digest = next(item["digest"] for item in artifacts if item["path"] == MARKER_PATH)
        if file_digest(qa_bytes) != qa_digest:
            raise OutputError("qa/.qa.yaml changed during finalization")
        qa = QaYaml.model_validate(yaml.safe_load(qa_bytes))
    except (yaml.YAMLError, UnicodeError, ValidationError) as error:
        raise OutputError(f"invalid {MARKER_PATH}: {error}") from error
    if qa.change.change_id != business.change_id:
        raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
    authored = load_authored_case_delta(
        write_root,
        change_id=business.change_id,
        locked=business.artifact_paths,
        declared=receipt.output_files,
        capability_leafs=leafs(business.capability_leafs),
        inventory=inventory,
        images=images,
    )
    try:
        require_impact_row_coverage(authored, inventory)
    except ValueError as error:
        raise OutputError(str(error)) from error
    validation_errors: list[str] = []
    try:
        reject_endpoint_literals(authored)
    except OutputError as error:
        validation_errors.append(str(error))
    if business.selected_test_families:
        try:
            require_selected_test_families(authored, business.selected_test_families)
        except OutputError as error:
            validation_errors.append(str(error))
    if business.ui_exploration is not None and business.api_discovery is not None:
        # Lazy: avoid generation↔quality import cycle at module load.
        from assurance_intake.domain.surface_guard import SurfaceMismatch, assert_cases_match_surface
        from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument

        try:
            api_document = ApiDiscoveryDocument.model_validate(thaw_json(business.api_discovery))
            ui_document = UiExplorationDocument.model_validate(thaw_json(business.ui_exploration))
        except ValidationError as error:
            raise OutputError(f"invalid surface document on case finalize: {error}") from error
        try:
            assert_cases_match_surface(
                authored,
                api_document,
                ui_document,
                set(business.selected_test_families),
            )
        except SurfaceMismatch as error:
            validation_errors.append(str(error))
    if authored.added or authored.modified:
        journey_keys = authenticated_journey_keys(
            project_root,
            plan.quality_goal.source_resource_digests,
        )
        try:
            matrix = load_minimum_coverage_matrix(
                write_root,
                relative=MATRIX_PATH,
                authored=authored,
                selected=plan.selected_test_families,
                journey_keys=journey_keys,
                images=images,
            )
            require_frozen_unresolved_rows(project_root, plan, matrix)
        except OutputError as error:
            validation_errors.append(str(error))
    else:
        matrix = read_minimum_coverage_matrix(write_root, relative=MATRIX_PATH, images=images)
        require_frozen_unresolved_rows(project_root, plan, matrix)
    if validation_errors:
        raise OutputError("; ".join(validation_errors))


__all__ = [
    "EXPLORATION_PATH",
    "MARKER_PATH",
    "MATRIX_PATH",
    "PROPOSAL_PATH",
    "CaseDeltaInputV1",
    "bind_case_delta_evidence",
    "case_delta_outputs",
    "finalize_inventory",
    "finalize_plan",
    "require_receipt_paths",
    "validate_case_delta",
]
