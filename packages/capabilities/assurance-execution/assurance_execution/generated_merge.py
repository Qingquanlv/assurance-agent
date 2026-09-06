from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal, Self, cast

from pydantic import Field, field_validator, model_validator

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


class ExecutionViewInputV1(FrozenModel):
    """Closed inputs needed to build a verified test-only execution view."""

    schema_version: Literal["1"] = "1"
    change_id: str
    batch_id: str
    execution_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    selected: tuple[str, ...] = Field(min_length=1)
    generated_files: tuple[GeneratedFileV2, ...] = Field(min_length=1)
    generated_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("change_id", "batch_id")
    @classmethod
    def _safe_identity_component(cls, value: str, info: Any) -> str:
        return _safe_component(value, label=info.field_name)

    @field_validator("selected")
    @classmethod
    def _closed_selected(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any("::" not in item or "\n" in item for item in value):
            raise ValueError("selected nodeids must be full and unique")
        for nodeid in value:
            _safe_target(nodeid.split("::", 1)[0])
        return value

    @model_validator(mode="after")
    def _authenticated_descriptors(self) -> Self:
        ordered = tuple(sorted(self.generated_files, key=lambda item: (item.target_path, item.family)))
        if self.generated_files != ordered:
            raise ValueError("generated descriptors must be canonically ordered")
        digest = canonical_digest(
            cast(JSONValue, [item.model_dump(mode="json") for item in self.generated_files])
        )
        if digest != self.generated_digest:
            raise ValueError("generated descriptor digest does not match")
        if any(
            item.staged_path != staged_generated_path(self.change_id, item.family, item.target_path)
            for item in self.generated_files
        ):
            raise ValueError("generated descriptor change identity does not match")
        return self


class MergedGeneratedSet(FrozenModel):
    files: tuple[GeneratedFileV2, ...]
    digest: str

    @property
    def sources(self) -> Mapping[str, str]:
        return MappingProxyType({item.target_path: item.staged_path for item in self.files})

    def execution_view_input(
        self,
        *,
        change_id: str,
        batch_id: str,
        execution_id: str,
        selected: tuple[str, ...],
    ) -> ExecutionViewInputV1:
        return ExecutionViewInputV1(
            change_id=change_id,
            batch_id=batch_id,
            execution_id=execution_id,
            selected=selected,
            generated_files=self.files,
            generated_digest=self.digest,
        )


def staged_generated_path(change_id: str, family: str, target_path: str) -> str:
    return (
        f"qa/changes/{_safe_component(change_id, label='change_id')}"
        f"/generated/{_closed_family(family)}/files/{_safe_target(target_path)}"
    )


def merge_generated(
    project_root: Path,
    change_id: str,
    families: tuple[str, ...],
) -> MergedGeneratedSet:
    project = Path(project_root)
    closed_change = _safe_component(change_id, label="change_id")
    selected: tuple[TestFamily, ...] = tuple(_closed_family(family) for family in families)
    collected: list[GeneratedFileV2] = []
    for family in selected:
        collected.extend(_collect_family(project, closed_change, family))
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


def _collect_family(project: Path, change_id: str, family: TestFamily) -> tuple[GeneratedFileV2, ...]:
    manifest_relative = f"qa/changes/{change_id}/codegen/{family}-generated-files.json"
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
    listed_targets: set[str] = set()
    for entry in listed:
        if not isinstance(entry, dict):
            raise ValueError(f"closed manifest membership is invalid: {family}")
        target = entry.get("target_path") or entry.get("repo_path")
        if not isinstance(target, str):
            raise ValueError(f"closed manifest membership is invalid: {family}")
        try:
            target = _safe_target(target)
            staged = staged_generated_path(change_id, family, target)
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
        listed_targets.add(target)
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
    _reject_extras(project, change_id, family, listed_targets)
    return tuple(collected)


def _reject_extras(project: Path, change_id: str, family: TestFamily, listed: set[str]) -> None:
    root = project.joinpath(*PurePosixPath(f"qa/changes/{change_id}/generated/{family}/files").parts)
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"generated family namespace is invalid: {family}")
    extras: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"generated family namespace contains a symlink: {family}")
        if not path.is_file():
            continue
        target = path.relative_to(root).as_posix()
        if target not in listed:
            extras.append(target)
    if extras:
        raise ValueError(f"extra on-disk generated file: {extras[0]}")


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
    "ExecutionViewInputV1",
    "GeneratedFileV2",
    "GeneratedOperation",
    "MergedGeneratedSet",
    "TestFamily",
    "merge_generated",
    "staged_generated_path",
]
