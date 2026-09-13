from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SKIP_PARTS = {"docs/superpowers/specs", "docs/superpowers/plans"}


def test_installed_python_has_no_legacy_change_tree() -> None:
    forbidden = (
        "qa/" + "changes/",
        "qa/" + "archive/",
        "qa/" + "changes",
        "qa/" + "archive",
    )
    hits: list[str] = []
    for path in (*(_ROOT / "packages").rglob("*.py"), *(_ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(_ROOT).as_posix()
        if any(part in relative for part in _SKIP_PARTS):
            continue
        text = path.read_text(encoding="utf-8")
        if any(token in text for token in forbidden):
            hits.append(relative)
    assert hits == []


def _write_status(qa: Path, change_id: str) -> None:
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "status.json").write_text(
        json.dumps({"change": {"change_id": change_id, "state": "running"}}),
        encoding="utf-8",
    )


def test_change_workspace_open_refuses_identity_mismatch(tmp_path: Path) -> None:
    from assurance_product.change_workspace import ChangeWorkspace

    project = (tmp_path / "project").resolve()
    _write_status(project / "qa", "OTHER-CHANGE")

    with pytest.raises(ValueError, match="change"):
        ChangeWorkspace.open(project, "BENCH-dept-001")


def test_change_workspace_prepare_refuses_identity_mismatch_like_aa_start(tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace

    project = (tmp_path / "project").resolve()
    project.mkdir()
    _write_status(project / "qa", "OTHER-CHANGE")

    with pytest.raises(ValueError, match="change"):
        prepare_change_workspace(project, "BENCH-dept-001")


def test_change_workspace_open_accepts_matching_identity(tmp_path: Path) -> None:
    from assurance_product.change_workspace import ChangeWorkspace

    project = (tmp_path / "project").resolve()
    _write_status(project / "qa", "BENCH-dept-001")

    workspace = ChangeWorkspace.open(project, "BENCH-dept-001")
    assert workspace.change_id == "BENCH-dept-001"
