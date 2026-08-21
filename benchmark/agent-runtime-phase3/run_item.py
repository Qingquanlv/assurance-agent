"""Fail-closed Phase 3 provider-live driver. Consumes only the committed manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_opencode.protocol import AcceptedOpenCodeProfile
from graph_engine.canonical import canonical_digest as engine_digest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.name != "manifest.json":
        raise SystemExit("driver must consume the committed manifest.json only")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("manifest must be a JSON object")
    return document


def _tree_digest(root: Path) -> str:
    files = []
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts
    ):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return engine_digest(files)


def _require_source_digest(repo: Path, relative: str, expected: str, label: str) -> None:
    actual = _tree_digest(repo / relative)
    if actual != expected:
        raise SystemExit(f"{label} source digest drifted: expected {expected}, found {actual}")


def _fail(message: str) -> int:
    print(f"phase3-live: {message}", file=sys.stderr)
    return 1


def _check_opencode(adapter: dict[str, Any]) -> int:
    endpoint = adapter["endpoint"].rstrip("/")
    try:
        health = _json_get(f"{endpoint}/global/health")
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as error:
        return _fail(f"OpenCode server at pinned endpoint {endpoint} is unavailable: {error}")
    version = health.get("version")
    if version != adapter["external_tool_version"]:
        return _fail(
            f"OpenCode version {version!r} does not match pinned {adapter['external_tool_version']!r}"
        )
    try:
        advertised = _json_get(f"{endpoint}/config")
        AcceptedOpenCodeProfile.model_validate(advertised)
    except Exception as error:
        return _fail(
            "pinned OpenCode protocol profile is unavailable on the live server "
            f"({adapter['protocol_profile']}): {error}"
        )
    token = os.environ.get(adapter["secret_env"])
    if not token:
        return _fail(f"OpenCode secret {adapter['secret_env']!r} is unset; refusing to invent credentials")
    return _fail(
        "OpenCode live prerequisite matched, but this driver does not substitute a fake "
        "and will not continue without a complete graph-ledger success path"
    )


def _check_cursor(adapter: dict[str, Any]) -> int:
    executable = Path(adapter["executable"])
    if executable.is_symlink() or not executable.is_file():
        return _fail(f"pinned Cursor executable is missing or not a regular file: {executable}")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != adapter["executable_digest"]:
        return _fail(
            f"Cursor executable digest {digest} does not match pinned {adapter['executable_digest']}"
        )
    reported = subprocess.run(  # noqa: S603
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    version = (reported.stdout or reported.stderr).strip().splitlines()[0] if reported.returncode == 0 else ""
    if version != adapter["external_tool_version"]:
        return _fail(f"Cursor version {version!r} does not match pinned {adapter['external_tool_version']!r}")
    token = os.environ.get(adapter["secret_env"])
    if not token:
        return _fail(f"Cursor secret {adapter['secret_env']!r} is unset; refusing to invent credentials")
    return _fail(
        "Cursor live prerequisite matched, but this driver does not substitute a fake "
        "and will not continue without a complete graph-ledger success path"
    )


def _json_get(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=5) as response:  # noqa: S310 - pinned local/release endpoint
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--adapter", choices=("opencode", "cursor"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    repo = _repo_root()
    manifest = _load_manifest(arguments.manifest.resolve())
    _require_source_digest(
        repo,
        manifest["fixture"]["source_root"],
        manifest["fixture"]["source_digest"],
        "fixture",
    )
    request_bytes = manifest["canonical_request"]["canonical_bytes"].encode("utf-8")
    if canonical_digest(json.loads(request_bytes)) != manifest["canonical_request"]["digest"]:
        return _fail("canonical request digest does not authenticate the pinned bytes")
    adapter = manifest["adapters"][arguments.adapter]
    _require_source_digest(repo, adapter["source_root"], adapter["source_digest"], arguments.adapter)
    if arguments.adapter == "opencode":
        return _check_opencode(adapter)
    return _check_cursor(adapter)


if __name__ == "__main__":
    raise SystemExit(main())
