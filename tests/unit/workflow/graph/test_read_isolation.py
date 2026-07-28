from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend


@pytest.mark.parametrize("domain", ("issue", "workflow", "eval"))
def test_retro_analyzer_sees_only_its_declared_slice_and_skill_bundle(
    tmp_path: Path,
    domain: str,
) -> None:
    project = tmp_path / "project"
    change_dir = project / "qa" / "changes" / "RETRO-RUN"
    change_dir.mkdir(parents=True)
    retro_dir = project / "qa" / "retro" / "retro-1"
    evidence_dir = retro_dir / "evidence"
    evidence_dir.mkdir(parents=True)
    for candidate_domain in ("issue", "workflow", "eval"):
        (evidence_dir / f"{candidate_domain}-slice.json").write_text(
            f'{{"domain":"{candidate_domain}"}}\n',
            encoding="utf-8",
        )
    issue_ledger = project / "qa" / "issues" / "events.jsonl"
    issue_ledger.parent.mkdir(parents=True)
    issue_ledger.write_text('{"secret":"mutable ledger"}\n', encoding="utf-8")
    skill_name = f"aa-retro-{domain}-analysis"
    skill_path = project / "skills" / skill_name / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("domain analysis instructions\n", encoding="utf-8")

    store = TreeStore(change_dir)
    tree_id = store.capture(project)
    claims = ResourceClaims(
        reads=(ResourcePath.parse(f"project:qa/retro/retro-1/evidence/{domain}-slice.json"),),
        writes=(ResourcePath.parse(f"project:qa/retro/retro-1/signals/{domain}.json"),),
        authorization_writes=(ResourcePath.parse(f"project:qa/retro/retro-1/signals/{domain}.json"),),
    )

    workspace = WorkspaceBackend(change_dir).create(
        task_id=f"analyze-{domain}",
        base_tree_id=tree_id,
        store=store,
        claims=claims,
        declared_reads_only=True,
        skill_name=skill_name,
    )

    assert (workspace.project_root / f"qa/retro/retro-1/evidence/{domain}-slice.json").is_file()
    assert (workspace.project_root / f"skills/{skill_name}/SKILL.md").is_file()
    assert not (workspace.project_root / "qa/issues").exists()
    for other_domain in {"issue", "workflow", "eval"} - {domain}:
        assert not (workspace.project_root / f"qa/retro/retro-1/evidence/{other_domain}-slice.json").exists()


def test_workspace_without_declared_read_isolation_keeps_unclaimed_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change_dir = project / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    app_file = project / "app" / "source.py"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("visible = True\n", encoding="utf-8")
    declared = project / "qa" / "retro" / "retro-1" / "context.json"
    declared.parent.mkdir(parents=True)
    declared.write_text("{}\n", encoding="utf-8")

    store = TreeStore(change_dir)
    workspace = WorkspaceBackend(change_dir).create(
        task_id="ordinary-target",
        base_tree_id=store.capture(project),
        store=store,
        claims=ResourceClaims(reads=(ResourcePath.parse("project:qa/retro/retro-1/context.json"),)),
    )

    assert (workspace.project_root / "app/source.py").is_file()
