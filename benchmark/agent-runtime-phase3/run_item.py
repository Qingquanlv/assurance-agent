"""Fail-closed Phase 3 provider-live driver through Engine.production."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_cursor.config import CursorAdapterConfig
from agent_runtime_opencode.config import OpenCodeAdapterConfig
from graph_engine.canonical import canonical_digest as engine_digest
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import InvocationWorkspaceBinding
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.models import attempt_directory_id
from graph_engine.runtime.planner import _start_token_id, activation_id, task_id
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    runtime_authorization_digest,
)
from graph_engine.runtime.seed import empty_invocation_seed

_AMBIENT_OVERRIDE_VARS = frozenset(
    {
        "OPENCODE_ENDPOINT",
        "OPENCODE_MODEL",
        "CURSOR_EXECUTABLE",
        "CURSOR_MODEL",
        "AA_MODEL",
        "PROVIDER_MODEL",
    }
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|secret)\s*[:=]\s*\S+|sk-[A-Za-z0-9-]+"
)
_WORKSPACE_PACKAGES = {
    "agent-runtime-fixture": ("examples/agent-runtime-fixture", "agent_runtime_fixture"),
    "agent-runtime-opencode": ("packages/clients/agent-runtime-opencode", "agent_runtime_opencode"),
    "agent-runtime-cursor": ("packages/clients/agent-runtime-cursor", "agent_runtime_cursor"),
}


@dataclass(frozen=True)
class ManifestItem:
    adapter: str
    adapter_version: str
    item_id: str
    binding_manifest: dict[str, Any]
    expected_artifacts: tuple[str, ...]
    secret_handle: str
    secret_env: str


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _reject_ambient_overrides() -> None:
    for name in _AMBIENT_OVERRIDE_VARS:
        if os.environ.get(name):
            raise SystemExit(f"ambient override {name!r} is forbidden")


def _reject_credentials_in_text(text: str, *, label: str) -> None:
    if _CREDENTIAL_PATTERN.search(text):
        raise SystemExit(f"credential material detected in {label}")


def _manifest_item(document: dict[str, Any], adapter: str) -> ManifestItem:
    items = document.get("items")
    if not isinstance(items, list):
        raise SystemExit("manifest must declare items[]")
    matches = [item for item in items if isinstance(item, dict) and item.get("adapter") == adapter]
    if len(matches) != 1:
        raise SystemExit(f"manifest must declare exactly one item for adapter {adapter!r}")
    item = matches[0]
    adapter_meta = document.get("adapters", {}).get(adapter)
    if not isinstance(adapter_meta, dict):
        raise SystemExit(f"manifest adapters.{adapter} is required")
    binding_manifest = item.get("binding_manifest")
    expected_artifacts = item.get("expected_artifacts")
    if not isinstance(binding_manifest, dict):
        raise SystemExit(f"item {item.get('id')!r} missing binding_manifest")
    if not isinstance(expected_artifacts, list) or not expected_artifacts:
        raise SystemExit(f"item {item.get('id')!r} missing expected_artifacts")
    return ManifestItem(
        adapter=adapter,
        adapter_version=str(item["adapter_version"]),
        item_id=str(item["id"]),
        binding_manifest=dict(binding_manifest),
        expected_artifacts=tuple(str(path) for path in expected_artifacts),
        secret_handle=str(adapter_meta["secret_handle"]),
        secret_env=str(adapter_meta["secret_env"]),
    )


def _frozen_request(manifest: dict[str, Any]) -> AgentRunRequest:
    request_bytes = manifest["canonical_request"]["canonical_bytes"].encode("utf-8")
    if canonical_digest(json.loads(request_bytes)) != manifest["canonical_request"]["digest"]:
        raise SystemExit("canonical request digest does not authenticate the pinned bytes")
    return AgentRunRequest.model_validate_json(request_bytes)


def _attempt_project_scope(*, engine_root: Path, invocation_id: str) -> str:
    token = _start_token_id("root", "run")
    act = activation_id("root", "run", 0, (token,))
    tid = task_id(act)
    attempt_dir = attempt_directory_id(invocation_id, tid, act, 1)
    return str(
        (engine_root / "invocations" / invocation_id / "workspace" / "attempts" / attempt_dir).resolve()
    )


def _locked_binding(item: ManifestItem, *, project_scope: str | None, model: str) -> dict[str, Any]:
    binding = dict(item.binding_manifest)
    forbidden = {"endpoint", "executable", "model"}
    for key in forbidden:
        if key in os.environ:
            raise SystemExit(f"ambient override via environment for {key!r} is forbidden")
    if item.adapter == "opencode":
        if project_scope is None:
            raise SystemExit("OpenCode project_scope is required")
        binding["project_scope"] = project_scope
        OpenCodeAdapterConfig.model_validate(binding)
    else:
        CursorAdapterConfig.model_validate(binding)
    locked = dict(binding)
    locked["model"] = model
    return locked


def _copied_package(
    stack: ExitStack,
    repo: Path,
    distribution: str,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
) -> tuple[Path, tuple[str, ...]]:
    cached = copies.get(distribution)
    if cached is not None:
        return cached
    relative, package = _WORKSPACE_PACKAGES[distribution]
    project_root = repo / relative
    dest = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
    shutil.copytree(
        project_root / package,
        dest / package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    files = tuple(sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()))
    copies[distribution] = (dest, files)
    return dest, files


def _editable_product(
    document: dict[str, object],
    repo: Path,
    stack: ExitStack,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
) -> EditableWheelProductSource:
    distribution = str(document["distribution"])
    root, files = _copied_package(stack, repo, distribution, copies)
    return EditableWheelProductSource(
        distribution=distribution,
        entrypoint_name=str(document["entrypoint_name"]),
        declaration_path=str(document["declaration_path"]),
        source_root=root,
        source_files=files,
    )


def _editable_plugin(
    document: dict[str, object],
    repo: Path,
    stack: ExitStack,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
) -> EditableWheelPluginSource:
    distribution = str(document["distribution"])
    root, files = _copied_package(stack, repo, distribution, copies)
    return EditableWheelPluginSource(
        distribution=distribution,
        entrypoint_name=str(document["entrypoint_name"]),
        declaration_path=str(document["declaration_path"]),
        source_root=root,
        source_files=files,
    )


def _activate_editable_imports(request: ResolutionRequest) -> None:
    roots: list[Path] = []
    if isinstance(request.product, EditableWheelProductSource):
        roots.append(request.product.source_root)
    for plugin in request.plugins:
        if isinstance(plugin, EditableWheelPluginSource):
            roots.append(plugin.source_root)
    for root in reversed(roots):
        sys.path.insert(0, str(root))
    prefixes = ("agent_runtime_fixture", "agent_runtime_opencode", "agent_runtime_cursor")
    for name in tuple(sys.modules):
        if name in prefixes or name.startswith(tuple(f"{prefix}." for prefix in prefixes)):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _patch_copied_binding_plugin(
    *,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
    adapter: str,
    binding_data: dict[str, Any],
    secret_handle: str,
) -> None:
    cached = copies.get("agent-runtime-fixture")
    if cached is None:
        raise SystemExit("fixture copy is required to patch locked binding data")
    root, _files = cached
    bindings_path = root / "agent_runtime_fixture" / "bindings.py"
    target = "runtime.opencode.execute" if adapter == "opencode" else "runtime.cursor.execute"
    plugin_class = "OpenCodeBindingPlugin" if adapter == "opencode" else "CursorBindingPlugin"
    payload = json.dumps(binding_data, indent=4, sort_keys=True)
    secret = json.dumps(secret_handle)
    appendix = f"""

def _phase3_locked_binding_contribute(ports: RegistryPorts) -> PluginContribution:
    if ports.engine_api != ENGINE_API_VERSION:
        raise ValueError(f"unsupported engine API: {{ports.engine_api!r}}")
    return PluginContribution(
        bindings=(
            CapabilityBindingContribution(
                capability_id=RUN_CAPABILITY_ID,
                target_capability_id={json.dumps(target)},
                data={payload},
                secret_handles=({secret},),
            ),
        ),
    )


{plugin_class}.contribute = staticmethod(_phase3_locked_binding_contribute)
"""
    bindings_path.write_text(bindings_path.read_text(encoding="utf-8") + appendix, encoding="utf-8")


def _resolve_composition(
    repo: Path,
    manifest: dict[str, Any],
    item: ManifestItem,
    locked_binding: dict[str, Any],
    stack: ExitStack,
) -> FrozenComposition:
    composition_path = repo / manifest["graph"]["composition_manifests"][item.adapter]
    document = json.loads(composition_path.read_text(encoding="utf-8"))
    copies: dict[str, tuple[Path, tuple[str, ...]]] = {}
    plugins = []
    for plugin in document["plugins"]:
        if plugin.get("kind") != "wheel_plugin":
            raise SystemExit(f"unsupported manifest plugin kind: {plugin.get('kind')!r}")
        plugins.append(_editable_plugin(plugin, repo, stack, copies))
    _patch_copied_binding_plugin(
        copies=copies,
        adapter=item.adapter,
        binding_data={key: value for key, value in locked_binding.items() if key != "model"},
        secret_handle=item.secret_handle,
    )
    request = ResolutionRequest(
        product=_editable_product(document["product"], repo, stack, copies),
        plugins=tuple(plugins),
    )
    _activate_editable_imports(request)
    return RegistryPlatform().resolve(request)


def _runtime_authorization(item: ManifestItem) -> InvocationRuntimeAuthorization:
    sources = (
        SecretSourceBinding(
            handle=item.secret_handle,
            source_kind="environment",
            source_locator=item.secret_env,
        ),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=sources,
        digest=runtime_authorization_digest(sources),
    )


def _json_get(url: str) -> dict[str, Any]:
    payload = _fetch_json(url)
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    return payload


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=5) as response:  # noqa: S310 - pinned local/release endpoint
        return json.loads(response.read().decode("utf-8"))


def _load_cursor_api_key_from_keychain() -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603
            ["security", "find-generic-password", "-s", "cursor-access-token", "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None


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
        _fetch_json(f"{endpoint}/config")
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return _fail(f"OpenCode /config is unavailable at pinned endpoint {endpoint}: {error}")
    try:
        _fetch_json(f"{endpoint}/session")
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return _fail(f"OpenCode /session is unavailable at pinned endpoint {endpoint}: {error}")
    secret_env = adapter["secret_env"]
    if not os.environ.get(secret_env):
        os.environ[secret_env] = ""
    return 0


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
    secret_env = adapter["secret_env"]
    if not os.environ.get(secret_env):
        token = _load_cursor_api_key_from_keychain()
        if not token:
            return _fail(
                f"Cursor secret {secret_env!r} is unset and keychain login is unavailable; "
                "refusing to invent credentials"
            )
        os.environ[secret_env] = token
    return 0


def _collect_expected_artifacts(output: Path, item: ManifestItem) -> dict[str, str]:
    collected: dict[str, str] = {}
    for relative in item.expected_artifacts:
        path = output / relative
        if not path.is_file():
            raise SystemExit(f"expected artifact missing: {relative}")
        collected[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return collected


def _workspace_binding(engine_root: Path) -> InvocationWorkspaceBinding:
    project_root = engine_root.parent / f".{engine_root.name}-project"
    attempts_root = engine_root.parent / f".{engine_root.name}-attempts"
    receipts_root = engine_root.parent / f".{engine_root.name}-receipts"
    for path in (project_root, attempts_root, receipts_root):
        path.mkdir(exist_ok=True)
    return InvocationWorkspaceBinding(
        project_root=project_root,
        attempts_root=attempts_root,
        receipts_root=receipts_root,
    )


def _run_engine(
    *,
    composition: FrozenComposition,
    authorization: InvocationRuntimeAuthorization,
    engine_root: Path,
    invocation_id: str,
) -> Any:
    engine = Engine.production(engine_root, authorization=authorization)
    try:
        handle = engine.start(
            composition,
            entrypoint="run",
            invocation_id=invocation_id,
            seed=empty_invocation_seed(),
            authorization=authorization,
            workspace_binding=_workspace_binding(engine_root),
        )
        try:
            return engine.run_until_blocked(handle)
        finally:
            handle.close()
    finally:
        engine.close()


def _attempt_write_root(output: Path, item: ManifestItem) -> Path:
    return output / "engine" / "invocations" / item.item_id / "attempts"


def _published_workspace_output(write_root: Path, expected_path: str) -> Path:
    matches = sorted(
        path
        for path in write_root.rglob(expected_path)
        if path.is_file() and not path.is_symlink() and "trees" not in path.parts
    )
    if not matches:
        leftover_head = write_root.parent / "workspace" / "HEAD.json"
        if leftover_head.is_file():
            raise SystemExit("whole-tree workspace HEAD is not the published write-root output")
        raise SystemExit(f"expected write-root output {expected_path!r} is missing under {write_root}")
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly one write-root output {expected_path!r} under {write_root}, found {len(matches)}"
        )
    return matches[0]


def _validate_workspace_output(manifest: dict[str, Any], output: Path, item: ManifestItem) -> None:
    expected_path = manifest["expected_output"]["path"]
    expected_digest = manifest["expected_output"]["digest"]
    write_root = _attempt_write_root(output, item)
    published = _published_workspace_output(write_root, expected_path)
    actual_digest = canonical_digest(json.loads(published.read_text(encoding="utf-8")))
    if actual_digest != expected_digest:
        raise SystemExit(
            f"workspace output digest mismatch: expected {expected_digest}, found {actual_digest}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--adapter", choices=("opencode", "cursor"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    _reject_ambient_overrides()
    repo = _repo_root()
    manifest = _load_manifest(arguments.manifest.resolve())
    item = _manifest_item(manifest, arguments.adapter)
    if item.adapter_version != "0.1.0":
        return _fail(f"unsupported adapter_version {item.adapter_version!r}")

    output = arguments.output.resolve()
    if output.exists() and any(output.iterdir()):
        return _fail(f"result directory must be fresh and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    engine_root = output / "engine"
    engine_root.mkdir()

    _require_source_digest(
        repo,
        manifest["fixture"]["source_root"],
        manifest["fixture"]["source_digest"],
        "fixture",
    )
    adapter_meta = manifest["adapters"][arguments.adapter]
    _require_source_digest(
        repo, adapter_meta["source_root"], adapter_meta["source_digest"], arguments.adapter
    )

    frozen_request = _frozen_request(manifest)
    project_scope = (
        _attempt_project_scope(engine_root=engine_root, invocation_id=item.item_id)
        if item.adapter == "opencode"
        else None
    )
    locked_binding = _locked_binding(
        item,
        project_scope=project_scope,
        model=frozen_request.execution.provider_model,
    )

    if arguments.adapter == "opencode":
        preflight = _check_opencode(adapter_meta)
    else:
        preflight = _check_cursor(adapter_meta)
    if preflight != 0:
        return preflight

    with ExitStack() as stack:
        composition = _resolve_composition(repo, manifest, item, locked_binding, stack)
        authorization = _runtime_authorization(item)
        run_result = _run_engine(
            composition=composition,
            authorization=authorization,
            engine_root=engine_root,
            invocation_id=item.item_id,
        )
        if run_result.status != manifest["success"]["status"]:
            return _fail(
                f"engine terminal status {run_result.status!r} != expected {manifest['success']['status']!r}"
            )

        _validate_workspace_output(manifest, output, item)
        artifacts = _collect_expected_artifacts(output, item)
        result = {
            "adapter": item.adapter,
            "adapter_version": item.adapter_version,
            "item_id": item.item_id,
            "model": locked_binding["model"],
            "lock_digest": composition.lock.digest,
            "terminal_status": run_result.status,
            "artifacts": artifacts,
        }
        _reject_credentials_in_text(json.dumps(result), label="result.json")
        _write_json_atomic(output / "result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
