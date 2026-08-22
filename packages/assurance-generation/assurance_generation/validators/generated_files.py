"""Generated-file, mapping, and codegen-fix candidate validators."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from graph_engine.plugin_api import CandidateWriteSet, ValidationContext, ValidationResult

from assurance_generation.contracts.agent import under_write_root
from assurance_generation.contracts.codegen import CodegenMapping, CodegenResultV1
from assurance_generation.contracts.families import LAYER_NAMES, LayerName
from assurance_generation.contracts.generated_files import GeneratedFilesV1
from assurance_generation.contracts.plans import canonical_relative_path

Family = LayerName
FAMILIES: tuple[Family, ...] = LAYER_NAMES


def closed_family(family: str) -> Family:
    if family not in FAMILIES:
        raise ValueError(f"unknown generation family: {family}")
    return family


FAMILY_TEST_ROOTS: dict[Family, tuple[str, ...]] = {
    "api": ("tests/api/", "tests/testdata/"),
    "e2e": ("tests/e2e/", "tests/testdata/"),
    "fuzz": ("tests/fuzz/", "tests/testdata/"),
    "performance": ("tests/perf/", "tests/testdata/"),
}
ALL_TEST_ROOTS: tuple[str, ...] = (
    "tests/api/",
    "tests/e2e/",
    "tests/fuzz/",
    "tests/perf/",
    "tests/testdata/",
)
FIX_TEST_ROOTS: tuple[str, ...] = ("tests/api/", "tests/e2e/", "tests/testdata/")
MAPPING_WRITE_ROOTS: tuple[str, ...] = ("qa/changes/", *ALL_TEST_ROOTS)
_OUTSIDE_REASON = "generation candidate may write only declared test paths"
_UNMAPPED_REASON = "generated test file is absent from the closed mapping: {path}"
_STALE_REASON = "codegen mapping is stale: {path}"
_MISSING_REASON = "codegen mapping is missing: {path}"
_EXTRA_REASON = "codegen mapping has an extra file: {path}"
_DUPLICATE_REASON = "codegen mapping has duplicate case IDs"
_DIGEST_REASON = "generated-file digest does not match workspace bytes: {path}"
_UNAPPROVED_REASON = "codegen fix proposal is not approved"
_BASELINE_REASON = "codegen fix baseline tree does not match the approved proposal"
_ALLOWED_REASON = "codegen fix write is not in the allowed file set: {path}"


def _canonical_relative(path: str) -> bool:
    try:
        canonical_relative_path(path)
    except ValueError:
        return False
    return True


def _is_mapping_target_module(path: str) -> bool:
    posix = PurePosixPath(path)
    if not posix.parts or posix.parts[0] != "tests" or posix.suffix != ".py":
        return False
    stem = posix.stem
    return stem.startswith("test_") or stem.endswith("_test")


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prefixed(digest: str) -> str:
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"


def _mapping_targets(mapping: CodegenMapping | None) -> set[str]:
    if mapping is None:
        return set()
    return {item.target_file for item in mapping.entries}


def _load_mapping(file_bytes: Mapping[str, bytes]) -> CodegenMapping | None:
    for path, payload in file_bytes.items():
        name = PurePosixPath(path).name
        if not name.endswith("-codegen-mapping.json") and name != "codegen-mapping.json":
            continue
        try:
            raw = json.loads(payload.decode("utf-8"))
            return CodegenMapping.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            continue
    return None


def _load_result(
    file_bytes: Mapping[str, bytes],
    *,
    capability_leafs: frozenset[str] | None,
) -> CodegenResultV1 | GeneratedFilesV1 | None:
    for path, payload in file_bytes.items():
        name = PurePosixPath(path).name
        if not name.endswith("-generated-files.json") and name != "generated-files.json":
            continue
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and "mapping" in raw:
            if capability_leafs is None:
                continue
            return CodegenResultV1.model_validate(raw, context={"capability_leafs": capability_leafs})
        try:
            return GeneratedFilesV1.model_validate(raw)
        except ValidationError:
            continue
    return None


class GeneratedFilesValidator:
    def __init__(
        self,
        *,
        family: str | None = None,
        mapping: CodegenMapping | None = None,
        capability_leafs: frozenset[str] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        write_roots: tuple[str, ...] | None = None,
        require_mapping: bool = True,
    ) -> None:
        self._family: Family | None = closed_family(family) if family is not None else None
        self._mapping = mapping
        self._capability_leafs = capability_leafs
        self._file_bytes = dict(file_bytes or {})
        if write_roots is not None:
            self._write_roots = write_roots
        elif self._family is not None:
            self._write_roots = FAMILY_TEST_ROOTS[self._family]
        else:
            self._write_roots = ALL_TEST_ROOTS
        self._require_mapping = require_mapping

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        mapping = self._mapping or _load_mapping(self._file_bytes)
        mapped = _mapping_targets(mapping)
        listed = tuple(item.path for item in candidate.files)
        for path in listed:
            if not _canonical_relative(path):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
            if self._require_mapping and _is_mapping_target_module(path) and path not in mapped:
                return ValidationResult(accepted=False, reason=_UNMAPPED_REASON.format(path=path))
            if not under_write_root(path, self._write_roots):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        extra_bytes = sorted(set(self._file_bytes) - set(listed))
        if extra_bytes:
            return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        if mapping is not None:
            mapping_check = _mapping_closure(candidate, mapping, self._file_bytes)
            if mapping_check is not None:
                return mapping_check
        try:
            document = _load_result(self._file_bytes, capability_leafs=self._capability_leafs)
        except ValidationError as error:
            return ValidationResult(accepted=False, reason=str(error))
        if document is not None:
            semantic = self._validate_document(candidate, document, mapping)
            if semantic is not None:
                return semantic
        return ValidationResult(accepted=True)

    def _validate_document(
        self,
        candidate: CandidateWriteSet,
        document: CodegenResultV1 | GeneratedFilesV1,
        mapping: CodegenMapping | None,
    ) -> ValidationResult | None:
        files = document.files
        writes = {item.path: item for item in candidate.files}
        if mapping is not None:
            mapped = _mapping_targets(mapping)
            for entry in files:
                if entry.role == "test_entry" and entry.repo_path not in mapped:
                    return ValidationResult(
                        accepted=False,
                        reason=_UNMAPPED_REASON.format(path=entry.repo_path),
                    )
        if (
            self._family is not None
            and isinstance(document, CodegenResultV1)
            and document.layer != self._family
        ):
            return ValidationResult(
                accepted=False,
                reason=f"generated-files family {document.layer!r} does not match {self._family}",
            )
        if self._capability_leafs is not None and isinstance(document, CodegenResultV1):
            unknown = [key for key in document.required_capabilities if key not in self._capability_leafs]
            if unknown:
                return ValidationResult(accepted=False, reason=f"unknown capability leaf: {unknown[0]}")
        for entry in files:
            write = writes.get(entry.repo_path)
            if entry.disposition in {"generated", "updated"}:
                if write is None:
                    return ValidationResult(
                        accepted=False,
                        reason=_MISSING_REASON.format(path=entry.repo_path),
                    )
                after = write.after_sha256
                if after is None or entry.content_sha256 != _prefixed(after):
                    return ValidationResult(
                        accepted=False,
                        reason=_DIGEST_REASON.format(path=entry.repo_path),
                    )
                if entry.disposition == "generated" and write.before_sha256 is not None:
                    return ValidationResult(
                        accepted=False,
                        reason=_DIGEST_REASON.format(path=entry.repo_path),
                    )
                if (
                    entry.disposition == "updated"
                    and write.before_sha256 is not None
                    and write.before_sha256 == write.after_sha256
                ):
                    return ValidationResult(
                        accepted=False,
                        reason=_DIGEST_REASON.format(path=entry.repo_path),
                    )
            if entry.repo_path in self._file_bytes:
                actual = _digest_bytes(self._file_bytes[entry.repo_path])
                if entry.content_sha256 != _prefixed(actual):
                    return ValidationResult(
                        accepted=False,
                        reason=_DIGEST_REASON.format(path=entry.repo_path),
                    )
        return None


def _mapping_closure(
    candidate: CandidateWriteSet,
    mapping: CodegenMapping,
    file_bytes: Mapping[str, bytes],
) -> ValidationResult | None:
    ids = tuple(item.case_id for item in mapping.entries)
    if len(ids) != len(set(ids)):
        return ValidationResult(accepted=False, reason=_DUPLICATE_REASON)
    writes = {item.path: item for item in candidate.files}
    mapped = _mapping_targets(mapping)
    for path in mapped:
        if not _canonical_relative(path):
            return ValidationResult(accepted=False, reason=_STALE_REASON.format(path=path))
        if path not in writes:
            return ValidationResult(accepted=False, reason=_MISSING_REASON.format(path=path))
        write = writes.get(path)
        if write is not None and path in file_bytes:
            actual = _digest_bytes(file_bytes[path])
            if write.after_sha256 != actual:
                return ValidationResult(accepted=False, reason=_STALE_REASON.format(path=path))
    for path, write in writes.items():
        if _is_mapping_target_module(path) and path not in mapped:
            return ValidationResult(accepted=False, reason=_EXTRA_REASON.format(path=path))
        del write
    return None


class CodegenMappingValidator:
    def __init__(
        self,
        *,
        mapping: CodegenMapping | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        write_roots: tuple[str, ...] = MAPPING_WRITE_ROOTS,
    ) -> None:
        self._mapping = mapping
        self._file_bytes = dict(file_bytes or {})
        self._write_roots = write_roots

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = tuple(item.path for item in candidate.files)
        for path in listed:
            if not _canonical_relative(path) or not under_write_root(path, self._write_roots):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
        mapping = self._mapping or _load_mapping(self._file_bytes)
        if mapping is None:
            return ValidationResult(accepted=True)
        try:
            CodegenMapping.model_validate(mapping.model_dump(mode="json"))
        except ValidationError as error:
            message = str(error)
            if "unique" in message:
                return ValidationResult(accepted=False, reason=_DUPLICATE_REASON)
            return ValidationResult(accepted=False, reason=message)
        return _mapping_closure(candidate, mapping, self._file_bytes) or ValidationResult(accepted=True)


class CodegenFixCandidateValidator:
    def __init__(
        self,
        *,
        family: str | None = None,
        baseline_tree_id: str | None = None,
        allowed_paths: tuple[str, ...] | None = None,
        mapping: CodegenMapping | None = None,
        approved_proposal: Mapping[str, Any] | None = None,
        file_bytes: Mapping[str, bytes] | None = None,
        write_roots: tuple[str, ...] = FIX_TEST_ROOTS,
    ) -> None:
        self._family: Family | None = closed_family(family) if family is not None else None
        self._baseline_tree_id = baseline_tree_id
        self._allowed_paths = tuple(sorted(set(allowed_paths or ())))
        self._mapping = mapping
        self._approved_proposal = dict(approved_proposal or {})
        self._file_bytes = dict(file_bytes or {})
        self._write_roots = write_roots

    def validate(self, candidate: CandidateWriteSet, context: ValidationContext) -> ValidationResult:
        del context
        listed = tuple(item.path for item in candidate.files)
        for path in listed:
            if not _canonical_relative(path) or not under_write_root(path, self._write_roots):
                return ValidationResult(accepted=False, reason=_OUTSIDE_REASON)
            if self._allowed_paths and path not in self._allowed_paths:
                return ValidationResult(accepted=False, reason=_ALLOWED_REASON.format(path=path))
        if self._approved_proposal and self._approved_proposal.get("status") != "approved":
            return ValidationResult(accepted=False, reason=_UNAPPROVED_REASON)
        if self._baseline_tree_id is not None and candidate.baseline_tree_id != self._baseline_tree_id:
            return ValidationResult(accepted=False, reason=_BASELINE_REASON)
        mapping = self._mapping or _load_mapping(self._file_bytes)
        if mapping is not None:
            if self._family is not None and mapping.layer != self._family:
                return ValidationResult(
                    accepted=False,
                    reason=f"mapping layer {mapping.layer!r} does not match {self._family}",
                )
            closed = _mapping_closure(candidate, mapping, self._file_bytes)
            if closed is not None:
                return closed
        return ValidationResult(accepted=True)


__all__ = [
    "ALL_TEST_ROOTS",
    "FAMILIES",
    "FAMILY_TEST_ROOTS",
    "CodegenFixCandidateValidator",
    "CodegenMappingValidator",
    "GeneratedFilesValidator",
]
