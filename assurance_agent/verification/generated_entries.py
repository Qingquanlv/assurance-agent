"""Strict per-layer plan/case mapping extraction for D16 generated-file authority."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.exceptions import AaError
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import table_rows
from assurance_agent.verification.profiles import get_layer_assurance_profile

MappingErrorCode = Literal[
    "missing_mapping",
    "duplicated_mapping",
    "interrupted_mapping",
    "malformed_mapping",
    "wrong_layer_mapping",
    "unmapped_table",
    "missing_schema_acquisition",
    "schema_case_mismatch",
]

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_BACKTICK_RE = re.compile(r"^`([^`]+)`$")


class MappingExtractionError(AaError):
    """Plan/case mapping cannot be derived under the selected layer contract."""

    def __init__(self, code: MappingErrorCode, message: str) -> None:
        self.code: MappingErrorCode = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class MappedTestEntry:
    case_id: str
    symbol: str
    target_file: str


@dataclass(frozen=True, slots=True)
class LayerMappingRelation:
    layer: LayerName
    entries: tuple[MappedTestEntry, ...]
    selected_case_ids: tuple[str, ...]
    schema_case_ids: tuple[str, ...] = ()


def extract_layer_mapping(
    *,
    layer: str,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
) -> LayerMappingRelation:
    """Derive the closed Case ID → symbol → Target File relation for one layer."""
    profile = get_layer_assurance_profile(layer)
    selected = derive_layer_applicability(cases, profile).case_ids
    if layer in {"api", "e2e"}:
        entries = _extract_named_mapping(
            plan_text,
            section="Test Function Mapping",
            required_headers=("case id", "test function", "target file"),
            layer=layer,
        )
        return LayerMappingRelation(layer=profile.layer, entries=entries, selected_case_ids=selected)
    if layer == "fuzz":
        entries = _extract_named_mapping(
            plan_text,
            section="Test Function Mapping",
            required_headers=("case id", "test function", "target file"),
            layer=layer,
        )
        schema_ids = _extract_schema_acquisition_case_ids(plan_text)
        mapped_ids = tuple(sorted({entry.case_id for entry in entries}))
        if schema_ids != mapped_ids:
            raise MappingExtractionError(
                "schema_case_mismatch",
                "Schema Acquisition case IDs must equal Test Function Mapping case IDs",
            )
        return LayerMappingRelation(
            layer=profile.layer,
            entries=entries,
            selected_case_ids=selected,
            schema_case_ids=schema_ids,
        )
    if layer == "performance":
        entries = _extract_named_mapping(
            plan_text,
            section="Task Mapping",
            required_headers=("case id", "task method", "target file"),
            layer=layer,
        )
        return LayerMappingRelation(layer=profile.layer, entries=entries, selected_case_ids=selected)
    raise MappingExtractionError("wrong_layer_mapping", f"unsupported assurance layer: {layer}")


def mapped_case_ids_for_path(relation: LayerMappingRelation, repo_path: str) -> tuple[str, ...]:
    """Return selected automated case IDs mapped to ``repo_path`` (canonical order)."""
    selected = set(relation.selected_case_ids)
    ids = sorted(
        {
            entry.case_id
            for entry in relation.entries
            if entry.target_file == repo_path and entry.case_id in selected
        }
    )
    return tuple(ids)


def selected_private_root_targets(relation: LayerMappingRelation, private_root: str) -> frozenset[str]:
    """Exact mapped target files under the selected private root."""
    prefix = private_root.rstrip("/") + "/"
    return frozenset(
        entry.target_file
        for entry in relation.entries
        if entry.case_id in set(relation.selected_case_ids)
        and (entry.target_file == private_root or entry.target_file.startswith(prefix))
    )


def _extract_named_mapping(
    plan_text: str,
    *,
    section: str,
    required_headers: tuple[str, ...],
    layer: str,
) -> tuple[MappedTestEntry, ...]:
    section_text = _extract_section(plan_text, section)
    if section_text is None:
        raise MappingExtractionError("missing_mapping", f"missing section: {section}")
    header: list[str] | None = None
    header_line: int | None = None
    rows: list[tuple[int, list[str]]] = []
    for lineno, cells in table_rows(section_text):
        lowered = [cell.lower() for cell in cells]
        if header is None and all(name in lowered for name in required_headers):
            header = lowered
            header_line = lineno
            continue
        if header is None:
            # A Case ID table that is not the required mapping is unmapped for this layer.
            if "case id" in lowered:
                raise MappingExtractionError(
                    "unmapped_table",
                    f"found Case ID table that is not {section} for layer {layer}",
                )
            continue
        if "case id" in lowered:
            raise MappingExtractionError(
                "interrupted_mapping",
                f"mapping table interrupted by a new header at line {lineno}",
            )
        rows.append((lineno, cells))

    if header is None or header_line is None:
        raise MappingExtractionError(
            "missing_mapping",
            f"{section} has no table with headers {required_headers}",
        )

    indexes = {name: header.index(name) for name in required_headers}
    width = max(indexes.values()) + 1
    seen_case: set[str] = set()
    seen_file_symbol: set[tuple[str, str]] = set()
    entries: list[MappedTestEntry] = []
    for lineno, cells in rows:
        if len(cells) < width:
            raise MappingExtractionError(
                "malformed_mapping",
                f"row at line {lineno} has too few columns",
            )
        case_id = _unwrap(cells[indexes["case id"]])
        symbol_key = "test function" if "test function" in indexes else "task method"
        symbol = _unwrap(cells[indexes[symbol_key]])
        target_file = _unwrap(cells[indexes["target file"]])
        if not case_id or not symbol or not target_file:
            raise MappingExtractionError(
                "malformed_mapping",
                f"empty mapping cell at line {lineno}",
            )
        if case_id in seen_case:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate case id {case_id!r}",
            )
        key = (target_file, symbol)
        if key in seen_file_symbol:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate symbol/path mapping {symbol!r} -> {target_file!r}",
            )
        if "\\" in target_file or target_file.startswith("/") or ".." in target_file.split("/"):
            raise MappingExtractionError(
                "malformed_mapping",
                f"unsafe target file at line {lineno}: {target_file}",
            )
        if layer == "api" and not target_file.startswith("tests/api/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"API mapping target outside tests/api: {target_file}",
            )
        if layer == "e2e" and not target_file.startswith("tests/e2e/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"E2E mapping target outside tests/e2e: {target_file}",
            )
        if layer == "fuzz" and not target_file.startswith("tests/fuzz/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"Fuzz mapping target outside tests/fuzz: {target_file}",
            )
        if layer == "performance" and not target_file.startswith("tests/perf/"):
            raise MappingExtractionError(
                "wrong_layer_mapping",
                f"Performance mapping target outside tests/perf: {target_file}",
            )
        seen_case.add(case_id)
        seen_file_symbol.add(key)
        entries.append(MappedTestEntry(case_id=case_id, symbol=symbol, target_file=target_file))
    if not entries:
        raise MappingExtractionError("missing_mapping", f"{section} table has no data rows")
    return tuple(entries)


def _extract_schema_acquisition_case_ids(plan_text: str) -> tuple[str, ...]:
    section_text = _extract_section(plan_text, "Schema Acquisition")
    if section_text is None:
        raise MappingExtractionError("missing_schema_acquisition", "missing section: Schema Acquisition")
    header: list[str] | None = None
    case_ids: list[str] = []
    seen: set[str] = set()
    for lineno, cells in table_rows(section_text):
        lowered = [cell.lower() for cell in cells]
        if header is None:
            if "case id" in lowered:
                header = lowered
            continue
        if "case id" in lowered:
            raise MappingExtractionError(
                "interrupted_mapping",
                f"Schema Acquisition interrupted at line {lineno}",
            )
        idx = header.index("case id")
        if len(cells) <= idx:
            raise MappingExtractionError(
                "malformed_mapping",
                f"Schema Acquisition row too short at line {lineno}",
            )
        case_id = _unwrap(cells[idx])
        if not case_id:
            raise MappingExtractionError(
                "malformed_mapping",
                f"empty Schema Acquisition case id at line {lineno}",
            )
        if case_id in seen:
            raise MappingExtractionError(
                "duplicated_mapping",
                f"duplicate Schema Acquisition case id {case_id!r}",
            )
        seen.add(case_id)
        case_ids.append(case_id)
    if header is None or not case_ids:
        raise MappingExtractionError(
            "missing_schema_acquisition",
            "Schema Acquisition has no Case ID rows",
        )
    return tuple(sorted(case_ids))


def _extract_section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    heading_lower = heading.strip().lower()
    start: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and match.group(1).strip().lower() == heading_lower:
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        if _HEADING_RE.match(lines[index].strip()):
            end = index
            break
    return "\n".join(lines[start:end])


def _unwrap(value: str) -> str:
    stripped = value.strip()
    match = _BACKTICK_RE.match(stripped)
    return match.group(1) if match else stripped


__all__ = [
    "LayerMappingRelation",
    "MappedTestEntry",
    "MappingExtractionError",
    "extract_layer_mapping",
    "mapped_case_ids_for_path",
    "selected_private_root_targets",
]
