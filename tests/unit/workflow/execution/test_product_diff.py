"""product_diff — snapshot-based changed-line detection for SUTs without .git.

The benchmark SUT has no git metadata, so ``raw/changed-lines.json`` (A1 diff
coverage, diff-scoped mutation) diffs the current product tree against a
post-batch snapshot under ``.aa/cache/diff-base/``. Fail-closed: any integrity
or IO problem yields ``None`` and the runner writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.workflow.execution import product_diff
from assurance_agent.workflow.execution.product_diff import (
    DIFF_BASE_REL,
    compute_changed_lines,
    snapshot_product_tree,
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_bytes(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _index(project_root: Path) -> dict[str, str]:
    return json.loads((project_root / DIFF_BASE_REL / "index.json").read_text(encoding="utf-8"))


def test_first_run_without_snapshot_returns_none(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "x = 1\n")
    assert compute_changed_lines(tmp_path) is None


def test_snapshot_then_edit_yields_added_and_replaced_lines_only(tmp_path: Path) -> None:
    """Unified-diff semantics: insert/replace mark new-file lines; delete marks none."""
    _write(tmp_path, "app/api/dept.py", "one\ntwo\nthree\n")
    snapshot_product_tree(tmp_path)
    _write(tmp_path, "app/api/dept.py", "one\nTWO\nthree\nfour\n")
    assert compute_changed_lines(tmp_path) == {"app/api/dept.py": [2, 4]}


def test_pure_deletion_contributes_no_lines(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\ntwo\nthree\n")
    snapshot_product_tree(tmp_path)
    _write(tmp_path, "app/api/dept.py", "one\nthree\n")
    assert compute_changed_lines(tmp_path) == {}


def test_new_file_marks_every_line_and_deleted_file_is_omitted(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    _write(tmp_path, "app/api/gone.py", "old\n")
    snapshot_product_tree(tmp_path)
    (tmp_path / "app" / "api" / "gone.py").unlink()
    _write(tmp_path, "app/api/user.py", "a\nb\nc\n")
    assert compute_changed_lines(tmp_path) == {"app/api/user.py": [1, 2, 3]}


def test_compute_is_deterministic_across_calls(tmp_path: Path) -> None:
    _write(tmp_path, "app/b.py", "1\n2\n")
    _write(tmp_path, "app/a.py", "x\n")
    snapshot_product_tree(tmp_path)
    _write(tmp_path, "app/b.py", "1\n2\n3\n")
    _write(tmp_path, "app/a.py", "y\n")
    first = compute_changed_lines(tmp_path)
    second = compute_changed_lines(tmp_path)
    assert first == second == {"app/a.py": [1], "app/b.py": [3]}
    assert first is not None
    assert list(first) == sorted(first)


def test_unchanged_tree_yields_empty_mapping_not_none(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    assert compute_changed_lines(tmp_path) == {}


def test_unchanged_binary_file_does_not_poison_text_diff(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    _write_bytes(tmp_path, "app/assets/login.webp", b"RIFF\xff\x00WEBPVP8 ")
    snapshot_product_tree(tmp_path)
    _write(tmp_path, "app/api/dept.py", "one\ntwo\n")

    assert compute_changed_lines(tmp_path) == {"app/api/dept.py": [2]}


def test_changed_binary_file_is_ignored_while_text_diff_is_measured(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    _write_bytes(tmp_path, "app/assets/login.webp", b"RIFF\xff\x00WEBPVP8 old")
    snapshot_product_tree(tmp_path)
    _write(tmp_path, "app/api/dept.py", "ONE\n")
    _write_bytes(tmp_path, "app/assets/login.webp", b"RIFF\xfe\x00WEBPVP8 new")

    assert compute_changed_lines(tmp_path) == {"app/api/dept.py": [1]}


def test_new_binary_file_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    _write_bytes(tmp_path, "app/assets/new.webp", b"RIFF\xff\x00WEBPVP8 new")

    assert compute_changed_lines(tmp_path) == {}


def test_utf8_decodable_nul_binary_file_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    _write_bytes(tmp_path, "app/assets/new.bin", b"binary\x00payload")

    assert compute_changed_lines(tmp_path) == {}


def test_pycache_and_pyc_are_excluded_from_base_and_diff(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    _write(tmp_path, "app/api/__pycache__/dept.cpython-311.pyc", "junk\n")
    snapshot_product_tree(tmp_path)
    assert not any("__pycache__" in rel for rel in _index(tmp_path))
    _write(tmp_path, "app/api/__pycache__/dept.cpython-311.pyc", "junk2\n")
    assert compute_changed_lines(tmp_path) == {}


def test_tampered_snapshot_copy_fails_integrity_and_returns_none(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    copy = tmp_path / DIFF_BASE_REL / "files" / "app" / "api" / "dept.py"
    copy.write_text("forged\n", encoding="utf-8")
    assert compute_changed_lines(tmp_path) is None


def test_corrupt_index_returns_none(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    (tmp_path / DIFF_BASE_REL / "index.json").write_text("{not json", encoding="utf-8")
    assert compute_changed_lines(tmp_path) is None


def test_missing_snapshot_copy_returns_none(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    (tmp_path / DIFF_BASE_REL / "files" / "app" / "api" / "dept.py").unlink()
    assert compute_changed_lines(tmp_path) is None


def test_empty_product_roots_fall_back_to_app(tmp_path: Path, monkeypatch) -> None:
    """``load_product_code_roots`` returning nothing must still snapshot ``app/``."""
    monkeypatch.setattr(product_diff, "load_product_code_roots", lambda _root: [])
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    assert "app/api/dept.py" in _index(tmp_path)


def test_snapshot_roundtrip_through_resnapshot(tmp_path: Path) -> None:
    """After a resnapshot the same tree diffs clean — the base advanced."""
    _write(tmp_path, "app/api/dept.py", "one\n")
    snapshot_product_tree(tmp_path)
    _write(tmp_path, "app/api/dept.py", "one\ntwo\n")
    assert compute_changed_lines(tmp_path) == {"app/api/dept.py": [2]}
    snapshot_product_tree(tmp_path)
    assert compute_changed_lines(tmp_path) == {}
