"""Semantic intake finalize handlers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts import (
    CaseReviewResultV1,
    CaseYamlAuthoring,
    MinimumCoverageMatrixAuthoring,
)
from assurance_intake.contracts.agent import (
    AgentFinalizeInputV1,
    ArtifactDigestV1,
    ArtifactListResultV1,
    CaseDesignOutputV1,
    CaseFinalizeInputV1,
    ReviewRepairActionV1,
    ReviewRepairContractV1,
)
from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.loop_history import build_loop_round_history
from assurance_intake.contracts.quality_goals import journey_keys_from_document
from assurance_intake.contracts.review import (
    CaseMinimumCoverageReview,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.contracts.plan import decode_plan
from assurance_intake.operations.agent_skills import InputError, failed_input, validate_input
from assurance_intake.operations.agent_skills import case_review_outputs


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


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
    change_root = "qa"
    allowed = {
        f"{change_root}/.qa.yaml",
        f"{change_root}/proposal.md",
        f"{change_root}/results/trace/minimum-coverage-matrix.json",
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
        locator_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        if artifact in payload.case_delta_paths and "case_id" in locator_paths:
            raise OutputError(
                "case_id identifies the case and cannot be an automatic repair field; "
                "use added or modified to authorize whole-case removal"
            )
        try:
            ReviewRepairActionV1(
                finding_id=finding_id,
                artifact=artifact,
                case_id=finding.locator.case_id,
                allowed_paths=locator_paths,
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


def _read_regular_bytes(workspace: Path, relative: str, *, kind: str) -> bytes:
    path = _workspace_file(workspace, relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OutputError(f"{kind} is missing or is not a regular file: {relative}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OutputError(f"{kind} is missing or is not a regular file: {relative}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature or len(data) != before.st_size:
        raise OutputError(f"{kind} changed while being read: {relative}")
    return data


_DATA_KNOWLEDGE_RESOURCE_ID = "assurance.product.configuration.data-knowledge"
_DATA_KNOWLEDGE_PATH = ".aa/data-knowledge.yaml"


def _authenticated_journey_keys(
    workspace: Path,
    source_resource_digests: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    expected_digest = dict(source_resource_digests).get(_DATA_KNOWLEDGE_RESOURCE_ID)
    if expected_digest is None:
        raise InputError("frozen assurance plan does not bind data knowledge")
    try:
        data = _read_regular_bytes(workspace, _DATA_KNOWLEDGE_PATH, kind="data knowledge")
    except OutputError as error:
        raise InputError(str(error)) from error
    if _file_digest(data) != expected_digest:
        raise InputError("data knowledge does not match the frozen assurance plan")
    try:
        document = yaml.safe_load(data)
        if not isinstance(document, Mapping):
            raise ValueError("data knowledge must be a mapping")
        return journey_keys_from_document(document)
    except (yaml.YAMLError, ValueError) as error:
        raise InputError(f"invalid data knowledge: {error}") from error


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


def _authenticate_review_repair_images(
    images: Mapping[str, bytes],
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        if not _allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        data = images.get(relative)
        if data is None:
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": _file_digest(data)})
    return artifacts


def _validation_repair_images(
    project_root: Path,
    write_root: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> dict[str, bytes]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    images: dict[str, bytes] = {}
    for relative in declared:
        if not _allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        candidate = _workspace_file(write_root, relative)
        if candidate.exists() or candidate.is_symlink():
            images[relative] = _read_regular_bytes(
                write_root,
                relative,
                kind="validation repair output",
            )
        else:
            images[relative] = _read_regular_bytes(
                project_root,
                relative,
                kind="validation repair baseline",
            )
    return images


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
        if parts != ("qa", "results", "explore", "exploration.json"):
            raise OutputError(f"invalid exploration.json path: {relative}")
        if not document.change_id:
            raise OutputError("exploration.json change_id does not match its change directory")
        if document.context_ref != "explore/context.json":
            raise OutputError("exploration.json context_ref must be explore/context.json")


def _require_selected_test_families(
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
        if not _allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        try:
            data = (
                images[relative]
                if images is not None
                else _read_regular_bytes(workspace, relative, kind="declared output file")
            )
            raw = yaml.safe_load(data)
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
    selected: tuple[str, ...],
    journey_keys: tuple[str, ...],
    images: Mapping[str, bytes] | None = None,
) -> MinimumCoverageMatrixAuthoring:
    document = _read_minimum_coverage_matrix(
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


def _read_minimum_coverage_matrix(
    workspace: Path,
    *,
    relative: str,
    images: Mapping[str, bytes] | None = None,
) -> MinimumCoverageMatrixAuthoring:
    try:
        data = (
            images[relative]
            if images is not None
            else _read_regular_bytes(workspace, relative, kind="minimum coverage matrix")
        )
        document = MinimumCoverageMatrixAuthoring.model_validate(json.loads(data))
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise OutputError(f"invalid minimum-coverage-matrix.json: {error}") from error
    return document


def _read_case_review_inputs(
    workspace: Path,
    payload: CaseFinalizeInputV1,
    matrix_relative: str,
) -> dict[str, bytes]:
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required for case-review scope validation")
    if {ref.path for ref in payload.case_refs} != set(payload.case_delta_paths):
        raise InputError("case_refs must authenticate every locked case.yaml for case review")
    matrix_refs = tuple(ref for ref in payload.preparation_refs if ref.path == matrix_relative)
    if len(matrix_refs) != 1:
        raise InputError("preparation_refs must authenticate the minimum coverage matrix for case review")
    images: dict[str, bytes] = {}
    for ref in (*payload.case_refs, *matrix_refs):
        data = _read_regular_bytes(workspace, ref.path, kind="case-review input")
        if _file_digest(data) != ref.digest:
            raise OutputError(f"case-review input digest mismatch: {ref.path}")
        images[ref.path] = data
    return images


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


def _path_allowed(path: tuple[str, ...], allowed: tuple[tuple[str, ...], ...]) -> bool:
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
    actions_by_case: dict[str, list[ReviewRepairActionV1]] = {}
    for action in actions:
        if action.case_id is None:
            raise OutputError(f"case.yaml review repair requires an exact case_id: {action.finding_id}")
        actions_by_case.setdefault(action.case_id, []).append(action)

    before_identity = {
        section: [case_id for case_id, item in before_entries.items() if item[0] == section]
        for section in ("added", "modified")
    }
    after_identity = {
        section: [case_id for case_id, item in after_entries.items() if item[0] == section]
        for section in ("added", "modified")
    }
    authorized_removals: set[str] = set()
    for case_id, (section, _, _) in before_entries.items():
        if case_id in after_entries:
            continue
        case_actions = actions_by_case.get(case_id, [])
        if case_actions and all(
            {_case_allowed_path(path) for path in action.allowed_paths} == {(section,)}
            for action in case_actions
        ):
            authorized_removals.add(case_id)
    authorized_additions: set[str] = set()
    for case_id, (section, _, _) in after_entries.items():
        if case_id in before_entries:
            continue
        case_actions = actions_by_case.get(case_id, [])
        if (
            section == "added"
            and case_actions
            and all(
                {_case_allowed_path(path) for path in action.allowed_paths} == {(section,)}
                for action in case_actions
            )
        ):
            authorized_additions.add(case_id)
    expected_existing_identity = {
        section: [case_id for case_id in case_ids if case_id not in authorized_removals]
        for section, case_ids in before_identity.items()
    }
    actual_existing_identity = {
        section: [case_id for case_id in case_ids if case_id not in authorized_additions]
        for section, case_ids in after_identity.items()
    }
    if expected_existing_identity != actual_existing_identity:
        raise OutputError(f"review repair added, removed, moved, or reordered a case: {artifact}")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise OutputError(f"repair baseline is not a case document: {artifact}")
    mutable_sections = {"added", "modified"}
    before_structure = {key: value for key, value in before.items() if key not in mutable_sections}
    after_structure = {key: value for key, value in after.items() if key not in mutable_sections}
    if before_structure != after_structure:
        raise OutputError(f"review repair changed case document structure: {artifact}")

    for case_id in set(before_entries) | set(after_entries):
        if case_id in authorized_removals or case_id in authorized_additions:
            continue
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
        allowed = tuple(_case_allowed_path(path) for action in case_actions for path in action.allowed_paths)
        outside = sorted(".".join(path) for path in changed if not _path_allowed(path, allowed))
        if outside:
            raise OutputError(f"review repair changed fields outside allowed_paths for {case_id}: {outside}")
        for action in case_actions:
            action_allowed = tuple(_case_allowed_path(path) for path in action.allowed_paths)
            if not any(_path_allowed(path, action_allowed) for path in changed):
                raise OutputError(f"review repair did not apply finding {action.finding_id}")


def _proposal_parts(data: bytes, *, artifact: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OutputError(f"review repair proposal is not UTF-8: {artifact}") from error
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        indented = content.lstrip(" ")
        indent = len(content) - len(indented)
        if fence is not None:
            marker, minimum = fence
            if indent <= 3 and indented.startswith(marker * minimum):
                run = len(indented) - len(indented.lstrip(marker))
                if run >= minimum and not indented[run:].strip():
                    fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", content)
        if opening is not None:
            token = opening.group(1)
            fence = (token[0], len(token))
            continue
        if content.startswith("# ") or content.startswith("## "):
            starts.append(index)
    if not starts:
        return text, ()
    preamble = "".join(lines[: starts[0]])
    sections: list[tuple[str, str]] = []
    for offset, start in enumerate(starts):
        stop = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
        heading = lines[start].rstrip("\r\n")
        sections.append((heading, "".join(lines[start + 1 : stop])))
    return preamble, tuple(sections)


def _validate_proposal_repair_document(
    *,
    artifact: str,
    before: bytes,
    after: bytes,
    actions: tuple[ReviewRepairActionV1, ...],
) -> None:
    if any(action.case_id is not None for action in actions):
        raise OutputError("proposal.md review repair case_id must be null")
    targets = tuple(action.allowed_paths[0] for action in actions)
    if len(targets) != len(set(targets)):
        raise OutputError("proposal.md review repair sections must not overlap")
    before_preamble, before_sections = _proposal_parts(before, artifact=artifact)
    after_preamble, after_sections = _proposal_parts(after, artifact=artifact)
    before_headings = tuple(heading for heading, _body in before_sections)
    after_headings = tuple(heading for heading, _body in after_sections)
    if before_preamble != after_preamble or before_headings != after_headings:
        raise OutputError(f"review repair changed proposal structure outside named section: {artifact}")
    for target in targets:
        if before_headings.count(target) != 1:
            raise OutputError(f"proposal.md repair heading must occur exactly once in the baseline: {target}")
    for (heading, before_body), (_after_heading, after_body) in zip(
        before_sections, after_sections, strict=True
    ):
        if heading not in targets and before_body != after_body:
            raise OutputError(f"review repair changed proposal outside named section: {heading}")
        if heading in targets and before_body == after_body:
            raise OutputError(f"review repair did not change named proposal section: {heading}")


def _matrix_rows(data: bytes, *, artifact: str) -> tuple[dict[str, object], ...]:
    try:
        document = MinimumCoverageMatrixAuthoring.model_validate(json.loads(data))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise OutputError(f"invalid repaired minimum-coverage-matrix.json {artifact}: {error}") from error
    return tuple(cast(dict[str, object], row.model_dump(mode="json")) for row in document.root)


def _validate_matrix_repair_document(
    *,
    artifact: str,
    before: bytes,
    after: bytes,
    actions: tuple[ReviewRepairActionV1, ...],
) -> None:
    if any(action.case_id is not None for action in actions):
        raise OutputError("minimum coverage matrix review repair case_id must be null")
    before_rows = _matrix_rows(before, artifact=artifact)
    after_rows = _matrix_rows(after, artifact=artifact)
    before_ids = tuple(cast(str, row["mrc_id"]) for row in before_rows)
    after_ids = tuple(cast(str, row["mrc_id"]) for row in after_rows)
    if before_ids != after_ids:
        raise OutputError(f"review repair changed MRC row identity or order: {artifact}")
    targets: set[str] = set()
    for action in actions:
        action_targets = action.allowed_paths
        expected_order = tuple(mrc_id for mrc_id in before_ids if mrc_id in action_targets)
        if action_targets != expected_order:
            raise OutputError(
                "minimum coverage matrix repair allowed_paths must be existing mrc_id values "
                "in baseline order"
            )
        overlap = targets.intersection(action_targets)
        if overlap:
            raise OutputError(f"minimum coverage matrix repair rows overlap: {sorted(overlap)}")
        targets.update(action_targets)

    stable_fields = ("mrc_id", "key", "required", "category", "layer")
    changed_targets: set[str] = set()
    for before_row, after_row in zip(before_rows, after_rows, strict=True):
        mrc_id = cast(str, before_row["mrc_id"])
        if mrc_id not in targets:
            if before_row != after_row:
                raise OutputError(f"review repair changed outside named MRC rows: {mrc_id}")
            continue
        if any(before_row[field] != after_row[field] for field in stable_fields):
            raise OutputError(f"review repair changed frozen MRC fields: {mrc_id}")
        if before_row != after_row:
            changed_targets.add(mrc_id)
    for action in actions:
        if not changed_targets.intersection(action.allowed_paths):
            raise OutputError(f"review repair did not apply finding {action.finding_id}")


def _validate_repair_document(
    *,
    artifact: str,
    before: bytes,
    after: bytes,
    actions: tuple[ReviewRepairActionV1, ...],
) -> None:
    if artifact.endswith("/case.yaml"):
        try:
            before_document = yaml.safe_load(before)
            after_document = yaml.safe_load(after)
        except yaml.YAMLError as error:
            raise OutputError(f"invalid repaired case.yaml {artifact}: {error}") from error
        _validate_case_repair_document(
            artifact=artifact,
            before=before_document,
            after=after_document,
            actions=actions,
        )
    elif artifact.endswith("/proposal.md"):
        _validate_proposal_repair_document(
            artifact=artifact,
            before=before,
            after=after,
            actions=actions,
        )
    elif artifact.endswith("/trace/minimum-coverage-matrix.json"):
        _validate_matrix_repair_document(
            artifact=artifact,
            before=before,
            after=after,
            actions=actions,
        )
    else:
        raise OutputError(f"review repair artifact type is not supported: {artifact}")


def _validate_review_repair(
    project_root: Path,
    write_root: Path,
    contract: ReviewRepairContractV1,
) -> dict[str, bytes]:
    review = _read_regular_bytes(project_root, contract.review_path, kind="case-review authority")
    if _file_digest(review) != contract.review_sha256:
        raise OutputError("case-review repair authority changed after prepare")
    actions_by_artifact: dict[str, list[ReviewRepairActionV1]] = {}
    for action in contract.actions:
        actions_by_artifact.setdefault(action.artifact, []).append(action)
    images: dict[str, bytes] = {}
    for relative, baseline_digest in contract.baseline_file_digests.items():
        baseline = _read_regular_bytes(project_root, relative, kind="review repair baseline")
        if _file_digest(baseline) != baseline_digest:
            raise OutputError(f"review repair baseline changed after prepare: {relative}")
        actions = actions_by_artifact.get(relative)
        if actions is None:
            candidate_path = _workspace_file(write_root, relative)
            if candidate_path.exists() or candidate_path.is_symlink():
                raise OutputError(f"review repair staged a non-target output: {relative}")
            images[relative] = baseline
            continue
        candidate = _read_regular_bytes(write_root, relative, kind="review repair output")
        if _file_digest(candidate) == baseline_digest:
            raise OutputError(f"review repair target did not change: {relative}")
        _validate_repair_document(
            artifact=relative,
            before=baseline,
            after=candidate,
            actions=tuple(actions),
        )
        images[relative] = candidate
    return images


class IntakeFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            artifacts = _finalize_artifact_list(payload, context.write_root)
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ExploreFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            if not payload.artifact_paths:
                raise InputError("artifact_paths must lock the expected output files")
            document = _artifact_list(payload)
            _case_change_id(payload.change_id)
            expected = {"qa/results/explore/exploration.json"}
            if set(document.output_files) != expected:
                raise OutputError("explore receipt must declare exactly exploration.json")
            artifacts = _finalize_artifact_list(payload, context.write_root)
            _validate_explore_outputs(context.write_root, document.output_files)
            return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class CaseDesignFinalizeHandler:
    input_model = CaseFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        parsed: CaseFinalizeInputV1 | None = None
        try:
            payload = validate_input(CaseFinalizeInputV1, request.input)
            parsed = payload
            change_id = _case_change_id(payload.change_id)
            try:
                plan_path = _workspace_file(context.project_root, payload.plan_ref.path)
                plan = decode_plan(plan_path.read_bytes(), payload.plan_ref)
            except (OSError, ValidationError, ValueError) as error:
                raise InputError(f"invalid frozen assurance plan: {error}") from error
            if plan.change_id != change_id or plan.plan_digest != payload.plan_digest:
                raise InputError("frozen assurance plan does not match case input")
            if plan.selected_test_families != payload.selected_test_families:
                raise InputError("case selected families do not match frozen assurance plan")
            capability_leafs = _leafs(payload.capability_leafs)
            receipt = _artifact_list(payload)
            change_root = "qa"
            for relative in receipt.output_files:
                if not relative.startswith(f"{change_root}/"):
                    raise OutputError("case-design receipt may contain only current change outputs")
            matrix_relative = f"{change_root}/results/trace/minimum-coverage-matrix.json"
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
            if payload.review_repair is not None:
                if tuple(receipt.output_files) != tuple(payload.review_repair.baseline_file_digests):
                    raise OutputError(
                        "review repair receipt must exactly match the frozen case-design outputs"
                    )
                images = _validate_review_repair(
                    context.project_root,
                    context.write_root,
                    payload.review_repair,
                )
                artifacts = _authenticate_review_repair_images(
                    images,
                    receipt.output_files,
                    payload.artifact_paths,
                )
            elif payload.validation_attempt == 1:
                images = _validation_repair_images(
                    context.project_root,
                    context.write_root,
                    receipt.output_files,
                    payload.artifact_paths,
                )
                artifacts = _authenticate_review_repair_images(
                    images,
                    receipt.output_files,
                    payload.artifact_paths,
                )
            else:
                images = None
                artifacts = _authenticate_files(
                    context.write_root,
                    receipt.output_files,
                    payload.artifact_paths,
                )
            authored = _load_authored_case_delta(
                context.write_root,
                change_id=change_id,
                locked=payload.artifact_paths,
                declared=receipt.output_files,
                capability_leafs=capability_leafs,
                images=images,
            )
            validation_errors: list[str] = []
            if payload.selected_test_families:
                try:
                    _require_selected_test_families(authored, payload.selected_test_families)
                except OutputError as error:
                    validation_errors.append(str(error))
            if authored.added or authored.modified:
                journey_keys = _authenticated_journey_keys(
                    context.project_root,
                    plan.quality_goal.source_resource_digests,
                )
                try:
                    _load_minimum_coverage_matrix(
                        context.write_root,
                        relative=matrix_relative,
                        authored=authored,
                        selected=plan.selected_test_families,
                        journey_keys=journey_keys,
                        images=images,
                    )
                except OutputError as error:
                    validation_errors.append(str(error))
            else:
                _read_minimum_coverage_matrix(
                    context.write_root,
                    relative=matrix_relative,
                    images=images,
                )
            if validation_errors:
                raise OutputError("; ".join(validation_errors))
            output = CaseDesignOutputV1(
                validation_status="pass",
                validation_attempt=payload.validation_attempt,
                artifacts=tuple(ArtifactDigestV1.model_validate(item) for item in artifacts),
                review_repair=payload.review_repair,
            )
            return TaskOutcome.succeeded(output.model_dump(mode="json"))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            if parsed is not None and parsed.review_repair is not None:
                # A review repair is bound to the committed baseline. Reject
                # invalid candidates before promotion so that retry keeps it.
                return failed_output(str(error))
            if isinstance(request.input, dict) and request.input.get("validation_attempt") == 0:
                repair_output = CaseDesignOutputV1(
                    validation_status="needs_fix",
                    validation_attempt=1,
                    validation_error=str(error)[:8192],
                    review_repair=parsed.review_repair if parsed is not None else None,
                )
                return TaskOutcome.succeeded(repair_output.model_dump(mode="json"))
            return failed_output(str(error))


class CaseReviewFinalizeHandler:
    input_model = CaseFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(CaseFinalizeInputV1, request.input)
            try:
                plan_path = _workspace_file(context.project_root, payload.plan_ref.path)
                plan = decode_plan(plan_path.read_bytes(), payload.plan_ref)
            except (OSError, ValidationError, ValueError) as error:
                raise InputError(f"invalid frozen assurance plan: {error}") from error
            if plan.plan_digest != payload.plan_digest:
                raise InputError("frozen assurance plan does not match case review input")
            try:
                document = CaseReviewResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            change_id = _case_change_id(payload.change_id or document.change_id)
            if document.change_id != change_id:
                raise OutputError("case review change_id does not match locked change_id")
            if plan.change_id != change_id:
                raise InputError("frozen assurance plan does not match case review change_id")
            _validate_case_review_repair_scope(document, payload, change_id=change_id)
            matrix_relative = "qa/results/trace/minimum-coverage-matrix.json"
            images = _read_case_review_inputs(context.project_root, payload, matrix_relative)
            authored = _load_authored_case_delta(
                context.project_root,
                change_id=change_id,
                locked=payload.case_delta_paths,
                declared=payload.case_delta_paths,
                capability_leafs=_leafs(payload.capability_leafs),
                images=images,
            )
            _require_selected_test_families(authored, plan.selected_test_families)
            matrix = _load_minimum_coverage_matrix(
                context.project_root,
                relative=matrix_relative,
                authored=authored,
                selected=plan.selected_test_families,
                journey_keys=_authenticated_journey_keys(
                    context.project_root,
                    plan.quality_goal.source_resource_digests,
                ),
                images=images,
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
            if payload.preparation_refs and payload.case_refs:
                artifacts = _authenticate_files(
                    context.write_root,
                    case_review_outputs(change_id),
                    payload.artifact_paths,
                )
                review_relative = "qa/results/review/case-review.json"
                review_ref = EvidenceArtifactRefV1.model_validate(
                    next(item for item in artifacts if item["path"] == review_relative)
                )
                input_refs = tuple(
                    sorted((*payload.preparation_refs, *payload.case_refs), key=lambda item: item.path)
                )
                input_digest = canonical_digest(
                    cast(JSONValue, [item.model_dump(mode="json") for item in input_refs])
                )
                history = build_loop_round_history(
                    change_id=change_id,
                    coverage_epoch=payload.coverage_epoch,
                    loop_kind="case_review",
                    family=None,
                    round_index=payload.review_round,
                    outcome=document.public_outcome or document.decision,
                    review_input_digest=input_digest,
                    source_refs=tuple(sorted((*input_refs, review_ref), key=lambda item: item.path)),
                )
                history_relative = (
                    f"qa/cases/reviews/epochs/{payload.coverage_epoch}/rounds/{payload.review_round}.json"
                )
                history_bytes = canonical_json_bytes(history.model_dump(mode="json")) + b"\n"
                history_path = context.write_root.joinpath(*history_relative.split("/"))
                history_path.parent.mkdir(parents=True, exist_ok=True)
                history_path.write_bytes(history_bytes)
                history_ref = EvidenceArtifactRefV1(
                    path=history_relative,
                    digest=_file_digest(history_bytes),
                )
                reviewed = ReviewedCaseV1(
                    change_id=change_id,
                    coverage_epoch=payload.coverage_epoch,
                    plan_digest=payload.plan_digest,
                    plan_ref=payload.plan_ref,
                    preparation_refs=payload.preparation_refs,
                    case_refs=payload.case_refs,
                    review_ref=review_ref,
                )
                manifest_relative = "qa/cases/reviewed-case.json"
                manifest_bytes = canonical_json_bytes(reviewed.model_dump(mode="json")) + b"\n"
                manifest_path = context.write_root.joinpath(*manifest_relative.split("/"))
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_bytes(manifest_bytes)
                output["artifacts"] = [
                    *artifacts,
                    {
                        "path": manifest_relative,
                        "digest": _file_digest(manifest_bytes),
                    },
                    history_ref.model_dump(mode="json"),
                ]
                output["reviewed_case"] = reviewed.model_dump(mode="json")
                output["history_ref"] = history_ref.model_dump(mode="json")
            else:
                output["artifacts"] = []
            return TaskOutcome.succeeded(output)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
