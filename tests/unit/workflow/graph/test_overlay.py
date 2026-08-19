from pathlib import Path

import pytest

from assurance_agent.workflow.graph.overlay import OverlayPathError, resolve_explicit_overlay


def test_explicit_overlay_accepts_aa_and_schemas_files(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / "schemas").mkdir()
    aa_file = tmp_path / ".aa" / "workflow-schema.yaml"
    schemas_file = tmp_path / "schemas" / "custom.yaml"
    aa_file.write_text("name: aa\n", encoding="utf-8")
    schemas_file.write_text("name: schemas\n", encoding="utf-8")

    assert resolve_explicit_overlay(tmp_path, Path(".aa/workflow-schema.yaml")) == aa_file.resolve()
    assert resolve_explicit_overlay(tmp_path, schemas_file) == schemas_file.resolve()


def test_explicit_overlay_rejects_missing_file(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    with pytest.raises(OverlayPathError, match="not found"):
        resolve_explicit_overlay(tmp_path, Path(".aa/missing.yaml"))


def test_explicit_overlay_rejects_path_outside_aa_or_schemas(tmp_path: Path) -> None:
    outside = tmp_path / "custom" / "workflow.yaml"
    outside.parent.mkdir()
    outside.write_text("name: outside\n", encoding="utf-8")
    with pytest.raises(OverlayPathError, match="must be under .aa/ or schemas/"):
        resolve_explicit_overlay(tmp_path, outside)


def test_explicit_overlay_rejects_parent_escape(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    secret = tmp_path / "secret.yaml"
    secret.write_text("name: secret\n", encoding="utf-8")
    with pytest.raises(OverlayPathError, match="must be under .aa/ or schemas/"):
        resolve_explicit_overlay(tmp_path, Path(".aa/../secret.yaml"))
