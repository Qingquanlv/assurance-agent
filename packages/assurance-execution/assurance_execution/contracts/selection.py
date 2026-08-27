"""Selected-target and closed test-mapping contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from assurance_generation.contracts import LayerName
from assurance_intake.contracts import CaseId, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _safe_project_relative_path(value: str) -> str:
    if (
        not value
        or value.startswith(("/", "\\", "~"))
        or "\\" in value
        or "*" in value
        or "\x00" in value
        or (len(value) >= 2 and value[1] == ":")
    ):
        raise ValueError("test path must be a safe project-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("test path must be a safe project-relative path")
    return value


def selected_test_file(value: str) -> str:
    """Return and validate the file portion of a pytest-style test selector."""

    path, separator, symbol = value.partition("::")
    _safe_project_relative_path(path)
    if separator and (not symbol or any(not part for part in symbol.split("::"))):
        raise ValueError("test selector must contain non-empty symbol segments")
    return path


class SelectedTargets(BaseModel):
    """Which of the four execution layers were selected for a batch."""

    model_config = _FROZEN

    api: bool
    e2e: bool
    fuzz: bool
    performance: bool


class ClosedMappingEntryV1(BaseModel):
    """One selected test bound to an existing case and typed capability leaf."""

    model_config = _FROZEN

    test: NonEmptyStr
    case_id: CaseId
    capability: NonEmptyStr
    layer: LayerName

    @field_validator("test")
    @classmethod
    def _safe_test_path(cls, value: str) -> str:
        selected_test_file(value)
        return value


class ClosedMappingV1(BaseModel):
    """Closed selected-test set: every selected path appears in mappings exactly once."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    selected: tuple[NonEmptyStr, ...]
    mappings: tuple[ClosedMappingEntryV1, ...]

    @field_validator("selected")
    @classmethod
    def _safe_selected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            selected_test_file(value)
        return values

    @model_validator(mode="after")
    def _selected_equals_mappings(self, info: ValidationInfo) -> Self:
        selected = tuple(self.selected)
        mapped = tuple(entry.test for entry in self.mappings)
        if (
            len(selected) != len(set(selected))
            or len(mapped) != len(set(mapped))
            or set(selected) != set(mapped)
        ):
            raise ValueError("mapping must equal selected tests")
        context = info.context or {}
        if "capability_leafs" in context:
            leafs = context.get("capability_leafs")
            if not isinstance(leafs, frozenset) or any(not isinstance(item, str) for item in leafs):
                raise ValueError("capability_leafs context must be a frozenset of declared typed leaves")
            for entry in self.mappings:
                if entry.capability not in leafs:
                    raise ValueError(f"unknown capability leaf: {entry.capability}")
        if "case_ids" in context:
            case_ids = context.get("case_ids")
            if not isinstance(case_ids, frozenset) or any(not isinstance(item, str) for item in case_ids):
                raise ValueError("case_ids context must be a frozenset of declared case ids")
            for entry in self.mappings:
                if entry.case_id not in case_ids:
                    raise ValueError(f"unknown case id: {entry.case_id}")
        return self
