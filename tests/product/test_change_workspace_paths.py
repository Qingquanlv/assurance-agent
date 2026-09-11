from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def change_workspace():
    import assurance_product.change_workspace as module

    return module


def make_project(tmp_path: Path) -> Path:
    project = (tmp_path / "project").resolve()
    (project / "qa").mkdir(parents=True)
    return project


def test_open_resolves_flat_qa_paths_without_creating_directories(tmp_path: Path, change_workspace) -> None:
    project = make_project(tmp_path)

    workspace = change_workspace.ChangeWorkspace.open(project, "BENCH-dept-001")

    assert workspace.paths.project_root == project
    assert workspace.paths.qa_root == project / "qa"
    assert workspace.paths.staging_root == project / "qa" / ".staging"
    assert workspace.paths.runtime_root == project / "qa" / ".runtime"
    assert workspace.paths.tests_root == project / "qa" / "tests"
    assert workspace.paths.cases_root == project / "qa" / "cases"
    assert workspace.paths.fixtures_root == project / "qa" / "fixtures"
    assert workspace.paths.results_root == project / "qa" / "results"
    assert workspace.paths.langgraph_root == workspace.paths.runtime_root / "langgraph"
    assert workspace.paths.langgraph_checkpoints == workspace.paths.langgraph_root / "checkpoints.sqlite3"
    assert workspace.paths.langgraph_leases == workspace.paths.langgraph_root / "leases"
    assert workspace.paths.langgraph_identities == workspace.paths.langgraph_root / "identities"
    assert workspace.change_id == "BENCH-dept-001"
    assert not hasattr(workspace.paths, "change_root")
    assert not hasattr(workspace.paths, "generated_root")
    assert not hasattr(workspace.paths, "apply_manifest")
    assert not workspace.paths.staging_root.exists()
    assert not workspace.paths.runtime_root.exists()
    assert not workspace.paths.langgraph_root.exists()
    assert not (project / "qa" / "changes").exists()


def test_prepare_creates_qa_then_runtime_and_staging(tmp_path: Path, change_workspace) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()

    workspace = change_workspace.ChangeWorkspace.prepare(project, "BENCH-dept-001")

    assert workspace.paths.qa_root.is_dir()
    assert not workspace.paths.qa_root.is_symlink()
    assert workspace.paths.staging_root.is_dir()
    assert workspace.paths.runtime_root.is_dir()
    assert {child.name for child in workspace.paths.runtime_root.iterdir()} == {
        "activities",
        "receipts",
    }
    assert workspace.change_id == "BENCH-dept-001"
    assert not (workspace.paths.qa_root / "changes").exists()


def test_initialize_creates_only_the_exact_change_workspace_directories(
    tmp_path: Path, change_workspace
) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")

    workspace.initialize()

    assert workspace.paths.staging_root.is_dir()
    assert workspace.paths.runtime_root.is_dir()
    assert {child.name for child in workspace.paths.runtime_root.iterdir()} == {
        "activities",
        "receipts",
    }
    assert not (workspace.paths.runtime_root / "ledger").exists()
    assert not workspace.paths.langgraph_root.exists()


def test_initialize_rejects_unknown_control_entries_before_mutation(tmp_path: Path, change_workspace) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    workspace.initialize()
    leftover_ledger = workspace.paths.runtime_root / "ledger"
    leftover_invocations = workspace.paths.runtime_root / "invocations"
    leftover_ledger.mkdir()
    leftover_invocations.mkdir()

    with pytest.raises(ValueError, match="incomplete layout"):
        workspace.initialize()

    assert leftover_ledger.is_dir()
    assert leftover_invocations.is_dir()


def test_initialize_allows_langgraph_control_subtree(tmp_path: Path, change_workspace) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    workspace.initialize()
    workspace.paths.langgraph_root.mkdir()
    workspace.paths.langgraph_leases.mkdir()
    workspace.paths.langgraph_identities.mkdir()
    workspace.paths.langgraph_checkpoints.write_bytes(b"")

    workspace.initialize()

    assert workspace.paths.langgraph_root.is_dir()
    assert not workspace.paths.langgraph_root.is_symlink()
    assert workspace.paths.langgraph_leases.is_dir()
    assert workspace.paths.langgraph_identities.is_dir()
    assert workspace.paths.langgraph_checkpoints.is_file()
    assert not workspace.paths.langgraph_checkpoints.is_symlink()
    assert {child.name for child in workspace.paths.runtime_root.iterdir()} == {
        "activities",
        "receipts",
        "langgraph",
    }


def test_initialize_rejects_symlinked_langgraph_and_unexpected_types(
    tmp_path: Path, change_workspace
) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    workspace.initialize()
    target = tmp_path / "langgraph-target"
    target.mkdir()
    workspace.paths.langgraph_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        workspace.initialize()

    assert workspace.paths.langgraph_root.is_symlink()
    workspace.paths.langgraph_root.unlink()
    workspace.paths.langgraph_root.mkdir()
    workspace.paths.langgraph_checkpoints.mkdir()

    with pytest.raises(ValueError):
        workspace.initialize()


def test_initialize_cleans_up_directories_created_before_a_real_obstruction(
    tmp_path: Path, change_workspace
) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    workspace.paths.staging_root.write_text("obstruction")

    with pytest.raises(ValueError):
        workspace.initialize()

    assert not workspace.paths.runtime_root.exists()
    assert workspace.paths.staging_root.is_file()


@pytest.mark.parametrize("symlink_name", [".runtime", ".staging"])
def test_initialize_rejects_symlinked_top_level_state_without_touching_target(
    tmp_path: Path, symlink_name: str, change_workspace
) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    target = tmp_path / f"{symlink_name}-target"
    target.mkdir()
    (workspace.paths.qa_root / symlink_name).symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        workspace.initialize()

    assert (workspace.paths.qa_root / symlink_name).is_symlink()
    assert target.is_dir()


def test_initialize_rejects_symlinked_runtime_child_without_touching_target(
    tmp_path: Path, change_workspace
) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    runtime = workspace.paths.runtime_root
    runtime.mkdir()
    target = tmp_path / "activities-target"
    target.mkdir()
    (runtime / "activities").symlink_to(target, target_is_directory=True)
    (runtime / "receipts").mkdir()
    workspace.paths.staging_root.mkdir()

    with pytest.raises(ValueError):
        workspace.initialize()

    assert (runtime / "activities").is_symlink()
    assert target.is_dir()


def test_initialize_rolls_back_directories_after_mid_creation_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_workspace
) -> None:
    workspace = change_workspace.ChangeWorkspace.open(make_project(tmp_path), "BENCH-dept-001")
    original_mkdir = change_workspace._mkdir

    def fail_at_staging(path: Path) -> None:
        if path == workspace.paths.staging_root:
            raise OSError("injected obstruction")
        original_mkdir(path)

    monkeypatch.setattr(change_workspace, "_mkdir", fail_at_staging)

    with pytest.raises(ValueError):
        workspace.initialize()

    assert not workspace.paths.runtime_root.exists()
    assert not workspace.paths.staging_root.exists()


@pytest.mark.parametrize("project", [Path("relative"), Path("missing")])
def test_open_rejects_noncanonical_or_missing_project(
    tmp_path: Path, project: Path, change_workspace
) -> None:
    if project.name == "missing":
        project = tmp_path / project
    with pytest.raises(ValueError):
        change_workspace.ChangeWorkspace.open(project, "BENCH-dept-001")


def test_open_rejects_a_file_as_project(tmp_path: Path, change_workspace) -> None:
    project = tmp_path / "project-file"
    project.write_text("not a directory")

    with pytest.raises(ValueError):
        change_workspace.ChangeWorkspace.open(project, "BENCH-dept-001")


def test_open_rejects_missing_qa_and_does_not_require_qa_changes(tmp_path: Path, change_workspace) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    with pytest.raises(ValueError):
        change_workspace.ChangeWorkspace.open(project, "BENCH-dept-001")

    (project / "qa").mkdir()
    workspace = change_workspace.ChangeWorkspace.open(project, "BENCH-dept-001")
    assert workspace.paths.qa_root == project / "qa"
    assert workspace.change_id == "BENCH-dept-001"


def test_open_rejects_symlinked_project_and_qa(tmp_path: Path, change_workspace) -> None:
    real_project = make_project(tmp_path)
    project_link = tmp_path / "project-link"
    project_link.symlink_to(real_project, target_is_directory=True)
    with pytest.raises(ValueError):
        change_workspace.ChangeWorkspace.open(project_link, "BENCH-dept-001")

    qa = real_project / "qa"
    target = tmp_path / "other-qa"
    target.mkdir()
    qa.rmdir()
    qa.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        change_workspace.ChangeWorkspace.open(real_project, "BENCH-dept-001")


@pytest.mark.parametrize("change_id", ["/", "..", "../escape", "nested/id", "NUL\x00id", ""])
def test_safe_change_id_rejects_absolute_separator_and_invalid_ids(change_id: str, change_workspace) -> None:
    with pytest.raises(ValueError):
        change_workspace.safe_change_id(change_id)


def test_safe_change_id_accepts_a_single_safe_component(change_workspace) -> None:
    assert change_workspace.safe_change_id("BENCH-dept-001") == "BENCH-dept-001"


def test_open_validates_change_id_without_joining_it_into_paths(tmp_path: Path, change_workspace) -> None:
    project = make_project(tmp_path)

    with pytest.raises(ValueError):
        change_workspace.ChangeWorkspace.open(project, "../escape")

    workspace = change_workspace.ChangeWorkspace.open(project, "BENCH-dept-001")
    assert "BENCH-dept-001" not in workspace.paths.qa_root.parts
    assert "BENCH-dept-001" not in workspace.paths.staging_root.parts
    assert workspace.change_id == "BENCH-dept-001"


@pytest.mark.parametrize("output", ["../escape.txt", "/tmp/escape.txt", "nested/../../escape", "bad\x00name"])
def test_safe_relative_path_rejects_paths_escaping_project(output: str, change_workspace) -> None:
    with pytest.raises(ValueError):
        change_workspace.safe_relative_path(output)


def test_safe_relative_path_returns_a_relative_path(change_workspace) -> None:
    assert change_workspace.safe_relative_path("generated/report.json") == Path("generated/report.json")
