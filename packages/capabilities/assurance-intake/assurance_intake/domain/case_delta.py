"""Case-delta evidence binding and output validation shared by case design and case repair."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import TypeVar, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agent_runtime_contracts.ops import ArtifactListResultV1, InputError, OutputError
from graph_engine.frozen_json import FrozenJSONValue, thaw_json

from assurance_intake.contracts.agent import SkillInputV1, canonical_relative_paths
from assurance_intake.contracts.cases import (
    CaseYamlAuthoring,
    MinimumCoverageMatrixAuthoring,
    QaYaml,
    require_impact_row_coverage,
)
from assurance_intake.contracts.common import SHA256_PATTERN, TestFamily, validate_family_tuple
from assurance_intake.contracts.explore import EXPLORATION_PATH, ExploreAdvisoryV1, PreparedExploreV1
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.artifacts import leafs
from assurance_intake.domain.case_checks import (
    authenticated_journey_keys,
    bound_obligations,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    reject_endpoint_literals,
    require_frozen_unresolved_rows,
    require_selected_test_families,
)
from assurance_intake.domain.case_modules import infer_case_delta_paths

MARKER_PATH = "qa/.qa.yaml"
PROPOSAL_PATH = "qa/proposal.md"
MATRIX_PATH = "qa/results/trace/minimum-coverage-matrix.json"
_CASE_TREE = "qa/cases"
_NAMED_CASE_REFS = ("marker_ref", "proposal_ref", "matrix_ref")


def _ref_mappings(value: object) -> list[dict[str, object]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    found: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, EvidenceArtifactRefV1):
            found.append(item.model_dump(mode="json"))
        elif isinstance(item, Mapping) and isinstance(item.get("path"), str):
            found.append(dict(item))
    return found


def _one_ref(value: object) -> dict[str, object] | None:
    if isinstance(value, EvidenceArtifactRefV1):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping) and isinstance(value.get("path"), str):
        return dict(value)
    return None


def _is_case_path(path: str) -> bool:
    return path == _CASE_TREE or path.startswith(f"{_CASE_TREE}/")


def fallback_preparation_refs(data: object) -> object:
    """Empty preparation refs fall back to the caller artifacts."""
    if not isinstance(data, dict) or _ref_mappings(data.get("preparation_refs")):
        return data
    artifacts = _ref_mappings(data.get("artifacts"))
    if artifacts:
        return {**data, "preparation_refs": artifacts}
    return data


def refresh_case_attempt_input(data: object) -> object:
    """Derive locked case paths and preparation refs from the ledger-backed handles."""
    if not isinstance(data, dict):
        return data
    updated: dict[str, object] = dict(cast(Mapping[str, object], fallback_preparation_refs(data)))
    case_refs = _ref_mappings(updated.get("case_refs"))
    if case_refs:
        updated["case_delta_paths"] = sorted(str(item["path"]) for item in case_refs)
    extras = [ref for key in _NAMED_CASE_REFS if (ref := _one_ref(updated.get(key))) is not None]
    if not extras:
        return updated
    found: dict[str, dict[str, object]] = {}
    for ref in (*_ref_mappings(updated.get("preparation_refs")), *extras):
        path = str(ref["path"])
        if _is_case_path(path):
            continue
        found[path] = ref
    updated["preparation_refs"] = [found[path] for path in sorted(found)]
    return updated


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


def bind_case_delta_evidence(
    business: CaseDeltaT,
    plan: ResolvedAssurancePlan,
    inventory: ChangeImpactInventoryV1,
    exploration: ExploreAdvisoryV1 | PreparedExploreV1 | None,
    ui_exploration: BaseModel | None,
    api_discovery: BaseModel | None,
) -> CaseDeltaT:
    """Apply case-design decisions to centrally authenticated evidence."""
    if business.selected_test_families != plan.selected_test_families:
        raise InputError("case selected families do not match frozen assurance plan")
    if inventory.change_id != business.change_id:
        raise InputError("impact-inventory.json change_id does not match case-design change_id")
    if exploration is not None:
        if exploration.change_id != business.change_id:
            raise InputError("exploration.json change_id does not match case-design change_id")
        if exploration.context_ref != "explore/context.json":
            raise InputError("exploration.json context_ref must be explore/context.json")
    updates: dict[str, object] = {"exploration": exploration, "impact_inventory": inventory}
    if ui_exploration is not None:
        updates["ui_exploration"] = ui_exploration.model_dump(mode="json")
    if api_discovery is not None:
        updates["api_discovery"] = api_discovery.model_dump(mode="json")
    business = business.model_copy(update=updates)
    try:
        inferred = infer_case_delta_paths(inventory)
    except ValueError:
        inferred = ()
    if inferred:
        business = business.model_copy(update={"case_delta_paths": inferred})
    elif not business.case_delta_paths:
        raise InputError("impact inventory does not imply any case module")
    return business


def validate_case_delta(
    *,
    business: CaseDeltaInputV1,
    plan: ResolvedAssurancePlan,
    inventory: ChangeImpactInventoryV1,
    exploration: ExploreAdvisoryV1 | PreparedExploreV1 | None,
    catalog: Mapping[str, object],
    knowledge: Mapping[str, object],
    receipt: ArtifactListResultV1,
    captured: Mapping[str, object],
) -> None:
    """Check the change marker, authored delta, surface, and coverage matrix the run produced."""
    qa = captured[MARKER_PATH]
    assert isinstance(qa, QaYaml)
    if qa.change.change_id != business.change_id:
        raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
    documents = {
        path: document
        for path, document in captured.items()
        if path in business.case_delta_paths and isinstance(document, CaseYamlAuthoring)
    }
    authored = load_authored_case_delta(
        locked=business.artifact_paths,
        declared=receipt.output_files,
        capability_leafs=leafs(business.capability_leafs),
        documents=documents,
        inventory=inventory,
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
    obligations = bound_obligations(exploration, catalog, knowledge)
    if authored.added or authored.modified:
        journey_keys = authenticated_journey_keys(knowledge)
        try:
            matrix = captured[MATRIX_PATH]
            assert isinstance(matrix, MinimumCoverageMatrixAuthoring)
            matrix = load_minimum_coverage_matrix(
                document=matrix,
                authored=authored,
                selected=plan.selected_test_families,
                journey_keys=journey_keys,
            )
            require_frozen_unresolved_rows(obligations, matrix)
        except OutputError as error:
            validation_errors.append(str(error))
    else:
        matrix = captured[MATRIX_PATH]
        assert isinstance(matrix, MinimumCoverageMatrixAuthoring)
        require_frozen_unresolved_rows(obligations, matrix)
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
    "validate_case_delta",
]
