from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from assurance_agent.eval.suts import load_sut_registry, resolve_sut_dir
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.execution.tree_hash import hash_product_tree, hash_test_tree


def test_resolve_sut_dir_from_registry(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    (engine / "eval").mkdir(parents=True)
    sut = engine / "benchmark" / "vue-fastapi-admin"
    (sut / "tests").mkdir(parents=True)
    (sut / "tests" / "test_api.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (sut / "app").mkdir()
    (sut / "app" / "api.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests_hash = hash_test_tree(sut).aggregate
    product_hash = hash_product_tree(sut, ["app"]).aggregate
    (engine / "eval" / "suts.yaml").write_text(
        yaml.safe_dump(
            {
                "suts": {
                    "vue-fastapi-admin": {
                        "local_dir": "benchmark/vue-fastapi-admin",
                        "pinned_rev": "rev-1",
                        "source_archive_id": "archive-1",
                        "tests_tree_sha256": tests_hash,
                        "product_tree_sha256": product_hash,
                        "product_roots": ["app"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = load_sut_registry(engine)
    assert "vue-fastapi-admin" in registry.suts
    assert resolve_sut_dir(engine) == sut.resolve()

    (sut / "app" / "api.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(AaError, match="product tree hash mismatch"):
        resolve_sut_dir(engine)
