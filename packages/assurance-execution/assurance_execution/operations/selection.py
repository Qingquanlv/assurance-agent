"""Build a closed selected-test mapping from reviewed generation mappings."""

from __future__ import annotations

from typing import cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.agent import SelectInputV1
from assurance_execution.contracts.selection import ClosedMappingEntryV1, ClosedMappingV1
from assurance_execution.operations.common import (
    InputError,
    failed_input,
    leafs_of,
    mapping_digest,
    validate_input,
)
from assurance_generation.contracts import CodegenMapping
from assurance_intake.contracts import CaseYamlAuthoring


def _capability_for_case(trace: dict[str, object], leafs: frozenset[str]) -> str:
    matches = tuple(sorted(key for key in trace if key in leafs))
    if not matches:
        raise InputError("unknown capability leaf: mapping case has no declared typed leaf")
    return matches[0]


class SelectHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(SelectInputV1, request.input)
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
                            test=item.target_file,
                            case_id=item.case_id,
                            capability=_capability_for_case(case.trace, leafs),
                            layer=mapping.layer,
                        )
                    )
            selected = tuple(entry.test for entry in entries)
            closed = ClosedMappingV1.model_validate(
                {"selected": selected, "mappings": [entry.model_dump(mode="json") for entry in entries]},
                context={"capability_leafs": leafs, "case_ids": case_ids},
            )
            return TaskOutcome.succeeded(
                cast(
                    JSONValue,
                    {
                        "selected_targets": payload.selected_targets.model_dump(mode="json"),
                        "mapping": closed.model_dump(mode="json"),
                        "mapping_digest": mapping_digest(closed),
                    },
                )
            )
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)
