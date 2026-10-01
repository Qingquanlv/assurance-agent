"""Bounded review-repair contracts and validation of repaired documents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from agent_runtime_contracts.ops import (
    OutputError,
)
from graph_engine.frozen_json import FrozenJSONValue
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts import (
    MinimumCoverageMatrixAuthoring,
)
from assurance_intake.domain.artifacts import file_digest, read_regular_bytes, workspace_file
from assurance_intake.domain.inputs import FIELD_PATH, MRC_ID, SHA256_PATTERN, canonical_relative_paths


class ReviewRepairActionV1(FrozenModel):
    finding_id: str = Field(min_length=1)
    artifact: str = Field(min_length=1)
    case_id: str | None = Field(default=None, min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    instructions: tuple[str, ...] = Field(min_length=1)

    @field_validator("artifact")
    @classmethod
    def _artifact(cls, value: str) -> str:
        return canonical_relative_paths((value,))[0]

    @field_validator("allowed_paths")
    @classmethod
    def _allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("review repair allowed path must be a non-empty string")
        if cleaned != value or len(value) != len(set(value)):
            raise ValueError("review repair allowed_paths must be trimmed and unique")
        return cleaned

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("review repair instructions must be non-empty strings")
        return cleaned

    @model_validator(mode="after")
    def _artifact_locator_is_bounded(self) -> ReviewRepairActionV1:
        if self.artifact.endswith("/case.yaml"):
            if self.case_id is None:
                raise ValueError("case.yaml repair requires an exact case_id")
            if any(FIELD_PATH.fullmatch(path) is None for path in self.allowed_paths):
                raise ValueError("case.yaml repair allowed_paths must be dotted field paths")
        elif self.artifact.endswith("/proposal.md"):
            if self.case_id is not None:
                raise ValueError("proposal.md repair case_id must be null")
            if len(self.allowed_paths) != 1 or not self.allowed_paths[0].startswith("## "):
                raise ValueError("proposal.md repair requires one full level-two Markdown heading")
            heading = self.allowed_paths[0]
            if "\n" in heading or "\r" in heading or not heading[3:].strip():
                raise ValueError("proposal.md repair requires one full level-two Markdown heading")
        elif self.artifact.endswith("/trace/minimum-coverage-matrix.json"):
            if self.case_id is not None:
                raise ValueError("minimum coverage matrix repair case_id must be null")
            if any(MRC_ID.fullmatch(path) is None for path in self.allowed_paths):
                raise ValueError("minimum coverage matrix repair allowed_paths must be exact mrc_id values")
        elif self.artifact.endswith("/.qa.yaml"):
            raise ValueError(".qa.yaml cannot be repaired automatically")
        else:
            raise ValueError("review repair artifact type is not supported")
        return self


class ReviewRepairContractV1(FrozenModel):
    review_path: str = Field(min_length=1)
    review_sha256: str = Field(pattern=SHA256_PATTERN)
    baseline_file_digests: dict[str, str] = Field(min_length=1)
    baseline_case_documents: FrozenJSONValue = Field(default_factory=dict)
    actions: tuple[ReviewRepairActionV1, ...] = Field(min_length=1)

    @field_validator("review_path")
    @classmethod
    def _review_path(cls, value: str) -> str:
        return canonical_relative_paths((value,))[0]

    @field_validator("baseline_file_digests")
    @classmethod
    def _baseline_file_digests(cls, value: dict[str, str]) -> dict[str, str]:
        canonical = canonical_relative_paths(tuple(value))
        if tuple(value) != canonical:
            raise ValueError("baseline_file_digests keys must be sorted canonical paths")
        if any(re.fullmatch(SHA256_PATTERN, digest) is None for digest in value.values()):
            raise ValueError("baseline_file_digests values must be sha256 digests")
        return value

    @model_validator(mode="after")
    def _actions_match_baseline(self) -> ReviewRepairContractV1:
        targets = {action.artifact for action in self.actions}
        missing = targets.difference(self.baseline_file_digests)
        if missing:
            raise ValueError(f"review repair targets are missing baseline files: {sorted(missing)}")
        identities = [(action.finding_id, action.artifact, action.case_id) for action in self.actions]
        if len(identities) != len(set(identities)):
            raise ValueError("review repair actions must be unique")
        return self


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


def validate_review_repair(
    project_root: Path,
    write_root: Path,
    contract: ReviewRepairContractV1,
) -> dict[str, bytes]:
    review = read_regular_bytes(project_root, contract.review_path, kind="case-review authority")
    if file_digest(review) != contract.review_sha256:
        raise OutputError("case-review repair authority changed after prepare")
    actions_by_artifact: dict[str, list[ReviewRepairActionV1]] = {}
    for action in contract.actions:
        actions_by_artifact.setdefault(action.artifact, []).append(action)
    images: dict[str, bytes] = {}
    for relative, baseline_digest in contract.baseline_file_digests.items():
        baseline = read_regular_bytes(project_root, relative, kind="review repair baseline")
        if file_digest(baseline) != baseline_digest:
            raise OutputError(f"review repair baseline changed after prepare: {relative}")
        actions = actions_by_artifact.get(relative)
        if actions is None:
            candidate_path = workspace_file(write_root, relative)
            if candidate_path.exists() or candidate_path.is_symlink():
                raise OutputError(f"review repair staged a non-target output: {relative}")
            images[relative] = baseline
            continue
        candidate = read_regular_bytes(write_root, relative, kind="review repair output")
        if file_digest(candidate) == baseline_digest:
            raise OutputError(f"review repair target did not change: {relative}")
        _validate_repair_document(
            artifact=relative,
            before=baseline,
            after=candidate,
            actions=tuple(actions),
        )
        images[relative] = candidate
    return images
