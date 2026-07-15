from pathlib import Path

from assurance_agent.workflow.execution.selection import resolve_selected_targets


def test_defaults_to_all_when_no_state_or_plans(tmp_path: Path) -> None:
    targets = resolve_selected_targets(tmp_path)
    assert (targets.api, targets.e2e, targets.fuzz, targets.performance) == (True, True, True, True)


def test_reads_selected_targets_from_workflow_state(tmp_path: Path) -> None:
    (tmp_path / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    targets = resolve_selected_targets(tmp_path)
    assert targets.api is True
    assert targets.e2e is False
    assert targets.fuzz is False


def test_falls_back_to_layers_key(tmp_path: Path) -> None:
    (tmp_path / "workflow-state.yaml").write_text(
        "layers:\n  api: false\n  e2e: true\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    targets = resolve_selected_targets(tmp_path)
    assert targets.e2e is True
    assert targets.api is False


def test_plan_presence_selects_targets(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "api-codegen-plan.md").write_text("plan", encoding="utf-8")
    (plans / "e2e-codegen-plan.md").write_text("plan", encoding="utf-8")
    targets = resolve_selected_targets(tmp_path)
    assert targets.api is True
    assert targets.e2e is True
    assert targets.fuzz is False
    assert targets.performance is False
