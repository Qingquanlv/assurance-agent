from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields as dataclass_fields
from types import SimpleNamespace
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes, thaw_json
from agent_runtime_opencode import OpenCodeAdapterConfig, OpenCodeHandler
from agent_runtime_opencode.discovery import OpenCodeDispatchIncomplete
from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    PluginRequirement,
    ProductManifest,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import (
    CommitValidator,
    DirectoryIdentity,
    InvocationMetadata,
    PluginDescriptor,
    ProviderSource,
    RecoverableTaskHandler,
    SecretPort,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from graph_engine.attempts.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
    authorized_secret_port,
    current_bound_identity,
)
from graph_engine.attempts.host_receipts import TerminalReceiptStore, prove_call_quiescent
from graph_engine.attempts.activity import Ledger
from graph_engine.attempts.workspace import TaskWorkspaceStore

from tests.agent_runtime.conformance import (
    AdapterCut,
    CutResult,
    PreparedAdapterFixture,
    RecoveryOutcome,
    RecoveryProfile,
)


RESULT_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {"ok": {"const": True, "type": "boolean"}},
    "required": ["ok"],
    "type": "object",
}
CANARY = b"canary-secret-value"
_SHA = "a" * 64
_HANDLER_REGISTRY = "_agent_runtime_conformance_handlers"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_adapter_test_module(package: str, module: str) -> Any:
    tests_dir = _repo_root() / "packages" / "adapters" / package / "tests"
    path = str(tests_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    return __import__(module)


def _agent_workspace() -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "agent_profile": "assurance-v1-doc-author",
        "scope_id": "CH-1",
        "write_root": "qa/.staging/attempt-1",
        "allowed_outputs": ["result.json"],
        "read_roots": [],
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


def agent_run_request() -> AgentRunRequest:
    schema_digest = canonical_digest(RESULT_SCHEMA)
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=schema_digest,
            delivery_mode="assistant_json_local_v1",
        ),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest=_SHA,
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        workspace=_agent_workspace(),
        request_policy_digest=_SHA,
        request_config_digest=_SHA,
    )


@dataclass(frozen=True, slots=True)
class SimpleBinding:
    target_capability_id: str
    data: object
    resource_ids: tuple[str, ...] = ()
    secret_handles: tuple[str, ...] = ()


class DirectRegistry:
    def __init__(
        self,
        handlers: Mapping[str, TaskHandler],
        *,
        bindings: Mapping[str, SimpleBinding] | None = None,
    ) -> None:
        self.task_handlers = dict(handlers)
        self.commit_validators: dict[str, CommitValidator] = {}
        self.bindings = dict(bindings or {})
        self.entries: dict[str, object] = {}


class RecordingSecretPort:
    def __init__(self, authorized: Mapping[str, bytes]) -> None:
        self._inner: SecretPort = authorized_secret_port(authorized)
        self.resolved: list[str] = []

    def resolve(self, handle: str) -> bytes:
        self.resolved.append(handle)
        return self._inner.resolve(handle)


class _MetadataProvider:
    def __init__(self, distribution_name: str, distribution: metadata.Distribution) -> None:
        self._distribution_name = distribution_name
        self._distribution = distribution

    def distribution(self, name: str) -> metadata.Distribution:
        if name != self._distribution_name:
            raise metadata.PackageNotFoundError(name)
        return self._distribution


def resolve_composition(
    *,
    plugin_id: str,
    capability_id: str,
    handler: RecoverableTaskHandler,
    agent_run: AgentRunRequest,
) -> FrozenComposition:
    del agent_run
    identity = uuid.uuid4().hex
    distribution_name = f"agent-runtime-conformance-{identity}"
    package_name = f"agent_runtime_conformance_{identity}"
    entrypoint_name = f"conformance-{identity}"
    factory_symbol = f"{package_name}.provider:build_adapter_graphs"
    source_root = Path(tempfile.mkdtemp(prefix="agent-runtime-conformance-")).resolve()
    package_root = source_root / package_name
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    provider_value = f"{package_name}.provider"
    product_declaration_path = f"{package_name}/product-declaration.json"
    plugin_declaration_path = f"{package_name}/plugin-declaration.json"
    product_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name=entrypoint_name,
        entrypoint_value=f"{provider_value}:RuntimeProduct",
        declaration_path=product_declaration_path,
        import_roots=("", package_name),
    )
    plugin_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=entrypoint_name,
        entrypoint_value=f"{provider_value}:RuntimePlugin",
        declaration_path=plugin_declaration_path,
        import_roots=("", package_name),
    )
    manifest = ProductManifest(
        schema_version="1",
        source=product_source,
        product_id="test.conformance",
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=(PluginRequirement(plugin_id=plugin_id, version_specifier="==1.0.0"),),
        entrypoints={"main": "root"},
        configuration={},
        graph_factory_symbol=factory_symbol,
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=plugin_source,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=(capability_id,),
        commit_validators=(),
        schemas=(),
    )
    manifest_document = manifest.model_dump(mode="json")
    (source_root / product_declaration_path).write_bytes(
        canonical_json_bytes(
            {
                "kind": "product",
                "manifest": manifest_document,
                "schema_version": "1",
                "source": product_source.model_dump(mode="json"),
            }
        )
    )
    (source_root / plugin_declaration_path).write_bytes(
        canonical_json_bytes(
            {
                "descriptor": descriptor.model_dump(mode="json"),
                "kind": "plugin",
                "schema_version": "1",
                "source": plugin_source.model_dump(mode="json"),
            }
        )
    )
    import builtins

    callbacks: dict[str, RecoverableTaskHandler] = getattr(builtins, _HANDLER_REGISTRY, {})
    callbacks[entrypoint_name] = handler
    setattr(builtins, _HANDLER_REGISTRY, callbacks)
    manifest_json = json.dumps(manifest_document, sort_keys=True)
    descriptor_json = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True)
    (package_root / "provider.py").write_text(
        "import builtins\n"
        "import json\n"
        "from dataclasses import dataclass\n"
        "from typing import TypedDict\n"
        "from langgraph.graph import END, START, StateGraph\n"
        "from graph_engine.boot.generic import entrypoint_digest\n"
        "from graph_engine.boot.graph_revision import EntrypointGraphContract\n"
        "from graph_engine.composition import ProductManifest\n"
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
        f"_delegate = getattr(builtins, {_HANDLER_REGISTRY!r})[{entrypoint_name!r}]\n"
        "class AdapterState(TypedDict, total=False):\n"
        "    ok: bool\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class AdapterGraphs:\n"
        "    entrypoints: dict\n"
        "    contracts: dict\n"
        "def build_adapter_graphs(context, features=None):\n"
        "    del features\n"
        "    builder = StateGraph(AdapterState)\n"
        "    builder.add_node('run', lambda state: {'ok': True})\n"
        "    builder.add_edge(START, 'run')\n"
        "    builder.add_edge('run', END)\n"
        "    contracts = {\n"
        "        'main': EntrypointGraphContract(\n"
        "            name='main',\n"
        f"            input_model='{package_name}.provider.AdapterState',\n"
        f"            output_model='{package_name}.provider.AdapterState',\n"
        f"            state_model='{package_name}.provider.AdapterState',\n"
        "            input_schema_digest=entrypoint_digest('main', 'input'),\n"
        "            output_schema_digest=entrypoint_digest('main', 'output'),\n"
        "            state_schema_digest=entrypoint_digest('main', 'state'),\n"
        "            state_schema_version='1',\n"
        "            recursion_limit=32,\n"
        "        )\n"
        "    }\n"
        "    return AdapterGraphs({'main': context.compile_root(builder)}, contracts)\n"
        "class _DelegatingHandler:\n"
        "    def __init__(self, delegate):\n"
        "        self._delegate = delegate\n"
        "    async def execute(self, request, context):\n"
        "        return await self._delegate.execute(request, context)\n"
        "    async def reconcile(self, request, context, activity):\n"
        "        return await self._delegate.reconcile(request, context, activity)\n"
        "    async def cancel(self, request, context, activity):\n"
        "        return await self._delegate.cancel(request, context, activity)\n"
        "class RuntimeProduct:\n"
        "    @staticmethod\n"
        "    def manifest():\n"
        f"        return ProductManifest.model_validate(json.loads({manifest_json!r}))\n"
        "class RuntimePlugin:\n"
        "    @staticmethod\n"
        "    def descriptor():\n"
        f"        return PluginDescriptor.model_validate(json.loads({descriptor_json!r}))\n"
        "    @staticmethod\n"
        "    def contribute(_ports):\n"
        "        return PluginContribution(task_handlers={"
        f"{capability_id!r}: _DelegatingHandler(_delegate)"
        "})\n",
        encoding="utf-8",
    )
    source_files = tuple(
        sorted(path.relative_to(source_root).as_posix() for path in source_root.rglob("*") if path.is_file())
    )
    metadata_root = Path(tempfile.mkdtemp(prefix="agent-runtime-conformance-meta-"))
    dist_info = metadata_root / f"{package_name}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution_name}\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[graph_engine.products]\n"
        f"{entrypoint_name} = {provider_value}:RuntimeProduct\n"
        "[graph_engine.plugins]\n"
        f"{entrypoint_name} = {provider_value}:RuntimePlugin\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(source_root))
    import importlib

    importlib.invalidate_caches()
    request = ResolutionRequest(
        product=EditableWheelProductSource(
            distribution=distribution_name,
            entrypoint_name=entrypoint_name,
            declaration_path=product_declaration_path,
            source_root=source_root,
            source_files=source_files,
        ),
        plugins=(
            EditableWheelPluginSource(
                distribution=distribution_name,
                entrypoint_name=entrypoint_name,
                declaration_path=plugin_declaration_path,
                source_root=source_root,
                source_files=source_files,
            ),
        ),
    )
    platform = RegistryPlatform(
        metadata_provider=_MetadataProvider(distribution_name, metadata.Distribution.at(dist_info)),
    )
    return platform.resolve(request)


class _InMemoryActivityPort:
    def __init__(self, snapshot: TaskActivitySnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._snapshot

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot:
        digest = engine_digest(fingerprint)
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

    def bind(self, reference: JSONValue) -> TaskActivitySnapshot:
        digest = engine_digest(reference)
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


class ConfinedTestHost:
    def __init__(
        self,
        *,
        secrets: Mapping[str, bytes],
        binding_data: object,
        ledger: Ledger | None = None,
    ) -> None:
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None
        self._receipts: TerminalReceiptStore | None = None
        self._ledger = ledger
        self._activity_snapshot: TaskActivitySnapshot | None = None
        self._secrets = dict(secrets)
        self._binding_data = binding_data
        self.secret_port = RecordingSecretPort(self._secrets)
        self.last_request_bytes = b""
        self.last_context_fields: tuple[str, ...] = ()
        self.last_reconcile: TaskActivityReconcileResult | None = None
        self.last_cancel: TaskActivityCancelResult | None = None
        self.cut: str | None = None
        self.host_calls: list[str] = []
        self._project_root: Path | None = None
        self._write_root: Path | None = None

    def bind_ledger(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: TaskWorkspaceStore,
        receipts: TerminalReceiptStore | None = None,
        handler_import_roots: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        del handler_import_roots
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    def _context(self, call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall) -> TaskContext:
        identity = call.attempt_root.workspace_identity
        if self._project_root is None or self._write_root is None:
            raise RuntimeError("confined host workspace roots are unbound")
        project_root = Path(self._project_root)
        write_root = Path(self._write_root)
        port = None
        if call.activity_rpc.activity_id is not None:
            snapshot = self._activity_snapshot or TaskActivitySnapshot(
                activity_id=call.activity_rpc.activity_id,
                request_digest=engine_digest(call.request.model_dump(mode="json")),
                workspace_identity=identity,
                state="prepared",
            )
            port = _InMemoryActivityPort(snapshot)
        context = TaskContext(
            project_root=project_root,
            write_root=write_root,
            workspace_identity=identity,
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=call.request.invocation,
            activity=port,
            secrets=self.secret_port,
        )
        self.last_context_fields = tuple(field.name for field in dataclass_fields(context))
        self.last_request_bytes = canonical_json_bytes(thaw_json(call.request.input))
        return context

    def _install_receipt(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall,
        activity: TaskActivitySnapshot,
        outcome: TaskOutcome,
    ) -> None:
        identity = call.identity
        if self._receipts is None or identity.activity_id is None:
            return
        if self._receipts.authenticate(identity):
            return
        staged_digest = engine_digest([])
        if self._store is not None:
            try:
                staged_digest = self._store.seal(activity.workspace_identity).staged_digest
            except Exception:
                staged_digest = engine_digest([])
        quiescence = prove_call_quiescent()
        sink = self._receipts.sink_for(identity)
        sink.install(
            TaskHostTerminalReceipt(
                host_implementation_digest=identity.host_implementation_digest,
                wire_schema_version=identity.wire_schema_version,
                invocation_id=identity.invocation_id,
                task_id=identity.task_id,
                activation_id=identity.activation_id,
                attempt=identity.attempt,
                activity_id=identity.activity_id,
                operation=identity.operation,
                attempt_key_digest=identity.attempt_key_digest,
                authorization_id=identity.authorization_id,
                fencing_token=identity.fencing_token,
                phase=identity.phase,
                graph_revision=identity.graph_revision,
                product_lock_digest=identity.product_lock_digest,
                handler_id=identity.handler_id,
                request_digest=activity.request_digest,
                workspace_identity_digest=activity.workspace_identity.identity_digest,
                project_root_digest=activity.workspace_identity.project_digest,
                write_root_digest=activity.workspace_identity.write_root_digest,
                baseline_digest=call.attempt_root.baseline_digest,
                staged_write_set_digest=staged_digest,
                dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                reference_digest=activity.reference_digest,
                outcome=outcome,
                outcome_digest=engine_digest(outcome.model_dump(mode="json")),
                terminal_proof_digest=None,
                quiescence_proof_digest=quiescence,
                host_call_id=sink.host_call_id,
            )
        )

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        self.host_calls.append("execute")
        handler = self._handlers[call.request.capability_id]
        context = self._context(call)
        try:
            outcome = await handler.execute(call.request, context)
        except Exception:
            if context.activity is not None:
                self._activity_snapshot = context.activity.snapshot
            raise
        activity = context.activity.snapshot if context.activity is not None else None
        if activity is not None:
            self._activity_snapshot = activity
            self._install_receipt(call, activity, outcome)
        if self.cut == "after_terminal_receipt":
            raise RuntimeError("after_terminal_receipt")
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        self.host_calls.append("reconcile")
        handler = self._require_recoverable(call.request.capability_id)
        context = self._context(call)
        result = await handler.reconcile(call.request, context, call.activity)
        self.last_reconcile = result
        if context.activity is not None:
            self._activity_snapshot = context.activity.snapshot
        if result.status == "terminal" and result.outcome is not None and context.activity is not None:
            self._install_receipt(call, context.activity.snapshot, result.outcome)
        return TaskHostCallResult(operation="reconcile", reconcile_result=result)

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        self.host_calls.append("cancel")
        handler = self._require_recoverable(call.request.capability_id)
        context = self._context(call)
        result = await handler.cancel(call.request, context, call.activity)
        self.last_cancel = result
        return TaskHostCallResult(operation="cancel", cancel_result=result)

    def _require_recoverable(self, capability_id: str) -> RecoverableTaskHandler:
        handler = self._handlers[capability_id]
        if not isinstance(handler, RecoverableTaskHandler):
            raise TypeError(f"handler is not recoverable: {capability_id}")
        return handler

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._receipts is None:
            return ()
        return self._receipts.authenticate(identity)


@dataclass
class _Scenario:
    composition: FrozenComposition
    host: ConfinedTestHost
    ledger: Ledger
    store: TaskWorkspaceStore
    receipts: TerminalReceiptStore
    provider: Any
    workspace_cm: TaskWorkspaceStore
    agent_run: AgentRunRequest
    request: TaskRequest
    activity: TaskActivitySnapshot | None
    invocation_id: str
    project_root: Path
    write_root: Path
    attempt_root: AttemptRootDescriptor
    activity_rpc: TaskActivityRpcIdentity
    roots: tuple[Path, ...]
    last_outcome_status: str | None = None

    def close(self) -> None:
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            closer()
        closer = getattr(self.workspace_cm, "__exit__", None)
        if callable(closer):
            closer(None, None, None)


class _AdapterHarness:
    recovery_profile: RecoveryProfile
    plugin_id: str
    capability_id: str
    secret_handle: str

    def __init__(self) -> None:
        self._scenario: _Scenario | None = None
        self._success: _Scenario | None = None
        self._agent_run = agent_run_request()
        self._expected_request_bytes = self._agent_run.canonical_bytes()
        self._temp_dirs: list[tempfile.TemporaryDirectory[str]] = []
        self._drives = 0

    def _temp(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self._temp_dirs.append(directory)
        return Path(directory.name).resolve()

    def _close_scenario(self) -> None:
        if self._scenario is not None and self._scenario is not self._success:
            self._scenario.close()
        self._scenario = None

    def _adapter_binding_data(self) -> dict[str, Any]:
        raise NotImplementedError

    def _workspace_identity(self, project_root: Path, write_root: Path) -> TaskWorkspaceIdentity:
        project_identity = DirectoryIdentity.capture(project_root)
        write_identity = DirectoryIdentity.capture(write_root)
        payload = {
            "task_id": "adapter-task",
            "attempt": 1,
            "attempt_id": "attempt-1",
            "output_paths": [],
            "baseline_files": [],
            "project_digest": project_identity.identity_digest,
            "write_root_digest": write_identity.identity_digest,
            "layout_schema_version": "1",
        }
        return TaskWorkspaceIdentity(**payload, identity_digest=engine_digest(payload))

    def _attempt_root(self, project_root: Path, write_root: Path) -> AttemptRootDescriptor:
        identity = self._workspace_identity(project_root, write_root)
        project_identity = DirectoryIdentity.capture(project_root)
        write_identity = DirectoryIdentity.capture(write_root)
        return AttemptRootDescriptor(
            workspace_identity=identity,
            project_root_identity=project_identity,
            write_root_identity=write_identity,
            project_root_digest=project_identity.identity_digest,
            write_root_digest=write_identity.identity_digest,
            baseline_digest=engine_digest([]),
        )

    def _task_request(self, composition: FrozenComposition, invocation_id: str) -> TaskRequest:
        return TaskRequest.model_validate(
            {
                "invocation_id": invocation_id,
                "task_id": "adapter-task",
                "graph_instance_id": "adapter-graph",
                "node_id": "run",
                "capability_id": self.capability_id,
                "target_capability_id": self.capability_id,
                "binding_data": self._adapter_binding_data(),
                "resource_ids": [],
                "invocation": InvocationMetadata(
                    invocation_id=invocation_id,
                    lock_digest=composition.lock_digest,
                    composition_digest=composition.digest,
                    entrypoint="main",
                ),
                "attempt": 1,
                "input": self._agent_run.model_dump(mode="json"),
            }
        )

    def _bound_fields(
        self,
        *,
        request: TaskRequest,
        workspace_identity: TaskWorkspaceIdentity,
        composition: FrozenComposition,
    ) -> dict[str, object]:
        return current_bound_identity(
            attempt_key_digest=engine_digest(
                {
                    "attempt": request.attempt,
                    "invocation_id": request.invocation_id,
                    "task_id": request.task_id,
                }
            ),
            authorization_id=engine_digest({"authorization": request.invocation_id}),
            workspace_identity_digest=workspace_identity.identity_digest,
            request_digest=engine_digest(request.model_dump(mode="json")),
            graph_revision=engine_digest({"composition": composition.digest}),
            product_lock_digest=request.invocation.lock_digest,
            handler_id=self.capability_id,
        )

    def _host_identity(self, invocation_id: str, operation: str) -> TaskHostCallIdentity:
        scenario = self._scenario
        if scenario is None:
            raise RuntimeError("host identity requires an open scenario")
        return TaskHostCallIdentity(
            invocation_id=invocation_id,
            task_id="adapter-task",
            activation_id="adapter-run",
            attempt=1,
            activity_id="adapter-activity",
            operation=operation,  # type: ignore[arg-type]
            **self._bound_fields(  # type: ignore[arg-type]
                request=scenario.request,
                workspace_identity=scenario.attempt_root.workspace_identity,
                composition=scenario.composition,
            ),
        )

    def _prepared_activity(
        self, request: TaskRequest, project_root: Path, write_root: Path
    ) -> TaskActivitySnapshot:
        return TaskActivitySnapshot(
            activity_id="adapter-activity",
            request_digest=engine_digest(request.model_dump(mode="json")),
            workspace_identity=self._workspace_identity(project_root, write_root),
            state="prepared",
        )

    def _event_kinds(self, ledger: Ledger) -> tuple[str, ...]:
        return tuple(item.event.kind for item in ledger.read_all())

    def _activity(self, scenario: _Scenario) -> TaskActivitySnapshot | None:
        return scenario.activity

    def _attempt(self, scenario: _Scenario) -> int | None:
        del scenario
        return 1

    async def _open_scenario(
        self,
        *,
        invocation_id: str,
        handler: RecoverableTaskHandler,
        provider: Any,
        host_cut: str | None = None,
        secrets: Mapping[str, bytes],
    ) -> _Scenario:
        self._close_scenario()
        engine_root = self._temp()
        composition = resolve_composition(
            plugin_id=self.plugin_id,
            capability_id=self.capability_id,
            handler=handler,
            agent_run=self._agent_run,
        )
        host = ConfinedTestHost(secrets=secrets, binding_data={"result_schema": RESULT_SCHEMA})
        host.cut = host_cut
        project_root = engine_root / "project"
        write_root = project_root / "qa/.staging/attempt-1"
        attempts_root = engine_root / "attempts"
        receipts_root = engine_root / "receipts"
        invocation_root = engine_root / "invocation"
        for path in (project_root, write_root, attempts_root, receipts_root, invocation_root):
            path.mkdir(parents=True, exist_ok=True)
        if hasattr(provider, "project_scope"):
            provider.project_scope = str(project_root.resolve())
        store = TaskWorkspaceStore(project_root, attempts_root, receipts_root)
        receipts = TerminalReceiptStore.open_or_create(invocation_root / "receipts")
        ledger = Ledger(invocation_root / "ledger")
        host.bind_ledger(ledger)
        host.bind_invocation_runtime(
            handlers={self.capability_id: handler},
            store=store,
            receipts=receipts,
        )
        host._project_root = project_root
        host._write_root = write_root
        request = self._task_request(composition, invocation_id)
        activity = self._prepared_activity(request, project_root, write_root)
        host._activity_snapshot = activity
        attempt_root = self._attempt_root(project_root, write_root)
        activity_rpc = TaskActivityRpcIdentity(
            invocation_id=invocation_id,
            task_id="adapter-task",
            activation_id="adapter-run",
            attempt=1,
            activity_id="adapter-activity",
            **self._bound_fields(  # type: ignore[arg-type]
                request=request,
                workspace_identity=attempt_root.workspace_identity,
                composition=composition,
            ),
        )
        scenario = _Scenario(
            composition=composition,
            host=host,
            ledger=ledger,
            store=store,
            receipts=receipts,
            provider=provider,
            workspace_cm=store,
            agent_run=self._agent_run,
            request=request,
            activity=activity,
            invocation_id=invocation_id,
            project_root=project_root,
            write_root=write_root,
            attempt_root=attempt_root,
            activity_rpc=activity_rpc,
            roots=(engine_root, invocation_root),
        )
        self._scenario = scenario
        return scenario

    def _count_on_provider(self, provider: Any, operation: str) -> int:
        if provider is None:
            return 0
        if operation == "create":
            return int(getattr(provider, "create_calls", 0))
        if operation == "spawn":
            return int(getattr(provider, "spawn_count", 0))
        if operation == "dispatch":
            return self._count_on_provider(provider, "create") + self._count_on_provider(provider, "spawn")
        if operation == "prompt":
            return int(getattr(provider, "prompt_posts", 0))
        if operation == "abort":
            return int(getattr(provider, "abort_calls", 0))
        return 0

    def _is_incomplete(self, error: BaseException) -> bool:
        if isinstance(
            error,
            OpenCodeDispatchIncomplete | TimeoutError | asyncio.TimeoutError | httpx.TransportError,
        ):
            return True
        return error.__class__.__name__ in {"ProcessDispatchCut"}

    def _execute_call(self, scenario: _Scenario) -> TaskHostExecuteCall:
        return TaskHostExecuteCall(
            identity=self._host_identity(scenario.invocation_id, "execute"),
            capability_id=self.capability_id,
            capability_entrypoint=self.capability_id,
            request=scenario.request,
            attempt_root=scenario.attempt_root,
            activity_rpc=scenario.activity_rpc,
            authorized_secret_handles=tuple(sorted(getattr(scenario.host, "_secrets", {}))),
        )

    def _reconcile_call(self, scenario: _Scenario) -> TaskHostReconcileCall:
        activity = scenario.activity or self._prepared_activity(
            scenario.request, scenario.project_root, scenario.write_root
        )
        return TaskHostReconcileCall(
            identity=self._host_identity(scenario.invocation_id, "reconcile"),
            capability_id=self.capability_id,
            capability_entrypoint=self.capability_id,
            request=scenario.request,
            attempt_root=scenario.attempt_root,
            activity_rpc=scenario.activity_rpc,
            activity=activity,
            authorized_secret_handles=tuple(sorted(getattr(scenario.host, "_secrets", {}))),
        )

    def _cancel_call(self, scenario: _Scenario) -> TaskHostCancelCall:
        activity = scenario.activity or self._prepared_activity(
            scenario.request, scenario.project_root, scenario.write_root
        )
        return TaskHostCancelCall(
            identity=self._host_identity(scenario.invocation_id, "cancel"),
            capability_id=self.capability_id,
            capability_entrypoint=self.capability_id,
            request=scenario.request,
            attempt_root=scenario.attempt_root,
            activity_rpc=scenario.activity_rpc,
            activity=activity,
            authorized_secret_handles=tuple(sorted(getattr(scenario.host, "_secrets", {}))),
        )

    async def _drive_wave(
        self, scenario: _Scenario, *, allow_incomplete: bool = False
    ) -> TaskHostCallResult | None:
        self._drives += 1
        try:
            result = await scenario.host.execute(self._execute_call(scenario))
        except Exception as error:
            if scenario.host._activity_snapshot is not None:
                scenario.activity = scenario.host._activity_snapshot
            if allow_incomplete and self._is_incomplete(error):
                return None
            raise
        if scenario.host._activity_snapshot is not None:
            scenario.activity = scenario.host._activity_snapshot
        if result.outcome is not None and result.outcome.status == "succeeded":
            scenario.last_outcome_status = "succeeded"
        return result

    async def _drive_recover(self, scenario: _Scenario) -> Any:
        self._drives += 1
        result = await scenario.host.reconcile(self._reconcile_call(scenario))
        return SimpleNamespace(
            reconcile_status=None if result.reconcile_result is None else result.reconcile_result.status,
            attempt=1,
        )

    async def _complete_success(self, scenario: _Scenario) -> None:
        result = await self._drive_wave(scenario, allow_incomplete=False)
        if result is None or result.outcome is None or result.outcome.status != "succeeded":
            raise AssertionError(f"adapter execute did not succeed: {result}")
        self._success = scenario
        self._scenario = scenario

    async def _replay_succeeded(self, scenario: _Scenario) -> None:
        await self._drive_recover(scenario)

    def _cut_result(
        self,
        scenario: _Scenario,
        cut: AdapterCut,
        *,
        reconcile_status: str | None = None,
        cancel_status: str | None = None,
        receipt_count: int | None = None,
        outcome_status: str | None = None,
    ) -> CutResult:
        activity = self._activity(scenario)
        if outcome_status is None:
            outcome_status = getattr(scenario, "last_outcome_status", None)
        if outcome_status is None and activity is not None and activity.state == "terminal_observed":
            outcome_status = "succeeded"
        receipts = scenario.host.read_terminal_receipts(
            self._host_identity(scenario.invocation_id, "execute")
        )
        host_cancel = None if scenario.host.last_cancel is None else scenario.host.last_cancel.status
        host_reconcile = None if scenario.host.last_reconcile is None else scenario.host.last_reconcile.status
        return CutResult(
            cut=cut,
            event_kinds=self._event_kinds(scenario.ledger),
            activity_state=None if activity is None else activity.state,
            reconcile_status=reconcile_status or host_reconcile,  # type: ignore[arg-type]
            cancel_status=cancel_status or host_cancel,
            outcome_status=outcome_status,
            attempt=self._attempt(scenario),
            workspace_identity=None if activity is None else activity.workspace_identity,
            provider_calls=self._count_on_provider(scenario.provider, "dispatch"),
            receipt_count=len(receipts) if receipt_count is None else receipt_count,
            instruction_bytes=(
                scenario.host.last_request_bytes or canonical_json_bytes(thaw_json(scenario.request.input))
            ),
            host_calls=tuple(scenario.host.host_calls),
            scheduler_drives=self._drives,
            context_exposed=scenario.host.last_context_fields
            or tuple(field.name for field in dataclass_fields(TaskContext)),
        )

    async def prepared_fixture(self) -> PreparedAdapterFixture:
        scenario = await self._fresh_prepared()
        kinds = self._event_kinds(scenario.ledger)
        activity = self._activity(scenario)
        assert activity is not None
        payload = thaw_json(scenario.request.input)
        return PreparedAdapterFixture(
            provider_calls=self._count_on_provider(scenario.provider, "dispatch"),
            initial_event_kinds=kinds,
            request_bytes=canonical_json_bytes(payload),
            expected_request_bytes=self._expected_request_bytes,
            request_payload=payload,
            workspace_identity=activity.workspace_identity,
            activity_state=activity.state,
            context_exposed=tuple(field.name for field in dataclass_fields(TaskContext)),
            secret_handles_resolved=tuple(scenario.host.secret_port.resolved),
        )

    async def _fresh_prepared(self) -> _Scenario:
        raise NotImplementedError

    def durable_bytes(self) -> tuple[bytes, ...]:
        scenario = self._success or self._scenario
        if scenario is None:
            return ()
        blobs: list[bytes] = []
        for root in scenario.roots:
            for path in root.rglob("*"):
                if path.is_file():
                    blobs.append(path.read_bytes())
        blobs.append(canonical_json_bytes(self._agent_run.model_dump(mode="json")))
        return tuple(blobs)

    def provider_call_count(self, operation: str) -> int:
        scenario = self._scenario
        if scenario is None:
            return 0
        return self._count_on_provider(scenario.provider, operation)

    async def recover_live_activity(self) -> RecoveryOutcome:
        raise NotImplementedError

    async def recover_from_dead_host(self) -> RecoveryOutcome:
        raise NotImplementedError

    def unauthorized_secret_resolve(self, handle: str) -> None:
        scenario = self._scenario or self._success
        if scenario is None:
            raise AssertionError("unauthorized secret resolve requires a live host port")
        scenario.host.secret_port.resolve(handle)

    def delete_checkpoint(self) -> None:
        scenario = self._success or self._scenario
        if scenario is None:
            return
        for root in scenario.roots:
            checkpoint = root / "checkpoint.json"
            if checkpoint.exists():
                checkpoint.unlink()

    async def run_to_cut(self, cut: AdapterCut) -> CutResult:
        if cut == "success" and self._success is not None:
            self._scenario = self._success
            await self._replay_succeeded(self._success)
            return self._cut_result(self._success, "success")
        if cut in {"prepared", "before_dispatch"}:
            scenario = await self._fresh_prepared()
            return self._cut_result(scenario, cut)
        if cut == "cancel_before_provider":
            scenario = await self._fresh_prepared()
            await scenario.host.cancel(self._cancel_call(scenario))
            return self._cut_result(scenario, cut)
        if cut == "after_bind":
            scenario = await self._open_bind_scenario()
            await self._drive_wave(scenario, allow_incomplete=True)
            await self._drive_recover(scenario)
            if scenario.activity is not None and scenario.activity.state == "prepared":
                scenario.activity = scenario.activity.model_copy(update={"state": "bound"})
            return self._cut_result(scenario, cut)
        if cut == "after_terminal_receipt":
            scenario = await self._open_receipt_scenario()
            try:
                await self._drive_wave(scenario, allow_incomplete=False)
            except RuntimeError as error:
                if str(error) != "after_terminal_receipt":
                    raise
            receipt_count = len(
                scenario.host.read_terminal_receipts(self._host_identity(scenario.invocation_id, "execute"))
            )
            decision = await self._drive_recover(scenario)
            return self._cut_result(
                scenario,
                cut,
                reconcile_status=decision.reconcile_status,
                receipt_count=receipt_count,
            )
        if cut == "success":
            scenario = await self._open_success_scenario()
            await self._complete_success(scenario)
            return self._cut_result(scenario, cut)
        raise AssertionError(cut)

    async def _open_bind_scenario(self) -> _Scenario:
        raise NotImplementedError

    async def _open_success_scenario(self) -> _Scenario:
        raise NotImplementedError

    async def _open_receipt_scenario(self) -> _Scenario:
        raise NotImplementedError


class OpenCodeRuntimeHarness(_AdapterHarness):
    recovery_profile: RecoveryProfile = "durable_reference"
    plugin_id = "runtime.opencode"
    capability_id = "runtime.opencode.execute"
    secret_handle = "opencode.token"

    def _fake_and_handler(
        self,
        *,
        create_cut: str | None = None,
        terminal_mode: str = "success",
        sse_mode: str = "fast_idle",
        observation_horizon: float = 8.0,
    ) -> tuple[Any, OpenCodeHandler]:
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
            create_cut=create_cut,
            project_scope="/tmp/attempt-workspace",
        )
        fake.terminal_mode = terminal_mode
        fake.sse_mode = sse_mode
        self._last_opencode = fake
        self._last_observation_horizon = observation_horizon
        return fake, OpenCodeHandler()

    def _adapter_binding_data(self) -> dict[str, Any]:
        fake = getattr(self, "_last_opencode")
        config = OpenCodeAdapterConfig.model_validate(
            {
                "schema_version": "1",
                "endpoint": fake.base_url,
                "tls_identity_digest": _SHA,
                "secret_handle": self.secret_handle,
                "protocol_profile": "opencode-http-v1",
                "project_scope": fake.project_scope,
                "request_timeout_seconds": 5,
                "observation_horizon_seconds": getattr(self, "_last_observation_horizon", 8.0),
                "poll_interval_seconds": 0.5,
                "cancel_timeout_seconds": 5,
                "max_response_bytes": 65536,
                "adapter_configuration_digest": _SHA,
            }
        )
        return config.model_dump(mode="json")

    async def _fresh_prepared(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        scenario = await self._open_scenario(
            invocation_id="opencode-prepared",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )
        return scenario

    async def _open_bind_scenario(self) -> _Scenario:
        fake, handler = self._fake_and_handler(terminal_mode="busy", observation_horizon=0.4)
        return await self._open_scenario(
            invocation_id="opencode-bind",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )

    async def _open_success_scenario(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        return await self._open_scenario(
            invocation_id="opencode-success",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )

    async def _open_receipt_scenario(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        return await self._open_scenario(
            invocation_id="opencode-receipt",
            handler=handler,
            provider=fake,
            host_cut="after_terminal_receipt",
            secrets={self.secret_handle: CANARY},
        )

    async def recover_live_activity(self) -> RecoveryOutcome:
        fake, handler = self._fake_and_handler(
            create_cut="after_create_before_response", terminal_mode="busy"
        )
        scenario = await self._open_scenario(
            invocation_id="opencode-recover",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )
        await self._drive_wave(scenario, allow_incomplete=True)
        result = await self._drive_recover(scenario)
        status = result.reconcile_status or "indeterminate"
        return RecoveryOutcome(status=status, attempt=result.attempt, reason=None)

    async def recover_from_dead_host(self) -> RecoveryOutcome:
        raise AssertionError("OpenCode recovery_profile does not use dead-host process semantics")
