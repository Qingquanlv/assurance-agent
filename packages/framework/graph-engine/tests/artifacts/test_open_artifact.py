from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from graph_engine.artifacts import ArtifactReadError, ArtifactRef, match_artifact_pattern, open_artifact


class _Payload(BaseModel):
    name: str


def _ref(path: str, data: bytes) -> ArtifactRef:
    return ArtifactRef(path=path, digest=hashlib.sha256(data).hexdigest())


def test_open_artifact_reads_bytes_and_decodes_json_or_yaml(tmp_path: Path) -> None:
    data = b'{"name":"menus"}\n'
    relative = "qa/cases/menus/case.json"
    target = tmp_path / "qa" / "cases" / "menus"
    target.mkdir(parents=True)
    (target / "case.json").write_bytes(data)
    ref = _ref(relative, data)

    assert open_artifact(tmp_path, ref) == data
    decoded = open_artifact(tmp_path, ref, model=_Payload)
    assert isinstance(decoded, _Payload)
    assert decoded.name == "menus"

    yaml_relative = "qa/cases/menus/case.yaml"
    yaml_data = b"name: menus\n"
    (target / "case.yaml").write_bytes(yaml_data)
    yaml_decoded = open_artifact(tmp_path, _ref(yaml_relative, yaml_data), model=_Payload, loader="yaml")
    assert yaml_decoded.name == "menus"


def test_open_artifact_rejects_path_traversal_and_absolute_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_bytes(b"secret")
    with pytest.raises(ArtifactReadError, match="canonical") as escaped:
        open_artifact(tmp_path, {"path": "../outside-secret.txt", "digest": "a" * 64})
    assert escaped.value.reason == "path"
    with pytest.raises(ArtifactReadError, match="canonical") as absolute:
        open_artifact(tmp_path, {"path": "/etc/passwd", "digest": "a" * 64})
    assert absolute.value.reason == "path"


def test_open_artifact_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_bytes(b"real")
    link = tmp_path / "qa"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    digest = hashlib.sha256(b"real").hexdigest()
    with pytest.raises(ArtifactReadError, match="symlink") as error:
        open_artifact(tmp_path, {"path": "qa", "digest": digest})
    assert error.value.reason == "symlink"

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "file.txt").write_bytes(b"inside")
    alias = tmp_path / "alias.txt"
    os.link(nested / "file.txt", alias)
    alias_digest = hashlib.sha256(b"inside").hexdigest()
    with pytest.raises(ArtifactReadError, match="single-link") as hard:
        open_artifact(tmp_path, {"path": "alias.txt", "digest": alias_digest})
    assert hard.value.reason == "irregular"


def test_open_artifact_rejects_digest_mismatch_and_bad_decode(tmp_path: Path) -> None:
    relative = "qa/note.txt"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"hello")
    with pytest.raises(ArtifactReadError, match="digest") as mismatch:
        open_artifact(tmp_path, {"path": relative, "digest": "b" * 64})
    assert mismatch.value.reason == "digest"
    with pytest.raises(ArtifactReadError, match="decode") as decoded:
        open_artifact(tmp_path, _ref(relative, b"hello"), model=_Payload)
    assert decoded.value.reason == "decode"


def test_case_glob_matches_nested_case_yaml_only() -> None:
    pattern = "qa/cases/**/case.yaml"
    assert match_artifact_pattern(pattern, "qa/cases/menus/case.yaml")
    assert match_artifact_pattern(pattern, "qa/cases/a/b/case.yaml")
    assert not match_artifact_pattern(pattern, "qa/proposal.md")
    assert not match_artifact_pattern(pattern, "qa/cases/menus/notes.yaml")
    assert not match_artifact_pattern(pattern, "../qa/cases/menus/case.yaml")
