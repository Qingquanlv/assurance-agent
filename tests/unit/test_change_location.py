from pathlib import Path

import pytest

from assurance_agent.change_location import (
    ChangeLocation,
    ChangeNotFoundError,
    archive_root,
    changes_root,
    resolve_change,
)
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.templates import InitAnswers, build_config_yaml


def _write_config(root: Path, *, changes: str = "./qa/changes", archive: str | None = None) -> None:
    (root / ".aa").mkdir(exist_ok=True)
    text = build_config_yaml(InitAnswers())
    if archive is not None:
        # Replace the default archive line when we need a custom value in later cases.
        text = text.replace("  archive: ./qa/archive\n", f"  archive: {archive}\n")
        if "archive:" not in text:
            text = text.replace(
                f"  changes: {changes}\n"
                if f"  changes: {changes}\n" in text
                else "  changes: ./qa/changes\n",
                f"  changes: {changes}\n  archive: {archive}\n",
            )
    (root / ".aa" / "config.yaml").write_text(text, encoding="utf-8")


def test_resolve_change_returns_active_location(tmp_path: Path) -> None:
    _write_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)

    loc = resolve_change(tmp_path, "CH-1")

    assert loc == ChangeLocation(
        project_root=tmp_path,
        change_id="CH-1",
        path=change,
        source="changes",
    )


def test_resolve_change_requires_config(tmp_path: Path) -> None:
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    with pytest.raises(ConfigNotFoundError):
        resolve_change(tmp_path, "CH-1")


def test_resolve_change_rejects_unsafe_id(tmp_path: Path) -> None:
    _write_config(tmp_path)
    with pytest.raises(UnsafeIdentifierError):
        resolve_change(tmp_path, "../evil")


def test_resolve_change_missing_raises(tmp_path: Path) -> None:
    _write_config(tmp_path)
    with pytest.raises(ChangeNotFoundError, match="not found"):
        resolve_change(tmp_path, "NOPE")


def test_resolve_change_archive_only_hints_archive(tmp_path: Path) -> None:
    _write_config(tmp_path)
    archived = tmp_path / "qa" / "archive" / "CH-1"
    archived.mkdir(parents=True)

    with pytest.raises(ChangeNotFoundError, match="archive") as exc:
        resolve_change(tmp_path, "CH-1")
    assert "active change" in str(exc.value).lower() or "write" in str(exc.value).lower()


# ---- prefer matrix: (only active | only archive | both) × (active | archive) ----
def test_prefer_archive_returns_active_when_only_active(tmp_path: Path) -> None:
    _write_config(tmp_path)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)

    loc = resolve_change(tmp_path, "CH-1", prefer="archive")
    assert loc.source == "changes"
    assert loc.path == change


def test_prefer_archive_returns_archive_when_only_archive(tmp_path: Path) -> None:
    _write_config(tmp_path)
    archived = tmp_path / "qa" / "archive" / "CH-1"
    archived.mkdir(parents=True)

    loc = resolve_change(tmp_path, "CH-1", prefer="archive")
    assert loc.source == "archive"
    assert loc.path == archived


def test_prefer_active_missing_when_only_archive(tmp_path: Path) -> None:
    _write_config(tmp_path)
    (tmp_path / "qa" / "archive" / "CH-1").mkdir(parents=True)

    with pytest.raises(ChangeNotFoundError, match="active change"):
        resolve_change(tmp_path, "CH-1", prefer="active")


def test_both_roots_coexist_is_not_ambiguous(tmp_path: Path) -> None:
    # Post-archive steady state: aa-archive copies (never moves) and preserves
    # qa/changes/<id>. Coexistence must resolve by preference, never raise (ADR-0002).
    _write_config(tmp_path)
    active = tmp_path / "qa" / "changes" / "CH-1"
    archived = tmp_path / "qa" / "archive" / "CH-1"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)

    assert resolve_change(tmp_path, "CH-1", prefer="active").path == active
    assert resolve_change(tmp_path, "CH-1", prefer="active").source == "changes"
    assert resolve_change(tmp_path, "CH-1", prefer="archive").path == archived
    assert resolve_change(tmp_path, "CH-1", prefer="archive").source == "archive"


def test_prefer_archive_missing_both_raises(tmp_path: Path) -> None:
    _write_config(tmp_path)
    with pytest.raises(ChangeNotFoundError, match="not found"):
        resolve_change(tmp_path, "CH-1", prefer="archive")


def test_roots_honor_custom_qa_paths(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text(
        """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./work/changes, archive: ./work/archive}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
""",
        encoding="utf-8",
    )
    assert changes_root(tmp_path) == tmp_path / "work" / "changes"
    assert archive_root(tmp_path) == tmp_path / "work" / "archive"


def test_resolve_respects_custom_qa_paths(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    # Minimal valid config with non-default paths (no archive key → default still applied via model).
    (tmp_path / ".aa" / "config.yaml").write_text(
        """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./work/changes, archive: ./work/archive}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
""",
        encoding="utf-8",
    )
    change = tmp_path / "work" / "changes" / "CH-9"
    change.mkdir(parents=True)

    loc = resolve_change(tmp_path, "CH-9")
    assert loc.path == change
    assert loc.source == "changes"


def test_legacy_config_without_archive_field_defaults(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text(
        """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./qa/changes}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
""",
        encoding="utf-8",
    )
    archived = tmp_path / "qa" / "archive" / "CH-1"
    archived.mkdir(parents=True)

    loc = resolve_change(tmp_path, "CH-1", prefer="archive")
    assert loc.path == archived
    assert loc.source == "archive"
