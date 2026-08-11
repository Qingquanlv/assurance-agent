"""Executable Case Design knowledge-proposal boundaries."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from assurance_agent import resources
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.workflow.graph.contracts import (
    ResourceClaims,
    ResourcePath,
    claims_conflict,
    load_execution_contracts,
)
from assurance_agent.workflow.graph.schema_v2 import NodeDef
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend, WorkspaceError


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return project


def _case_design_claims(project: Path) -> ResourceClaims:
    return load_execution_contracts(project).claims_for(NodeDef(uses="skill:aa-case-design"))


def test_case_design_declares_only_the_expected_write_boundaries(tmp_path: Path) -> None:
    claims = _case_design_claims(_project(tmp_path))
    expected = {
        ("change", ".qa.yaml"),
        ("change", "proposal.md"),
        ("change", "cases/**"),
        ("change", "trace/minimum-coverage-matrix.yaml"),
        ("change", "plans/data-knowledge.proposal.api.yaml"),
        ("change", "plans/data-knowledge.proposal.e2e.yaml"),
    }

    assert {(path.root, path.pattern) for path in claims.writes} == expected
    assert {(path.root, path.pattern) for path in claims.authorization_writes} == expected


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_case_design_freeze_allows_exact_knowledge_proposal_leaf(
    tmp_path: Path,
    layer: str,
) -> None:
    """Catch Case Design contracts that cannot emit the proposal the Skill requires."""
    project = _project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id=f"case-design-{layer}",
        base_tree_id=store.capture(project),
        store=store,
    )
    proposal = workspace.change_dir / "plans" / f"data-knowledge.proposal.{layer}.yaml"
    proposal.parent.mkdir(parents=True)
    proposal.write_text('schema_version: "1"\nmode: delta\n', encoding="utf-8")

    logical_path = f"change:plans/data-knowledge.proposal.{layer}.yaml"
    claims = _case_design_claims(project)
    proposal_reader = ResourceClaims(reads=(ResourcePath.parse(logical_path),))

    assert claims_conflict(claims, proposal_reader)
    write_set = store.freeze_write_set(workspace, claims=claims)

    assert [entry.logical_path for entry in write_set.entries] == [logical_path]


@pytest.mark.parametrize(
    "relative_path",
    [
        "plans/api-plan.md",
        "plans/data-knowledge.proposal.fuzz.yaml",
        "plans/data-knowledge.proposal.api.yaml.bak",
    ],
)
def test_case_design_freeze_still_rejects_unrelated_or_lookalike_plan_leaf(
    tmp_path: Path,
    relative_path: str,
) -> None:
    """Catch broad plans authorization added in place of exact proposal leaves."""
    project = _project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    workspace = WorkspaceBackend(change).create(
        task_id="case-design-unrelated-plan",
        base_tree_id=store.capture(project),
        store=store,
    )
    plan = workspace.change_dir / relative_path
    plan.parent.mkdir(parents=True)
    plan.write_text("# API plan\n", encoding="utf-8")

    with pytest.raises(
        WorkspaceError,
        match=r"forbidden write outside authorization_writes: change:plans/",
    ):
        store.freeze_write_set(workspace, claims=_case_design_claims(project))


def test_case_design_canonical_missing_key_proposal_matches_runtime_schema() -> None:
    """Catch examples that teach Case Design an envelope runtime will reject."""
    skill = resources.read_text("skills", "aa-case-design", "SKILL.md")
    match = re.search(
        r"### Canonical missing-key proposal\s+```yaml\s+(.*?)\s+```",
        skill,
        re.DOTALL,
    )

    assert match is not None, "aa-case-design must provide a canonical missing-key proposal"
    proposal = DataKnowledgeProposal.model_validate(yaml.safe_load(match.group(1)))

    assert proposal.mode == "delta"
    assert proposal.based_on_l1_version == 1
    assert proposal.discovered_candidates == [
        {
            "id": "DK-CASE-USER-001",
            "knowledge_key": "entities.user.constraints.password_reset_requires_current_password",
            "evidence": "app/api/v1/user/user.py:reset_password",
        }
    ]
