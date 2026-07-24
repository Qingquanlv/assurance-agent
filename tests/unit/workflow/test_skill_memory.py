from __future__ import annotations

from pathlib import Path

from assurance_agent.workflow.skill_memory import load_skill_memory


def test_load_skill_memory_filters_deprecated_and_injects_active_rules(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".aa" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "aa-api-codegen.md").write_text(
        "# rules\n- keep active rule\n- deprecated: old rule\n- another active\n",
        encoding="utf-8",
    )
    text = load_skill_memory(tmp_path, "aa-api-codegen")
    assert "keep active rule" in text
    assert "another active" in text
    assert "deprecated:" not in text
    assert "old rule" not in text


def test_load_skill_memory_truncates_at_8kib(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".aa" / "memory"
    memory_dir.mkdir(parents=True)
    long_line = "x" * 200
    lines = [long_line for _ in range(60)]
    (memory_dir / "aa-run.md").write_text("\n".join(lines), encoding="utf-8")
    text = load_skill_memory(tmp_path, "aa-run")
    assert text.endswith("... [truncated]")
    assert len(text.encode("utf-8")) <= 8 * 1024


def test_load_skill_memory_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_skill_memory(tmp_path, "aa-run") == ""
