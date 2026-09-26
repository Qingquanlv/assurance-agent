from __future__ import annotations

from pathlib import Path
from functools import partial

import pytest


def test_definition_is_stable_and_cannot_silently_change(tmp_path: Path) -> None:
    from assurance_product.task_records import define_task, read_task

    task = tmp_path / "task"
    task.mkdir()
    define = partial(
        define_task,
        project_dir=tmp_path,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )
    first = define()
    assert define().task_id == first.task_id
    assert read_task(task) == first
    with pytest.raises(ValueError, match="already configured"):
        define(requirement="Different input")


def test_definition_prepares_task_qa_directory_before_any_run(tmp_path: Path) -> None:
    from assurance_product.task_records import define_task

    task = tmp_path / "task"
    task.mkdir()
    define_task(
        project_dir=tmp_path,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )

    assert (task / "qa").is_dir()
    assert not (task / "qa" / ".qa.yaml").exists()


def test_definition_rejects_a_qa_symlink(tmp_path: Path) -> None:
    from assurance_product.task_records import define_task

    task = tmp_path / "task"
    task.mkdir()
    (task / "qa").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="qa directory must be real"):
        define_task(
            project_dir=tmp_path,
            task_directory=task,
            name="User QA",
            base_ref="main",
            requirement="Cover user CRUD.",
            families=("api",),
        )
    assert not (task / ".aa" / "task.json").exists()


def test_copied_definition_is_not_adopted(tmp_path: Path) -> None:
    from assurance_product.task_records import define_task, read_task

    task = tmp_path / "task"
    task.mkdir()
    define_task(
        project_dir=tmp_path,
        task_directory=task,
        name="User QA",
        base_ref="main",
        requirement="Cover user CRUD.",
        families=("api",),
    )
    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / ".aa").mkdir()
    (copied / ".aa" / "task.json").write_text(
        (task / ".aa" / "task.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid task identity"):
        read_task(copied)


def test_managed_records_stay_out_of_change_evidence() -> None:
    from assurance_product.task_records import managed_baseline

    visible = managed_baseline(
        (
            {"path": ".aa/task.json"},
            {"path": ".aa/runs/BOOT-1/bootstrap-status.json"},
            {"path": "src/app.py"},
        )
    )
    assert visible == ("src/app.py",)
