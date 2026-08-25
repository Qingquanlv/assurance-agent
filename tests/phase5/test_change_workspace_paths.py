from __future__ import annotations

from pathlib import Path

import pytest

from assurance_product.change_workspace import (
    ChangeWorkspace,
    safe_change_id,
    safe_relative_path,
)


def make_project(tmp_path: Path) -> Path:
    project = (tmp_path / "project").resolve()
    (project / "qa" / "changes" / "BENCH-dept-001").mkdir(parents=True)
    return project


def test_open_resolves_change_local_paths_without_creating_directories(tmp_path: Path) -> None:
    project = make_project(tmp_path)

    workspace = ChangeWorkspace.open(project, "BENCH-dept-001")

    assert workspace.paths.project_root == project
    assert workspace.paths.change_root == project / "qa" / "changes" / "BENCH-dept-001"
    assert workspace.paths.staging_root == workspace.paths.change_root / ".staging"
    assert workspace.paths.runtime_root == workspace.paths.change_root / ".runtime"
    assert workspace.paths.generated_root == workspace.paths.change_root / "generated"
    assert workspace.paths.apply_manifest == workspace.paths.change_root / "apply-manifest.json"
    assert not workspace.paths.staging_root.exists()
    assert not workspace.paths.runtime_root.exists()


def test_initialize_creates_only_the_exact_change_workspace_directories(tmp_path: Path) -> None:
    workspace = ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")

    workspace.initialize()

    assert workspace.paths.staging_root.is_dir()
    assert workspace.paths.runtime_root.is_dir()
    assert {child.name for child in workspace.paths.runtime_root.iterdir()} == {
        "ledger",
        "activities",
        "receipts",
    }
    assert not workspace.paths.generated_root.exists()


@pytest.mark.parametrize("project", [Path("relative"), Path("missing")])
def test_open_rejects_noncanonical_or_missing_project(tmp_path: Path, project: Path) -> None:
    if project.name == "missing":
        project = tmp_path / project
    with pytest.raises(ValueError):
        ChangeWorkspace.open(project, "BENCH-dept-001")


def test_open_rejects_symlinked_project_and_qa_changes(tmp_path: Path) -> None:
    real_project = make_project(tmp_path)
    project_link = tmp_path / "project-link"
    project_link.symlink_to(real_project, target_is_directory=True)
    with pytest.raises(ValueError):
        ChangeWorkspace.open(project_link, "BENCH-dept-001")

    qa_changes = real_project / "qa" / "changes"
    target = tmp_path / "other-changes"
    target.mkdir()
    (qa_changes / "BENCH-dept-001").rmdir()
    qa_changes.rmdir()
    qa_changes.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        ChangeWorkspace.open(real_project, "BENCH-dept-001")


@pytest.mark.parametrize("change_id", ["/", "..", "../escape", "nested/id", "NUL\x00id", ""])
def test_safe_change_id_rejects_absolute_separator_and_invalid_ids(change_id: str) -> None:
    with pytest.raises(ValueError):
        safe_change_id(change_id)


def test_safe_change_id_accepts_a_single_safe_component() -> None:
    assert safe_change_id("BENCH-dept-001") == "BENCH-dept-001"


@pytest.mark.parametrize("output", ["../escape.txt", "/tmp/escape.txt", "nested/../../escape", "bad\x00name"])
def test_safe_relative_path_rejects_paths_escaping_project(output: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(output)


def test_safe_relative_path_returns_a_relative_path() -> None:
    assert safe_relative_path("generated/report.json") == Path("generated/report.json")


def test_historical_change_artifacts_are_under_qa_changes_and_tests_are_outside(tmp_path: Path) -> None:
    project = tmp_path / "legacy-shaped"
    (project / "qa" / "changes" / "CH-legacy" / "proposal.md").parent.mkdir(parents=True)
    (project / "qa" / "changes" / "CH-legacy" / "proposal.md").write_text("proposal")
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "api" / "test_item.py").write_text("def test_item(): pass\n")

    change_artifact = project / "qa" / "changes" / "CH-legacy" / "proposal.md"
    test_file = project / "tests" / "api" / "test_item.py"
    assert change_artifact.is_file()
    assert change_artifact.relative_to(project).parts[:3] == ("qa", "changes", "CH-legacy")
    assert test_file.relative_to(project).parts[:1] == ("tests",)
    assert not test_file.is_relative_to(project / "qa" / "changes" / "CH-legacy")
