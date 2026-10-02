"""Checks on authored case deltas and the minimum coverage matrix."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    InputError,
    OutputError,
)

from assurance_intake.contracts import (
    CaseReviewResultV1,
    CaseYamlAuthoring,
    MinimumCoverageMatrixAuthoring,
)
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.obligations import PreparedObligationV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import (
    normalized_auto_fix_edits,
)
from assurance_intake.domain.artifacts import allowed_by_lock, file_digest, read_regular_bytes
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.obligations import (
    journey_keys_from_document,
    normalize_goal_obligations,
)

_DATA_KNOWLEDGE_RESOURCE_ID = "assurance.product.configuration.data-knowledge"


_DATA_KNOWLEDGE_PATH = ".aa/data-knowledge.yaml"


_CAPABILITY_CATALOG_RESOURCE_ID = "assurance.product.configuration.capability-catalog"


_CAPABILITY_CATALOG_PATH = ".aa/capability-catalog.json"


def _authenticated_capability_leafs(
    workspace: Path,
    source_resource_digests: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    expected_digest = dict(source_resource_digests).get(_CAPABILITY_CATALOG_RESOURCE_ID)
    if expected_digest is None:
        raise InputError("frozen assurance plan does not bind the capability catalog")
    try:
        data = read_regular_bytes(workspace, _CAPABILITY_CATALOG_PATH, kind="capability catalog")
    except OutputError as error:
        raise InputError(str(error)) from error
    if file_digest(data) != expected_digest:
        raise InputError("capability catalog does not match the frozen assurance plan")
    try:
        document = json.loads(data)
        raw = document.get("typed_leafs") if isinstance(document, Mapping) else None
        if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
            raise ValueError("typed_leafs must be a list of non-empty strings")
        leafs = tuple(raw)
        if leafs != tuple(sorted(set(leafs))):
            raise ValueError("typed_leafs must be sorted and unique")
        return leafs
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise InputError(f"invalid capability catalog: {error}") from error


def authenticated_journey_keys(
    workspace: Path,
    source_resource_digests: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    expected_digest = dict(source_resource_digests).get(_DATA_KNOWLEDGE_RESOURCE_ID)
    if expected_digest is None:
        raise InputError("frozen assurance plan does not bind data knowledge")
    try:
        data = read_regular_bytes(workspace, _DATA_KNOWLEDGE_PATH, kind="data knowledge")
    except OutputError as error:
        raise InputError(str(error)) from error
    if file_digest(data) != expected_digest:
        raise InputError("data knowledge does not match the frozen assurance plan")
    try:
        document = yaml.safe_load(data)
        if not isinstance(document, Mapping):
            raise ValueError("data knowledge must be a mapping")
        return journey_keys_from_document(document)
    except (yaml.YAMLError, ValueError) as error:
        raise InputError(f"invalid data knowledge: {error}") from error


def require_selected_test_families(
    document: CaseYamlAuthoring,
    selected: tuple[str, ...],
) -> None:
    required_cases = tuple(
        entry
        for entry in (*document.added, *document.modified)
        if entry.status == "active" and entry.automation.required
    )
    conflicts = [
        f"{entry.case_id} ({entry.type.lower()})"
        for entry in required_cases
        if entry.type.lower() not in selected
    ]
    if conflicts:
        raise OutputError(
            "family scope conflict: required automated cases need unselected test families: "
            + ", ".join(conflicts)
        )
    authored = {entry.type.lower() for entry in required_cases}
    missing = [family for family in selected if family not in authored]
    if missing:
        inactive = [
            f"{entry.case_id} ({entry.status})"
            for entry in (*document.added, *document.modified)
            if entry.type.lower() in missing and entry.automation.required and entry.status != "active"
        ]
        hint = "; each needs a case of that type with status: active and automation.required: true"
        if inactive:
            hint += "; only status: active counts: " + ", ".join(inactive[:12])
        raise OutputError(
            "case design is missing required automated cases for selected test families: "
            + ", ".join(missing)
            + hint
        )


def load_authored_case_delta(
    workspace: Path,
    *,
    change_id: str,
    locked: tuple[str, ...],
    declared: tuple[str, ...],
    capability_leafs: frozenset[str],
    inventory: ChangeImpactInventoryV1 | None = None,
    images: Mapping[str, bytes] | None = None,
) -> CaseYamlAuthoring:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    root_relative = "qa/cases"
    declared_cases = sorted(
        relative
        for relative in declared
        if relative.startswith(f"{root_relative}/") and relative.endswith("/case.yaml")
    )
    if not declared_cases:
        return CaseYamlAuthoring.model_construct(
            schema_version="1.0",
            added=[],
            modified=[],
            removed=[],
        )
    relative_files = declared_cases

    schema_version: str | None = None
    aggregate: dict[str, object] = {"added": [], "modified": [], "removed": []}
    for relative in relative_files:
        if not allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        try:
            data = (
                images[relative]
                if images is not None
                else read_regular_bytes(workspace, relative, kind="declared output file")
            )
            raw = yaml.safe_load(data)
            document = CaseYamlAuthoring.model_validate(
                raw,
                context={"capability_leafs": capability_leafs, "inventory": inventory},
            )
        except (OSError, yaml.YAMLError, ValidationError, TypeError, ValueError) as error:
            raise OutputError(f"invalid written case.yaml {relative}: {error}") from error
        if schema_version is None:
            schema_version = document.schema_version
        elif document.schema_version != schema_version:
            raise OutputError("written case.yaml files use inconsistent schema_version values")
        for section in ("added", "modified", "removed"):
            cast(list[object], aggregate[section]).extend(document.model_dump(mode="json")[section])

    aggregate["schema_version"] = schema_version
    try:
        return CaseYamlAuthoring.model_validate(
            aggregate,
            context={"capability_leafs": capability_leafs},
        )
    except ValidationError as error:
        raise OutputError(f"invalid aggregate written case delta: {error}") from error


_ENDPOINT_LITERAL = re.compile(r"(?<![A-Za-z])(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S*")


_NATURAL_LANGUAGE_FIELDS = (
    "title",
    "objective",
    "summary",
    "preconditions",
    "test_data",
    "steps",
    "assertions",
    "postconditions",
    "edge_cases",
)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def reject_endpoint_literals(authored: CaseYamlAuthoring) -> None:
    """HTTP method + path belongs in the API plan, except schema-owned Fuzz/Performance fields."""

    hits: list[str] = []
    for entry in (*authored.added, *authored.modified):
        for field in _NATURAL_LANGUAGE_FIELDS:
            for text in _strings(getattr(entry, field, None)):
                for match in _ENDPOINT_LITERAL.finditer(text):
                    hits.append(f"{entry.case_id}.{field}: {match.group(0)!r}")
    if hits:
        shown = "; ".join(hits[:12])
        more = f"; and {len(hits) - 12} more" if len(hits) > 12 else ""
        raise OutputError(
            "case.yaml natural-language fields must not contain an HTTP method + endpoint path; "
            "describe the operation in words (for example 调用用户创建接口) and leave method/path "
            "to the API plan; only automation.fuzz and automation.performance may name endpoints: "
            f"{shown}{more}"
        )


def load_minimum_coverage_matrix(
    workspace: Path,
    *,
    relative: str,
    authored: CaseYamlAuthoring,
    selected: tuple[str, ...],
    journey_keys: tuple[str, ...],
    images: Mapping[str, bytes] | None = None,
) -> MinimumCoverageMatrixAuthoring:
    document = read_minimum_coverage_matrix(
        workspace,
        relative=relative,
        images=images,
    )

    cases = {entry.case_id: entry for entry in (*authored.added, *authored.modified)}
    category_layer = {
        "api": "api",
        "negative": "api",
        "data_integrity": "api",
        "e2e": "e2e",
        "e2e_if_enabled": "e2e",
    }
    journey_cases: set[str] = set()
    for row in document.root:
        expected_layer = (
            row.layer
            or (category_layer.get(row.category) if row.category else None)
            or ("e2e" if row.key in journey_keys else None)
        )
        required_families = (
            {"api", "e2e"}
            if expected_layer == "both"
            else {expected_layer}
            if expected_layer is not None
            else set()
        )
        for case_id in row.covered_by_cases:
            case = cases.get(case_id)
            if case is None:
                raise OutputError(
                    f"minimum coverage row {row.mrc_id} references unknown authored case: {case_id}"
                )
            if expected_layer in {"api", "e2e"} and case.type.lower() != expected_layer:
                raise OutputError(
                    f"minimum coverage row {row.mrc_id} requires {expected_layer} case coverage"
                )
            required_families.add(case.type.lower())
        if expected_layer == "e2e" and row.key in journey_keys:
            journey_cases.update(row.covered_by_cases)
        excluded = sorted(required_families.difference(selected))
        if row.required and excluded:
            raise OutputError(
                f"family scope conflict: required minimum coverage row {row.mrc_id} "
                f"needs unselected test families: {', '.join(excluded)}"
            )
    unmapped_e2e = sorted(
        case.case_id
        for case in cases.values()
        if case.type == "E2E" and case.automation.required and case.case_id not in journey_cases
    )
    if unmapped_e2e:
        raise OutputError(
            f"E2E cases have no valid journey mapping: {unmapped_e2e}; "
            f"authenticated journey keys: {list(journey_keys)}"
        )
    return document


_STATUS_COVERED = re.compile(r"(?<![a-z_])covered(?!_by)")


_MATRIX_MUTABLE_FIELDS = frozenset({"status", "covered_by_cases", "skip_reason"})


def bound_obligations(workspace: Path, plan: ResolvedAssurancePlan) -> tuple[PreparedObligationV1, ...]:
    ref = plan.quality_goal.obligations_ref
    try:
        data = read_regular_bytes(workspace, ref.path, kind="frozen exploration")
    except OutputError as error:
        raise InputError(str(error)) from error
    if file_digest(data) != ref.digest:
        raise InputError("frozen exploration does not match the assurance plan")
    try:
        exploration = load_exploration_document(data)
        return normalize_goal_obligations(
            exploration,
            capability_leafs=frozenset(
                _authenticated_capability_leafs(
                    workspace,
                    plan.quality_goal.source_resource_digests,
                )
            ),
            journey_keys=frozenset(
                authenticated_journey_keys(
                    workspace,
                    plan.quality_goal.source_resource_digests,
                )
            ),
        )
    except (ValueError, ValidationError) as error:
        raise InputError(f"frozen exploration obligations are invalid: {error}") from error


def _instruction_sets_covered(instructions: tuple[str, ...]) -> bool:
    return _STATUS_COVERED.search("\n".join(instructions).lower()) is not None


def review_requests_matrix_coverage(document: CaseReviewResultV1) -> bool:
    if document.public_outcome != "needs_fix":
        return False
    for item in document.auto_fix_plan:
        if not isinstance(item, Mapping):
            continue
        artifact = item.get("artifact")
        if not isinstance(artifact, str) or not artifact.endswith("/trace/minimum-coverage-matrix.json"):
            continue
        try:
            edits = normalized_auto_fix_edits(item)
        except ValueError:
            continue
        if _instruction_sets_covered(edits):
            return True
    return False


def reject_unbound_covered_repairs(
    document: CaseReviewResultV1,
    obligations: tuple[PreparedObligationV1, ...],
) -> None:
    """Refuse a patch that covers a row whose frozen key is still empty."""
    if document.public_outcome != "needs_fix":
        return
    bound = {row.mrc_id: row for row in obligations}
    findings = {finding.id: finding for finding in document.findings}
    for item in document.auto_fix_plan:
        if not isinstance(item, Mapping):
            continue
        finding_id = item.get("finding_id")
        artifact = item.get("artifact")
        if not isinstance(finding_id, str) or finding_id not in findings:
            continue
        if not isinstance(artifact, str) or not artifact.endswith("/trace/minimum-coverage-matrix.json"):
            continue
        try:
            edits = normalized_auto_fix_edits(item)
        except ValueError:
            continue
        if not _instruction_sets_covered(edits):
            continue
        raw_key = findings[finding_id].locator.key or ""
        paths = tuple(part.strip() for part in raw_key.split(",") if part.strip())
        targets = tuple(bound) if paths and set(paths) <= _MATRIX_MUTABLE_FIELDS else paths
        unbound = [mrc_id for mrc_id in targets if mrc_id in bound and bound[mrc_id].key is None]
        if unbound:
            raise OutputError(
                "review repair cannot mark an unbound MRC covered while its frozen key is empty: "
                + ", ".join(unbound)
            )


def require_frozen_unresolved_rows(
    workspace: Path,
    plan: ResolvedAssurancePlan,
    matrix: MinimumCoverageMatrixAuthoring,
) -> None:
    obligations = bound_obligations(workspace, plan)
    unresolved = {row.mrc_id for row in obligations if row.key is None}
    mapped = {row.mrc_id for row in matrix.root if row.key is None}
    missing = sorted(unresolved - mapped)
    if missing:
        raise OutputError(f"unresolved MRC rows missing or rebound: {missing}")
    invented = sorted(mapped - unresolved)
    if invented:
        raise OutputError(f"unresolved MRC rows do not match the frozen plan: {invented}")


def read_minimum_coverage_matrix(
    workspace: Path,
    *,
    relative: str,
    images: Mapping[str, bytes] | None = None,
) -> MinimumCoverageMatrixAuthoring:
    try:
        data = (
            images[relative]
            if images is not None
            else read_regular_bytes(workspace, relative, kind="minimum coverage matrix")
        )
        document = MinimumCoverageMatrixAuthoring.model_validate(json.loads(data))
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise OutputError(f"invalid minimum-coverage-matrix.json: {error}") from error
    return document
