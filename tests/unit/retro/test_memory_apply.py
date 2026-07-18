from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.exceptions import AaError
from assurance_agent.retro.apply import (
    apply_memory_proposal,
    deprecate_memory_block,
    resolve_memory_target,
)
from assurance_agent.retro.types import RetroProposal


def _proposal(target: str | None = ".aa/memory/aa-run.md") -> RetroProposal:
    return RetroProposal(
        id="P-1",
        apply_kind="memory_append",
        eval_suite="workflow-run",
        target=target,
        problem="flaky fixture use",
        proposed_change="remember to check fixtures",
        evidence_ids=["CH-1#F-1"],
    )


def test_apply_live_appends_marker_block_once(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    target = sut / ".aa" / "memory" / "aa-run.md"
    target.parent.mkdir(parents=True)
    target.write_text("# memory\n", encoding="utf-8")

    assert apply_memory_proposal(sut, "retro-1", _proposal()) is True
    first = target.read_text(encoding="utf-8")
    assert "<!-- retro:retro-1#P-1 evidence:CH-1#F-1 -->" in first
    assert "- remember to check fixtures" in first
    assert "<!-- /retro -->" in first
    assert first.startswith("# memory\n")

    assert apply_memory_proposal(sut, "retro-1", _proposal()) is False
    assert target.read_text(encoding="utf-8") == first
    assert first.count("retro:retro-1#P-1") == 1


def test_apply_live_creates_missing_target(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    assert apply_memory_proposal(sut, "retro-1", _proposal()) is True
    target = sut / ".aa" / "memory" / "aa-run.md"
    assert "retro:retro-1#P-1" in target.read_text(encoding="utf-8")


def test_apply_stage_mode_renders_live_base_without_touching_live(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    target = sut / ".aa" / "memory" / "aa-run.md"
    target.parent.mkdir(parents=True)
    target.write_text("# live memory\n", encoding="utf-8")
    stage = tmp_path / "stage"

    assert apply_memory_proposal(sut, "retro-1", _proposal(), stage_dir=stage) is True
    staged = (stage / "aa-run.md").read_text(encoding="utf-8")
    assert staged.startswith("# live memory\n")
    assert "retro:retro-1#P-1" in staged
    # live file untouched
    assert target.read_text(encoding="utf-8") == "# live memory\n"
    # stage-mode repeat keeps the staged copy instead of duplicating the block
    assert apply_memory_proposal(sut, "retro-1", _proposal(), stage_dir=stage) is False
    assert (stage / "aa-run.md").read_text(encoding="utf-8").count("retro:retro-1#P-1") == 1


def test_resolve_memory_target_rejects_unsafe(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    (sut / ".aa" / "memory").mkdir(parents=True)
    with pytest.raises(AaError, match="empty"):
        resolve_memory_target(sut, None)
    with pytest.raises(AaError, match="absolute"):
        resolve_memory_target(sut, "/etc/passwd")
    with pytest.raises(AaError, match="outside"):
        resolve_memory_target(sut, "../escape.md")
    with pytest.raises(AaError, match="outside"):
        resolve_memory_target(sut, ".aa/memory/../../escape.md")
    with pytest.raises(AaError, match="outside"):
        resolve_memory_target(sut, ".aa/other/x.md")
    with pytest.raises(AaError, match="symlink"):
        link = sut / ".aa" / "memory" / "link.md"
        link.symlink_to(sut / ".aa" / "memory")
        resolve_memory_target(sut, ".aa/memory/link.md")
    with pytest.raises(AaError, match="regular file"):
        (sut / ".aa" / "memory" / "dir.md").mkdir()
        resolve_memory_target(sut, ".aa/memory/dir.md")
    # safe target resolves inside .aa/memory
    ok = resolve_memory_target(sut, ".aa/memory/aa-run.md")
    assert ok == (sut / ".aa" / "memory" / "aa-run.md").resolve()


def test_apply_rejects_unsafe_target(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    with pytest.raises(AaError, match="outside"):
        apply_memory_proposal(sut, "retro-1", _proposal(target="../evil.md"))
    assert not (sut.parent / "evil.md").exists()


def test_deprecate_memory_block_marks_deprecated_and_idempotent(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    apply_memory_proposal(sut, "retro-1", _proposal())
    target = sut / ".aa" / "memory" / "aa-run.md"

    assert deprecate_memory_block(sut, "retro-1", _proposal()) is True
    content = target.read_text(encoding="utf-8")
    assert "- deprecated: remember to check fixtures" in content
    # marker retained so the block stays auditable (no physical delete)
    assert "retro:retro-1#P-1" in content

    assert deprecate_memory_block(sut, "retro-1", _proposal()) is False
    assert target.read_text(encoding="utf-8") == content


def test_deprecate_memory_block_missing_file_or_block(tmp_path: Path) -> None:
    sut = tmp_path / "sut"
    with pytest.raises(AaError, match="memory file not found"):
        deprecate_memory_block(sut, "retro-1", _proposal())
    target = sut / ".aa" / "memory" / "aa-run.md"
    target.parent.mkdir(parents=True)
    target.write_text("# memory\n", encoding="utf-8")
    with pytest.raises(AaError, match="applied block not found"):
        deprecate_memory_block(sut, "retro-1", _proposal())
