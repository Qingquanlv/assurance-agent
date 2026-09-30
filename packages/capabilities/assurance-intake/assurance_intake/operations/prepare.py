"""Intake Agent request construction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runtime_contracts import AgentRunRequest, ResultContract
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    result_contract_from,
    skill_request,
)
from graph_engine.plugin_api import TaskContext

from assurance_intake.contracts.explore import EXPLORE_AGENT_OUTPUT_PATHS
from assurance_intake.resource_loader import resource_bytes, resource_text

INTAKE_SKILL = "skills/aa-intake/SKILL.md"
INTAKE_PERSONA = "personas/intake-host.md"
EXPLORE_SKILL = "skills/aa-explore/SKILL.md"
EXPLORE_PERSONA = "personas/explorer.md"
CASE_DESIGN_SKILL = "skills/aa-case-design/SKILL.md"
CASE_DESIGN_REPAIR_SKILL = "skills/aa-case-repair/SKILL.md"
CASE_DESIGN_PERSONA = "personas/doc-author.md"
CASE_REVIEW_SKILL = "skills/aa-case-reviewer/SKILL.md"
CASE_REVIEW_PERSONA = "personas/reviewer.md"

INTAKE_RESULT_ID = "assurance.intake.result.intake.v1"
EXPLORE_RESULT_ID = "assurance.intake.result.explore.v1"
CASE_DESIGN_RESULT_ID = "assurance.intake.result.case-design.v1"
CASE_REVIEW_RESULT_ID = "assurance.intake.result.case-review.v1"

_RESULT_FILES: Mapping[str, str] = {
    INTAKE_RESULT_ID: "result-contracts/intake.v1.schema.json",
    EXPLORE_RESULT_ID: "result-contracts/explore.v1.schema.json",
    CASE_DESIGN_RESULT_ID: "result-contracts/case-design.v1.schema.json",
    CASE_REVIEW_RESULT_ID: "result-contracts/case-review.v1.schema.json",
}


def intake_outputs(change_id: str) -> tuple[str, ...]:
    del change_id
    return ("qa/.qa.yaml",)


def explore_outputs(change_id: str) -> tuple[str, ...]:
    del change_id
    return EXPLORE_AGENT_OUTPUT_PATHS


def case_design_outputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                *case_delta_paths,
                "qa/.qa.yaml",
                "qa/proposal.md",
                "qa/results/trace/minimum-coverage-matrix.json",
            )
        )
    )


def case_review_outputs(
    change_id: str,
    *,
    coverage_epoch: int = 0,
    review_round: int = 0,
) -> tuple[str, ...]:
    del change_id, coverage_epoch, review_round
    return tuple(
        sorted(
            (
                "qa/results/review/case-review.json",
                "qa/results/review/case-review-summary.md",
            )
        )
    )


def case_review_inputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    change_root = "qa"
    return tuple(
        sorted(
            (
                f"{change_root}/.qa.yaml",
                *case_delta_paths,
                f"{change_root}/proposal.md",
                f"{change_root}/requirement.md",
                f"{change_root}/results/trace/minimum-coverage-matrix.json",
            )
        )
    )


def result_contract(schema_id: str) -> ResultContract:
    return result_contract_from(schema_id, json.loads(resource_bytes(_RESULT_FILES[schema_id])))


def prepare_request(
    *,
    skill_path: str,
    persona_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
    planning_facts: dict[str, Any] | None = None,
) -> AgentRunRequest:
    return skill_request(
        skill_text=resource_text(skill_path),
        persona_text=resource_text(persona_path),
        business=business,
        business_extra=None if planning_facts is None else {"planning_facts": planning_facts},
        binding=binding,
        result=result_contract(result_schema_id),
        roots=context,
        allowed_outputs=allowed_outputs,
        scope_id=business.change_id,
    )


def materialize_requirement(text: str) -> bytes:
    return (text.removesuffix("\n") + "\n").encode("utf-8")


def write_prepare_file(write_root: Path, relative: str, data: bytes) -> None:
    path = write_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
