from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.safety import (
    HealingGuardError,
    assert_product_tree_unchanged_in_healing,
    assert_test_tree_unchanged_or_healing,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _manifest(change_dir: Path, tests_sha: str, files: dict[str, str], product_sha: str) -> None:
    _write(
        change_dir / "execution" / "execution-manifest.yaml",
        yaml.safe_dump(
            {
                "batch_id": "20260101-000000",
                "tests_tree_sha256": tests_sha,
                "test_files_sha256": files,
                "product_tree_sha256": product_sha,
                "final_status": "PASS",
                "result_files": {},
            }
        ),
    )


def test_tests_changed_without_healing_is_blocked(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 1\n")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    prior = hash_test_tree(tmp_path)
    _manifest(change_dir, prior.aggregate, prior.files, "p0")
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 2  # tampered\n")
    with pytest.raises(HealingGuardError, match="TESTS-CHANGED-WITHOUT-HEALING"):
        assert_test_tree_unchanged_or_healing(tmp_path, "CH-1", allow_test_changes=False)


def test_allow_test_changes_override_permits(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 1\n")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    prior = hash_test_tree(tmp_path)
    _manifest(change_dir, prior.aggregate, prior.files, "p0")
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x():\n    assert 2\n")
    result = assert_test_tree_unchanged_or_healing(tmp_path, "CH-1", allow_test_changes=True)
    assert result.tests_changed is True
    assert len(result.changed_files) == 1


def test_no_prior_manifest_is_noop(tmp_path: Path) -> None:
    (tmp_path / "qa" / "changes" / "CH-1").mkdir(parents=True)
    result = assert_test_tree_unchanged_or_healing(tmp_path, "CH-1", allow_test_changes=False)
    assert result.tests_changed is False


def test_product_changed_during_healing_is_blocked(tmp_path: Path) -> None:
    from assurance_agent.workflow.core.events import append_event_strict

    _write(tmp_path / "app" / "main.py", "v1\n")
    _write(tmp_path / "tests" / "api" / "test_x.py", "def test_x(): pass\n")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    test_tree = hash_test_tree(tmp_path)
    from assurance_agent.workflow.execution.tree_hash import hash_product_tree
    product_tree = hash_product_tree(tmp_path, ["app"])
    _manifest(change_dir, test_tree.aggregate, test_tree.files, product_tree.aggregate)
    append_event_strict(change_dir, {
        "source": "heal", "type": "healing_entry_baseline_pinned",
        "artifact_file": "healing/entry-baseline.json", "artifact_sha256": "x",
        "entry_batch_id": "b1", "episode_id": "e1",
    })
    _write(tmp_path / "app" / "main.py", "v2\n")
    with pytest.raises(HealingGuardError, match="PRODUCT-CHANGED-DURING-HEALING"):
        assert_product_tree_unchanged_in_healing(tmp_path, "CH-1", roots=["app"])
