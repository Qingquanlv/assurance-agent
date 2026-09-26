from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("node_id", "logical_path"),
    [
        ("intake.intake", "qa/requirement.md"),
        ("intake.explore", "qa/results/explore/exploration.json"),
        ("intake.case-design", "qa/cases/system/user/case.yaml"),
        ("intake.case-review", "qa/results/review/case-review.json"),
        ("quality.fact-baseline", "qa/results/facts/fact-baseline.json"),
        ("generation.api.codegen", "qa/results/codegen/api-generated-files.json"),
        ("generation.api.codegen-review", "qa/results/review/api-codegen-review.json"),
        ("quality.inspect", "qa/results/inspect/inspection.json"),
        ("quality.issue-analyze", "qa/results/inspect/issue-analysis.json"),
        ("quality.report", "qa/results/report/report.md"),
        ("improvement.retro", "qa/results/retro/retro.json"),
        ("improvement.retro-workflow-analysis", "qa/results/retro/retro-workflow-analysis.json"),
        ("improvement.retro-issue-analysis", "qa/results/retro/retro-issue-analysis.json"),
        ("improvement.retro-eval-analysis", "qa/results/retro/retro-eval-analysis.json"),
    ],
)
def test_current_node_outputs_cover_each_agent_panel(tmp_path: Path, node_id: str, logical_path: str) -> None:
    from assurance_product.current_node_output import list_current_node_outputs

    path = tmp_path / logical_path
    path.parent.mkdir(parents=True)
    path.write_text("current", encoding="utf-8")

    outputs = list_current_node_outputs(tmp_path, f"{node_id}/finalize")["outputs"]
    assert [item["logical_path"] for item in outputs] == [logical_path]


def test_current_node_outputs_are_scoped_to_the_node(tmp_path: Path) -> None:
    from assurance_product.current_node_output import list_current_node_outputs

    cases = tmp_path / "qa" / "cases" / "system" / "user"
    cases.mkdir(parents=True)
    (cases / "case.yaml").write_text("case: current\n", encoding="utf-8")
    review = tmp_path / "qa" / "results" / "review"
    review.mkdir(parents=True)
    (review / "case-review.json").write_text('{"status":"current"}', encoding="utf-8")

    design = list_current_node_outputs(tmp_path, "intake.case-design/finalize")
    reviewed = list_current_node_outputs(tmp_path, "intake.case-review/finalize")

    assert design["source"] == "current_worktree"
    assert [item["logical_path"] for item in design["outputs"]] == ["qa/cases/system/user/case.yaml"]
    assert [item["logical_path"] for item in reviewed["outputs"]] == ["qa/results/review/case-review.json"]


def test_current_node_outputs_put_the_primary_document_first(tmp_path: Path) -> None:
    from assurance_product.current_node_output import list_current_node_outputs

    for path in (
        "qa/.qa.yaml",
        "qa/requirement.md",
        "qa/results/retro/context.json",
        "qa/results/retro/retro-workflow-analysis.json",
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("current", encoding="utf-8")

    intake = list_current_node_outputs(tmp_path, "intake.intake")["outputs"]
    retro = list_current_node_outputs(tmp_path, "improvement.retro-workflow-analysis")["outputs"]
    assert intake[0]["logical_path"] == "qa/requirement.md"
    assert retro[0]["logical_path"] == "qa/results/retro/retro-workflow-analysis.json"


def test_current_node_preview_is_bounded_and_rejects_another_node_output(tmp_path: Path) -> None:
    from assurance_product.current_node_output import (
        CurrentNodeOutputError,
        list_current_node_outputs,
        preview_current_node_output,
    )

    explore = tmp_path / "qa" / "results" / "explore"
    explore.mkdir(parents=True)
    (explore / "exploration.json").write_text("x" * 64_005, encoding="utf-8")
    output = list_current_node_outputs(tmp_path, "intake.explore")["outputs"][0]

    preview = preview_current_node_output(tmp_path, "intake.explore", output["output_id"])
    assert preview["preview"] == "x" * 64_000
    assert preview["truncated"] is True
    with pytest.raises(CurrentNodeOutputError, match="not available for this node"):
        preview_current_node_output(tmp_path, "intake.case-design", output["output_id"])


def test_current_node_outputs_do_not_follow_symlinks_outside_qa(tmp_path: Path) -> None:
    from assurance_product.current_node_output import list_current_node_outputs

    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    report = tmp_path / "qa" / "results" / "report"
    report.mkdir(parents=True)
    (report / "report.md").symlink_to(outside)

    assert list_current_node_outputs(tmp_path, "quality.report")["outputs"] == []


def test_current_node_outputs_do_not_follow_a_linked_qa_root(tmp_path: Path) -> None:
    from assurance_product.current_node_output import list_current_node_outputs

    external = tmp_path / "external" / "results" / "report"
    external.mkdir(parents=True)
    (external / "report.md").write_text("private", encoding="utf-8")
    (tmp_path / "qa").symlink_to(tmp_path / "external", target_is_directory=True)

    assert list_current_node_outputs(tmp_path, "quality.report")["outputs"] == []


def test_operator_current_output_lists_and_previews(cli_runner, tmp_path: Path) -> None:
    from assurance_product.cli import app

    report = tmp_path / "qa" / "results" / "report" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Current report\n", encoding="utf-8")
    listed = cli_runner.invoke(
        app,
        [
            "operator",
            "current-output",
            "--project-dir",
            str(tmp_path),
            "--node-id",
            "quality.report/finalize",
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    listing = json.loads(listed.output)
    assert listing["source"] == "current_worktree"
    output_id = listing["outputs"][0]["output_id"]

    preview = cli_runner.invoke(
        app,
        [
            "operator",
            "current-output",
            "--project-dir",
            str(tmp_path),
            "--node-id",
            "quality.report/finalize",
            "--output-id",
            output_id,
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["preview"] == "# Current report\n"
