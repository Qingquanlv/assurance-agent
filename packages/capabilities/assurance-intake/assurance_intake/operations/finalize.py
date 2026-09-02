"""Semantic intake finalize handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
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
from assurance_intake.contracts.agent import (
    AgentFinalizeInputV1,
    ArtifactListResultV1,
    ReviewRepairActionV1,
    ReviewRepairContractV1,
)
from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.review import (
    CaseMinimumCoverageReview,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
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
    return thaw_json(payload.agent_result.result_payload)


def _validate_case_review_repair_scope(
    document: CaseReviewResultV1,
    payload: AgentFinalizeInputV1,
    *,
    change_id: str,
) -> None:
    if document.public_outcome != "needs_fix":
        return
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required to lock case-review automatic repairs")
    change_root = f"qa/changes/{change_id}"
    allowed = {
        f"{change_root}/.qa.yaml",
        f"{change_root}/proposal.md",
        f"{change_root}/trace/minimum-coverage-matrix.json",
        *payload.case_delta_paths,
    }
    if document.next_action != "run_case_design":
        raise OutputError("needs_fix case review next_action must be run_case_design")
    findings = {finding.id: finding for finding in document.findings}
    if not document.auto_fix_plan:
        raise OutputError("needs_fix case review must provide at least one auto_fix_plan item")
    planned_findings: set[str] = set()
    for item in document.auto_fix_plan:
        if not isinstance(item, Mapping):
            raise OutputError("case review auto_fix_plan items must be mappings")
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise OutputError("case review auto_fix_plan finding_id must reference an existing finding")
        if finding_id in planned_findings:
            raise OutputError("case review auto_fix_plan must not duplicate a finding_id")
        planned_findings.add(finding_id)
        finding = findings[finding_id]
        flags = finding.model_extra or {}
        if flags.get("auto_fix_allowed") is not True or flags.get("human_review_required") is not False:
            raise OutputError(
                "automatic repair finding must explicitly allow auto-fix and forbid human review"
            )
        if finding.severity in {"critical", "blocking"}:
            raise OutputError("critical or blocking case review finding cannot be auto-fixed")
        artifact = item.get("artifact")
        if not isinstance(artifact, str) or artifact not in allowed:
            raise OutputError(
                f"automatic repair artifact is outside the locked case-design write set: {artifact!r}"
            )
        if artifact != finding.locator.artifact:
            raise OutputError("automatic repair artifact must match the finding locator")
        try:
            case_id = normalized_auto_fix_case_id(item, finding.locator.case_id)
            edits = normalized_auto_fix_edits(item)
        except ValueError as error:
            raise OutputError(str(error)) from error
        if case_id != finding.locator.case_id:
            raise OutputError("automatic repair case_id must match the finding locator")
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise OutputError("automatic case repair requires an exact locator key")
        try:
            ReviewRepairActionV1(
                finding_id=finding_id,
                artifact=artifact,
                case_id=finding.locator.case_id,
                allowed_paths=tuple(sorted({part.strip() for part in key.split(",") if part.strip()})),
                instructions=edits,
            )
        except ValidationError as error:
            raise OutputError(f"invalid automatic repair locator: {error}") from error


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


def _case_entries(document: object, *, artifact: str) -> dict[str, tuple[str, int, Mapping[str, object]]]:
    if not isinstance(document, Mapping):
        raise OutputError(f"repair baseline is not a case document: {artifact}")
    entries: dict[str, tuple[str, int, Mapping[str, object]]] = {}
    for section in ("added", "modified"):
        raw_entries = document.get(section)
        if not isinstance(raw_entries, list):
            raise OutputError(f"repair baseline has invalid {section}: {artifact}")
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, Mapping):
                raise OutputError(f"repair baseline has invalid case entry: {artifact}")
            case_id = raw_entry.get("case_id")
            if not isinstance(case_id, str) or not case_id or case_id in entries:
                raise OutputError(f"repair baseline has invalid or duplicate case_id: {artifact}")
            entries[case_id] = (section, index, raw_entry)
    return entries


def _changed_paths(before: object, after: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changed: set[tuple[str, ...]] = set()
        for key in set(before) | set(after):
            child = (*prefix, str(key))
            if key not in before or key not in after:
                changed.add(child)
            else:
                changed.update(_changed_paths(before[key], after[key], child))
        return changed
    if isinstance(before, list) and isinstance(after, list):
        return set() if before == after else {prefix}
    return set() if before == after else {prefix}


def _path_allowed(path: tuple[str, ...], allowed: tuple[str, ...]) -> bool:
    return any(path[: len(candidate)] == candidate for candidate in allowed)


def _case_allowed_path(path: str) -> tuple[str, ...]:
    if path.startswith("trace.") and len(path) > len("trace."):
        return ("trace", path[len("trace.") :])
    return tuple(path.split("."))


def _validate_case_repair_document(
    *,
    artifact: str,
    before: object,
    after: object,
    actions: tuple[ReviewRepairActionV1, ...],
) -> None:
    before_entries = _case_entries(before, artifact=artifact)
    after_entries = _case_entries(after, artifact=artifact)
    before_identity = {
        section: [case_id for case_id, item in before_entries.items() if item[0] == section]
        for section in ("added", "modified")
    }
    after_identity = {
        section: [case_id for case_id, item in after_entries.items() if item[0] == section]
        for section in ("added", "modified")
    }
    if before_identity != after_identity:
        raise OutputError(f"review repair added, removed, moved, or reordered a case: {artifact}")
    before_removed = before.get("removed") if isinstance(before, Mapping) else None
    after_removed = after.get("removed") if isinstance(after, Mapping) else None
    before_schema = before.get("schema_version") if isinstance(before, Mapping) else None
    after_schema = after.get("schema_version") if isinstance(after, Mapping) else None
    if before_removed != after_removed or before_schema != after_schema:
        raise OutputError(f"review repair changed case document structure: {artifact}")

    actions_by_case: dict[str, list[ReviewRepairActionV1]] = {}
    for action in actions:
        if action.case_id is None:
            raise OutputError(f"case.yaml review repair requires an exact case_id: {action.finding_id}")
        actions_by_case.setdefault(action.case_id, []).append(action)
    for case_id in set(before_entries) | set(after_entries):
        if case_id not in before_entries or case_id not in after_entries:
            raise OutputError(f"review repair changed case identity: {case_id}")
        before_entry = before_entries[case_id][2]
        after_entry = after_entries[case_id][2]
        case_actions = actions_by_case.get(case_id)
        if case_actions is None:
            if before_entry != after_entry:
                raise OutputError(f"review repair changed non-target case: {case_id}")
            continue
        changed = _changed_paths(before_entry, after_entry)
        allowed = tuple(
            _case_allowed_path(path) for action in case_actions for path in action.allowed_paths
        )
        outside = sorted(".".join(path) for path in changed if not _path_allowed(path, allowed))
        if outside:
            raise OutputError(f"review repair changed fields outside allowed_paths for {case_id}: {outside}")
        for action in case_actions:
            action_allowed = tuple(_case_allowed_path(path) for path in action.allowed_paths)
            if not any(_path_allowed(path, action_allowed) for path in changed):
                raise OutputError(f"review repair did not apply finding {action.finding_id}")


def _validate_review_repair(workspace: Path, contract: ReviewRepairContractV1) -> None:
    review_path = _workspace_file(workspace, contract.review_path)
    if not review_path.is_file() or _file_digest(review_path.read_bytes()) != contract.review_sha256:
        raise OutputError("case-review repair authority changed after prepare")
    actions_by_artifact: dict[str, list[ReviewRepairActionV1]] = {}
    for action in contract.actions:
        actions_by_artifact.setdefault(action.artifact, []).append(action)
    baseline_documents = thaw_json(contract.baseline_case_documents)
    if not isinstance(baseline_documents, Mapping):
        raise OutputError("case-review repair baseline documents are invalid")
    for relative, baseline_digest in contract.baseline_file_digests.items():
        path = _workspace_file(workspace, relative)
        if not path.is_file():
            raise OutputError(f"review repair output is missing: {relative}")
        current_digest = _file_digest(path.read_bytes())
        actions = actions_by_artifact.get(relative)
        if actions is None:
            if current_digest != baseline_digest:
                raise OutputError(f"review repair non-target output changed: {relative}")
            continue
        if current_digest == baseline_digest:
            raise OutputError(f"review repair target did not change: {relative}")
        if relative.endswith("/case.yaml"):
            before = baseline_documents.get(relative)
            try:
                after = yaml.safe_load(path.read_bytes())
            except yaml.YAMLError as error:
                raise OutputError(f"invalid repaired case.yaml {relative}: {error}") from error
            _validate_case_repair_document(
                artifact=relative,
                before=before,
                after=after,
                actions=tuple(actions),
            )


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
            artifacts = _finalize_artifact_list(payload, context.project_root)
            _validate_explore_outputs(context.project_root, document.output_files)
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
            if payload.review_repair is not None:
                _validate_review_repair(context.project_root, payload.review_repair)
            authored = _load_authored_case_delta(
                context.project_root,
                change_id=change_id,
                locked=payload.artifact_paths,
                declared=receipt.output_files,
                capability_leafs=capability_leafs,
            )
            authored_json = authored.model_dump(mode="json")
            authored_json.update(_review_round_fields(request.input))
            authored_json["validation_status"] = "pass"
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
            if isinstance(request.input, dict) and request.input.get("validation_attempt") == 0:
                repair_output: dict[str, object] = {
                    "validation_status": "needs_fix",
                    "validation_attempt": 1,
                    "validation_error": str(error)[:8192],
                    "review_repair": None,
                    **_review_round_fields(request.input),
                }
                if "payload" in locals() and payload.review_repair is not None:
                    repair_output["review_repair"] = payload.review_repair.model_dump(mode="json")
                return TaskOutcome.succeeded(cast(JSONValue, repair_output))
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
            _validate_case_review_repair_scope(document, payload, change_id=change_id)
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
