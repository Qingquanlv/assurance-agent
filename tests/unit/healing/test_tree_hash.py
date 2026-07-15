from pathlib import Path

from assurance_agent.workflow.execution.tree_hash import diff_trees, hash_test_tree


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_tree_hash_is_order_independent(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "b" / "test_b.py", "x = 1\n")
    _write(tmp_path / "tests" / "a" / "test_a.py", "x = 2\n")
    first = hash_test_tree(tmp_path)
    _write(tmp_path / "tests" / "a" / "test_a.py", "x = 2\n")
    second = hash_test_tree(tmp_path)
    assert first.aggregate == second.aggregate
    assert first.files == second.files


def test_tree_hash_ignores_pycache_and_pyc(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x(): pass\n")
    baseline = hash_test_tree(tmp_path)
    cache = tmp_path / "tests" / "api" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "test_x.cpython-311.pyc").write_bytes(b"pyc")
    (tmp_path / "tests" / "api" / "stale.pyc").write_bytes(b"pyc")
    assert hash_test_tree(tmp_path).aggregate == baseline.aggregate


def test_tree_hash_reports_added_removed_and_modified_files(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "api" / "keep.py", "a = 1\n")
    _write(tmp_path / "tests" / "api" / "gone.py", "b = 1\n")
    baseline = hash_test_tree(tmp_path).files
    _write(tmp_path / "tests" / "api" / "keep.py", "a = 2\n")
    (tmp_path / "tests" / "api" / "gone.py").unlink()
    _write(tmp_path / "tests" / "api" / "new.py", "c = 1\n")
    current = hash_test_tree(tmp_path).files
    changed = diff_trees(baseline, current)
    assert "tests/api/keep.py" in changed
    assert "tests/api/gone.py" in changed
    assert "tests/api/new.py" in changed
