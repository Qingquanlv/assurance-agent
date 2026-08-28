from __future__ import annotations

import asyncio
import builtins
import fcntl
import hashlib
import importlib
from importlib import metadata
import json
import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import ValidationError

import graph_engine.runtime.engine as engine_runtime
import graph_engine.runtime.ledger as ledger_runtime
import graph_engine.runtime.scheduler as scheduler_runtime
from graph_engine import ENGINE_API_VERSION
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    PluginRequirement,
    ProductManifest,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.graph.schema import WorkflowDef
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import (
    EffectApplyResult,
    EffectPolicy,
    EffectReconcileResult,
    InvocationWorkspaceBinding,
    PluginDescriptor,
    PromotionReceipt,
    ProviderSource,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskFailure,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.runtime.activity import LedgerTaskActivityPort
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostExecuteCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.host_receipts import TerminalReceiptStore, prove_call_quiescent
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.secret_sources import InvocationRuntimeAuthorization
from graph_engine.runtime.seed import InvocationSeed, empty_invocation_seed
from graph_engine.runtime.engine import (
    Engine as RuntimeEngine,
    EngineConflictError,
    EngineError,
    EnginePublicationIndeterminate,
    InvocationHandle,
    RunResult,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphFailed,
    GraphStarted,
    InterruptResumed,
    InvocationFinished,
    NodeActivated,
    NodeCompleted,
    NodeFailed,
    NodeInterrupted,
    RuntimeEvent,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskLeaseAcquired,
    TaskLeaseHeartbeat,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.ledger import Ledger, LedgerIntegrityError
from graph_engine.runtime.invocation_lock import InvocationDrift
from graph_engine.runtime.models import ProjectionError, fold_events
from graph_engine.runtime.planner import activation_id, plan_next, task_id
from graph_engine.runtime.scheduler import FakeClock
from graph_engine.runtime.scheduler import Scheduler
from graph_engine.runtime.task_workspace import (
    PromotionPublicationIndeterminate,
    TaskWorkspaceStore,
)


class Engine(RuntimeEngine):
    """Test harness that supplies the mandatory process-local workspace roots."""

    def _test_workspace_binding(self, invocation_id: str) -> InvocationWorkspaceBinding:
        root = self._root / ".test-workspaces" / canonical_digest({"invocation_id": invocation_id})
        project_root = root / "project"
        attempts_root = root / "attempts"
        receipts_root = root / "promotion-receipts"
        for directory in (project_root, attempts_root, receipts_root):
            directory.mkdir(parents=True, exist_ok=True)
        return InvocationWorkspaceBinding(project_root, attempts_root, receipts_root)

    def start(
        self,
        composition: FrozenComposition,
        *,
        entrypoint: str,
        invocation_id: str,
        seed: InvocationSeed,
        authorization: InvocationRuntimeAuthorization,
        workspace_binding: InvocationWorkspaceBinding | None = None,
    ) -> InvocationHandle:
        return super().start(
            composition,
            entrypoint=entrypoint,
            invocation_id=invocation_id,
            seed=seed,
            authorization=authorization,
            workspace_binding=workspace_binding or self._test_workspace_binding(invocation_id),
        )

    def open(
        self,
        invocation_id: str,
        composition: FrozenComposition,
        *,
        authorization: InvocationRuntimeAuthorization,
        workspace_binding: InvocationWorkspaceBinding | None = None,
    ) -> InvocationHandle:
        return super().open(
            invocation_id,
            composition,
            authorization=authorization,
            workspace_binding=workspace_binding or self._test_workspace_binding(invocation_id),
        )


def _project_state_digest(handle: InvocationHandle) -> str:
    project_root = handle._workspace_binding.project_root
    files = [
        [
            path.relative_to(project_root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        ]
        for path in sorted(project_root.rglob("*"))
        if path.is_file()
    ]
    return canonical_digest(cast(JSONValue, files))


def _directory_state(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.fsencode(os.readlink(path))))
        elif path.is_dir():
            entries.append((relative, "directory", b""))
        else:
            entries.append((relative, "file", path.read_bytes()))
    return tuple(entries)


def _workspace_state(
    binding: InvocationWorkspaceBinding,
) -> tuple[tuple[tuple[str, str, bytes], ...], ...]:
    return tuple(
        _directory_state(root)
        for root in (binding.project_root, binding.attempts_root, binding.receipts_root)
    )


def _workspace_binding_at(root: Path) -> InvocationWorkspaceBinding:
    project_root = root / "project"
    attempts_root = root / "attempts"
    receipts_root = root / "receipts"
    for directory in (project_root, attempts_root, receipts_root):
        directory.mkdir(parents=True, exist_ok=True)
    return InvocationWorkspaceBinding(project_root, attempts_root, receipts_root)


class _FunctionHandler:
    def __init__(
        self,
        implementation: Callable[[TaskRequest, TaskContext], Awaitable[TaskOutcome]],
    ) -> None:
        self._implementation = implementation

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await self._implementation(request, context)


class _MetadataProvider:
    def __init__(self, distribution_name: str, distribution: metadata.Distribution) -> None:
        self._distribution_name = distribution_name
        self._distribution = distribution

    def distribution(self, name: str) -> metadata.Distribution:
        if name != self._distribution_name:
            raise metadata.PackageNotFoundError(name)
        return self._distribution


_CALLBACK_REGISTRY_NAME = "_graph_engine_runtime_test_callbacks"
_CALLBACKS: dict[str, Mapping[str, TaskHandler]] = {}
setattr(builtins, _CALLBACK_REGISTRY_NAME, _CALLBACKS)
_EFFECT_CALLBACK_REGISTRY_NAME = "_graph_engine_runtime_test_effect_callbacks"
_EFFECT_CALLBACKS: dict[str, Mapping[str, object]] = {}
setattr(builtins, _EFFECT_CALLBACK_REGISTRY_NAME, _EFFECT_CALLBACKS)
_EFFECT_POLICY_REGISTRY_NAME = "_graph_engine_runtime_test_effect_policies"
_EFFECT_POLICIES: dict[str, EffectPolicy] = {}
setattr(builtins, _EFFECT_POLICY_REGISTRY_NAME, _EFFECT_POLICIES)
_ENGINE_INTENT_SCHEMA = (
    b'{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"],"additionalProperties":false}'
)
_ENGINE_RECEIPT_SCHEMA = (
    b'{"type":"object","properties":{"remote_id":{"type":"string"}},"required":["remote_id"],'
    b'"additionalProperties":false}'
)


def _resolved(
    workflow: dict[str, object],
    handlers: Mapping[str, TaskHandler] | None = None,
    *,
    effect_handlers: Mapping[str, object] | None = None,
    effect_policy: EffectPolicy | None = None,
) -> FrozenComposition:
    """Resolve an authenticated editable test distribution through the public platform."""

    parsed_workflow = WorkflowDef.model_validate(workflow)
    selected_handlers = dict(handlers or {})
    selected_effects = dict(effect_handlers or {})
    identity = uuid.uuid4().hex
    distribution_name = f"graph-engine-runtime-test-{identity}"
    package_name = f"graph_engine_runtime_test_{identity}"
    entrypoint_name = f"runtime-{identity}"
    source_root = Path(tempfile.mkdtemp(prefix="graph-engine-runtime-source-")).resolve()
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
        import_roots=("",),
    )
    plugin_source = ProviderSource(
        distribution=distribution_name,
        version="1.0.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name=entrypoint_name,
        entrypoint_value=f"{provider_value}:RuntimePlugin",
        declaration_path=plugin_declaration_path,
        import_roots=("",),
    )
    manifest = ProductManifest(
        schema_version="1",
        source=product_source,
        product_id="test.product",
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=(PluginRequirement(plugin_id="test.empty", version_specifier="==1.0.0"),),
        entrypoints=dict(parsed_workflow.entrypoints),
        configuration={},
        workflow=parsed_workflow,
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=plugin_source,
        plugin_id="test.empty",
        plugin_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        task_handlers=tuple(sorted(selected_handlers)),
        commit_validators=(),
        schemas=("test.empty.intent", "test.empty.receipt") if selected_effects else (),
        effects=tuple(sorted(selected_effects)),
    )
    manifest_document = manifest.model_dump(mode="json")
    manifest_document["workflow"] = parsed_workflow.model_dump(mode="json", exclude_defaults=True)
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
    callback_key = f"runtime-{identity}"
    _CALLBACKS[callback_key] = selected_handlers
    _EFFECT_CALLBACKS[callback_key] = selected_effects
    _EFFECT_POLICIES[callback_key] = effect_policy or EffectPolicy(
        max_attempts=3,
        timeout_seconds=30,
        backoff_seconds=0,
    )
    manifest_json = json.dumps(manifest_document, sort_keys=True)
    descriptor_json = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True)
    (package_root / "provider.py").write_text(
        "import builtins\n"
        "import json\n"
        "from graph_engine.composition import ProductManifest\n"
        "from graph_engine.plugin_api import (\n"
        "    EffectRegistration,\n"
        "    PluginContribution,\n"
        "    PluginDescriptor,\n"
        "    SchemaContribution,\n"
        ")\n"
        f"_callbacks = getattr(builtins, {_CALLBACK_REGISTRY_NAME!r})[{callback_key!r}]\n"
        f"_effect_callbacks = getattr(builtins, {_EFFECT_CALLBACK_REGISTRY_NAME!r}).get({callback_key!r}, {{}})\n"
        f"_effect_policy = getattr(builtins, {_EFFECT_POLICY_REGISTRY_NAME!r})[{callback_key!r}]\n"
        "class _DelegatingHandler:\n"
        "    def __init__(self, delegate):\n"
        "        self._delegate = delegate\n"
        "    async def execute(self, request, context):\n"
        "        return await self._delegate.execute(request, context)\n"
        "class _DelegatingRecoverableHandler:\n"
        "    def __init__(self, delegate):\n"
        "        self._delegate = delegate\n"
        "    async def execute(self, request, context):\n"
        "        return await self._delegate.execute(request, context)\n"
        "    async def reconcile(self, request, context, activity):\n"
        "        return await self._delegate.reconcile(request, context, activity)\n"
        "    async def cancel(self, request, context, activity):\n"
        "        return await self._delegate.cancel(request, context, activity)\n"
        "class _DelegatingEffect:\n"
        "    def __init__(self, delegate):\n"
        "        self._delegate = delegate\n"
        "    async def apply(self, intent, key):\n"
        "        return await self._delegate.apply(intent, key)\n"
        "    async def reconcile(self, intent, key):\n"
        "        return await self._delegate.reconcile(intent, key)\n"
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
        "        schemas = ()\n"
        "        effects = ()\n"
        "        if _effect_callbacks:\n"
        "            schemas = (\n"
        f"                SchemaContribution('test.empty.intent', 'application/schema+json', {_ENGINE_INTENT_SCHEMA!r}),\n"
        f"                SchemaContribution('test.empty.receipt', 'application/schema+json', {_ENGINE_RECEIPT_SCHEMA!r}),\n"
        "            )\n"
        "            effects = tuple(\n"
        "                EffectRegistration(\n"
        "                    kind=kind,\n"
        "                    intent_schema_id='test.empty.intent',\n"
        "                    receipt_schema_id='test.empty.receipt',\n"
        "                    handler=_DelegatingEffect(handler),\n"
        "                    policy=_effect_policy,\n"
        "                )\n"
        "                for kind, handler in _effect_callbacks.items()\n"
        "            )\n"
        "        return PluginContribution(\n"
        "            task_handlers={\n"
        "                key: (\n"
        "                    _DelegatingRecoverableHandler(value)\n"
        "                    if hasattr(value, 'reconcile') and hasattr(value, 'cancel')\n"
        "                    else _DelegatingHandler(value)\n"
        "                )\n"
        "                for key, value in _callbacks.items()\n"
        "            },\n"
        "            schemas=schemas,\n"
        "            effects=effects,\n"
        "        )\n",
        encoding="utf-8",
    )
    source_files = tuple(
        sorted(path.relative_to(source_root).as_posix() for path in source_root.rglob("*") if path.is_file())
    )
    metadata_root = Path(tempfile.mkdtemp(prefix="graph-engine-runtime-metadata-"))
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
        metadata_provider=_MetadataProvider(
            distribution_name,
            metadata.Distribution.at(dist_info),
        )
    )
    return platform.resolve(request)


def _structural_product(
    *,
    name: str,
    nodes: dict[str, object],
    edges: list[dict[str, str]],
    start: str,
    maximum: int,
) -> FrozenComposition:
    return _resolved(
        {
            "name": name,
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": maximum,
                    "start": start,
                    "nodes": nodes,
                    "edges": edges,
                }
            },
        }
    )


def _rewrite_ledger(ledger_root: Path, events: tuple[object, ...]) -> None:
    envelopes: list[JSONValue] = []
    for sequence, raw_event in enumerate(events, start=1):
        event = cast(RuntimeEvent, raw_event)
        event_json = cast(JSONValue, event.model_dump(mode="json"))
        envelopes.append(
            {
                "seq": sequence,
                "event": event_json,
                "event_sha256": canonical_digest({"seq": sequence, "event": event_json}),
            }
        )
    for batch in ledger_root.glob("[0-9]*.json"):
        batch.unlink()
    final = ledger_root / f"{1:010d}-{len(envelopes):010d}.json"
    final.write_bytes(canonical_json_bytes(envelopes))


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    def __init__(self) -> None:
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: TaskWorkspaceStore,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        workspace = self._store.begin(
            task_id=call.attempt_root.workspace_identity.task_id,
            attempt=call.attempt_root.workspace_identity.attempt,
            output_paths=call.attempt_root.workspace_identity.output_paths,
        )
        assert workspace.identity == call.attempt_root.workspace_identity
        outcome = await handler.execute(
            call.request,
            TaskContext(
                project_root=workspace.project_root,
                write_root=workspace.write_root,
                workspace_identity=workspace.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)


class _ReceiptInstallingTestHost:
    """Test host that installs an authenticated terminal receipt after execute."""

    _FINGERPRINT = {"endpoint": "https://127.0.0.1:1", "profile": "test"}
    _REFERENCE = {"id": "ext-1"}

    def __init__(self) -> None:
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: TaskWorkspaceStore | None = None
        self._receipts: TerminalReceiptStore | None = None
        self._ledger: Ledger | None = None

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

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        workspace = self._store.begin(
            task_id=call.attempt_root.workspace_identity.task_id,
            attempt=call.attempt_root.workspace_identity.attempt,
            output_paths=call.attempt_root.workspace_identity.output_paths,
        )
        assert workspace.identity == call.attempt_root.workspace_identity
        port = None
        if call.activity_rpc.activity_id is not None and self._ledger is not None:
            port = LedgerTaskActivityPort(ledger=self._ledger, identity=call.activity_rpc)
            port.mark_dispatch_started(cast(JSONValue, self._FINGERPRINT))
            port.bind(cast(JSONValue, self._REFERENCE))
        outcome = await handler.execute(
            call.request,
            TaskContext(
                project_root=workspace.project_root,
                write_root=workspace.write_root,
                workspace_identity=workspace.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
                activity=port,
            ),
        )
        if port is not None:
            self._install_receipt(call.identity, port.snapshot, outcome)
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: object) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(
                status="indeterminate",
                reason="receipt-installing test host does not reconcile",
            ),
        )

    async def cancel(self, call: object) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(
                status="indeterminate",
                reason="receipt-installing test host does not cancel",
            ),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._receipts is None:
            return ()
        return self._receipts.authenticate(identity)

    def _install_receipt(
        self,
        identity: TaskHostCallIdentity,
        activity: TaskActivitySnapshot,
        outcome: TaskOutcome,
    ) -> None:
        if self._receipts is None or identity.activity_id is None:
            return
        assert self._store is not None
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
                request_digest=activity.request_digest,
                workspace_identity_digest=activity.workspace_identity.identity_digest,
                project_root_digest=activity.workspace_identity.project_digest,
                write_root_digest=activity.workspace_identity.write_root_digest,
                baseline_digest=canonical_digest(
                    [item.model_dump(mode="json") for item in activity.workspace_identity.baseline_files]
                ),
                staged_write_set_digest=self._store.seal(activity.workspace_identity).staged_digest,
                dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                reference_digest=activity.reference_digest,
                outcome=outcome,
                outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
                terminal_proof_digest=None,
                quiescence_proof_digest=prove_call_quiescent(),
                host_call_id=sink.host_call_id,
            )
        )


class _RecordingEffectHandler:
    def __init__(
        self,
        *,
        apply_result: EffectApplyResult,
        reconcile_result: EffectReconcileResult | None = None,
    ) -> None:
        self.apply_calls = 0
        self.reconcile_calls = 0
        self._apply_result = apply_result
        self._reconcile_result = reconcile_result

    async def apply(self, _intent: object, _key: str) -> EffectApplyResult:
        self.apply_calls += 1
        return self._apply_result

    async def reconcile(self, _intent: object, _key: str) -> EffectReconcileResult:
        self.reconcile_calls += 1
        assert self._reconcile_result is not None
        return self._reconcile_result


def _task_product(
    handler: Callable[..., Awaitable[TaskOutcome]],
    *,
    nested: bool = False,
    effect_handlers: Mapping[str, object] | None = None,
    effect_policy: EffectPolicy | None = None,
) -> FrozenComposition:
    root_start = "child" if nested else "work"
    root_nodes: dict[str, object]
    root_edges: list[dict[str, str]]
    graphs: dict[str, object]
    task = {
        "kind": "task",
        "capability": "test.empty.run",
        "retry": "once",
        "timeout": "short",
    }
    if nested:
        root_nodes = {
            "child": {"kind": "subgraph", "graph": "nested"},
            "end": {"kind": "end"},
        }
        root_edges = [{"from": "child", "to": "end"}]
        graphs = {
            "root": {
                "max_activations": 3,
                "start": root_start,
                "nodes": root_nodes,
                "edges": root_edges,
            },
            "nested": {
                "max_activations": 2,
                "start": "work",
                "nodes": {"work": task, "end": {"kind": "end"}},
                "edges": [{"from": "work", "to": "end"}],
            },
        }
    else:
        graphs = {
            "root": {
                "max_activations": 2,
                "start": root_start,
                "nodes": {"work": task, "end": {"kind": "end"}},
                "edges": [{"from": "work", "to": "end"}],
            }
        }
    return _resolved(
        {
            "name": "task-test",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": graphs,
        },
        {"test.empty.run": _FunctionHandler(handler)},
        effect_handlers=effect_handlers,
        effect_policy=effect_policy,
    )


class _SucceedingRecoverableHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"ok": True})

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="not_dispatched")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


class _FixedOutcomeRecoverableHandler(_SucceedingRecoverableHandler):
    def __init__(self, outcome: TaskOutcome) -> None:
        self._outcome = outcome

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return self._outcome


class _PromotionCutCrash(RuntimeError):
    pass


def _leave_unfinalized_terminal_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: TaskHandler,
    invocation_id: str,
) -> FrozenComposition:
    product = _recoverable_task_product(handler)
    host = _ReceiptInstallingTestHost()
    engine = Engine(tmp_path, clock=FakeClock(10.0), host=host)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id=invocation_id,
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    try:
        host.bind_ledger(Ledger(handle.invocation_root / "ledger"))

        def crash(name: str) -> None:
            if name == "before_receipt_cleanup":
                raise _PromotionCutCrash(name)

        monkeypatch.setattr(scheduler_runtime, "_promotion_cut", crash)
        with pytest.raises(_PromotionCutCrash, match="before_receipt_cleanup"):
            engine.run_until_blocked(handle)
        kinds = [item.event.kind for item in Ledger(handle.invocation_root / "ledger").read_all()]
        assert "task_activity_terminal_observed" in kinds
        assert "task_attempt_succeeded" not in kinds
        assert "task_attempt_failed" not in kinds
        assert "task_attempt_stopped" not in kinds
    finally:
        monkeypatch.setattr(scheduler_runtime, "_promotion_cut", lambda _name: None)
        handle.close()
        engine.close()
    return product


def _recoverable_task_product(handler: TaskHandler) -> FrozenComposition:
    return _resolved(
        {
            "name": "task-test",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "work",
                    "nodes": {
                        "work": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "work", "to": "end"}],
                }
            },
        },
        {"test.empty.run": handler},
    )


def _two_task_product(handler: Callable[..., Awaitable[TaskOutcome]]) -> FrozenComposition:
    task = {
        "kind": "task",
        "capability": "test.empty.run",
        "retry": "once",
        "timeout": "short",
    }
    return _resolved(
        {
            "name": "two-task-publication",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 3,
                    "start": "first",
                    "nodes": {
                        "first": task,
                        "second": task,
                        "end": {"kind": "end"},
                    },
                    "edges": [
                        {"from": "first", "to": "second"},
                        {"from": "second", "to": "end"},
                    ],
                }
            },
        },
        {"test.empty.run": _FunctionHandler(handler)},
    )


def _sibling_task_product() -> FrozenComposition:
    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("unused")

    return _resolved(
        {
            "name": "sibling-terminal-cause",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 3,
                    "start": "split",
                    "nodes": {
                        "split": {"kind": "gate", "expression": "true"},
                        "a": {"kind": "subgraph", "graph": "child"},
                        "b": {"kind": "subgraph", "graph": "child"},
                    },
                    "edges": [
                        {"from": "split", "to": "a"},
                        {"from": "split", "to": "b"},
                    ],
                },
                "child": {
                    "max_activations": 1,
                    "start": "work",
                    "nodes": {
                        "work": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                        }
                    },
                    "edges": [],
                },
            },
        },
        {"test.empty.run": _FunctionHandler(unused)},
    )


def _forge_unrelated_failed_sibling(
    root: Path,
    terminal_status: Literal["failed", "stopped"],
) -> tuple[FrozenComposition, str]:
    product = _sibling_task_product()
    invocation_id = f"unrelated-sibling-{terminal_status}"
    engine = Engine(root, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id=invocation_id,
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    structural = plan_next(product.workflow, fold_events(envelopes))
    ledger.append_batch(structural.events, expected_next_seq=envelopes[-1].seq + 1)
    projection = fold_events(ledger.read_all())
    tasks = plan_next(product.workflow, projection).tasks
    child_a = next(
        graph.graph_instance_id for graph in projection.graph_instances if graph.parent_node_id == "a"
    )
    child_b = next(
        graph.graph_instance_id for graph in projection.graph_instances if graph.parent_node_id == "b"
    )
    selected = next(task for task in tasks if task.graph_instance_id == child_a)
    attempt_events: tuple[object, ...] = (
        TaskAttemptStarted(
            activation_id=selected.activation_id,
            attempt=1,
            lease_expires_at="40",
        ),
        TaskLeaseAcquired(
            task_id=selected.task_id,
            activation_id=selected.activation_id,
            attempt=1,
            owner_id="selected-owner",
            acquired_at=10,
            heartbeat_at=10,
            expires_at=40,
        ),
    )
    if terminal_status == "failed":
        failure = TaskOutcome.failed("invalid_input", "selected failure").failure
        assert failure is not None
        attempt_events += (
            TaskAttemptFailed(
                activation_id=selected.activation_id,
                attempt=1,
                failure=failure,
            ),
        )
    else:
        attempt_events += (
            TaskAttemptStopped(
                activation_id=selected.activation_id,
                attempt=1,
                reason="operator_stop",
            ),
        )
    ledger.append_batch(  # type: ignore[arg-type]
        attempt_events,
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert terminal.terminal == terminal_status
    forged_events = tuple(envelope.event for envelope in ledger.read_all()) + (
        GraphFailed(graph_instance_id=child_b, reason="unrelated sibling failure"),
        *terminal.events,
    )
    _rewrite_ledger(handle.invocation_root / "ledger", forged_events)
    assert fold_events(ledger.read_all()).status == terminal_status
    return product, invocation_id


def _parallel_task_product(*, activation_bound: bool = False) -> FrozenComposition:
    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("unused")

    nodes: dict[str, object] = {
        "split": {"kind": "gate", "expression": "true"},
        "sibling": {
            "kind": "task",
            "capability": "test.empty.run",
            "retry": "twice",
            "timeout": "short",
        },
    }
    edges = [{"from": "split", "to": "sibling"}]
    if activation_bound:
        nodes["z-loop"] = {"kind": "gate", "expression": "true"}
        edges.extend(
            (
                {"from": "split", "to": "z-loop"},
                {"from": "z-loop", "to": "z-loop"},
            )
        )
    else:
        nodes["cause"] = {
            "kind": "task",
            "capability": "test.empty.run",
            "retry": "twice",
            "timeout": "short",
        }
        edges.append({"from": "split", "to": "cause"})
    return _resolved(
        {
            "name": "parallel-task-settlement",
            "entrypoints": {"main": "root"},
            "retry": {"twice": {"max_attempts": 2, "retry_on": ["transient"]}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 3,
                    "start": "split",
                    "nodes": nodes,
                    "edges": edges,
                }
            },
        },
        {"test.empty.run": _FunctionHandler(unused)},
    )


def _parallel_task_ledger(
    root: Path,
    invocation_id: str,
    *,
    activation_bound: bool = False,
) -> tuple[FrozenComposition, Path, dict[str, str]]:
    product = _parallel_task_product(activation_bound=activation_bound)
    with Engine(root, clock=FakeClock(10), host=_InProcessTestHost()) as engine:
        with engine.start(
            product,
            entrypoint="main",
            invocation_id=invocation_id,
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        ) as handle:
            ledger_root = handle.invocation_root / "ledger"
            ledger = Ledger(ledger_root)
            initial = ledger.read_all()
            structural = plan_next(product.workflow, fold_events(initial))
            structural_events = structural.events
            if activation_bound:
                structural_events = tuple(
                    event
                    for event in structural_events
                    if event.kind not in {"graph_failed", "invocation_finished"}
                )
            else:
                assert structural.terminal is None
            ledger.append_batch(
                structural_events,
                expected_next_seq=initial[-1].seq + 1,
            )
            projection = fold_events(ledger.read_all())
            activations = {
                activation.node_id: activation.activation_id
                for activation in projection.activations
                if activation.node_id in {"cause", "sibling"}
            }
    return product, ledger_root, activations


def _failed_attempt_events(
    activation_id_: str,
    failure_kind: Literal["invalid_input", "transient"],
    *,
    attempt: int = 1,
) -> tuple[RuntimeEvent, ...]:
    failure = TaskOutcome.failed(failure_kind, f"{failure_kind} failure").failure
    assert failure is not None
    events: tuple[RuntimeEvent, ...] = (
        TaskAttemptStarted(
            activation_id=activation_id_,
            attempt=attempt,
            lease_expires_at="40",
        ),
    )
    events += (
        TaskLeaseAcquired(
            task_id=task_id(activation_id_),
            activation_id=activation_id_,
            attempt=attempt,
            owner_id=f"owner-{activation_id_}-{attempt}",
            acquired_at=10,
            heartbeat_at=10,
            expires_at=40,
        ),
    )
    return events + (
        TaskAttemptFailed(
            activation_id=activation_id_,
            attempt=attempt,
            failure=failure,
        ),
    )


def _stopped_attempt_events(
    activation_id_: str,
    *,
    attempt: int = 1,
) -> tuple[RuntimeEvent, ...]:
    events: tuple[RuntimeEvent, ...] = (
        TaskAttemptStarted(
            activation_id=activation_id_,
            attempt=attempt,
            lease_expires_at="40",
        ),
    )
    events += (
        TaskLeaseAcquired(
            task_id=task_id(activation_id_),
            activation_id=activation_id_,
            attempt=attempt,
            owner_id=f"owner-{activation_id_}-{attempt}",
            acquired_at=10,
            heartbeat_at=10,
            expires_at=40,
        ),
    )
    return events + (
        TaskAttemptStopped(
            activation_id=activation_id_,
            attempt=attempt,
            reason="operator_stop",
        ),
    )


def _nested_task_interrupt_product(
    handler: Callable[..., Awaitable[TaskOutcome]],
) -> FrozenComposition:
    return _resolved(
        {
            "name": "crash-matrix",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "child",
                    "nodes": {
                        "child": {"kind": "subgraph", "graph": "nested"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "nested": {
                    "max_activations": 3,
                    "start": "work",
                    "nodes": {
                        "work": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                            "resources": {"writes": ["out.txt"]},
                        },
                        "review": {
                            "kind": "interrupt",
                            "reason": "matrix review",
                            "actions": ["approve"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [
                        {"from": "work", "to": "review"},
                        {"from": "review", "to": "end"},
                    ],
                },
            },
        },
        {"test.empty.run": _FunctionHandler(handler)},
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(tmp_path)


@pytest.fixture
def resolved_subgraph_product() -> FrozenComposition:
    return _resolved(
        {
            "name": "subgraph-test",
            "entrypoints": {"main": "parent"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "parent": {
                    "max_activations": 3,
                    "start": "child",
                    "nodes": {
                        "child": {
                            "kind": "subgraph",
                            "graph": "nested",
                            "input": {"child": "done"},
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "nested": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                },
            },
        }
    )


@pytest.fixture
def resolved_interrupt_product() -> FrozenComposition:
    return _resolved(
        {
            "name": "interrupt-test",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "review",
                    "nodes": {
                        "review": {
                            "kind": "interrupt",
                            "reason": "human review",
                            "actions": ["approve", "reject"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "review", "to": "end"}],
                }
            },
        }
    )


def test_engine_has_no_default_product(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    with pytest.raises(TypeError, match="composition"):
        engine.start(
            entrypoint="main",
            invocation_id="missing-product",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )  # type: ignore[call-arg]


def test_subgraph_completion_returns_to_parent(
    engine: Engine, resolved_subgraph_product: FrozenComposition
) -> None:
    handle = engine.start(
        resolved_subgraph_product,
        entrypoint="main",
        invocation_id="inv-sub",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)
    assert result.status == "succeeded"
    assert result.output == {"child": "done"}


def test_interrupt_requires_explicit_resume(
    engine: Engine, resolved_interrupt_product: FrozenComposition
) -> None:
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="inv-int",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    blocked = engine.run_until_blocked(handle)
    assert blocked.status == "interrupted"
    assert blocked.actions == ("approve", "reject")
    resumed = engine.resume(handle, action="approve", payload={"reviewer": "human"})
    assert engine.run_until_blocked(resumed).status == "succeeded"


def test_multilevel_subgraphs_have_canonical_instances_and_return_in_order(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "nested-test",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "child",
                    "nodes": {
                        "child": {"kind": "subgraph", "graph": "child"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "child": {
                    "max_activations": 2,
                    "start": "grandchild",
                    "nodes": {
                        "grandchild": {
                            "kind": "subgraph",
                            "graph": "grandchild",
                            "input": {"depth": 2},
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "grandchild", "to": "end"}],
                },
                "grandchild": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                },
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="nested",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    result = engine.run_until_blocked(handle)

    assert result.status == "succeeded"
    assert result.output == {"depth": 2}
    children = [
        graph for graph in result.projection.graph_instances if graph.parent_activation_id is not None
    ]
    assert len(children) == 2
    for child in children:
        assert child.graph_instance_id == canonical_digest(
            {"parent_activation_id": child.parent_activation_id, "graph_id": child.graph_id}
        )


def test_nested_interrupt_blocks_root_and_records_exact_location(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "nested-interrupt",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "child",
                    "nodes": {
                        "child": {"kind": "subgraph", "graph": "nested"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "nested": {
                    "max_activations": 2,
                    "start": "review",
                    "nodes": {
                        "review": {
                            "kind": "interrupt",
                            "reason": "nested review",
                            "actions": ["continue"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "review", "to": "end"}],
                },
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="nested-int",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    blocked = engine.run_until_blocked(handle)

    pending = blocked.projection.pending_interrupt
    assert blocked.status == "interrupted"
    assert blocked.reason == "nested review"
    assert pending is not None
    assert pending.graph_instance_id != "root"
    assert pending.actions == ("continue",)
    assert pending.model_dump(mode="json")["input"] == {"config": {}, "tokens": [{}]}


def test_resume_mismatch_repeat_and_payload_freezing_do_not_append(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "resume-test",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "review",
                    "nodes": {
                        "review": {
                            "kind": "interrupt",
                            "reason": "review",
                            "actions": ["approve"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "review", "to": "end"}],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="resume",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    engine.run_until_blocked(handle)
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()

    with pytest.raises(EngineError, match="not allowed"):
        engine.resume(handle, action="reject", payload=None)
    assert ledger.read_all() == before

    payload: dict[str, JSONValue] = {"reviewers": ["a"]}
    engine.resume(handle, action="approve", payload=payload)
    cast(list[str], payload["reviewers"]).append("mutated")
    after = ledger.read_all()
    resumed = next(item.event for item in after if item.event.kind == "interrupt_resumed")
    assert resumed.model_dump(mode="json")["payload"] == {"reviewers": ["a"]}
    with pytest.raises(EngineError, match="no pending"):
        engine.resume(handle, action="approve", payload=None)
    assert ledger.read_all() == after


def test_concurrent_resume_commits_only_one_atomic_sequence(
    tmp_path: Path, resolved_interrupt_product: FrozenComposition
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="resume-race",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    engine.run_until_blocked(handle)

    def attempt() -> object:
        try:
            return engine.resume(handle, action="approve", payload={"race": True})
        except EngineError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: attempt(), range(2)))

    assert sum(not isinstance(outcome, EngineError) for outcome in outcomes) == 1
    kinds = [item.event.kind for item in Ledger(handle.invocation_root / "ledger").read_all()]
    assert kinds.count("interrupt_resumed") == 1
    assert kinds.count("node_completed") == 1


@pytest.mark.parametrize("invocation_id", ["../escape", "a/b", r"a\\b", ".", "..", ""])
def test_invocation_id_is_confined(tmp_path: Path, invocation_id: str) -> None:
    product = _resolved(
        {
            "name": "confinement",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    with pytest.raises(EngineError, match="invalid invocation id"):
        Engine(tmp_path).start(
            product,
            entrypoint="main",
            invocation_id=invocation_id,
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_lock_digest_mismatch_without_appending(tmp_path: Path) -> None:
    first = _resolved(
        {
            "name": "first",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    second = _resolved(
        {
            "name": "second",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    handle = Engine(tmp_path).start(
        first,
        entrypoint="main",
        invocation_id="digest",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()

    with pytest.raises(InvocationDrift, match="lock|composition"):
        Engine(tmp_path).open("digest", second, authorization=empty_runtime_authorization())

    assert ledger.read_all() == before


def test_duplicate_start_rejects_digest_mismatch_without_replacing_invocation(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="duplicate",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()
    mismatched = _resolved(
        {
            "name": "mismatched-duplicate",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )

    with pytest.raises(InvocationDrift, match="lock|composition"):
        engine.start(
            mismatched,
            entrypoint="main",
            invocation_id="duplicate",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )

    assert ledger.read_all() == before
    assert handle._workspace_binding.project_root.is_dir()


def test_repeated_start_with_changed_root_input_rejects_drift_and_preserves_state(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    workspace_binding = _workspace_binding_at(tmp_path / "workspace")
    (workspace_binding.project_root / "authoritative.txt").write_bytes(b"project")
    (workspace_binding.attempts_root / "authoritative.txt").write_bytes(b"attempts")
    (workspace_binding.receipts_root / "authoritative.txt").write_bytes(b"receipts")
    original_seed = empty_invocation_seed(root_input={"request": "original"})
    changed_seed = empty_invocation_seed(root_input={"request": "changed"})

    with (
        Engine(tmp_path / "engine") as engine,
        engine.start(
            resolved_interrupt_product,
            entrypoint="main",
            invocation_id="changed-root-input",
            seed=original_seed,
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        ) as handle,
    ):
        ledger_before = handle.ledger.read_all()
        ledger_files_before = _directory_state(handle.invocation_root / "ledger")
        workspace_before = _workspace_state(workspace_binding)
        workspace_identity_before = workspace_binding.identity_digest

        with pytest.raises(InvocationDrift, match="seed|root input|start intent"):
            engine.start(
                resolved_interrupt_product,
                entrypoint="main",
                invocation_id="changed-root-input",
                seed=changed_seed,
                authorization=empty_runtime_authorization(),
                workspace_binding=workspace_binding,
            )

        assert handle.ledger.read_all() == ledger_before
        assert _directory_state(handle.invocation_root / "ledger") == ledger_files_before
        assert _workspace_state(workspace_binding) == workspace_before
        assert _workspace_binding_at(tmp_path / "workspace").identity_digest == workspace_identity_before


def test_repeated_start_with_changed_workspace_binding_rejects_drift_and_preserves_state(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    authoritative = _workspace_binding_at(tmp_path / "authoritative-workspace")
    foreign = _workspace_binding_at(tmp_path / "foreign-workspace")
    for label, binding in (("authoritative", authoritative), ("foreign", foreign)):
        (binding.project_root / "state.txt").write_text(label, encoding="utf-8")
        (binding.attempts_root / "state.txt").write_text(label, encoding="utf-8")
        (binding.receipts_root / "state.txt").write_text(label, encoding="utf-8")
    seed = empty_invocation_seed(root_input={"request": "same"})

    with (
        Engine(tmp_path / "engine") as engine,
        engine.start(
            resolved_interrupt_product,
            entrypoint="main",
            invocation_id="changed-workspace-binding",
            seed=seed,
            authorization=empty_runtime_authorization(),
            workspace_binding=authoritative,
        ) as handle,
    ):
        ledger_before = handle.ledger.read_all()
        ledger_files_before = _directory_state(handle.invocation_root / "ledger")
        authoritative_before = _workspace_state(authoritative)
        foreign_before = _workspace_state(foreign)
        authoritative_identity_before = authoritative.identity_digest
        foreign_identity_before = foreign.identity_digest

        with pytest.raises(InvocationDrift, match="workspace binding"):
            engine.start(
                resolved_interrupt_product,
                entrypoint="main",
                invocation_id="changed-workspace-binding",
                seed=seed,
                authorization=empty_runtime_authorization(),
                workspace_binding=foreign,
            )

        assert handle.ledger.read_all() == ledger_before
        assert _directory_state(handle.invocation_root / "ledger") == ledger_files_before
        assert _workspace_state(authoritative) == authoritative_before
        assert _workspace_state(foreign) == foreign_before
        assert (
            _workspace_binding_at(tmp_path / "authoritative-workspace").identity_digest
            == authoritative_identity_before
        )
        assert (
            _workspace_binding_at(tmp_path / "foreign-workspace").identity_digest == foreign_identity_before
        )


def test_missing_host_fails_closed_without_calling_handler(tmp_path: Path) -> None:
    called = False

    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        nonlocal called
        called = True
        return TaskOutcome.succeeded("unsafe")

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10.0))
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="no-host",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    result = engine.run_until_blocked(handle)

    assert result.status == "failed"
    assert not called


def test_explicit_host_runs_task_and_terminal_invocation_reopens(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"ran": True})

    product = _task_product(handler)
    clock = FakeClock(10.0)
    engine = Engine(tmp_path, clock=clock, host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="hosted",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)

    assert result.status == "succeeded"
    assert result.output == {"ran": True}
    reopened = Engine(tmp_path, clock=clock, host=_InProcessTestHost()).open(
        "hosted", product, authorization=empty_runtime_authorization()
    )
    second = Engine(tmp_path, clock=clock, host=_InProcessTestHost()).open(
        "hosted", product, authorization=empty_runtime_authorization()
    )
    assert second._workspace_binding.identity_digest == reopened._workspace_binding.identity_digest


def test_engine_settles_effects_before_task_and_node_success(tmp_path: Path) -> None:
    effect = _RecordingEffectHandler(apply_result=EffectApplyResult.applied({"remote_id": "r1"}))
    calls = 0

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal calls
        calls += 1
        return TaskOutcome.succeeded(
            {"ran": True},
            effects=(context.effect("test.empty.audit", {"n": 1}),),
        )

    product = _task_product(handler, effect_handlers={"test.empty.audit": effect})
    engine = Engine(tmp_path, clock=FakeClock(10.0), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="effectful",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)

    assert result.status == "succeeded"
    assert result.output == {"ran": True}
    assert calls == 1
    assert effect.apply_calls == 1
    kinds = [envelope.event.kind for envelope in Ledger(handle.invocation_root / "ledger").read_all()]
    assert kinds.index("effect_receipt_recorded") < kinds.index("task_attempt_succeeded")
    assert kinds.index("task_attempt_succeeded") < kinds.index("node_completed")


def test_engine_effect_receipt_publication_crash_replays_without_reapply(tmp_path: Path) -> None:
    class DurableFileEffect:
        def __init__(self, marker: Path) -> None:
            self._marker = marker

        async def apply(self, _intent: object, key: str) -> EffectApplyResult:
            descriptor = os.open(self._marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, f"{key}\n".encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return EffectApplyResult.applied({"remote_id": "r1"})

        async def reconcile(self, _intent: object, _key: str) -> EffectReconcileResult:
            if self._marker.is_file():
                return EffectReconcileResult.applied({"remote_id": "r1"})
            return EffectReconcileResult(status="not_applied")

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded(
            {"ran": True},
            effects=(context.effect("test.empty.audit", {"n": 1}),),
        )

    for boundary in ("pending_fsynced", "final_installed", "directory_fsynced"):
        case_root = tmp_path / boundary
        marker = case_root / "external-effect.log"
        marker.parent.mkdir(parents=True)
        effect = DurableFileEffect(marker)
        product = _task_product(handler, effect_handlers={"test.empty.audit": effect})
        with Engine(case_root / "engine", clock=FakeClock(10.0), host=_InProcessTestHost()) as engine:
            with engine.start(
                product,
                entrypoint="main",
                invocation_id="effect-receipt-cut",
                seed=empty_invocation_seed(),
                authorization=empty_runtime_authorization(),
            ):
                pass

        process_id = os.fork()
        if process_id == 0:

            def crash_after_external_apply(name: str) -> None:
                if marker.is_file() and name == boundary:
                    os._exit(93)

            ledger_runtime._append_boundary = crash_after_external_apply
            child_engine = Engine(
                case_root / "engine",
                clock=FakeClock(10.0),
                host=_InProcessTestHost(),
            )
            child_handle = child_engine.open(
                "effect-receipt-cut",
                product,
                authorization=empty_runtime_authorization(),
            )
            child_engine.run_until_blocked(child_handle)
            os._exit(94)

        _child, status = os.waitpid(process_id, 0)
        assert os.waitstatus_to_exitcode(status) == 93

        with Engine(
            case_root / "engine",
            clock=FakeClock(10.0),
            host=_InProcessTestHost(),
        ) as recovered_engine:
            with recovered_engine.open(
                "effect-receipt-cut",
                product,
                authorization=empty_runtime_authorization(),
            ) as recovered_handle:
                result = recovered_engine.run_until_blocked(recovered_handle)
                events = recovered_handle.ledger.read_all()

        assert result.status == "succeeded"
        assert result.output == {"ran": True}
        assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
        kinds = tuple(envelope.event.kind for envelope in events)
        assert kinds.count("effect_apply_started") == 1
        assert kinds.count("effect_receipt_recorded") == 1
        assert kinds.count("task_attempt_succeeded") == 1
        assert result.projection.effects[0].status == "applied"
        assert thaw_json(result.projection.effects[0].receipt) == {"remote_id": "r1"}


def test_engine_returns_generic_effect_pending_result(tmp_path: Path) -> None:
    effect = _RecordingEffectHandler(
        apply_result=EffectApplyResult(
            status="transient",
            failure=TaskFailure(kind="transient", message="busy"),
        ),
        reconcile_result=EffectReconcileResult(status="pending"),
    )

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded(
            {"ran": True},
            effects=(context.effect("test.empty.audit", {"n": 1}),),
        )

    product = _task_product(handler, effect_handlers={"test.empty.audit": effect})
    engine = Engine(tmp_path, clock=FakeClock(10.0), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="effect-pending",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)

    assert result.status == "interrupted"
    assert result.terminal_reason == "effect_pending"
    assert result.actions == ()
    kinds = [envelope.event.kind for envelope in Ledger(handle.invocation_root / "ledger").read_all()]
    assert "task_attempt_succeeded" not in kinds
    assert effect.apply_calls == 1
    assert effect.reconcile_calls == 1


@pytest.mark.parametrize(
    ("apply_result", "reconcile_result", "effect_policy"),
    [
        (
            EffectApplyResult(
                status="permanent",
                failure=TaskFailure(kind="external_effect", message="denied", retryable=False),
            ),
            None,
            None,
        ),
        (EffectApplyResult.applied({"n": 1}), None, None),
        (
            EffectApplyResult(
                status="transient",
                failure=TaskFailure(kind="transient", message="busy"),
            ),
            EffectReconcileResult(status="not_applied"),
            EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0),
        ),
    ],
)
def test_engine_non_last_effect_failure_returns_typed_failed_without_rerun(
    tmp_path: Path,
    apply_result: EffectApplyResult,
    reconcile_result: EffectReconcileResult | None,
    effect_policy: EffectPolicy | None,
) -> None:
    effect = _RecordingEffectHandler(apply_result=apply_result, reconcile_result=reconcile_result)
    calls = 0

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal calls
        calls += 1
        return TaskOutcome.succeeded(
            {"ran": True},
            effects=(
                context.effect("test.empty.audit", {"n": 1}),
                context.effect("test.empty.audit", {"n": 2}),
            ),
        )

    product = _task_product(
        handler,
        effect_handlers={"test.empty.audit": effect},
        effect_policy=effect_policy,
    )
    engine = Engine(tmp_path, clock=FakeClock(10.0), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="effect-multi-fail",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)

    assert result.status == "failed"
    assert calls == 1
    assert effect.apply_calls == 1
    ledger = Ledger(handle.invocation_root / "ledger")
    kinds = [envelope.event.kind for envelope in ledger.read_all()]
    assert kinds.count("task_promotion_completed") == 1
    assert "task_attempt_succeeded" not in kinds
    assert "task_attempt_failed" not in kinds
    assert kinds.count("task_attempt_committed_effect_failed") == 1
    folded = fold_events(ledger.read_all())
    attempt = folded.activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    prepared = attempt.prepared_commit
    assert prepared is not None
    assert prepared.promotion_receipt_digest is not None
    assert tuple(item.status for item in folded.effects) == ("permanently_failed", "permanently_failed")
    assert folded.status == "failed"


def test_engine_permanent_effect_failure_is_explicitly_committed_not_ordinary_failed(
    tmp_path: Path,
) -> None:
    effect = _RecordingEffectHandler(
        apply_result=EffectApplyResult(
            status="permanent",
            failure=TaskFailure(kind="external_effect", message="denied", retryable=False),
        )
    )
    calls = 0

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal calls
        calls += 1
        return TaskOutcome.succeeded(
            {"ran": True},
            effects=(context.effect("test.empty.audit", {"n": 1}),),
        )

    product = _task_product(handler, effect_handlers={"test.empty.audit": effect})
    engine = Engine(tmp_path, clock=FakeClock(10.0), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="effect-permanent",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)

    assert result.status == "failed"
    assert calls == 1
    ledger = Ledger(handle.invocation_root / "ledger")
    kinds = [envelope.event.kind for envelope in ledger.read_all()]
    assert kinds.count("task_promotion_completed") == 1
    assert "task_attempt_succeeded" not in kinds
    assert "task_attempt_failed" not in kinds
    assert kinds.count("task_attempt_committed_effect_failed") == 1
    attempt = fold_events(ledger.read_all()).activations[-1].attempts[-1]
    assert attempt.status == "committed_effect_failed"
    prepared = attempt.prepared_commit
    assert prepared is not None
    assert prepared.promotion_receipt_digest is not None


def test_open_applies_heartbeat_heavy_history_linearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("unused")

    product = _task_product(unused)
    clock = FakeClock(100.0)
    with Engine(tmp_path, clock=clock, host=_InProcessTestHost()) as bootstrap:
        with bootstrap.start(
            product,
            entrypoint="main",
            invocation_id="heartbeat-heavy",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        ) as handle:
            ledger = Ledger(handle.invocation_root / "ledger")
            initial = ledger.read_all()
            planned = plan_next(product.workflow, fold_events(initial))
            assert len(planned.tasks) == 1
            task = planned.tasks[0]
            ledger.append_batch(
                planned.events,
                expected_next_seq=initial[-1].seq + 1,
            )
            ledger.append_batch(
                (
                    TaskAttemptStarted(
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        lease_expires_at="10000",
                    ),
                    TaskLeaseAcquired(
                        task_id=task.task_id,
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        owner_id="heartbeat-owner",
                        acquired_at=0.0,
                        heartbeat_at=0.0,
                        expires_at=10_000.0,
                    ),
                ),
                expected_next_seq=ledger.read_all()[-1].seq + 1,
            )
            heartbeat_count = 64
            ledger.append_batch(
                tuple(
                    TaskLeaseHeartbeat(
                        task_id=task.task_id,
                        activation_id=task.activation_id,
                        attempt=task.attempt,
                        owner_id="heartbeat-owner",
                        heartbeat_at=float(index),
                        expires_at=10_000.0,
                    )
                    for index in range(1, heartbeat_count + 1)
                ),
                expected_next_seq=ledger.read_all()[-1].seq + 1,
            )
            envelopes = ledger.read_all()
            assert (
                sum(isinstance(envelope.event, TaskLeaseHeartbeat) for envelope in envelopes)
                == heartbeat_count
            )
            assert fold_events(envelopes).status == "running"

    processed_envelopes = 0
    original_digest_check = EventEnvelope.has_valid_digest

    def count_processed_envelope(envelope: EventEnvelope) -> bool:
        nonlocal processed_envelopes
        processed_envelopes += 1
        return original_digest_check(envelope)

    monkeypatch.setattr(EventEnvelope, "has_valid_digest", count_processed_envelope)
    with Engine(tmp_path, clock=clock, host=_InProcessTestHost()) as engine:
        with engine.open("heartbeat-heavy", product, authorization=empty_runtime_authorization()):
            pass

    assert processed_envelopes <= 12 * len(envelopes)


def test_child_task_failure_and_stop_propagate_to_root(tmp_path: Path) -> None:
    async def fail(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.failed("invalid_input", "bad child")

    failed_product = _task_product(fail, nested=True)
    failed_engine = Engine(tmp_path / "failed", clock=FakeClock(10), host=_InProcessTestHost())
    failed_handle = failed_engine.start(
        failed_product,
        entrypoint="main",
        invocation_id="child-failed",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    failed = failed_engine.run_until_blocked(failed_handle)
    assert failed.status == "failed"
    assert all(graph.status == "failed" for graph in failed.projection.graph_instances)

    async def stop(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.stopped("operator_stop")

    stopped_product = _task_product(stop, nested=True)
    stopped_engine = Engine(tmp_path / "stopped", clock=FakeClock(10), host=_InProcessTestHost())
    stopped_handle = stopped_engine.start(
        stopped_product,
        entrypoint="main",
        invocation_id="child-stopped",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    stopped = stopped_engine.run_until_blocked(stopped_handle)
    assert stopped.status == "stopped"
    assert stopped.reason == "operator_stop"
    assert all(graph.status == "failed" for graph in stopped.projection.graph_instances)


def test_corrupt_checkpoint_is_ignored_in_favor_of_ledger(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "checkpoint",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="checkpoint",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    (handle.invocation_root / "checkpoint.json").write_bytes(b"not-json")

    reopened = Engine(tmp_path).open("checkpoint", product, authorization=empty_runtime_authorization())
    result = Engine(tmp_path).open("checkpoint", product, authorization=empty_runtime_authorization())

    assert fold_events(reopened.ledger.read_all()) == fold_events(result.ledger.read_all())


def test_open_reclaims_expired_lease_before_running(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("should-not-run")

    product = _task_product(handler)
    first_clock = FakeClock(10.0)
    engine = Engine(tmp_path, clock=first_clock, host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="expired",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan.tasks[0]
    next_seq = ledger.read_all()[-1].seq + 1
    ledger.append_batch(
        (
            TaskAttemptStarted(
                activation_id=task.activation_id,
                attempt=task.attempt,
                lease_expires_at="20",
            ),
            TaskLeaseAcquired(
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                owner_id="crashed-owner",
                acquired_at=10.0,
                heartbeat_at=10.0,
                expires_at=20.0,
            ),
        ),
        expected_next_seq=next_seq,
    )

    reopened_engine = Engine(tmp_path, clock=FakeClock(21.0), host=_InProcessTestHost())
    reopened = reopened_engine.open("expired", product, authorization=empty_runtime_authorization())
    result = reopened_engine.run_until_blocked(reopened)

    assert result.status == "failed"
    assert any(item.event.kind == "task_attempt_failed" for item in ledger.read_all())


class _RecoverableOpenHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded()

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="running")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


class _DirectRecoverableRegistry:
    def __init__(self, handlers: dict[str, TaskHandler]) -> None:
        self.task_handlers = handlers
        self.commit_validators: dict[str, object] = {}
        self.bindings: dict[str, object] = {}


def test_open_does_not_reclaim_expired_recoverable_activity(tmp_path: Path) -> None:
    async def unused(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        raise AssertionError("disposable execute must not run")

    product = _task_product(unused)
    start_clock = FakeClock(10.0)
    engine = Engine(tmp_path, clock=start_clock, host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="expired-recoverable",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan_next(product.workflow, fold_events(ledger.read_all())).tasks[0]
    owner_id = canonical_digest(
        {
            "invocation_id": "expired-recoverable",
            "lock_digest": product.lock_digest,
            "role": "engine-scheduler",
        }
    )
    store = handle.workspace
    try:
        scheduler = Scheduler(
            _DirectRecoverableRegistry({task.capability_id: _RecoverableOpenHandler()}),
            store,
            ledger,
            _InProcessTestHost(),
            owner_id=owner_id,
            clock=start_clock,
            lease_seconds=10.0,
            lock_digest=product.lock_digest,
            composition_digest=product.digest,
            entrypoint="main",
        )
        scheduler.start_recoverable(task)
    finally:
        store.close()
    handle.close()
    engine.close()

    reopened_engine = Engine(tmp_path, clock=FakeClock(21.0), host=_InProcessTestHost())
    reopened = reopened_engine.open(
        "expired-recoverable", product, authorization=empty_runtime_authorization()
    )
    try:
        kinds = [item.event.kind for item in ledger.read_all()]
        assert "task_attempt_failed" not in kinds
        projection = fold_events(ledger.read_all())
        attempt = projection.activations[-1].attempts[-1]
        assert attempt.attempt == 1
        assert attempt.status == "running"
        assert attempt.activity is not None
    finally:
        reopened.close()
        reopened_engine.close()


def test_run_until_blocked_publishes_task_attempt_succeeded_for_recoverable_success(
    tmp_path: Path,
) -> None:
    product = _recoverable_task_product(_SucceedingRecoverableHandler())
    host = _ReceiptInstallingTestHost()
    engine = Engine(tmp_path, clock=FakeClock(10.0), host=host)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="recoverable-success",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    host.bind_ledger(Ledger(handle.invocation_root / "ledger"))
    result = engine.run_until_blocked(handle)
    kinds = [item.event.kind for item in Ledger(handle.invocation_root / "ledger").read_all()]
    assert result.status == "succeeded"
    assert "task_activity_terminal_observed" in kinds
    assert "task_attempt_succeeded" in kinds
    assert kinds.index("task_activity_terminal_observed") < kinds.index("task_attempt_succeeded")


def test_recover_publishes_task_attempt_succeeded_for_expired_succeeded_terminal_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _leave_unfinalized_terminal_observed(
        tmp_path,
        monkeypatch,
        _SucceedingRecoverableHandler(),
        "expired-observed-success",
    )
    host = _ReceiptInstallingTestHost()
    engine = Engine(tmp_path, clock=FakeClock(41.0), host=host)
    handle = engine.open("expired-observed-success", product, authorization=empty_runtime_authorization())
    try:
        host.bind_ledger(Ledger(handle.invocation_root / "ledger"))
        asyncio.run(handle.recover())
        kinds = [item.event.kind for item in Ledger(handle.invocation_root / "ledger").read_all()]
        attempt = (
            fold_events(Ledger(handle.invocation_root / "ledger").read_all()).activations[-1].attempts[-1]
        )
        assert "task_attempt_succeeded" in kinds
        assert "task_attempt_failed" not in kinds
        assert attempt.status == "succeeded"
        result = engine.run_until_blocked(handle)
        assert result.status == "succeeded"
    finally:
        handle.close()
        engine.close()


def test_public_recover_translates_promotion_publication_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _leave_unfinalized_terminal_observed(
        tmp_path,
        monkeypatch,
        _SucceedingRecoverableHandler(),
        "recover-promotion-indeterminate",
    )
    engine = Engine(tmp_path, clock=FakeClock(41.0), host=_ReceiptInstallingTestHost())
    handle = engine.open(
        "recover-promotion-indeterminate",
        product,
        authorization=empty_runtime_authorization(),
    )

    def indeterminate(*_args: object, **_kwargs: object) -> object:
        raise PromotionPublicationIndeterminate("injected promotion publication uncertainty")

    monkeypatch.setattr(TaskWorkspaceStore, "promote", indeterminate)
    try:
        with pytest.raises(EnginePublicationIndeterminate, match="promotion"):
            asyncio.run(handle.recover())
    finally:
        handle.close()
        engine.close()


@pytest.mark.parametrize("mode", ["failed", "stopped"])
def test_recover_publishes_matching_outcome_for_expired_failed_or_stopped_terminal_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    if mode == "failed":
        outcome = TaskOutcome.failed("transient", "provider failed")
        expected_kind = "task_attempt_failed"
        unexpected_kind = "task_attempt_stopped"
    else:
        outcome = TaskOutcome.stopped("operator-stop")
        expected_kind = "task_attempt_stopped"
        unexpected_kind = "task_attempt_failed"
    product = _leave_unfinalized_terminal_observed(
        tmp_path,
        monkeypatch,
        _FixedOutcomeRecoverableHandler(outcome),
        f"expired-observed-{mode}",
    )
    host = _ReceiptInstallingTestHost()
    engine = Engine(tmp_path, clock=FakeClock(41.0), host=host)
    handle = engine.open(f"expired-observed-{mode}", product, authorization=empty_runtime_authorization())
    try:
        host.bind_ledger(Ledger(handle.invocation_root / "ledger"))
        try:
            asyncio.run(handle.recover())
        except ProjectionError as error:
            raise AssertionError("expired observed failed/stopped must not fold-reject") from error
        kinds = [item.event.kind for item in Ledger(handle.invocation_root / "ledger").read_all()]
        attempt = (
            fold_events(Ledger(handle.invocation_root / "ledger").read_all()).activations[-1].attempts[-1]
        )
        assert expected_kind in kinds
        assert unexpected_kind not in kinds
        assert "task_attempt_succeeded" not in kinds
        assert attempt.status == mode
        try:
            engine.run_until_blocked(handle)
        except ProjectionError as error:
            raise AssertionError("expired observed failed/stopped must not fold-reject") from error
    finally:
        handle.close()
        engine.close()


def test_concurrent_open_reclaim_has_one_winner_and_one_engine_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("unused")

    product = _task_product(handler)
    handle = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).start(
        product,
        entrypoint="main",
        invocation_id="reclaim-race",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan.tasks[0]
    ledger.append_batch(
        (
            TaskAttemptStarted(
                activation_id=task.activation_id,
                attempt=task.attempt,
                lease_expires_at="20",
            ),
            TaskLeaseAcquired(
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                owner_id="expired-owner",
                acquired_at=10,
                heartbeat_at=10,
                expires_at=20,
            ),
        ),
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    reclaim_entered = threading.Event()
    release_reclaim = threading.Event()
    real_reclaim = Scheduler.reclaim_expired

    def synchronize_reclaim(
        self: Scheduler,
        leases: object = None,
    ) -> tuple[str, ...]:
        reclaim_entered.set()
        assert release_reclaim.wait(timeout=5)
        return real_reclaim(self, leases)  # type: ignore[arg-type]

    monkeypatch.setattr(Scheduler, "reclaim_expired", synchronize_reclaim)

    def open_once() -> object:
        try:
            return Engine(tmp_path, clock=FakeClock(21), host=_InProcessTestHost()).open(
                "reclaim-race",
                product,
                authorization=empty_runtime_authorization(),
            )
        except EngineError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(open_once)
        assert reclaim_entered.wait(timeout=5)
        loser = pool.submit(open_once)
        try:
            loser_outcome = loser.result(timeout=5)
        finally:
            release_reclaim.set()
        outcomes = (winner.result(timeout=5), loser_outcome)

    assert sum(isinstance(item, EngineConflictError) for item in outcomes) == 1
    assert sum(not isinstance(item, EngineError) for item in outcomes) == 1
    assert [item.event.kind for item in ledger.read_all()].count("task_attempt_failed") == 1


def test_concurrent_reopeners_execute_one_live_persisted_attempt_exclusively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    async def task_handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("once")

    class BlockingHost:
        def __init__(self) -> None:
            self._handlers: Mapping[str, TaskHandler] = {}
            self._store: TaskWorkspaceStore | None = None

        def bind_invocation_runtime(
            self,
            *,
            handlers: Mapping[str, TaskHandler],
            store: TaskWorkspaceStore,
        ) -> None:
            self._handlers = handlers
            self._store = store

        async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
            nonlocal calls
            assert self._store is not None
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                entered.set()
                assert release.wait(timeout=5)
            handler = self._handlers[call.request.capability_id]
            workspace = self._store.begin(
                task_id=call.attempt_root.workspace_identity.task_id,
                attempt=call.attempt_root.workspace_identity.attempt,
                output_paths=call.attempt_root.workspace_identity.output_paths,
            )
            assert workspace.identity == call.attempt_root.workspace_identity
            outcome = await handler.execute(
                call.request,
                TaskContext(
                    project_root=workspace.project_root,
                    write_root=workspace.write_root,
                    workspace_identity=workspace.identity,
                    heartbeat=lambda: None,
                    cancel_requested=lambda: False,
                    invocation=call.request.invocation,
                ),
            )
            return TaskHostCallResult(operation="execute", outcome=outcome)

    product = _task_product(task_handler)
    root = tmp_path / "exclusive-recovery"
    bootstrap = Engine(root, clock=FakeClock(10), host=BlockingHost())
    initial = bootstrap.start(
        product,
        entrypoint="main",
        invocation_id="exclusive",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    def stop_after_attempt_started(phase: str, events: object) -> None:
        if phase == "after" and any(
            getattr(event, "kind", None) == "task_attempt_started"
            for event in events  # type: ignore[union-attr]
        ):
            raise RuntimeError("stop after persisted task start")

    with monkeypatch.context() as crash:
        crash.setattr(ledger_runtime, "_validated_append_boundary", stop_after_attempt_started)
        with pytest.raises(RuntimeError, match="persisted task start"):
            bootstrap.run_until_blocked(initial)

    host = BlockingHost()
    first_engine = Engine(root, clock=FakeClock(10), host=host)
    second_engine = Engine(root, clock=FakeClock(10), host=host)
    first_handle = first_engine.open("exclusive", product, authorization=empty_runtime_authorization())
    second_handle = second_engine.open("exclusive", product, authorization=empty_runtime_authorization())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_engine.run_until_blocked, first_handle)
        assert entered.wait(timeout=5)
        second = pool.submit(second_engine.run_until_blocked, second_handle)
        try:
            with pytest.raises(EngineConflictError, match="runner|claim"):
                second.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5).status == "succeeded"

    assert calls == 1


class _CrashAfterAppendEngine(Engine):
    def __init__(self, root: Path, cut: int) -> None:
        super().__init__(root)
        self.cut = cut
        self.appended = 0

    def _append(self, ledger, events, existing) -> None:  # type: ignore[no-untyped-def]
        super()._append(ledger, events, existing)
        self.appended += 1
        if self.appended == self.cut:
            raise RuntimeError("simulated process crash after committed batch")


def _finish_interrupt_invocation(
    root: Path,
    product: FrozenComposition,
    *,
    cut: int | None,
) -> tuple[str, str]:
    engine: Engine = Engine(root) if cut is None else _CrashAfterAppendEngine(root, cut)
    handle = None
    try:
        handle = engine.start(
            product,
            entrypoint="main",
            invocation_id="crash-test",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )
    except RuntimeError:
        engine = Engine(root)
        try:
            handle = engine.open("crash-test", product, authorization=empty_runtime_authorization())
        except EngineError:
            handle = engine.start(
                product,
                entrypoint="main",
                invocation_id="crash-test",
                seed=empty_invocation_seed(),
                authorization=empty_runtime_authorization(),
            )
    assert handle is not None
    try:
        blocked = engine.run_until_blocked(handle)
    except RuntimeError:
        engine = Engine(root)
        handle = engine.open("crash-test", product, authorization=empty_runtime_authorization())
        blocked = engine.run_until_blocked(handle)
    if blocked.status == "interrupted":
        try:
            handle = engine.resume(handle, action="approve", payload={"durable": True})
        except RuntimeError:
            engine = Engine(root)
            handle = engine.open("crash-test", product, authorization=empty_runtime_authorization())
            projection = fold_events(Ledger(handle.invocation_root / "ledger").read_all())
            if projection.pending_interrupt is not None:
                handle = engine.resume(handle, action="approve", payload={"durable": True})
        try:
            final = engine.run_until_blocked(handle)
        except RuntimeError:
            engine = Engine(root)
            handle = engine.open("crash-test", product, authorization=empty_runtime_authorization())
            final = engine.run_until_blocked(handle)
    else:
        final = blocked
    assert final.status == "succeeded"
    result_digest = canonical_digest(
        cast(
            JSONValue,
            {
                "output": thaw_json(final.output),
                "status": final.status,
                "terminal_reason": final.terminal_reason,
            },
        )
    )
    return result_digest, _project_state_digest(handle)


@pytest.mark.parametrize("cut", [1, 2, 3, 4])
def test_reopen_after_each_structural_batch_has_identical_final_digests(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    cut: int,
) -> None:
    baseline = _finish_interrupt_invocation(tmp_path / "baseline", resolved_interrupt_product, cut=None)
    recovered = _finish_interrupt_invocation(tmp_path / f"cut-{cut}", resolved_interrupt_product, cut=cut)

    assert recovered == baseline


def _finish_crash_matrix_invocation(
    root: Path,
    product: FrozenComposition,
) -> tuple[str, str]:
    engine = Engine(root, clock=FakeClock(10), host=_InProcessTestHost())
    try:
        handle = engine.open("matrix", product, authorization=empty_runtime_authorization())
    except EngineError:
        handle = engine.start(
            product,
            entrypoint="main",
            invocation_id="matrix",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )
    result = engine.run_until_blocked(handle)
    if result.status == "interrupted":
        handle = engine.resume(handle, action="approve", payload={"durable": True})
        result = engine.run_until_blocked(handle)
    assert result.status == "succeeded"
    result_digest = canonical_digest(
        cast(
            JSONValue,
            {
                "output": thaw_json(result.output),
                "status": result.status,
                "terminal_reason": result.terminal_reason,
            },
        )
    )
    return result_digest, _project_state_digest(handle)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process-crash fork semantics")
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "event_kind",
    [
        "graph_started",
        "task_attempt_started",
        "task_attempt_succeeded",
        "interrupt_resumed",
    ],
)
def test_fresh_open_after_exact_facade_crash_matrix_has_identical_digests(
    tmp_path: Path,
    phase: str,
    event_kind: str,
) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.write_root / "out.txt").write_bytes(b"deterministic")
        return TaskOutcome.succeeded({"task": "done"})

    product = _nested_task_interrupt_product(handler)
    baseline = _finish_crash_matrix_invocation(tmp_path / "baseline", product)
    crash_root = tmp_path / f"{event_kind}-{phase}"
    Engine(crash_root, clock=FakeClock(10), host=_InProcessTestHost()).start(
        product,
        entrypoint="main",
        invocation_id="matrix",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    process_id = os.fork()
    if process_id == 0:

        def crash_boundary(current_phase: str, events: object) -> None:
            if current_phase == phase and any(
                getattr(event, "kind", None) == event_kind
                for event in events  # type: ignore[union-attr]
            ):
                os._exit(91)

        ledger_runtime._validated_append_boundary = crash_boundary
        child_engine = Engine(crash_root, clock=FakeClock(10), host=_InProcessTestHost())
        child_handle = child_engine.open("matrix", product, authorization=empty_runtime_authorization())
        child_result = child_engine.run_until_blocked(child_handle)
        if child_result.status == "interrupted":
            child_handle = child_engine.resume(
                child_handle,
                action="approve",
                payload={"durable": True},
            )
            child_engine.run_until_blocked(child_handle)
        os._exit(0)

    _child, status = os.waitpid(process_id, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    recovered = _finish_crash_matrix_invocation(crash_root, product)

    assert recovered == baseline


def test_open_cannot_reclaim_expired_lease_from_active_claimed_runner(tmp_path: Path) -> None:
    handler_entered = threading.Event()
    release_handler = threading.Event()
    runner_outcome: list[object] = []

    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        handler_entered.set()
        assert release_handler.wait(timeout=5)
        return TaskOutcome.succeeded("done")

    clock = FakeClock(10)
    product = _task_product(handler)
    engine = Engine(tmp_path, clock=clock, host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="active-expired",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    def run() -> None:
        try:
            runner_outcome.append(engine.run_until_blocked(handle))
        except BaseException as error:
            runner_outcome.append(error)

    runner = threading.Thread(target=run, name="active-runner")
    runner.start()
    assert handler_entered.wait(timeout=5)
    clock.advance(31)
    try:
        with pytest.raises(EngineConflictError, match="runner|claim"):
            Engine(tmp_path, clock=clock, host=_InProcessTestHost()).open(
                "active-expired",
                product,
                authorization=empty_runtime_authorization(),
            )
        assert not any(
            envelope.event.kind == "task_attempt_failed"
            for envelope in Ledger(handle.invocation_root / "ledger").read_all()
        )
    finally:
        release_handler.set()
        runner.join(timeout=5)

    assert not runner.is_alive()
    assert len(runner_outcome) == 1


def test_runner_claim_rejects_directory_entry_replacement_after_lock(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="replaced-runner-claim",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()
    runner_claim = handle.invocation_root / ".engine-runner.lock"
    real_flock = fcntl.flock
    replaced = False

    def replace_after_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if not replaced:
            replaced = True
            runner_claim.unlink()
            runner_claim.write_bytes(b"replacement")

    monkeypatch.setattr(fcntl, "flock", replace_after_lock)

    with pytest.raises(EngineError, match="runner claim|stable"):
        engine.run_until_blocked(handle)

    assert ledger.read_all() == before


def test_distinct_authenticated_handler_sources_have_distinct_lock_bound_handles(
    tmp_path: Path,
) -> None:
    async def first(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("first")

    async def second(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("second")

    first_product = _task_product(first)
    second_product = _task_product(second)
    assert first_product.lock_digest != second_product.lock_digest
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    first_handle = engine.start(
        first_product,
        entrypoint="main",
        invocation_id="first-binding",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    second_handle = engine.start(
        second_product,
        entrypoint="main",
        invocation_id="second-binding",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    assert engine.run_until_blocked(first_handle).output == "first"
    assert engine.run_until_blocked(second_handle).output == "second"


def test_open_rejects_a_distinct_authenticated_handler_source(tmp_path: Path) -> None:
    async def original(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("original")

    async def reopened(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("reopened")

    original_product = _task_product(original)
    reopened_product = _task_product(reopened)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    original_handle = engine.start(
        original_product,
        entrypoint="main",
        invocation_id="open-binding-reopened",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    with pytest.raises(InvocationDrift):
        engine.open("open-binding-reopened", reopened_product, authorization=empty_runtime_authorization())
    assert engine.run_until_blocked(original_handle).output == "original"


def test_open_requires_the_exact_lock_for_the_compiled_workflow(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "terminal-validation",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="terminal-validation",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    adversarial = _resolved(
        {
            "name": "foreign",
            "entrypoints": {"main": "foreign"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "foreign": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )

    with pytest.raises(InvocationDrift):
        Engine(tmp_path).open("terminal-validation", adversarial, authorization=empty_runtime_authorization())


def test_open_rejects_self_digested_success_with_terminal_reason(tmp_path: Path) -> None:
    product = _structural_product(
        name="success-reason",
        nodes={"end": {"kind": "end"}},
        edges=[],
        start="end",
        maximum=1,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="success-reason",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)
    assert (
        RunResult(
            status="succeeded",
            projection=result.projection,
        ).terminal_reason
        is None
    )
    with pytest.raises(ValidationError, match="successful run"):
        RunResult(
            status="succeeded",
            terminal_reason="forged-success-reason",
            projection=result.projection,
        )

    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        envelope.event.model_copy(update={"terminal_reason": "forged-success-reason"})
        if isinstance(envelope.event, InvocationFinished)
        else envelope.event
        for envelope in Ledger(ledger_root).read_all()
    )
    _rewrite_ledger(ledger_root, forged_events)

    with pytest.raises(LedgerIntegrityError, match="successful invocation"):
        Engine(tmp_path).open("success-reason", product, authorization=empty_runtime_authorization())


def test_open_rejects_terminal_history_with_omitted_fanout_branch(tmp_path: Path) -> None:
    product = _structural_product(
        name="omitted-fanout",
        nodes={
            "split": {"kind": "gate", "expression": "true"},
            "left": {"kind": "end"},
            "right": {"kind": "end"},
        },
        edges=[
            {"from": "split", "to": "left"},
            {"from": "split", "to": "right"},
        ],
        start="split",
        maximum=3,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="omitted-fanout",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    events = tuple(envelope.event for envelope in Ledger(ledger_root).read_all())
    left_token = next(event for event in events if isinstance(event, TokenOffered) and event.target == "left")
    left_activation = next(
        event for event in events if isinstance(event, NodeActivated) and event.node_id == "left"
    )
    forged_events = tuple(
        event
        for event in events
        if not (
            (isinstance(event, TokenOffered | TokenConsumed) and event.token_id == left_token.token_id)
            or (
                isinstance(event, NodeActivated | NodeCompleted)
                and event.activation_id == left_activation.activation_id
            )
        )
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="event history"):
        Engine(tmp_path).open("omitted-fanout", product, authorization=empty_runtime_authorization())


@pytest.mark.parametrize("mutation", ["token_id", "payload"])
def test_open_rejects_changed_canonical_edge_token(
    tmp_path: Path,
    mutation: str,
) -> None:
    invocation_id = f"changed-edge-{mutation}"
    product = _structural_product(
        name=invocation_id,
        nodes={
            "split": {"kind": "gate", "expression": "true"},
            "end": {"kind": "end"},
        },
        edges=[{"from": "split", "to": "end"}],
        start="split",
        maximum=2,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id=invocation_id,
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    events = tuple(envelope.event for envelope in Ledger(ledger_root).read_all())
    edge_token = next(
        event for event in events if isinstance(event, TokenOffered) and event.source == "split"
    )
    end_activation = next(
        event for event in events if isinstance(event, NodeActivated) and event.node_id == "end"
    )
    if mutation == "token_id":
        forged_token_id = "forged-edge-token"
        forged_activation_id = activation_id("root", "end", 0, (forged_token_id,))
        forged_events = tuple(
            event.model_copy(update={"token_id": forged_token_id})
            if isinstance(event, TokenOffered | TokenConsumed) and event.token_id == edge_token.token_id
            else event.model_copy(
                update={
                    "activation_id": forged_activation_id,
                    "token_ids": (forged_token_id,),
                }
            )
            if isinstance(event, NodeActivated) and event.activation_id == end_activation.activation_id
            else event.model_copy(update={"activation_id": forged_activation_id})
            if isinstance(event, NodeCompleted) and event.activation_id == end_activation.activation_id
            else event
            for event in events
        )
    else:
        forged_output = {"forged": True}
        forged_events = tuple(
            event.model_copy(update={"payload": forged_output})
            if isinstance(event, TokenOffered) and event.token_id == edge_token.token_id
            else event.model_copy(update={"output": forged_output})
            if (isinstance(event, NodeCompleted) and event.activation_id == end_activation.activation_id)
            or isinstance(event, GraphCompleted)
            else event
            for event in events
        )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="event history"):
        Engine(tmp_path).open(invocation_id, product, authorization=empty_runtime_authorization())


def test_open_rejects_token_from_false_condition(tmp_path: Path) -> None:
    product = _structural_product(
        name="false-condition-token",
        nodes={
            "split": {"kind": "gate", "expression": "true"},
            "end": {"kind": "end"},
            "forbidden": {"kind": "end"},
        },
        edges=[
            {"from": "split", "to": "end", "condition": "output.value == true"},
            {"from": "split", "to": "forbidden", "condition": "output.value == false"},
        ],
        start="split",
        maximum=3,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="false-condition-token",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    events = tuple(envelope.event for envelope in Ledger(ledger_root).read_all())
    source_activation = next(
        event for event in events if isinstance(event, NodeActivated) and event.node_id == "split"
    )
    source_completion = next(
        event
        for event in events
        if isinstance(event, NodeCompleted) and event.activation_id == source_activation.activation_id
    )
    false_token_id = canonical_digest(
        {
            "edge_index": 1,
            "graph_instance_id": "root",
            "kind": "edge",
            "source": "split",
            "source_activation_id": source_activation.activation_id,
            "target": "forbidden",
        }
    )
    false_activation_id = activation_id("root", "forbidden", 0, (false_token_id,))
    forged_branch: tuple[RuntimeEvent, ...] = (
        TokenOffered(
            token_id=false_token_id,
            graph_instance_id="root",
            source="split",
            target="forbidden",
            payload=source_completion.output,
        ),
        TokenConsumed(token_id=false_token_id, graph_instance_id="root", node_id="forbidden"),
        NodeActivated(
            activation_id=false_activation_id,
            graph_instance_id="root",
            node_id="forbidden",
            token_ids=(false_token_id,),
        ),
        NodeCompleted(activation_id=false_activation_id, output=source_completion.output),
    )
    completion_index = next(index for index, event in enumerate(events) if isinstance(event, GraphCompleted))
    forged_events = (*events[:completion_index], *forged_branch, *events[completion_index:])
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="event history"):
        Engine(tmp_path).open("false-condition-token", product, authorization=empty_runtime_authorization())


def test_open_rejects_extra_duplicate_edge_token(tmp_path: Path) -> None:
    product = _structural_product(
        name="extra-edge-token",
        nodes={
            "split": {"kind": "gate", "expression": "true"},
            "end": {"kind": "end"},
        },
        edges=[{"from": "split", "to": "end"}],
        start="split",
        maximum=3,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="extra-edge-token",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    events = tuple(envelope.event for envelope in Ledger(ledger_root).read_all())
    edge_token = next(
        event for event in events if isinstance(event, TokenOffered) and event.source == "split"
    )
    extra_token_id = "extra-edge-token"
    extra_activation_id = activation_id("root", "end", 1, (extra_token_id,))
    extra_events: tuple[RuntimeEvent, ...] = (
        edge_token.model_copy(update={"token_id": extra_token_id}),
        TokenConsumed(token_id=extra_token_id, graph_instance_id="root", node_id="end"),
        NodeActivated(
            activation_id=extra_activation_id,
            graph_instance_id="root",
            node_id="end",
            token_ids=(extra_token_id,),
        ),
        NodeCompleted(activation_id=extra_activation_id, output=edge_token.payload),
    )
    completion_index = next(index for index, event in enumerate(events) if isinstance(event, GraphCompleted))
    forged_events = (*events[:completion_index], *extra_events, *events[completion_index:])
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="event history"):
        Engine(tmp_path).open("extra-edge-token", product, authorization=empty_runtime_authorization())


def test_open_rejects_non_earliest_available_token_consumption(tmp_path: Path) -> None:
    product = _structural_product(
        name="non-earliest-token",
        nodes={
            "split": {"kind": "gate", "expression": "true"},
            "left": {"kind": "gate", "expression": "true"},
            "right": {"kind": "gate", "expression": "true"},
            "work": {"kind": "gate", "expression": "true"},
        },
        edges=[
            {"from": "split", "to": "left"},
            {"from": "split", "to": "right"},
            {"from": "left", "to": "work"},
            {"from": "right", "to": "work"},
        ],
        start="split",
        maximum=6,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="non-earliest-token",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger_root = handle.invocation_root / "ledger"
    initial = Ledger(ledger_root).read_all()
    planned = plan_next(product.workflow, fold_events(initial))
    consume_index = next(
        index
        for index, event in enumerate(planned.events)
        if isinstance(event, TokenConsumed) and event.node_id == "work"
    )
    prefix = planned.events[:consume_index]
    available = sorted(
        (event for event in prefix if isinstance(event, TokenOffered) and event.target == "work"),
        key=lambda event: event.token_id,
    )
    assert len(available) == 2
    forged_token = available[1]
    forged_activation_id = activation_id("root", "work", 0, (forged_token.token_id,))
    forged_events = (
        *(envelope.event for envelope in initial),
        *prefix,
        TokenConsumed(
            token_id=forged_token.token_id,
            graph_instance_id="root",
            node_id="work",
        ),
        NodeActivated(
            activation_id=forged_activation_id,
            graph_instance_id="root",
            node_id="work",
            token_ids=(forged_token.token_id,),
        ),
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "running"

    with pytest.raises(EngineError, match="event history"):
        Engine(tmp_path).open("non-earliest-token", product, authorization=empty_runtime_authorization())


def test_open_rejects_non_earliest_token_for_all_join_predecessor(tmp_path: Path) -> None:
    product = _structural_product(
        name="non-earliest-all-join",
        nodes={
            "split": {"kind": "gate", "expression": "true"},
            "left": {"kind": "gate", "expression": "true"},
            "right": {"kind": "gate", "expression": "true"},
            "joined": {"kind": "join", "join": "all"},
        },
        edges=[
            {"from": "split", "to": "left"},
            {"from": "split", "to": "right"},
            {"from": "left", "to": "joined"},
            {"from": "left", "to": "joined"},
            {"from": "right", "to": "joined"},
        ],
        start="split",
        maximum=4,
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="non-earliest-all-join",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger_root = handle.invocation_root / "ledger"
    initial = Ledger(ledger_root).read_all()
    planned = plan_next(product.workflow, fold_events(initial))
    consume_index = next(
        index
        for index, event in enumerate(planned.events)
        if isinstance(event, TokenConsumed) and event.node_id == "joined"
    )
    prefix = planned.events[:consume_index]
    left_tokens = sorted(
        (
            event
            for event in prefix
            if isinstance(event, TokenOffered) and event.target == "joined" and event.source == "left"
        ),
        key=lambda event: event.token_id,
    )
    right_token = next(
        event
        for event in prefix
        if isinstance(event, TokenOffered) and event.target == "joined" and event.source == "right"
    )
    assert len(left_tokens) == 2
    forged_token_ids = (left_tokens[1].token_id, right_token.token_id)
    forged_activation_id = activation_id("root", "joined", 0, forged_token_ids)
    forged_events = (
        *(envelope.event for envelope in initial),
        *prefix,
        *(
            TokenConsumed(token_id=token_id_, graph_instance_id="root", node_id="joined")
            for token_id_ in forged_token_ids
        ),
        NodeActivated(
            activation_id=forged_activation_id,
            graph_instance_id="root",
            node_id="joined",
            token_ids=forged_token_ids,
        ),
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "running"

    with pytest.raises(EngineError, match="event history"):
        Engine(tmp_path).open("non-earliest-all-join", product, authorization=empty_runtime_authorization())


def test_open_rejects_terminal_task_completed_without_committed_attempt(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"forged": False})

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-task",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    removed_kinds = {
        "task_attempt_started",
        "task_lease_acquired",
        "task_lease_heartbeat",
        "task_attempt_succeeded",
        "head_advanced",
        "task_commit_prepared",
        "task_promotion_completed",
    }
    events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if envelope.event.kind not in removed_kinds
    )
    forged = tuple(
        EventEnvelope.from_event(sequence, event) for sequence, event in enumerate(events, start=1)
    )
    for batch in ledger_root.glob("[0-9]*.json"):
        batch.unlink()
    final = ledger_root / f"{1:010d}-{len(forged):010d}.json"
    final.write_bytes(
        canonical_json_bytes(cast(JSONValue, [envelope.model_dump(mode="json") for envelope in forged]))
    )
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="completed task|committed successful"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-task",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_completed_graph_without_completed_end_activation(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "forged-empty-terminal",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "gate",
                    "nodes": {
                        "gate": {"kind": "gate", "expression": "false"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "gate", "to": "end", "condition": "false"}],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-empty-terminal",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger_root = handle.invocation_root / "ledger"
    initial = Ledger(ledger_root).read_all()
    projection = fold_events(initial)
    planned = plan_next(product.workflow, projection)
    assert [event.kind for event in planned.events] == [
        "token_consumed",
        "node_activated",
        "node_completed",
    ]
    forged_events = (
        *(envelope.event for envelope in initial),
        *planned.events,
        GraphCompleted(graph_instance_id="root", output=None),
        InvocationFinished(invocation_id="forged-empty-terminal", status="succeeded"),
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="completed end activation"):
        Engine(tmp_path).open("forged-empty-terminal", product, authorization=empty_runtime_authorization())


def test_open_rejects_forged_root_input_even_with_matching_start_token(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "root-input",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-root-input",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        event.model_copy(update={"input": {"forged": True}})
        if isinstance(event, GraphStarted)
        else event.model_copy(update={"payload": {"forged": True}})
        if event.kind == "token_offered" and event.source is None
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="root graph input"):
        Engine(tmp_path).open("forged-root-input", product, authorization=empty_runtime_authorization())


def test_open_rejects_forged_terminal_structural_output(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "structural-output",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-structural-output",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        event.model_copy(update={"output": {"forged": True}})
        if event.kind in {"node_completed", "graph_completed"}
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="non-canonical output"):
        Engine(tmp_path).open(
            "forged-structural-output", product, authorization=empty_runtime_authorization()
        )


@pytest.mark.parametrize("status", ["failed", "stopped"])
def test_open_rejects_terminal_status_without_compiled_causal_proof(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    status: str,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id=f"forged-{status}",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    existing = ledger.read_all()
    ledger.append_batch(
        (
            InvocationFinished(
                invocation_id=f"forged-{status}",
                status=status,  # type: ignore[arg-type]
                terminal_reason=f"forged-{status}",
            ),
        ),
        expected_next_seq=existing[-1].seq + 1,
    )
    assert fold_events(ledger.read_all()).status == status

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path).open(
            f"forged-{status}", resolved_interrupt_product, authorization=empty_runtime_authorization()
        )


def test_open_rejects_stopped_child_without_compiled_failure_propagation(tmp_path: Path) -> None:
    async def stop(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.stopped("operator_stop")

    product = _task_product(stop, nested=True)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-stop-propagation",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "stopped"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if envelope.event.kind not in {"graph_failed", "node_failed"}
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "stopped"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-stop-propagation",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_failed_child_without_compiled_failure_propagation(tmp_path: Path) -> None:
    async def fail(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.failed("invalid_input", "bad child")

    product = _task_product(fail, nested=True)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-failure-propagation",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    result = engine.run_until_blocked(handle)
    assert result.status == "failed"
    root_graph_id = next(
        graph.graph_instance_id
        for graph in result.projection.graph_instances
        if graph.parent_graph_instance_id is None
    )
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if not (
            (envelope.event.kind == "graph_failed" and envelope.event.graph_instance_id != root_graph_id)
            or (envelope.event.kind == "node_failed" and envelope.event.failure.kind == "internal")
        )
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "failed"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-failure-propagation",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_task_failure_with_unrelated_failed_sibling(tmp_path: Path) -> None:
    product, invocation_id = _forge_unrelated_failed_sibling(tmp_path, "failed")

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            invocation_id,
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_stop_with_unrelated_failed_sibling(tmp_path: Path) -> None:
    product, invocation_id = _forge_unrelated_failed_sibling(tmp_path, "stopped")

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            invocation_id,
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_task_failure_with_omitted_exhausted_sibling_settlement(
    tmp_path: Path,
) -> None:
    product, ledger_root, activations = _parallel_task_ledger(
        tmp_path,
        "omitted-exhausted-settlement",
    )
    ledger = Ledger(ledger_root)
    outcomes = (
        *_failed_attempt_events(activations["cause"], "invalid_input"),
        *_failed_attempt_events(activations["sibling"], "invalid_input"),
    )
    ledger.append_batch(  # type: ignore[arg-type]
        outcomes,
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert terminal.terminal == "failed"
    failed_activation_ids = tuple(
        event.activation_id for event in terminal.events if isinstance(event, NodeFailed)
    )
    assert len(failed_activation_ids) == 2
    omitted_activation_id = failed_activation_ids[-1]
    forged_terminal = tuple(
        event
        for event in terminal.events
        if not (isinstance(event, NodeFailed) and event.activation_id == omitted_activation_id)
    )
    _rewrite_ledger(
        ledger_root,
        tuple(envelope.event for envelope in ledger.read_all()) + forged_terminal,
    )
    projection = fold_events(Ledger(ledger_root).read_all())
    omitted = next(
        activation
        for activation in projection.activations
        if activation.activation_id == omitted_activation_id
    )
    assert projection.status == "failed"
    assert omitted.status == "active"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "omitted-exhausted-settlement",
            product,
            authorization=empty_runtime_authorization(),
        )


@pytest.mark.parametrize("terminal_kind", ["failed", "stopped"])
def test_open_rejects_terminal_task_without_canonical_lease_binding(
    tmp_path: Path,
    terminal_kind: Literal["failed", "stopped"],
) -> None:
    invocation_id = f"missing-terminal-lease-{terminal_kind}"
    product, ledger_root, activations = _parallel_task_ledger(tmp_path, invocation_id)
    ledger = Ledger(ledger_root)
    attempt_events = (
        _failed_attempt_events(activations["cause"], "invalid_input")
        if terminal_kind == "failed"
        else _stopped_attempt_events(activations["cause"])
    )
    ledger.append_batch(attempt_events, expected_next_seq=ledger.read_all()[-1].seq + 1)
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert terminal.terminal == terminal_kind
    ledger.append_batch(
        terminal.events,
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    _rewrite_ledger(
        ledger_root,
        tuple(
            envelope.event
            for envelope in ledger.read_all()
            if not isinstance(envelope.event, TaskLeaseAcquired)
        ),
    )
    assert fold_events(ledger.read_all()).status == terminal_kind

    with pytest.raises(EngineError, match="canonical lease"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            invocation_id,
            product,
            authorization=empty_runtime_authorization(),
        )


@pytest.mark.parametrize("terminal_kind", ["failed", "stopped", "activation_bound"])
def test_open_rejects_terminal_with_retryable_sibling_missing_canonical_lease(
    tmp_path: Path,
    terminal_kind: Literal["failed", "stopped", "activation_bound"],
) -> None:
    invocation_id = f"missing-retryable-sibling-lease-{terminal_kind}"
    product, ledger_root, activations = _parallel_task_ledger(
        tmp_path,
        invocation_id,
        activation_bound=terminal_kind == "activation_bound",
    )
    ledger = Ledger(ledger_root)
    events: tuple[RuntimeEvent, ...] = _failed_attempt_events(
        activations["sibling"],
        "transient",
    )
    if terminal_kind == "failed":
        events = (*_failed_attempt_events(activations["cause"], "invalid_input"), *events)
    elif terminal_kind == "stopped":
        events = (*_stopped_attempt_events(activations["cause"]), *events)
    ledger.append_batch(events, expected_next_seq=ledger.read_all()[-1].seq + 1)
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    expected_status = "failed" if terminal_kind != "stopped" else "stopped"
    assert terminal.terminal == expected_status
    ledger.append_batch(terminal.events, expected_next_seq=ledger.read_all()[-1].seq + 1)
    _rewrite_ledger(
        ledger_root,
        tuple(
            envelope.event
            for envelope in ledger.read_all()
            if not (
                isinstance(envelope.event, TaskLeaseAcquired)
                and envelope.event.activation_id == activations["sibling"]
            )
        ),
    )
    assert fold_events(ledger.read_all()).status == expected_status

    with pytest.raises(EngineError, match="canonical lease"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            invocation_id,
            product,
            authorization=empty_runtime_authorization(),
        )


@pytest.mark.parametrize("terminal_kind", ["failed", "stopped"])
def test_open_rejects_terminal_task_with_earlier_attempt_missing_canonical_lease(
    tmp_path: Path,
    terminal_kind: Literal["failed", "stopped"],
) -> None:
    invocation_id = f"missing-earlier-attempt-lease-{terminal_kind}"
    product, ledger_root, activations = _parallel_task_ledger(tmp_path, invocation_id)
    cause = activations["cause"]
    terminal_events = (
        _failed_attempt_events(cause, "invalid_input", attempt=2)
        if terminal_kind == "failed"
        else _stopped_attempt_events(cause, attempt=2)
    )
    ledger = Ledger(ledger_root)
    ledger.append_batch(
        (
            *_failed_attempt_events(cause, "transient"),
            *terminal_events,
        ),
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert terminal.terminal == terminal_kind
    ledger.append_batch(terminal.events, expected_next_seq=ledger.read_all()[-1].seq + 1)
    _rewrite_ledger(
        ledger_root,
        tuple(
            envelope.event
            for envelope in ledger.read_all()
            if not (
                isinstance(envelope.event, TaskLeaseAcquired)
                and envelope.event.activation_id == cause
                and envelope.event.attempt == 1
            )
        ),
    )
    assert fold_events(ledger.read_all()).status == terminal_kind

    with pytest.raises(EngineError, match="canonical lease"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            invocation_id,
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_task_failure_with_forged_retryable_sibling_settlement(
    tmp_path: Path,
) -> None:
    product, ledger_root, activations = _parallel_task_ledger(
        tmp_path,
        "forged-retryable-task-failure",
    )
    ledger = Ledger(ledger_root)
    sibling_events = _failed_attempt_events(activations["sibling"], "transient")
    ledger.append_batch(  # type: ignore[arg-type]
        (
            *_failed_attempt_events(activations["cause"], "invalid_input"),
            *sibling_events,
        ),
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert terminal.terminal == "failed"
    sibling_failure = sibling_events[-1]
    assert isinstance(sibling_failure, TaskAttemptFailed)
    _rewrite_ledger(
        ledger_root,
        tuple(envelope.event for envelope in ledger.read_all())
        + (
            NodeFailed(
                activation_id=activations["sibling"],
                failure=sibling_failure.failure,
            ),
            *terminal.events,
        ),
    )
    projection = fold_events(Ledger(ledger_root).read_all())
    assert projection.status == "failed"
    assert any(
        activation.activation_id == activations["sibling"] and activation.status == "failed"
        for activation in projection.activations
    )

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-retryable-task-failure",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_stop_with_forged_retryable_sibling_settlement(tmp_path: Path) -> None:
    product, ledger_root, activations = _parallel_task_ledger(
        tmp_path,
        "forged-retryable-stop",
    )
    ledger = Ledger(ledger_root)
    sibling_events = _failed_attempt_events(activations["sibling"], "transient")
    ledger.append_batch(  # type: ignore[arg-type]
        (*_stopped_attempt_events(activations["cause"]), *sibling_events),
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    assert terminal.terminal == "stopped"
    sibling_failure = sibling_events[-1]
    assert isinstance(sibling_failure, TaskAttemptFailed)
    _rewrite_ledger(
        ledger_root,
        tuple(envelope.event for envelope in ledger.read_all())
        + (
            NodeFailed(
                activation_id=activations["sibling"],
                failure=sibling_failure.failure,
            ),
            *terminal.events,
        ),
    )
    assert fold_events(Ledger(ledger_root).read_all()).status == "stopped"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-retryable-stop",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_activation_bound_with_forged_retryable_task_settlement(
    tmp_path: Path,
) -> None:
    product, ledger_root, activations = _parallel_task_ledger(
        tmp_path,
        "forged-retryable-activation-bound",
        activation_bound=True,
    )
    ledger = Ledger(ledger_root)
    sibling_events = _failed_attempt_events(activations["sibling"], "transient")
    ledger.append_batch(  # type: ignore[arg-type]
        sibling_events,
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    sibling_failure = sibling_events[-1]
    assert isinstance(sibling_failure, TaskAttemptFailed)
    reason = "max_activations_exceeded:root"
    _rewrite_ledger(
        ledger_root,
        tuple(envelope.event for envelope in ledger.read_all())
        + (
            NodeFailed(
                activation_id=activations["sibling"],
                failure=sibling_failure.failure,
            ),
            GraphFailed(graph_instance_id="root", reason=reason),
            InvocationFinished(
                invocation_id="forged-retryable-activation-bound",
                status="failed",
                terminal_reason=reason,
            ),
        ),
    )
    assert fold_events(Ledger(ledger_root).read_all()).status == "failed"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-retryable-activation-bound",
            product,
            authorization=empty_runtime_authorization(),
        )


@pytest.mark.parametrize("terminal_kind", ["failed", "stopped"])
def test_open_accepts_exact_terminal_with_unsettled_retryable_sibling(
    tmp_path: Path,
    terminal_kind: Literal["failed", "stopped"],
) -> None:
    invocation_id = f"exact-{terminal_kind}-with-retryable-sibling"
    product, ledger_root, activations = _parallel_task_ledger(tmp_path, invocation_id)
    ledger = Ledger(ledger_root)
    sibling_events = _failed_attempt_events(
        activations["sibling"],
        "transient",
    )
    if terminal_kind == "failed":
        cause_events = _failed_attempt_events(activations["cause"], "invalid_input")
    else:
        cause_events = _stopped_attempt_events(activations["cause"])
    events: tuple[RuntimeEvent, ...] = (
        *cause_events[:2],
        *sibling_events[:2],
        cause_events[2],
        sibling_events[2],
    )
    ledger.append_batch(  # type: ignore[arg-type]
        events,
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    terminal = plan_next(product.workflow, fold_events(ledger.read_all()))
    expected_status = "failed" if terminal_kind != "stopped" else "stopped"
    assert terminal.terminal == expected_status
    assert not any(
        isinstance(event, NodeFailed) and event.activation_id == activations["sibling"]
        for event in terminal.events
    )
    ledger.append_batch(
        terminal.events,
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )

    with Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()) as engine:
        with engine.open(invocation_id, product, authorization=empty_runtime_authorization()) as handle:
            result = engine.run_until_blocked(handle)
    sibling = next(
        activation
        for activation in result.projection.activations
        if activation.activation_id == activations["sibling"]
    )
    assert result.status == expected_status
    assert sibling.status == "active"
    assert sibling.attempts[-1].status == "failed"
    assert sibling.attempts[-1].failure is not None
    assert sibling.attempts[-1].failure.kind == "transient"


def test_open_rejects_activation_bound_failure_without_next_ready_activation(
    tmp_path: Path,
) -> None:
    product = _resolved(
        {
            "name": "no-ready-at-bound",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="no-ready-at-bound",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    reason = "max_activations_exceeded:root"
    forged_events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if envelope.event.kind not in {"graph_completed", "invocation_finished"}
    ) + (
        GraphFailed(graph_instance_id="root", reason=reason),
        InvocationFinished(
            invocation_id="no-ready-at-bound",
            status="failed",
            terminal_reason=reason,
        ),
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "failed"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path).open("no-ready-at-bound", product, authorization=empty_runtime_authorization())


def test_open_rejects_task_failure_relabelled_as_activation_bound(tmp_path: Path) -> None:
    async def fail(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.failed("invalid_input", "bad task")

    product = _resolved(
        {
            "name": "task-failure-at-bound",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "work",
                    "nodes": {
                        "work": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                        }
                    },
                    "edges": [],
                }
            },
        },
        {"test.empty.run": _FunctionHandler(fail)},
    )
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="task-failure-at-bound",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "failed"
    ledger_root = handle.invocation_root / "ledger"
    reason = "max_activations_exceeded:root"
    forged_events = tuple(
        event.model_copy(update={"reason": reason})
        if event.kind == "graph_failed"
        else event.model_copy(update={"terminal_reason": reason})
        if event.kind == "invocation_finished"
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "failed"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "task-failure-at-bound",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_accepts_exact_planner_derived_activation_bound_failure(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "real-activation-bound",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "loop",
                    "nodes": {"loop": {"kind": "gate", "expression": "true"}},
                    "edges": [{"from": "loop", "to": "loop"}],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="real-activation-bound",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    failed = engine.run_until_blocked(handle)
    assert failed.status == "failed"
    assert failed.reason == "max_activations_exceeded:root"

    reopened_engine = Engine(tmp_path)
    reopened = reopened_engine.open(
        "real-activation-bound", product, authorization=empty_runtime_authorization()
    )
    replayed = reopened_engine.run_until_blocked(reopened)
    assert replayed.status == "failed"
    assert replayed.reason == "max_activations_exceeded:root"


def test_open_rejects_activation_bound_failure_missing_deterministic_settlement(
    tmp_path: Path,
) -> None:
    product = _resolved(
        {
            "name": "incomplete-activation-bound",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "loop",
                    "nodes": {"loop": {"kind": "gate", "expression": "true"}},
                    "edges": [{"from": "loop", "to": "loop"}],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="incomplete-activation-bound",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "failed"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if envelope.event.kind != "node_completed"
        and not (envelope.event.kind == "token_offered" and envelope.event.source is not None)
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "failed"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path).open(
            "incomplete-activation-bound", product, authorization=empty_runtime_authorization()
        )


def test_open_rejects_a_different_compiled_activation_bound_by_lock(tmp_path: Path) -> None:
    def cyclic_product(maximum: int) -> FrozenComposition:
        return _resolved(
            {
                "name": "over-bound-history",
                "entrypoints": {"main": "root"},
                "retry": {},
                "timeout": {},
                "graphs": {
                    "root": {
                        "max_activations": maximum,
                        "start": "loop",
                        "nodes": {"loop": {"kind": "gate", "expression": "true"}},
                        "edges": [{"from": "loop", "to": "loop"}],
                    }
                },
            }
        )

    source_product = cyclic_product(2)
    compiled_product = cyclic_product(1)
    engine = Engine(tmp_path)
    handle = engine.start(
        source_product,
        entrypoint="main",
        invocation_id="over-bound-history",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "failed"
    ledger_root = handle.invocation_root / "ledger"
    projection = fold_events(Ledger(ledger_root).read_all())
    assert len(projection.activations) == 2
    assert projection.status == "failed"

    with pytest.raises(InvocationDrift):
        Engine(tmp_path).open(
            "over-bound-history", compiled_product, authorization=empty_runtime_authorization()
        )


def test_open_rejects_noncanonical_task_id_in_terminal_ledger(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("done")

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="forged-task-id",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        event.model_copy(update={"task_id": "forged-task-id"})
        if event.kind
        in {
            "task_lease_acquired",
            "task_lease_heartbeat",
            "head_advanced",
            "task_commit_prepared",
            "task_promotion_completed",
        }
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="canonical task id"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-task-id",
            product,
            authorization=empty_runtime_authorization(),
        )


def test_open_rejects_forged_child_input_even_with_matching_start_token(
    tmp_path: Path,
    resolved_subgraph_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_subgraph_product,
        entrypoint="main",
        invocation_id="forged-child-input",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    child_id = next(
        event.graph_instance_id
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
        if isinstance(event, GraphStarted) and event.parent_activation_id is not None
    )
    forged_events = tuple(
        event.model_copy(update={"input": {"forged": True}})
        if isinstance(event, GraphStarted) and event.graph_instance_id == child_id
        else event.model_copy(update={"payload": {"forged": True}})
        if event.kind == "token_offered" and event.graph_instance_id == child_id and event.source is None
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="parent binding"):
        Engine(tmp_path).open(
            "forged-child-input", resolved_subgraph_product, authorization=empty_runtime_authorization()
        )


def test_interrupt_runtime_metadata_is_required() -> None:
    with pytest.raises(ValidationError):
        NodeInterrupted(activation_id="activation", interrupt_id="interrupt")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        InterruptResumed(interrupt_id="interrupt", action="continue")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "fields",
    [
        {"parent_graph_instance_id": "root"},
        {"parent_node_id": "child"},
        {"parent_activation_id": "activation"},
        {
            "parent_graph_instance_id": "root",
            "parent_node_id": "child",
        },
    ],
)
def test_graph_started_parent_binding_is_all_or_none(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="parent"):
        GraphStarted.model_validate({"graph_instance_id": "child", "graph_id": "nested", **fields})


def test_graph_started_root_has_no_parent_and_uses_graph_id_as_instance_id() -> None:
    with pytest.raises(ValidationError, match="root"):
        GraphStarted(graph_instance_id="not-root", graph_id="root")


def test_open_translates_reclaim_conflict_to_engine_conflict(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Engine(tmp_path).start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="open-conflict",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    def conflict(_scheduler: Scheduler) -> tuple[str, ...]:
        from graph_engine.runtime.ledger import LedgerConflictError

        raise LedgerConflictError("lost reclaim CAS")

    monkeypatch.setattr(Scheduler, "reclaim_expired", conflict)
    with pytest.raises(EngineConflictError, match="advanced"):
        Engine(tmp_path).open(
            "open-conflict", resolved_interrupt_product, authorization=empty_runtime_authorization()
        )


def test_run_translates_unreadable_success_reconciliation_to_engine_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = False
    append_failed = False

    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        nonlocal armed
        armed = True
        return TaskOutcome.succeeded("published")

    def fail_after_final_install(name: str) -> None:
        nonlocal append_failed
        if armed and name == "final_installed":
            append_failed = True
            raise OSError("append result unavailable")

    original_read = Ledger.read_all

    def fail_reconciliation_read(self: Ledger) -> tuple[EventEnvelope, ...]:
        if append_failed:
            raise OSError("reconciliation unavailable")
        return original_read(self)

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="indeterminate-success",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_final_install)
    monkeypatch.setattr(Ledger, "read_all", fail_reconciliation_read)

    with pytest.raises(EnginePublicationIndeterminate, match="indeterminate"):
        engine.run_until_blocked(handle)


def test_engine_managed_successor_cannot_consume_output_before_promotion_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def handler(request: TaskRequest, context: TaskContext) -> TaskOutcome:
        calls.append(request.node_id)
        if request.node_id == "produce":
            (context.write_root / "out.txt").write_bytes(b"candidate")
        else:
            assert (context.project_root / "out.txt").read_bytes() == b"candidate"
        return TaskOutcome.succeeded({"node": request.node_id})

    product = _resolved(
        {
            "name": "receipt-gated-successor",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 3,
                    "start": "produce",
                    "nodes": {
                        "produce": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                            "resources": {"writes": ["out.txt"]},
                        },
                        "consume": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                            "resources": {"reads": ["out.txt"]},
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [
                        {"from": "produce", "to": "consume"},
                        {"from": "consume", "to": "end"},
                    ],
                }
            },
        },
        {"test.empty.run": _FunctionHandler(handler)},
    )
    real_install = TaskWorkspaceStore._install_completed_receipt
    fail_once = True

    def fail_receipt_once(
        self: TaskWorkspaceStore,
        expected_receipt: PromotionReceipt,
        *,
        receipt_name: str,
    ) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("injected receipt publication failure")
        real_install(self, expected_receipt, receipt_name=receipt_name)

    monkeypatch.setattr(TaskWorkspaceStore, "_install_completed_receipt", fail_receipt_once)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(
        product,
        entrypoint="main",
        invocation_id="receipt-gated-successor",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )

    with pytest.raises(EnginePublicationIndeterminate, match="promotion"):
        engine.run_until_blocked(handle)

    assert calls == ["produce"]
    kinds = [envelope.event.kind for envelope in Ledger(handle.invocation_root / "ledger").read_all()]
    assert "task_commit_prepared" in kinds
    assert "task_promotion_completed" not in kinds
    assert "task_attempt_failed" not in kinds

    assert engine.run_until_blocked(handle).status == "succeeded"
    assert calls == ["produce", "consume"]


def test_initialization_failure_before_ledger_leaves_exactly_recoverable_identity(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(name: str) -> None:
        if name == "workspace_roots_ready":
            raise OSError("simulated pre-ledger crash")

    monkeypatch.setattr(engine_runtime, "_initialization_boundary", crash, raising=False)
    engine = Engine(tmp_path)
    with pytest.raises(OSError, match="pre-ledger"):
        engine.start(
            resolved_interrupt_product,
            entrypoint="main",
            invocation_id="retryable",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )
    invocation_root = tmp_path / "invocations" / "retryable"
    assert (invocation_root / "invocation.lock.json").read_bytes() == (
        resolved_interrupt_product.lock.canonical_bytes
    )
    assert not (invocation_root / "ledger").exists()

    monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _name: None, raising=False)
    assert (
        engine.start(
            resolved_interrupt_product,
            entrypoint="main",
            invocation_id="retryable",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        ).invocation_id
        == "retryable"
    )


def test_symlinked_invocation_namespace_is_rejected_without_external_write(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "engine"
    root.mkdir()
    (root / "invocations").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="namespace|symlink|trusted"):
        Engine(root).start(
            resolved_interrupt_product,
            entrypoint="main",
            invocation_id="escape",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )
    assert tuple(outside.iterdir()) == ()


def test_symlinked_engine_root_component_is_rejected_before_external_creation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-root"
    outside.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / "redirect").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="trusted|root|namespace"):
        Engine(anchor / "redirect" / "engine")

    assert tuple(outside.iterdir()) == ()


def test_namespace_swap_after_handle_binding_fails_closed_without_external_write(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    root = tmp_path / "engine"
    engine = Engine(root)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="pinned",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    trusted = root / "invocations"
    held = root / "held-invocations"
    trusted.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    trusted.symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="namespace"):
        engine.run_until_blocked(handle)
    assert tuple(outside.iterdir()) == ()


def test_leaf_swap_after_handle_binding_is_rejected_as_stale(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="leaf",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    invocation = tmp_path / "invocations" / "leaf"
    invocation.rename(tmp_path / "invocations" / "held-leaf")
    outside = tmp_path / "outside-leaf"
    outside.mkdir()
    invocation.symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvocationDrift, match="anchor|directory|identity"):
        engine.run_until_blocked(handle)
    assert tuple(outside.iterdir()) == ()


def test_engine_and_handle_lifecycle_is_context_managed_idempotent_and_fail_closed(
    tmp_path: Path,
    resolved_subgraph_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    with engine:
        handle = engine.start(
            resolved_subgraph_product,
            entrypoint="main",
            invocation_id="lifecycle",
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
        )
        with handle:
            assert engine.run_until_blocked(handle).status == "succeeded"
        handle.close()
        with pytest.raises(EngineError, match="closed"):
            _ = handle.workspace
        with pytest.raises(EngineError, match="closed"):
            engine.run_until_blocked(handle)

    engine.close()
    with pytest.raises(EngineError, match="closed"):
        engine.open("lifecycle", resolved_subgraph_product, authorization=empty_runtime_authorization())


def test_repeated_open_close_does_not_grow_invocation_descriptors(
    tmp_path: Path,
    resolved_subgraph_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    initial = engine.start(
        resolved_subgraph_product,
        entrypoint="main",
        invocation_id="descriptor-growth",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(initial).status == "succeeded"
    initial.close()
    baseline = len(os.listdir("/dev/fd"))

    for _index in range(50):
        with engine.open(
            "descriptor-growth", resolved_subgraph_product, authorization=empty_runtime_authorization()
        ):
            pass

    assert len(os.listdir("/dev/fd")) <= baseline + 1
    engine.close()


def test_workspace_store_remains_bound_after_handle_descriptor_is_reused(tmp_path: Path) -> None:
    async def first_handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.write_root / "out.txt").write_bytes(b"first")
        return TaskOutcome.succeeded("first")

    async def second_handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.write_root / "out.txt").write_bytes(b"second")
        return TaskOutcome.succeeded("second")

    root = tmp_path / "workspace-fd-reuse"
    first_product = _nested_task_interrupt_product(first_handler)
    second_product = _nested_task_interrupt_product(second_handler)
    engine = Engine(root, clock=FakeClock(10), host=_InProcessTestHost())
    first = engine.start(
        first_product,
        entrypoint="main",
        invocation_id="first",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    second = engine.start(
        second_product,
        entrypoint="main",
        invocation_id="second",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(first).status == "interrupted"
    assert engine.run_until_blocked(second).status == "interrupted"
    store = first.workspace
    first_project = first._workspace_binding.project_root
    second_project = second._workspace_binding.project_root
    assert (first_project / "out.txt").read_bytes() == b"first"
    assert (second_project / "out.txt").read_bytes() == b"second"

    reused_descriptor = first._invocation_fd
    first.close()
    opened: list[int] = []
    try:
        for _index in range(32):
            descriptor = os.open(second.invocation_root, os.O_RDONLY | os.O_DIRECTORY)
            opened.append(descriptor)
            if descriptor == reused_descriptor:
                break
        assert reused_descriptor in opened
        assert store.project_root == first_project
        assert (store.project_root / "out.txt").read_bytes() == b"first"
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()
        for descriptor in opened:
            os.close(descriptor)


def test_workspace_store_lifecycle_is_idempotent_and_does_not_grow_descriptors(
    tmp_path: Path,
    resolved_subgraph_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_subgraph_product,
        entrypoint="main",
        invocation_id="workspace-store-lifecycle",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    baseline = len(os.listdir("/dev/fd"))

    for _index in range(50):
        store = handle.workspace
        assert store.project_root == handle._workspace_binding.project_root
        store.close()

    assert len(os.listdir("/dev/fd")) <= baseline + 1
    store = handle.workspace
    store.close()
    store.close()
    with pytest.raises((OSError, TypeError, AttributeError)):
        store.begin(task_id="closed", attempt=1, output_paths=("out.txt",))


def test_repeated_failed_initialization_closes_internal_workspace_with_retained_tracebacks(
    tmp_path: Path,
    resolved_subgraph_product: FrozenComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Engine(tmp_path)
    failures: list[BaseException] = []
    baseline = len(os.listdir("/dev/fd"))

    def fail_after_workspace(phase: str) -> None:
        if phase == "workspace_roots_ready":
            raise RuntimeError("fail after workspace initialization")

    monkeypatch.setattr(engine_runtime, "_initialization_boundary", fail_after_workspace)
    try:
        for _index in range(24):
            try:
                engine.start(
                    resolved_subgraph_product,
                    entrypoint="main",
                    invocation_id="retained-start-failure",
                    seed=empty_invocation_seed(),
                    authorization=empty_runtime_authorization(),
                )
            except RuntimeError as error:
                failures.append(error)
        assert len(failures) == 24
        assert len(os.listdir("/dev/fd")) <= baseline + 1
    finally:
        failures.clear()
        engine.close()


@pytest.mark.parametrize("failure_stage", ["recovery", "validation"])
def test_repeated_failed_open_closes_internal_workspace_with_retained_tracebacks(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    bootstrap = Engine(tmp_path)
    handle = bootstrap.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id=f"retained-open-{failure_stage}",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    handle.close()
    bootstrap.close()
    opener = Engine(tmp_path)
    failures: list[BaseException] = []
    if failure_stage == "recovery":

        def fail_recovery(
            self: Engine,
            projection: object,
            store: TaskWorkspaceStore,
        ) -> None:
            del self, projection, store
            raise RuntimeError("fail activity workspace recovery")

        monkeypatch.setattr(Engine, "_authenticate_live_activity_workspaces", fail_recovery)
    else:
        real_validate = Engine._validate_workflow
        validation_calls = 0

        def fail_second_validation(
            self: Engine,
            product: FrozenComposition,
            projection: object,
        ) -> None:
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls % 2 == 0:
                raise RuntimeError("fail post-recovery validation")
            real_validate(self, product, projection)  # type: ignore[arg-type]

        monkeypatch.setattr(Engine, "_validate_workflow", fail_second_validation)
    baseline = len(os.listdir("/dev/fd"))
    try:
        for _index in range(24):
            try:
                opener.open(
                    f"retained-open-{failure_stage}",
                    resolved_interrupt_product,
                    authorization=empty_runtime_authorization(),
                )
            except RuntimeError as error:
                failures.append(error)
        assert len(failures) == 24
        assert len(os.listdir("/dev/fd")) <= baseline + 1
    finally:
        failures.clear()
        opener.close()


def test_repeated_failed_run_closes_internal_workspace_with_retained_tracebacks(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="retained-run-failure",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    failures: list[BaseException] = []

    def fail_workspace_preflight(
        self: Engine,
        handle: InvocationHandle,
        composition: FrozenComposition,
        invocation_root: Path,
        invocation_fd: int,
        store: TaskWorkspaceStore,
    ) -> RunResult:
        del self, handle, composition, invocation_root, invocation_fd, store
        raise RuntimeError("fail workspace preflight")

    monkeypatch.setattr(Engine, "_run_until_blocked_with_store", fail_workspace_preflight)
    baseline = len(os.listdir("/dev/fd"))
    try:
        for _index in range(24):
            try:
                engine.run_until_blocked(handle)
            except RuntimeError as error:
                failures.append(error)
        assert len(failures) == 24
        assert len(os.listdir("/dev/fd")) <= baseline + 1
    finally:
        failures.clear()
        handle.close()
        engine.close()


def test_open_rejects_interrupt_metadata_that_differs_from_compiled_definition(
    tmp_path: Path,
    resolved_interrupt_product: FrozenComposition,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="interrupt-metadata",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
    )
    assert engine.run_until_blocked(handle).status == "interrupted"
    handle = engine.resume(handle, action="approve", payload={"reviewed": True})
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    for batch in sorted(ledger_root.glob("[0-9]*.json")):
        document = json.loads(batch.read_bytes())
        changed = False
        for index, raw in enumerate(document):
            if raw["event"].get("kind") == "node_interrupted":
                event = NodeInterrupted.model_validate(raw["event"], strict=False)
                document[index] = EventEnvelope.from_event(
                    raw["seq"],
                    event.model_copy(update={"reason": "forged reason"}),
                ).model_dump(mode="json")
                changed = True
        if changed:
            batch.write_bytes(canonical_json_bytes(cast(JSONValue, document)))
            break

    with pytest.raises(EngineError, match="workflow|interrupt|reason"):
        Engine(tmp_path).open(
            "interrupt-metadata", resolved_interrupt_product, authorization=empty_runtime_authorization()
        )
