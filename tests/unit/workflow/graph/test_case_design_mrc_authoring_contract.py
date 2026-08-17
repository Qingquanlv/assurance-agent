"""Case Design must see every runtime-enforced MRC authoring invariant."""

from pathlib import Path

from assurance_agent import resources
from assurance_agent.verification.contract_render import render_output_contract
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2


MATRIX_OUTPUT = "change:trace/minimum-coverage-matrix.json"


def test_case_design_declares_the_mrc_matrix_as_an_output() -> None:
    schema = load_workflow_v2(Path.cwd())

    assert MATRIX_OUTPUT in schema.graphs["intake"].nodes["case-design"].outputs


def test_case_design_prompt_declares_unique_mrc_row_identity() -> None:
    schema = load_workflow_v2(Path.cwd())
    outputs = schema.graphs["intake"].nodes["case-design"].outputs

    clause = render_output_contract(outputs)

    assert "trace/minimum-coverage-matrix.json must be a MinimumCoverageMatrix" in clause
    assert "mrc_id and key must each be unique across the entire matrix" in clause


def test_case_design_skill_self_review_requires_unique_mrc_row_identity() -> None:
    skill = resources.read_text("skills", "aa-case-design", "SKILL.md")

    assert "mrc_id and key must each be unique across the entire matrix" in skill
    assert "duplicate mrc_id or key" in skill
