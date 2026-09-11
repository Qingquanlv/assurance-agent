from __future__ import annotations

import json
from pathlib import Path

from assurance_intake.contracts.planning_facts import build_planning_facts


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _facts(root: Path, *leafs: str, targets: tuple[str, ...] = ()) -> dict:
    return build_planning_facts(
        root, change_id="CH-1", capability_leafs=leafs, families=("api",), target_files=targets
    )


def test_facts_observe_ignored_files_without_importing_or_disclosing_values(tmp_path: Path) -> None:
    _write(tmp_path, ".gitignore", "tests/\napp/\n")
    _write(tmp_path, "qa/requirement.md", "Read `app/schema.py`.")
    _write(tmp_path, "app/schema.py", "raise RuntimeError('must not import SUT')\n")
    _write(
        tmp_path,
        ".aa/data-knowledge.yaml",
        "auth:\n  admin:\n    symbol: tests.api.conftest.token\n    secret: never-emit-catalog-secret\n",
    )
    _write(
        tmp_path,
        "tests/api/conftest.py",
        "import pytest as pt\nfrom os import getenv as env\n@pt.fixture(name='admin_token')\ndef token(request, /, *, scope='never-emit-default-secret'):\n    return env('API_ADMIN_TOKEN', 'never-emit-env-secret')\n",
    )
    facts = _facts(tmp_path, "auth.admin")
    files = {file["path"]: file for file in facts["files"]}
    source = files["tests/api/conftest.py"]
    assert source["symbols"][0]["fixture_name"] == "admin_token"
    assert source["symbols"][0]["signature"] == "token(request, /, *, scope=...)"
    assert source["environment_names"] == ["API_ADMIN_TOKEN"]
    assert files["app/schema.py"]["status"] == "observed"
    assert facts["declared_symbols"][0]["status"] == "observed"
    assert "never-emit" not in json.dumps(facts)


def test_dynamic_imports_missing_modules_and_syntax_errors_remain_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".aa/data-knowledge.yaml",
        "auth:\n  imported:\n    symbol: tests.api.conftest.external\n  missing:\n    symbol: tests.api.missing.token\n",
    )
    _write(
        tmp_path,
        "tests/api/conftest.py",
        "from plugin import external\npytest_plugins = ['external_plugin']\n",
    )
    _write(tmp_path, "tests/config.py", "def broken syntax\n")
    facts = _facts(tmp_path, "auth.imported", "auth.missing")
    assert all(item["status"] == "unknown" for item in facts["declared_symbols"])
    files = {file["path"]: file for file in facts["files"]}
    assert files["tests/config.py"]["parse_status"] == "unknown"
    assert files["tests/api/missing.py"]["status"] == "unknown"


def test_index_is_content_deterministic_and_refreshes_after_source_change(tmp_path: Path) -> None:
    _write(tmp_path, "tests/api/conftest.py", "def helper(): pass\n")
    before = _facts(tmp_path)
    assert before == _facts(tmp_path)
    _write(tmp_path, "tests/api/conftest.py", "def helper(parent_id=None): pass\n")
    after = _facts(tmp_path)
    assert after["digest"] != before["digest"]
    assert after["inputs"] == before["inputs"]


def test_index_rejects_symlinked_parents_and_bounds_large_files(tmp_path: Path) -> None:
    _write(tmp_path, "external/conftest.py", "def leaked(): pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/api").symlink_to(tmp_path / "external", target_is_directory=True)
    _write(tmp_path, "tests/config.py", "x" * (256 * 1024 + 1))
    facts = _facts(tmp_path, targets=("tests/../external/conftest.py",))
    files = {file["path"]: file for file in facts["files"]}
    assert files["tests/api/conftest.py"]["reason"] == "symlink"
    assert files["tests/config.py"]["reason"] == "size_limit"
    assert "leaked" not in json.dumps(facts)


def test_index_respects_capability_scope_and_reads_target_ancestor_fixtures(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".aa/data-knowledge.yaml",
        "auth:\n  allowed:\n    symbol: tests.api.conftest.token\n  other:\n    symbol: tests.private.hidden.token\n  browser:\n    symbol: tests.e2e.conftest.login\n",
    )
    _write(tmp_path, "tests/api/dept/conftest.py", "from pytest import fixture as f\n@f\ndef child(): pass\n")
    facts = _facts(tmp_path, "auth.allowed", "auth.browser", targets=("tests/api/dept/test_dept.py",))
    files = {file["path"]: file for file in facts["files"]}
    assert "tests/private/hidden.py" not in files
    assert "tests/e2e/conftest.py" not in files
    assert files["tests/api/dept/conftest.py"]["symbols"][0]["fixture_name"] == "child"
    assert facts["capability_leafs"] == ["auth.allowed", "auth.browser"]
