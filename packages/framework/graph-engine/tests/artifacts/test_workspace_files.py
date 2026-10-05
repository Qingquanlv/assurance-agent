from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import BaseModel

from graph_engine.artifacts import stage_json_artifact, under_root


class _Note(BaseModel):
    text: str


def test_under_root_matches_a_root_or_its_children() -> None:
    assert under_root("qa", ("qa",))
    assert under_root("qa/cases/a.json", ("qa/",))
    assert under_root("report/a.json", ("report/", "qa/"))
    assert not under_root("qa-extra/a.json", ("qa",))
    assert not under_root("other/a.json", ("qa/", "report/"))
    assert not under_root("/qa/a.json", ("qa",))


def test_stage_json_artifact_is_idempotent_for_identical_documents(tmp_path: Path) -> None:
    first = stage_json_artifact(tmp_path, "qa/note.json", _Note(text="a"))
    second = stage_json_artifact(tmp_path, "qa/note.json", _Note(text="a"))

    staged = (tmp_path / "qa" / "note.json").read_bytes()
    assert first == second
    assert first.digest == hashlib.sha256(staged).hexdigest()
    assert staged == b'{"text":"a"}\n'


def test_stage_json_artifact_rejects_different_bytes_symlinks_and_non_files(tmp_path: Path) -> None:
    stage_json_artifact(tmp_path, "qa/note.json", _Note(text="a"))
    with pytest.raises(ValueError, match="different bytes"):
        stage_json_artifact(tmp_path, "qa/note.json", _Note(text="b"))
    with pytest.raises(ValueError, match="canonical and relative"):
        stage_json_artifact(tmp_path, "../escape.json", _Note(text="a"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        stage_json_artifact(tmp_path, "link/note.json", _Note(text="a"))
    (tmp_path / "qa" / "dir").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        stage_json_artifact(tmp_path, "qa/dir", _Note(text="a"))
