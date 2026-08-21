from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes, thaw_json
from agent_runtime_cursor import CursorAdapterConfig, CursorHandler
from agent_runtime_opencode import OpenCodeAdapterConfig, OpenCodeHandler
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import (
    AttemptWorkspaceIdentity,
    InvocationMetadata,
    SecretHandleUnauthorized,
    TaskActivitySnapshot,
    TaskContext,
    TaskRequest,
)

from agent_runtime_fixture import (
    EXPECTED_AGENT_RUN_REQUEST_BYTES,
    assemble_request,
    fixture_config,
    fixture_resources,
)

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT = _REPO / "examples" / "agent-runtime-fixture"
_MANIFESTS = _FIXTURE_ROOT / "manifests"
_SHA = "a" * 64
_STRUCTURED = {"status": "ok", "artifact": "result.json"}
_OPENCODE = "runtime.opencode.execute"
_CURSOR = "runtime.cursor.execute"


@dataclass
class FixtureBinding:
    target: str
    composition: Any
    request: AgentRunRequest
    request_bytes: bytes
    captured_request_bytes: bytes
    lock_digest: str
    result: AgentRunResult | None = None
    _keep_alive: ExitStack = field(default_factory=ExitStack, repr=False)


class _SecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


class _ActivityPort:
    def __init__(self, snapshot: TaskActivitySnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._snapshot

    def mark_dispatch_started(self, fingerprint: object) -> TaskActivitySnapshot:
        digest = canonical_digest(fingerprint)
        current = self._snapshot.dispatch_fingerprint_digest
        if current is not None:
            if current != digest:
                raise ValueError("dispatch fingerprint drifted from the durable activity")
            return self._snapshot
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "dispatch_started",
                "dispatch_fingerprint": fingerprint,
                "dispatch_fingerprint_digest": digest,
            }
        )
        return self._snapshot

    def bind(self, reference: object) -> TaskActivitySnapshot:
        digest = canonical_digest(reference)
        current = self._snapshot.reference_digest
        if current is not None:
            if current != digest:
                raise ValueError("activity reference changed after bind")
            return self._snapshot
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "bound",
                "reference": reference,
                "reference_digest": digest,
            }
        )
        return self._snapshot


def _load_adapter_test_module(package: str, module: str) -> Any:
    tests_dir = _REPO / "packages" / package / "tests"
    path = str(tests_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    return __import__(module)


def _manifest_name(target: str) -> str:
    if target == _OPENCODE:
        return "opencode.json"
    if target == _CURSOR:
        return "cursor.json"
    raise ValueError(f"unsupported adapter target: {target}")


_WORKSPACE_PACKAGES = {
    "agent-runtime-fixture": (_REPO / "examples" / "agent-runtime-fixture", "agent_runtime_fixture"),
    "agent-runtime-opencode": (_REPO / "packages" / "agent-runtime-opencode", "agent_runtime_opencode"),
    "agent-runtime-cursor": (_REPO / "packages" / "agent-runtime-cursor", "agent_runtime_cursor"),
}


def _copied_package(
    stack: ExitStack,
    distribution: str,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
) -> tuple[Path, tuple[str, ...]]:
    cached = copies.get(distribution)
    if cached is not None:
        return cached
    project_root, package = _WORKSPACE_PACKAGES[distribution]
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
    stack: ExitStack,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
) -> EditableWheelProductSource:
    distribution = str(document["distribution"])
    root, files = _copied_package(stack, distribution, copies)
    return EditableWheelProductSource(
        distribution=distribution,
        entrypoint_name=str(document["entrypoint_name"]),
        declaration_path=str(document["declaration_path"]),
        source_root=root,
        source_files=files,
    )


def _editable_plugin(
    document: dict[str, object],
    stack: ExitStack,
    copies: dict[str, tuple[Path, tuple[str, ...]]],
) -> EditableWheelPluginSource:
    distribution = str(document["distribution"])
    root, files = _copied_package(stack, distribution, copies)
    return EditableWheelPluginSource(
        distribution=distribution,
        entrypoint_name=str(document["entrypoint_name"]),
        declaration_path=str(document["declaration_path"]),
        source_root=root,
        source_files=files,
    )


def _resolution_request(target: str, stack: ExitStack) -> ResolutionRequest:
    document = json.loads((_MANIFESTS / _manifest_name(target)).read_text(encoding="utf-8"))
    copies: dict[str, tuple[Path, tuple[str, ...]]] = {}
    plugins: list[EditableWheelPluginSource] = []
    for plugin in document["plugins"]:
        kind = plugin.get("kind")
        if kind != "wheel_plugin":
            raise ValueError(f"unsupported manifest plugin kind: {kind}")
        plugins.append(_editable_plugin(plugin, stack, copies))
    return ResolutionRequest(
        product=_editable_product(document["product"], stack, copies),
        plugins=tuple(plugins),
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
    prefixes = (
        "agent_runtime_fixture",
        "agent_runtime_opencode",
        "agent_runtime_cursor",
    )
    for name in tuple(sys.modules):
        if name in prefixes or name.startswith(tuple(f"{prefix}." for prefix in prefixes)):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _workflow_input(composition: Any) -> object:
    graph = composition.workflow.graphs[composition.manifest.entrypoints["run"]]
    return thaw_json(graph.nodes[graph.start].definition.input)


async def resolve_fixture_composition(target: str) -> FixtureBinding:
    stack = ExitStack()
    request_sources = _resolution_request(target, stack)
    _activate_editable_imports(request_sources)
    composition = RegistryPlatform().resolve(request_sources)
    request = assemble_request(fixture_resources(), fixture_config())
    request_bytes = request.canonical_bytes()
    assert canonical_json_bytes(_workflow_input(composition)) == request_bytes
    binding = composition.registries.capabilities.bindings["fixture.binding.run"]
    assert binding.target_capability_id == target
    return FixtureBinding(
        target=target,
        composition=composition,
        request=request,
        request_bytes=request_bytes,
        captured_request_bytes=request_bytes,
        lock_digest=composition.lock_digest,
        _keep_alive=stack,
    )


def _task_request(fixture: FixtureBinding, workspace_root: Path) -> tuple[TaskRequest, _ActivityPort]:
    binding = fixture.composition.registries.capabilities.bindings["fixture.binding.run"]
    request = TaskRequest.model_validate(
        {
            "invocation_id": "fixture-inv",
            "task_id": "fixture-task",
            "graph_instance_id": "fixture-graph",
            "node_id": "run",
            "capability_id": "fixture.binding.run",
            "target_capability_id": fixture.target,
            "binding_data": thaw_json(binding.data),
            "resource_ids": list(binding.resource_ids),
            "invocation": InvocationMetadata(
                invocation_id="fixture-inv",
                lock_digest=fixture.lock_digest,
                composition_digest=fixture.composition.digest,
                entrypoint="run",
            ),
            "attempt": 1,
            "input": fixture.request.model_dump(mode="json"),
        }
    )
    snapshot = TaskActivitySnapshot(
        activity_id="fixture-activity",
        request_digest=canonical_digest(request.model_dump(mode="json")),
        workspace_identity=AttemptWorkspaceIdentity(
            attempt_directory_id=workspace_root.name,
            baseline_tree_id=_SHA,
            attempt_identity_digest="b" * 64,
        ),
        state="prepared",
    )
    return request, _ActivityPort(snapshot)


def _context(workspace_root: Path, port: _ActivityPort, secrets: dict[str, bytes]) -> TaskContext:
    return TaskContext(
        workspace_root=workspace_root,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="fixture-inv",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="run",
        ),
        activity=port,
        secrets=_SecretPort(secrets),
    )


def _cursor_success_bytes(cwd: str) -> bytes:
    init = {"type": "system", "subtype": "init", "cwd": cwd, "session_id": "sess-fixture"}
    terminal = {
        "is_error": False,
        "result": _STRUCTURED,
        "session_id": "sess-fixture",
        "subtype": "success",
        "type": "result",
    }
    return (json.dumps(init) + "\n" + json.dumps(terminal) + "\n").encode("utf-8")


async def _run_opencode(fixture: FixtureBinding) -> AgentRunResult:
    fake_mod = _load_adapter_test_module("agent-runtime-opencode", "fake_server")
    profile_mod = __import__("agent_runtime_opencode.protocol", fromlist=["OpenCodeProtocolProfile"])
    fake = fake_mod.OpenCodeFakeServer(
        profile=profile_mod.OpenCodeProtocolProfile.model_validate(
            {
                "prompt_idempotency": "conflict-on-body-drift",
                "metadata_supported": True,
                "sse_supported": True,
                "poll_fallback_supported": True,
            }
        ),
        project_scope="/tmp/attempt-workspace",
    )
    fake.terminal_mode = "success"
    fake.sse_mode = "fast_idle"
    fake.structured_result = _STRUCTURED
    config = OpenCodeAdapterConfig.model_validate(
        {
            "schema_version": "1",
            "endpoint": fake.base_url,
            "tls_identity_digest": _SHA,
            "secret_handle": "opencode.token",
            "protocol_profile": "opencode-http-v1",
            "project_scope": fake.project_scope,
            "request_timeout_seconds": 5,
            "observation_horizon_seconds": 8,
            "max_response_bytes": 65536,
        }
    )
    try:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw, "attempt-1")
            workspace.mkdir()
            request, port = _task_request(fixture, workspace)
            outcome = await OpenCodeHandler(config).execute(
                request,
                _context(workspace, port, {"opencode.token": b"fixture-opencode-secret"}),
            )
    finally:
        fake.close()
    assert outcome.status == "succeeded", outcome
    result = AgentRunResult.model_validate(thaw_json(outcome.output))
    fixture.captured_request_bytes = fixture.request.canonical_bytes()
    fixture.result = result
    return result


async def _run_cursor(fixture: FixtureBinding) -> AgentRunResult:
    fake_mod = _load_adapter_test_module("agent-runtime-cursor", "fake_process_host")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        executable = root / "cursor"
        executable.write_bytes(b"cursor-binary")
        executable.chmod(0o755)
        workspace = root / "attempt-1"
        workspace.mkdir()
        host = fake_mod.FakeConfinedProcessHost(status="exited", exit_code=0)

        def _on_wait(_receipt: object) -> None:
            host.stdout = _cursor_success_bytes(str(host.launches[-1].cwd))

        host.wait_hook = _on_wait
        config = CursorAdapterConfig.model_validate(
            {
                "schema_version": "1",
                "executable": str(executable),
                "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "expected_version": "1.0.0",
                "secret_handle": "cursor.api-key",
                "environment_names": ["PATH", "CURSOR_API_KEY"],
                "graceful_cancel_seconds": 5,
                "forced_cancel_seconds": 10,
                "max_output_bytes": 65536,
                "max_line_bytes": 4096,
            }
        )
        request, port = _task_request(fixture, workspace)
        outcome = await CursorHandler(config, host).execute(
            request,
            _context(workspace, port, {"cursor.api-key": b"fixture-cursor-secret"}),
        )
    assert outcome.status == "succeeded", outcome
    result = AgentRunResult.model_validate(thaw_json(outcome.output))
    fixture.captured_request_bytes = fixture.request.canonical_bytes()
    fixture.result = result
    return result


async def run_fixture(fixture: FixtureBinding) -> AgentRunResult:
    if fixture.target == _OPENCODE:
        return await _run_opencode(fixture)
    if fixture.target == _CURSOR:
        return await _run_cursor(fixture)
    raise ValueError(f"unsupported adapter target: {fixture.target}")


async def run_both_fixture_bindings() -> tuple[FixtureBinding, FixtureBinding]:
    opencode = await resolve_fixture_composition(_OPENCODE)
    cursor = await resolve_fixture_composition(_CURSOR)
    opencode.result = await run_fixture(opencode)
    cursor.result = await run_fixture(cursor)
    return opencode, cursor


def _python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("target", [_OPENCODE, _CURSOR])
async def test_fixture_rebinds_without_engine_change(target: str) -> None:
    fixture = await resolve_fixture_composition(target)
    result = await run_fixture(fixture)
    assert fixture.captured_request_bytes == EXPECTED_AGENT_RUN_REQUEST_BYTES
    assert result.structured_result == _STRUCTURED


async def test_adapter_rebinding_changes_lock_and_evidence_not_request() -> None:
    opencode, cursor = await run_both_fixture_bindings()
    assert opencode.request_bytes == cursor.request_bytes
    assert opencode.request_bytes == EXPECTED_AGENT_RUN_REQUEST_BYTES
    assert opencode.lock_digest != cursor.lock_digest
    assert opencode.result is not None and cursor.result is not None
    assert opencode.result.structured_result == cursor.result.structured_result == _STRUCTURED
    assert opencode.result.evidence_digest != cursor.result.evidence_digest
    assert opencode.composition.manifest.workflow == cursor.composition.manifest.workflow
    assert (
        opencode.composition.registries.resources.entries["fixture.runtime.instructions"].content
        == cursor.composition.registries.resources.entries["fixture.runtime.instructions"].content
    )


def test_fixture_import_does_not_load_adapters_or_assurance() -> None:
    forbidden = {
        "agent_runtime_opencode",
        "agent_runtime_cursor",
        "assurance_agent",
        "assurance_kernel",
    }
    fixture_root = _FIXTURE_ROOT / "agent_runtime_fixture"
    for path in _python_files(fixture_root):
        assert not (forbidden & _imported_modules(path)), path


def test_graph_engine_source_does_not_import_the_fixture() -> None:
    engine_root = _REPO / "packages" / "graph-engine" / "graph_engine"
    for path in _python_files(engine_root):
        assert "agent_runtime_fixture" not in _imported_modules(path), path
