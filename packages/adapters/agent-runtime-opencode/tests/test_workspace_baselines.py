from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from graph_engine.plugin_api import StagedFile

from agent_runtime_opencode.workspace_binding import materialize_allowed_baselines


def _baseline(path: Path, logical: str) -> StagedFile:
    contents = path.read_bytes()
    return StagedFile(
        path=logical,
        before_sha256=hashlib.sha256(contents).hexdigest(),
        before_mode=path.stat().st_mode & 0o777,
    )


def test_materialize_allowed_baselines_copies_only_exact_provider_outputs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_root = tmp_path / "write-root"
    allowed = project / "change" / "case.yaml"
    excluded = project / "change" / "proposal.md"
    allowed.parent.mkdir(parents=True)
    write_root.mkdir()
    allowed.write_text("before-case\n", encoding="utf-8")
    excluded.write_text("before-proposal\n", encoding="utf-8")

    materialize_allowed_baselines(
        project_root=project,
        write_root=write_root,
        baseline_files=(
            _baseline(allowed, "change/case.yaml"),
            _baseline(excluded, "change/proposal.md"),
        ),
        allowed_outputs=("change/case.yaml",),
    )

    assert (write_root / "change" / "case.yaml").read_text(encoding="utf-8") == "before-case\n"
    assert not (write_root / "change" / "proposal.md").exists()


def test_materialize_allowed_baselines_rejects_authenticated_source_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_root = tmp_path / "write-root"
    source = project / "change" / "case.yaml"
    source.parent.mkdir(parents=True)
    write_root.mkdir()
    source.write_text("before\n", encoding="utf-8")
    baseline = _baseline(source, "change/case.yaml")
    source.write_text("drifted\n", encoding="utf-8")

    with pytest.raises(ValueError, match="baseline source drifted"):
        materialize_allowed_baselines(
            project_root=project,
            write_root=write_root,
            baseline_files=(baseline,),
            allowed_outputs=("change/case.yaml",),
        )
