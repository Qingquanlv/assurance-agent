from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _run_cli(*arguments: str, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if pythonpath is not None:
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(pythonpath) if not existing else f"{pythonpath}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-m", "graph_engine", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _installed_toy_a_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    package = site / "graph_engine_toy_a"
    source = Path(__file__).parents[4] / "examples" / "graph-engine-toy-a" / "graph_engine_toy_a"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dist_info = site / "graph_engine_toy_a-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: graph-engine-toy-a\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[graph_engine.products]\n"
        "toy-a = graph_engine_toy_a.product:ToyAProduct\n\n"
        "[graph_engine.plugins]\n"
        "toy-a = graph_engine_toy_a.plugin:ToyAPlugin\n",
        encoding="utf-8",
    )
    record = dist_info / "RECORD"
    relative_record = record.relative_to(site).as_posix()
    rows: list[str] = []
    for path in sorted(item for item in site.rglob("*") if item.is_file()):
        content = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append(f"{path.relative_to(site).as_posix()},sha256={digest},{len(content)}")
    rows.append(f"{relative_record},,")
    record.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return site


def test_run_requires_an_explicit_product() -> None:
    completed = _run_cli("run")

    assert completed.returncode == 2
    assert "product distribution is required" in completed.stderr


def test_cli_rejects_ambient_editable_source_inference() -> None:
    completed = _run_cli(
        "compile",
        "--product-dist",
        "graph-engine-toy-a",
        "--product-entrypoint",
        "toy-a",
        "--plugin-dist",
        "graph-engine-toy-a",
        "--plugin-entrypoint",
        "toy-a",
    )

    assert completed.returncode == 1
    assert "editable distribution requires an explicit source root and file tuple" in completed.stderr


def test_compile_emits_product_and_compiled_digests(tmp_path: Path) -> None:
    site = _installed_toy_a_site(tmp_path)
    completed = _run_cli(
        "compile",
        "--product-dist",
        "graph-engine-toy-a",
        "--product-entrypoint",
        "toy-a",
        "--plugin-dist",
        "graph-engine-toy-a",
        "--plugin-entrypoint",
        "toy-a",
        pythonpath=site,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["product_id"] == "toy.a"
    assert document["product_distribution"] == "graph-engine-toy-a"
    assert document["product_entrypoint"] == "toy-a"
    assert len(document["lock_digest"]) == 64
    assert len(document["compiled_digest"]) == 64


def test_run_executes_only_the_explicit_product_plugin_bundle(tmp_path: Path) -> None:
    site = _installed_toy_a_site(tmp_path)
    invocation_root = tmp_path / "invocations"
    completed = _run_cli(
        "run",
        "--product-dist",
        "graph-engine-toy-a",
        "--product-entrypoint",
        "toy-a",
        "--plugin-dist",
        "graph-engine-toy-a",
        "--plugin-entrypoint",
        "toy-a",
        "--entrypoint",
        "hello",
        "--invocation-id",
        "smoke",
        "--root",
        str(invocation_root),
        pythonpath=site,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["product_id"] == "toy.a"
    assert document["plugin_distributions"] == ["graph-engine-toy-a"]
    assert document["plugin_entrypoints"] == ["toy-a"]
    assert document["invocation_id"] == "smoke"
    assert document["status"] == "succeeded"
    assert document["output"] == {"message": "hello Ada"}
    assert len(document["compiled_digest"]) == 64
    assert len(document["lock_digest"]) == 64
    assert len(document["ledger_digest"]) == 64
    assert "final_tree_id" not in document
    assert (invocation_root / "invocations" / "smoke").is_dir()

    repeated = _run_cli(
        "run",
        "--product-dist",
        "graph-engine-toy-a",
        "--product-entrypoint",
        "toy-a",
        "--plugin-dist",
        "graph-engine-toy-a",
        "--plugin-entrypoint",
        "toy-a",
        "--entrypoint",
        "hello",
        "--invocation-id",
        "smoke",
        "--root",
        str(invocation_root),
        pythonpath=site,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["lock_digest"] == document["lock_digest"]


def test_ambient_entrypoint_only_flags_are_not_accepted() -> None:
    completed = _run_cli("compile", "--product", "toy-a")

    assert completed.returncode == 2
    assert "unrecognized arguments" in completed.stderr
