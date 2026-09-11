from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Literal, cast

from agent_runtime_contracts.qa_paths import qa_join
from assurance_generation.contracts.codegen import durable_test_path
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

TestFamily = Literal["api", "e2e", "fuzz", "performance"]
FAMILIES: tuple[TestFamily, ...] = ("api", "e2e", "fuzz", "performance")
GeneratedOperation = Literal["generated", "updated", "reused"]


class GeneratedFileV2(FrozenModel):
    target_path: str
    staged_path: str
    sha256: str
    mode: int
    operation: GeneratedOperation
    family: TestFamily


class MergedGeneratedSet(FrozenModel):
    files: tuple[GeneratedFileV2, ...]
    digest: str

    @property
    def sources(self) -> Mapping[str, str]:
        return MappingProxyType({item.target_path: item.staged_path for item in self.files})


def staged_generated_path(target_path: str) -> str:
    return durable_test_path(_safe_target(target_path))


def merge_generated(
    project_root: Path,
    change_id: str,
    families: tuple[str, ...],
) -> MergedGeneratedSet:
    project = Path(project_root)
    _safe_component(change_id, label="change_id")
    selected: tuple[TestFamily, ...] = tuple(_closed_family(family) for family in families)
    collected: list[GeneratedFileV2] = []
    for family in selected:
        collected.extend(_collect_family(project, family))
    merged = _collapse_ownership(collected)
    ordered = tuple(sorted(merged, key=lambda item: (item.target_path, item.family)))
    digest = canonical_digest(cast(JSONValue, [item.model_dump(mode="json") for item in ordered]))
    return MergedGeneratedSet(files=ordered, digest=digest)


def _safe_component(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path component")
    if "\x00" in value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a safe path component")
    return value


def _closed_family(family: str) -> TestFamily:
    if family not in FAMILIES:
        raise ValueError(f"unknown generation family: {family}")
    return cast(TestFamily, family)


def _safe_target(target_path: str) -> str:
    posix = PurePosixPath(target_path)
    windows = PureWindowsPath(target_path)
    if (
        not target_path
        or target_path.startswith(("/", "~"))
        or "\\" in target_path
        or "\x00" in target_path
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or posix.as_posix() != target_path
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError(f"target path must be canonical and relative: {target_path}")
    return target_path


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _collect_family(project: Path, family: TestFamily) -> tuple[GeneratedFileV2, ...]:
    manifest_relative = qa_join(f"codegen/{family}-generated-files.json")
    manifest_path = _regular_file(project, manifest_relative, missing="closed manifest")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"closed manifest is invalid: {manifest_relative}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"closed manifest is invalid: {manifest_relative}")
    listed = raw.get("files")
    if not isinstance(listed, list) or not listed:
        raise ValueError(f"closed manifest membership is empty: {family}")
    collected: list[GeneratedFileV2] = []
    for entry in listed:
        if not isinstance(entry, dict):
            raise ValueError(f"closed manifest membership is invalid: {family}")
        target = entry.get("target_path") or entry.get("repo_path")
        if not isinstance(target, str):
            raise ValueError(f"closed manifest membership is invalid: {family}")
        try:
            target = _safe_target(target)
            staged = staged_generated_path(target)
        except ValueError as error:
            raise ValueError(f"target path must be canonical and relative: {target}") from error
        path = _regular_file(project, staged, missing="closed manifest")
        payload = path.read_bytes()
        digest = _digest_bytes(payload)
        declared = entry.get("content_sha256") or entry.get("sha256")
        if isinstance(declared, str) and declared != digest:
            raise ValueError(f"closed manifest digest does not match workspace bytes: {target}")
        operation = entry.get("operation") or entry.get("disposition") or "generated"
        if operation not in {"generated", "updated", "reused"}:
            raise ValueError(f"closed manifest operation is invalid: {target}")
        collected.append(
            GeneratedFileV2(
                target_path=target,
                staged_path=staged,
                sha256=digest,
                mode=stat.S_IMODE(path.stat().st_mode),
                operation=cast(GeneratedOperation, operation),
                family=family,
            )
        )
    return tuple(collected)


def _collapse_ownership(files: tuple[GeneratedFileV2, ...] | list[GeneratedFileV2]) -> list[GeneratedFileV2]:
    grouped: dict[str, list[GeneratedFileV2]] = {}
    for item in files:
        grouped.setdefault(item.target_path, []).append(item)
    merged: list[GeneratedFileV2] = []
    for target, owners in grouped.items():
        identities = {(item.sha256, item.mode, item.operation) for item in owners}
        if len(identities) != 1:
            raise ValueError(f"conflicting generated ownership: {target}")
        merged.append(sorted(owners, key=lambda item: item.family)[0])
    return merged


def _regular_file(project: Path, relative: str, *, missing: str) -> Path:
    path = project.joinpath(*PurePosixPath(relative).parts)
    try:
        path.resolve().relative_to(project.resolve())
    except ValueError as error:
        raise ValueError(f"{missing} path escapes the project: {relative}") from error
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"{missing} member is missing: {relative}")
    return path


__all__ = [
    "FAMILIES",
    "GeneratedFileV2",
    "GeneratedOperation",
    "MergedGeneratedSet",
    "TestFamily",
    "merge_generated",
    "staged_generated_path",
]
