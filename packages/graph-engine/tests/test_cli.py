from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "graph_engine", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_run_requires_an_explicit_product() -> None:
    completed = _run_cli("run")

    assert completed.returncode == 2
    assert "product is required" in completed.stderr


def test_compile_emits_product_and_compiled_digests() -> None:
    completed = _run_cli("compile", "--product", "toy-a")

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["product_id"] == "toy.a"
    assert document["product_entrypoint"] == "toy-a"
    assert len(document["product_digest"]) == 64
    assert len(document["compiled_digest"]) == 64


def test_run_executes_only_the_explicit_product_plugin_bundle(tmp_path: Path) -> None:
    completed = _run_cli(
        "run",
        "--product",
        "toy-a",
        "--plugin",
        "toy-a",
        "--entrypoint",
        "hello",
        "--invocation-id",
        "smoke",
        "--root",
        str(tmp_path),
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["product_id"] == "toy.a"
    assert document["plugin_entrypoints"] == ["toy-a"]
    assert document["invocation_id"] == "smoke"
    assert document["status"] == "succeeded"
    assert document["output"] == {"message": "hello Ada"}
    assert len(document["compiled_digest"]) == 64
    assert len(document["product_digest"]) == 64
    assert len(document["ledger_digest"]) == 64
    assert len(document["final_tree_id"]) == 64
    assert (tmp_path / "invocations" / "smoke").is_dir()
