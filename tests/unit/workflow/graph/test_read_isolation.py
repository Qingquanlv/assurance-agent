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


def test_mechanical_declared_reads_hide_cross_layer_inputs_and_forbid_cross_layer_checks(
    tmp_path: Path,
) -> None:
    from assurance_agent.workflow.graph.workspace import WorkspaceError

    project = tmp_path / "project"
    change_dir = project / "qa" / "changes" / "CH-FUZZ"
    change_dir.mkdir(parents=True)

    fuzz_plan = change_dir / "plans" / "fuzz-plan.md"
    fuzz_plan.parent.mkdir(parents=True)
    fuzz_plan.write_text("# fuzz plan\n", encoding="utf-8")
    fuzz_review = change_dir / "review" / "fuzz-plan-review.json"
    fuzz_review.parent.mkdir(parents=True)
    fuzz_review.write_text('{"decision":"pass"}\n', encoding="utf-8")
    cross_plan = change_dir / "plans" / "api-plan.md"
    cross_plan.write_text("# api plan\n", encoding="utf-8")
    cross_review = change_dir / "review" / "api-plan-review.json"
    cross_review.write_text('{"decision":"pass"}\n', encoding="utf-8")
    cross_checks = change_dir / "review" / "api-plan-checks.json"
    cross_checks.write_text('{"schema_version":"2"}\n', encoding="utf-8")
    cases = change_dir / "cases" / "FUZZ-001" / "case.yaml"
    cases.parent.mkdir(parents=True)
    cases.write_text("schema_version: '1.0'\n", encoding="utf-8")
    repo_l1 = project / ".aa" / "data-knowledge.yaml"
    repo_l1.parent.mkdir(parents=True)
    repo_l1.write_text("version: 1\ncapabilities: {}\n", encoding="utf-8")

    store = TreeStore(change_dir)
    tree_id = store.capture(project)
    checks = "change:review/fuzz-plan-checks.json"
    claims = ResourceClaims(
        reads=(
            ResourcePath.parse("change:plans/fuzz-plan.md"),
            ResourcePath.parse("change:plans/fuzz-codegen-plan.md"),
            ResourcePath.parse("change:review/fuzz-plan-review.json"),
            ResourcePath.parse("change:cases/**"),
            ResourcePath.parse("repo:.aa/data-knowledge.yaml"),
        ),
        writes=(ResourcePath.parse(checks),),
        authorization_writes=(ResourcePath.parse(checks),),
        synchronized=(ResourcePath.parse("project:.aa/data-knowledge.yaml"),),
        exclusive=("project:data-knowledge",),
    )
    workspace = WorkspaceBackend(change_dir).create(
        task_id="mechanical-fuzz",
        base_tree_id=tree_id,
        store=store,
        claims=claims,
        declared_reads_only=True,
    )

    assert (workspace.change_dir / "plans" / "fuzz-plan.md").is_file()
    assert (workspace.change_dir / "review" / "fuzz-plan-review.json").is_file()
    assert not (workspace.change_dir / "plans" / "api-plan.md").exists()
    assert not (workspace.change_dir / "review" / "api-plan-review.json").exists()
    assert not (workspace.change_dir / "review" / "api-plan-checks.json").exists()

    leaked = workspace.change_dir / "review" / "api-plan-checks.json"
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_text('{"leaked":true}\n', encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"forbidden write outside authorization_writes"):
        store.freeze_write_set(workspace, claims=claims)
