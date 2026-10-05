"""Build a closed selected-test mapping from reviewed generation mappings."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError

from assurance_execution.contracts.agent import SelectInputV1
from assurance_execution.contracts.selection import ClosedMappingEntryV1, ClosedMappingV1
from assurance_execution.operations.common import leafs_of
from assurance_generation.contracts import CodegenMapping
from assurance_intake.contracts import CaseYamlAuthoring


def _capability_for_case(trace: Mapping[str, object], leafs: frozenset[str]) -> str:
    matches = tuple(sorted(key for key in trace if key in leafs))
    if not matches:
        raise InputError("unknown capability leaf: mapping case has no declared typed leaf")
    return matches[0]


def close_mappings(payload: SelectInputV1) -> ClosedMappingV1:
    leafs = leafs_of(payload.capability_leafs)
    case_ids = leafs_of(payload.case_ids)
    try:
        cases = CaseYamlAuthoring.model_validate(
            payload.reviewed_cases,
            context={"capability_leafs": leafs},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error
    by_id = {entry.case_id: entry for entry in (*cases.added, *cases.modified)}
    entries: list[ClosedMappingEntryV1] = []
    for raw_mapping in payload.mappings:
        try:
            mapping = CodegenMapping.model_validate(raw_mapping)
        except ValidationError as error:
            raise InputError(str(error)) from error
        if not getattr(payload.selected_targets, mapping.layer):
            continue
        for item in mapping.entries:
            if item.case_id not in case_ids:
                raise InputError(f"unknown case id: {item.case_id}")
            case = by_id.get(item.case_id)
            if case is None:
                raise InputError(f"unknown case id: {item.case_id}")
            entries.append(
                ClosedMappingEntryV1(
                    test=f"{item.target_file}::{item.symbol.replace('.', '::')}",
                    case_id=item.case_id,
                    capability=_capability_for_case(case.trace, leafs),
                    layer=mapping.layer,
                )
            )
    entries.sort(key=lambda entry: (entry.layer, entry.test, entry.case_id))
    selected = tuple(entry.test for entry in entries)
    try:
        return ClosedMappingV1.model_validate(
            {"selected": selected, "mappings": [entry.model_dump(mode="json") for entry in entries]},
            context={"capability_leafs": leafs, "case_ids": case_ids},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error
