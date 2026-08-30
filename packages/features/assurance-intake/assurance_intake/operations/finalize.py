"""Semantic intake finalize handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts import (
    CaseReviewResultV1,
    CaseYamlAuthoring,
    MinimumCoverageMatrixAuthoring,
)
from assurance_intake.contracts.agent import AgentFinalizeInputV1, ArtifactListResultV1
from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.review import CaseMinimumCoverageReview
from assurance_intake.operations.agent_skills import InputError, failed_input, validate_input


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def _review_round_fields(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    used = raw.get("rounds_used")
    budget = raw.get("rounds_budget")
    fields: dict[str, int] = {}
    if isinstance(used, int) and used >= 0:
        fields["rounds_used"] = used
    if isinstance(budget, int) and budget >= 0:
        fields["rounds_budget"] = budget
    return fields


def _finalize_payload(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    cleaned = dict(raw)
    cleaned.pop("rounds_used", None)
    cleaned.pop("rounds_budget", None)
    return cleaned


def _leafs(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


def _require_known_leafs(keys: Iterable[str], leafs: frozenset[str], *, kind: str) -> None:
    for key in keys:
        if key not in leafs:
            raise OutputError(f"{kind} references unknown capability leaf: {key}")


def _file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _workspace_file(workspace: Path, relative: str) -> Path:
    if not _canonical_relative(relative):
        raise OutputError(f"output file path must be canonical and relative: {relative}")
    path = workspace
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise OutputError(f"declared output file is a symlink: {relative}")
    if path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    if path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def _allowed_by_lock(relative: str, locked: tuple[str, ...]) -> bool:
    return any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in locked)


def _authenticate_files(
    workspace: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        if not _allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": _file_digest(path.read_bytes())})
    return artifacts


def _artifact_list(payload: AgentFinalizeInputV1) -> ArtifactListResultV1:
    try:
        return ArtifactListResultV1.model_validate(_structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error


def _finalize_artifact_list(payload: AgentFinalizeInputV1, workspace: Path) -> list[dict[str, str]]:
    if not payload.artifact_paths:
        raise InputError("artifact_paths must lock the expected output files")
    document = _artifact_list(payload)
    return _authenticate_files(workspace, document.output_files, payload.artifact_paths)


def _validate_explore_outputs(workspace: Path, declared: tuple[str, ...]) -> None:
    for relative in declared:
        if not relative.endswith("/explore/exploration.json"):
            continue
        path = _workspace_file(workspace, relative)
        try:
            document = ExploreAdvisoryV1.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as error:
            raise OutputError(f"invalid exploration.json: {error}") from error
        parts = PurePosixPath(relative).parts
        if len(parts) < 5 or parts[:2] != ("qa", "changes"):
            raise OutputError(f"invalid exploration.json path: {relative}")
        if document.change_id != parts[2]:
            raise OutputError("exploration.json change_id does not match its change directory")
        if document.context_ref != "explore/context.json":
            raise OutputError("exploration.json context_ref must be explore/context.json")


def _require_selected_test_families(
    document: CaseYamlAuthoring,
    selected: tuple[str, ...],
) -> None:
    authored = {
        entry.type.lower() for entry in (*document.added, *document.modified) if entry.automation.required
    }
    missing = [family for family in selected if family not in authored]
    if missing:
        raise OutputError(
            "case design is missing required automated cases for selected test families: "
            + ", ".join(missing)
        )


def _case_change_id(value: str | None) -> str:
    if value is None:
        raise InputError("change_id is required for case-design finalize")
    posix = PurePosixPath(value)
    if len(posix.parts) != 1 or not _canonical_relative(value):
        raise InputError("change_id must be a canonical path segment")
    return value


def _load_authored_case_delta(
    workspace: Path,
    *,
    change_id: str,
    locked: tuple[str, ...],
    declared: tuple[str, ...],
    capability_leafs: frozenset[str],
) -> CaseYamlAuthoring:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    root_relative = f"qa/changes/{change_id}/cases"
    declared_cases = sorted(
        relative
        for relative in declared
        if relative.startswith(f"{root_relative}/") and relative.endswith("/case.yaml")
    )
    if not declared_cases:
        return CaseYamlAuthoring.model_construct(
            schema_version="1",
            added=[],
            modified=[],
            removed=[],
        )
    relative_files = declared_cases

    schema_version: str | None = None
    aggregate: dict[str, object] = {"added": [], "modified": [], "removed": []}
    for relative in relative_files:
        if not _allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        path = _workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        try:
            raw = yaml.safe_load(path.read_bytes())
            document = CaseYamlAuthoring.model_validate(
                raw,
                context={"capability_leafs": capability_leafs},
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


def _load_minimum_coverage_matrix(
    workspace: Path,
    *,
    relative: str,
    authored: CaseYamlAuthoring,
) -> MinimumCoverageMatrixAuthoring:
    document = _read_minimum_coverage_matrix(workspace, relative=relative)

    cases = {entry.case_id: entry for entry in (*authored.added, *authored.modified)}
    category_layer = {
        "api": "api",
        "negative": "api",
        "data_integrity": "api",
        "e2e": "e2e",
        "e2e_if_enabled": "e2e",
    }
    for row in document.root:
        expected_layer = row.layer or (category_layer.get(row.category) if row.category else None)
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
    return document


def _read_minimum_coverage_matrix(
    workspace: Path,
    *,
    relative: str,
) -> MinimumCoverageMatrixAuthoring:
    path = _workspace_file(workspace, relative)
    try:
        document = MinimumCoverageMatrixAuthoring.model_validate(json.loads(path.read_bytes()))
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise OutputError(f"invalid minimum-coverage-matrix.json: {error}") from error
    return document


class IntakeFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            artifacts = _finalize_artifact_list(payload, context.project_root)
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ExploreFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            if not payload.artifact_paths:
                raise InputError("artifact_paths must lock the expected output files")
            document = _artifact_list(payload)
            change_id = _case_change_id(payload.change_id)
            expected = {f"qa/changes/{change_id}/explore/exploration.json"}
            if set(document.output_files) != expected:
                raise OutputError("explore receipt must declare exactly exploration.json")
            _validate_explore_outputs(context.project_root, document.output_files)
            artifacts = _finalize_artifact_list(payload, context.project_root)
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CaseDesignFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, _finalize_payload(request.input))
            change_id = _case_change_id(payload.change_id)
            capability_leafs = _leafs(payload.capability_leafs)
            receipt = _artifact_list(payload)
            change_root = f"qa/changes/{change_id}"
            for relative in receipt.output_files:
                if not relative.startswith(f"{change_root}/"):
                    raise OutputError("case-design receipt may contain only current change outputs")
            matrix_relative = f"{change_root}/trace/minimum-coverage-matrix.json"
            required = {
                f"{change_root}/.qa.yaml",
                f"{change_root}/proposal.md",
                matrix_relative,
            }
            missing = sorted(required.difference(receipt.output_files))
            if missing:
                raise OutputError(
                    "case-design receipt is missing required output files: " + ", ".join(missing)
                )
            if not payload.case_delta_paths:
                raise InputError("case_delta_paths must lock at least one exact case.yaml output")
            declared_cases = {
                relative
                for relative in receipt.output_files
                if relative.startswith(f"{change_root}/cases/") and relative.endswith("/case.yaml")
            }
            expected_cases = set(payload.case_delta_paths)
            if declared_cases != expected_cases:
                missing_cases = sorted(expected_cases - declared_cases)
                unexpected_cases = sorted(declared_cases - expected_cases)
                raise OutputError(
                    "case-design receipt case paths do not match locked case_delta_paths; "
                    f"missing={missing_cases}, unexpected={unexpected_cases}"
                )
            _authenticate_files(
                context.project_root,
                receipt.output_files,
                payload.artifact_paths,
            )
            authored = _load_authored_case_delta(
                context.project_root,
                change_id=change_id,
                locked=payload.artifact_paths,
                declared=receipt.output_files,
                capability_leafs=capability_leafs,
            )
            authored_json = authored.model_dump(mode="json")
            authored_json.update(_review_round_fields(request.input))
            if payload.selected_test_families:
                _require_selected_test_families(authored, payload.selected_test_families)
            if authored.added or authored.modified:
                _load_minimum_coverage_matrix(
                    context.project_root,
                    relative=matrix_relative,
                    authored=authored,
                )
            else:
                _read_minimum_coverage_matrix(context.project_root, relative=matrix_relative)
            return TaskOutcome.succeeded(authored_json)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CaseReviewFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, _finalize_payload(request.input))
            try:
                document = CaseReviewResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            change_id = _case_change_id(payload.change_id or document.change_id)
            if document.change_id != change_id:
                raise OutputError("case review change_id does not match locked change_id")
            matrix = _read_minimum_coverage_matrix(
                context.project_root,
                relative=f"qa/changes/{change_id}/trace/minimum-coverage-matrix.json",
            )
            required = [row for row in matrix.root if row.required]
            expected_projection = {
                "total_required": len(required),
                "covered": sum(row.status == "covered" for row in required),
                "skipped_by_scope": sum(row.status == "skipped_by_scope" for row in required),
                "missing": [row.key for row in required if row.status == "skipped_by_scope"],
            }
            document = document.model_copy(
                update={"minimum_coverage": CaseMinimumCoverageReview.model_validate(expected_projection)}
            )
            output = document.model_dump(mode="json")
            output.update(_review_round_fields(request.input))
            if "artifacts" not in output:
                output["artifacts"] = []
            return TaskOutcome.succeeded(output)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
