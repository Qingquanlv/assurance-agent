from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from graph_engine.canonical import JSONValue, canonical_json_bytes
from assurance_execution.contracts import ExecutionAgentResultV1
from assurance_execution.resource_loader import resource_bytes
from execution_fixtures import VALID_LEAFS  # pyright: ignore[reportMissingImports]

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_execution" / "resources"
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "workflow-state.json",
    "aa risk",
    "claude code",
    "codex",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|workflow-state\.json|aa risk|claude code|\bcodex\b",
    re.IGNORECASE,
)


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def test_execution_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        "personas/executor.md",
        "result-contracts/execution.v1.schema.json",
        "runner/aa_observe.py",
    )
    missing = [item for item in required if not (_RESOURCES / item).is_file()]
    assert missing == []
    hits = [
        path.relative_to(_RESOURCES).as_posix()
        for path in _resource_files()
        if path.suffix in {".md", ".json", ".py"} and _TOKEN.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
    lowered = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in _resource_files()
        if path.suffix in {".md", ".json", ".py"}
    )
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contract_matches_capability_schema() -> None:
    assert resource_bytes("result-contracts/execution.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ExecutionAgentResultV1.model_json_schema())
    )
    assert VALID_LEAFS


def test_cache_safe_pytest_recipe_does_not_dirty_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    external_hypothesis = tmp_path / "external-hypothesis"
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "HYPOTHESIS_STORAGE_DIRECTORY": str(external_hypothesis),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=candidate,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (candidate / ".pytest_cache").exists()
    assert not list(candidate.rglob("__pycache__"))
    assert not (candidate / ".hypothesis").exists()
