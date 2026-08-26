from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields as dataclass_fields
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from agent_runtime_contracts import (
    AgentRunRequest,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes, thaw_json
from agent_runtime_cursor import CursorAdapterConfig, CursorHandler
from agent_runtime_cursor.handler import CursorDispatchIncomplete
from agent_runtime_opencode import OpenCodeAdapterConfig, OpenCodeHandler
from agent_runtime_opencode.discovery import OpenCodeDispatchIncomplete
from graph_engine import ENGINE_API_VERSION, Engine
from graph_engine.canonical import canonical_digest as engine_digest
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
from graph_engine.plugin_api import (
    CommitValidator,
    InvocationWorkspaceBinding,
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
)
from graph_engine.runtime.activity import LedgerTaskActivityPort
from graph_engine.runtime.engine import InvocationHandle
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
    authorized_secret_port,
)
from graph_engine.runtime.host_receipts import TerminalReceiptStore, prove_call_quiescent
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import PlannedTask, fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import AttemptResult, Scheduler, SystemClock
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.task_workspace import TaskWorkspaceStore

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
    tests_dir = _repo_root() / "packages" / package / "tests"
    path = str(tests_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    return __import__(module)


def agent_run_request() -> AgentRunRequest:
    schema_digest = canonical_digest(RESULT_SCHEMA)
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=schema_digest,
            extraction_mode="structured",
        ),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest=_SHA,
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        request_policy_digest=_SHA,
        request_config_digest=_SHA,
    )


@dataclass(frozen=True, slots=True)
class SimpleBinding:
    target_capability_id: str
    data: object
    resource_ids: tuple[str, ...] = ()


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
    parsed_workflow = WorkflowDef.model_validate(
        {
            "name": "adapter-conformance",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 30}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "run",
                    "nodes": {
                        "run": {
                            "kind": "task",
                            "capability": capability_id,
                            "retry": "once",
                            "timeout": "short",
                            "input": agent_run.model_dump(mode="json"),
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "run", "to": "end"}],
                }
            },
        }
    )
    identity = uuid.uuid4().hex
    distribution_name = f"agent-runtime-conformance-{identity}"
    package_name = f"agent_runtime_conformance_{identity}"
    entrypoint_name = f"conformance-{identity}"
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
        product_id="test.conformance",
        product_version="1.0.0",
        engine_api=ENGINE_API_VERSION,
        plugins=(PluginRequirement(plugin_id=plugin_id, version_specifier="==1.0.0"),),
        entrypoints=dict(parsed_workflow.entrypoints),
        configuration={},
        workflow=parsed_workflow,
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
    import builtins

    callbacks: dict[str, RecoverableTaskHandler] = getattr(builtins, _HANDLER_REGISTRY, {})
    callbacks[entrypoint_name] = handler
    setattr(builtins, _HANDLER_REGISTRY, callbacks)
    manifest_json = json.dumps(manifest_document, sort_keys=True)
    descriptor_json = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True)
    (package_root / "provider.py").write_text(
        "import builtins\n"
        "import json\n"
        "from graph_engine.composition import ProductManifest\n"
        "from graph_engine.plugin_api import PluginContribution, PluginDescriptor\n"
        f"_delegate = getattr(builtins, {_HANDLER_REGISTRY!r})[{entrypoint_name!r}]\n"
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
        self._secrets = dict(secrets)
        self._binding_data = binding_data
        self.secret_port = RecordingSecretPort(self._secrets)
        self.last_request_bytes = b""
        self.last_context_fields: tuple[str, ...] = ()
        self.last_reconcile: TaskActivityReconcileResult | None = None
        self.last_cancel: TaskActivityCancelResult | None = None
        self.cut: str | None = None
        self.host_calls: list[str] = []

    def bind_ledger(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: TaskWorkspaceStore,
        receipts: TerminalReceiptStore | None = None,
    ) -> None:
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    def _context(self, call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall) -> TaskContext:
        assert self._store is not None
        identity = call.attempt_root.workspace_identity
        binding = self._store.begin(
            task_id=identity.task_id,
            attempt=identity.attempt,
            output_paths=identity.output_paths,
        )
        port = None
        if call.activity_rpc.activity_id is not None and self._ledger is not None:
            port = LedgerTaskActivityPort(ledger=self._ledger, identity=call.activity_rpc)
        context = TaskContext(
            project_root=binding.project_root,
            write_root=binding.write_root,
            workspace_identity=binding.identity,
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
        identity: TaskHostCallIdentity,
        activity: TaskActivitySnapshot,
        outcome: TaskOutcome,
    ) -> None:
        if self._receipts is None or identity.activity_id is None:
            return
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
                request_digest=activity.request_digest,
                workspace_identity_digest=activity.workspace_identity.identity_digest,
                project_root_digest=activity.workspace_identity.project_digest,
                write_root_digest=activity.workspace_identity.write_root_digest,
                baseline_digest=activity.workspace_identity.identity_digest,
                staged_write_set_digest=activity.staged_write_set_digest or ("d" * 64),
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
        outcome = await handler.execute(call.request, context)
        activity = context.activity.snapshot if context.activity is not None else None
        if activity is not None:
            self._install_receipt(call.identity, activity, outcome)
        if self.cut == "after_terminal_receipt":
            raise RuntimeError("after_terminal_receipt")
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        self.host_calls.append("reconcile")
        handler = self._require_recoverable(call.request.capability_id)
        context = self._context(call)
        result = await handler.reconcile(call.request, context, call.activity)
        self.last_reconcile = result
        if result.status == "terminal" and result.outcome is not None and context.activity is not None:
            self._install_receipt(call.identity, context.activity.snapshot, result.outcome)
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
    engine: Engine
    handle: InvocationHandle
    composition: FrozenComposition
    host: ConfinedTestHost
    scheduler: Scheduler
    ledger: Ledger
    store: TaskWorkspaceStore
    task: PlannedTask
    receipts: TerminalReceiptStore
    provider: Any
    workspace_cm: TaskWorkspaceStore
    agent_run: AgentRunRequest
    roots: tuple[Path, ...]

    def close(self) -> None:
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            closer()
        closer = getattr(self.workspace_cm, "__exit__", None)
        if callable(closer):
            closer(None, None, None)
        self.handle.close()
        self.engine.close()


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

    def _unwrap_task(self, task: PlannedTask) -> PlannedTask:
        payload = thaw_json(task.input)
        if isinstance(payload, dict) and "config" in payload:
            payload = payload["config"]
        return task.model_copy(update={"input": payload})

    def _registry(self, composition: FrozenComposition) -> DirectRegistry:
        return DirectRegistry(
            composition.registries.capabilities.task_handlers,
            bindings={
                self.capability_id: SimpleBinding(
                    target_capability_id=self.capability_id,
                    data={"result_schema": RESULT_SCHEMA},
                )
            },
        )

    def _event_kinds(self, ledger: Ledger) -> tuple[str, ...]:
        return tuple(item.event.kind for item in ledger.read_all())

    def _activity(self, ledger: Ledger) -> Any:
        projection = fold_events(ledger.read_all())
        for activation in reversed(projection.activations):
            if activation.attempts and activation.attempts[-1].activity is not None:
                return activation.attempts[-1].activity
        return None

    def _attempt(self, ledger: Ledger) -> int | None:
        projection = fold_events(ledger.read_all())
        for activation in reversed(projection.activations):
            if activation.attempts:
                return activation.attempts[-1].attempt
        return None

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
        engine = Engine(engine_root, clock=SystemClock(), host=host)
        project_root = engine_root.parent / f".{engine_root.name}-project"
        attempts_root = engine_root.parent / f".{engine_root.name}-attempts"
        receipts_root = engine_root.parent / f".{engine_root.name}-receipts"
        for path in (project_root, attempts_root, receipts_root):
            path.mkdir(exist_ok=True)
        workspace_binding = InvocationWorkspaceBinding(
            project_root=project_root,
            attempts_root=attempts_root,
            receipts_root=receipts_root,
        )
        handle = engine.start(
            composition,
            entrypoint="main",
            invocation_id=invocation_id,
            seed=empty_invocation_seed(),
            authorization=empty_runtime_authorization(),
            workspace_binding=workspace_binding,
        )
        ledger = Ledger(handle.invocation_root / "ledger")
        host.bind_ledger(ledger)
        envelopes = ledger.read_all()
        plan = plan_next(composition.workflow, fold_events(envelopes))
        if plan.events:
            ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
        envelopes = ledger.read_all()
        plan = plan_next(composition.workflow, fold_events(envelopes))
        assert plan.tasks
        task = self._unwrap_task(plan.tasks[0])
        receipts = TerminalReceiptStore.open_or_create(handle.invocation_root / "receipts")
        workspace_cm = handle.workspace
        store = workspace_cm
        owner_id = engine_digest(
            {
                "invocation_id": invocation_id,
                "lock_digest": composition.lock_digest,
                "role": "engine-scheduler",
            }
        )
        scheduler = Scheduler(
            self._registry(composition),
            store,
            ledger,
            host,
            owner_id=owner_id,
            clock=SystemClock(),
            lease_seconds=120.0,
            lock_digest=composition.lock_digest,
            composition_digest=composition.digest,
            entrypoint="main",
            receipts=receipts,
        )
        scenario = _Scenario(
            engine=engine,
            handle=handle,
            composition=composition,
            host=host,
            scheduler=scheduler,
            ledger=ledger,
            store=store,
            task=task,
            receipts=receipts,
            provider=provider,
            workspace_cm=workspace_cm,
            agent_run=self._agent_run,
            roots=(engine_root, handle.invocation_root),
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
            OpenCodeDispatchIncomplete
            | CursorDispatchIncomplete
            | TimeoutError
            | asyncio.TimeoutError
            | httpx.TransportError,
        ):
            return True
        return error.__class__.__name__ == "ProcessDispatchCut"

    async def _drive_wave(
        self, scenario: _Scenario, *, allow_incomplete: bool = False
    ) -> AttemptResult | None:
        self._drives += 1
        try:
            results = await scenario.scheduler.run_wave((scenario.task,))
        except Exception as error:
            if allow_incomplete and self._is_incomplete(error):
                return None
            raise
        return results[0] if results else None

    async def _drive_recover(self, scenario: _Scenario) -> Any:
        self._drives += 1
        return await scenario.scheduler.recover_activity(scenario.task)

    def _drain_planner(self, scenario: _Scenario) -> None:
        while True:
            envelopes = scenario.ledger.read_all()
            projection = fold_events(envelopes)
            if projection.status in {"succeeded", "failed", "stopped"}:
                return
            plan = plan_next(scenario.composition.workflow, projection)
            if plan.events:
                scenario.ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
                continue
            raise AssertionError(
                f"planner stalled after recoverable success: status={projection.status!r} tasks={len(plan.tasks)}"
            )

    async def _complete_success(self, scenario: _Scenario) -> None:
        await self._drive_wave(scenario, allow_incomplete=False)
        self._drain_planner(scenario)
        projection = fold_events(scenario.ledger.read_all())
        if projection.status != "succeeded":
            raise AssertionError(f"projection status is {projection.status!r}, expected succeeded")
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
    ) -> CutResult:
        activity = self._activity(scenario.ledger)
        projection = fold_events(scenario.ledger.read_all())
        outcome_status: str | None = None
        if projection.status in {"succeeded", "failed", "stopped"}:
            outcome_status = projection.status
        elif activity is not None and activity.terminal is not None:
            outcome_status = activity.terminal.status
        receipts = ()
        if activity is not None:
            identity = scenario.scheduler._host_call_identity(  # noqa: SLF001
                scenario.task, activity.activity_id, "execute"
            )
            receipts = scenario.host.read_terminal_receipts(identity)
        host_cancel = None if scenario.host.last_cancel is None else scenario.host.last_cancel.status
        host_reconcile = None if scenario.host.last_reconcile is None else scenario.host.last_reconcile.status
        return CutResult(
            cut=cut,
            event_kinds=self._event_kinds(scenario.ledger),
            activity_state=None if activity is None else activity.state,
            reconcile_status=reconcile_status or host_reconcile,  # type: ignore[arg-type]
            cancel_status=cancel_status or host_cancel,
            outcome_status=outcome_status,
            attempt=self._attempt(scenario.ledger),
            workspace_identity=None if activity is None else activity.workspace_identity,
            provider_calls=self._count_on_provider(scenario.provider, "dispatch"),
            receipt_count=len(receipts) if receipt_count is None else receipt_count,
            instruction_bytes=scenario.host.last_request_bytes,
            host_calls=tuple(scenario.host.host_calls),
            scheduler_drives=self._drives,
            context_exposed=scenario.host.last_context_fields,
        )

    async def prepared_fixture(self) -> PreparedAdapterFixture:
        scenario = await self._fresh_prepared()
        kinds = self._event_kinds(scenario.ledger)
        activity = self._activity(scenario.ledger)
        assert activity is not None
        payload = thaw_json(scenario.task.input)
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
        for path in scenario.handle.invocation_root.rglob("*"):
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
        checkpoint = scenario.handle.invocation_root / "checkpoint.json"
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
            await scenario.scheduler.cancel_activity(scenario.task, reason="conformance-cancel")
            return self._cut_result(scenario, cut)
        if cut == "after_bind":
            scenario = await self._open_bind_scenario()
            await self._drive_wave(scenario, allow_incomplete=True)
            await self._drive_recover(scenario)
            return self._cut_result(scenario, cut)
        if cut == "after_terminal_receipt":
            scenario = await self._open_receipt_scenario()
            try:
                await self._drive_wave(scenario, allow_incomplete=False)
            except RuntimeError as error:
                if str(error) != "after_terminal_receipt":
                    raise
            activity = self._activity(scenario.ledger)
            receipt_count = 0
            if activity is not None:
                identity = scenario.scheduler._host_call_identity(  # noqa: SLF001
                    scenario.task, activity.activity_id, "execute"
                )
                receipt_count = len(scenario.host.read_terminal_receipts(identity))
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
        config = OpenCodeAdapterConfig.model_validate(
            {
                "schema_version": "1",
                "endpoint": fake.base_url,
                "tls_identity_digest": _SHA,
                "secret_handle": self.secret_handle,
                "protocol_profile": "opencode-http-v1",
                "project_scope": fake.project_scope,
                "request_timeout_seconds": 5,
                "observation_horizon_seconds": observation_horizon,
                "max_response_bytes": 65536,
            }
        )
        return fake, OpenCodeHandler(config)

    async def _fresh_prepared(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        scenario = await self._open_scenario(
            invocation_id="opencode-prepared",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )
        scenario.scheduler.start_recoverable(scenario.task)
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


class CursorRuntimeHarness(_AdapterHarness):
    recovery_profile: RecoveryProfile = "confined_process"
    plugin_id = "runtime.cursor"
    capability_id = "runtime.cursor.execute"
    secret_handle = "cursor.api-key"

    def _cursor_bin(self, root: Path) -> tuple[str, str]:
        path = (root / "cursor").resolve()
        path.write_bytes(b"cursor-binary")
        path.chmod(0o755)
        return str(path), hashlib.sha256(path.read_bytes()).hexdigest()

    def _fake_and_handler(
        self, *, cut: str | None = None, status: str = "exited"
    ) -> tuple[Any, CursorHandler]:
        fake_mod = _load_adapter_test_module("agent-runtime-cursor", "fake_process_host")
        root = self._temp()
        executable, digest = self._cursor_bin(root)
        host = fake_mod.FakeConfinedProcessHost(
            cut=cut,
            status=status,
            exit_code=None if status == "running" else 0,
        )
        config = CursorAdapterConfig.model_validate(
            {
                "schema_version": "1",
                "executable": executable,
                "executable_digest": digest,
                "expected_version": "1.0.0",
                "secret_handle": self.secret_handle,
                "environment_names": ["PATH", "CURSOR_API_KEY"],
                "graceful_cancel_seconds": 5,
                "forced_cancel_seconds": 10,
                "max_output_bytes": 65536,
                "max_line_bytes": 4096,
            }
        )
        return host, CursorHandler(config, host)

    async def _fresh_prepared(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        scenario = await self._open_scenario(
            invocation_id="cursor-prepared",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )
        scenario.scheduler.start_recoverable(scenario.task)
        return scenario

    async def _open_bind_scenario(self) -> _Scenario:
        fake, handler = self._fake_and_handler(cut="after_bind", status="running")
        return await self._open_scenario(
            invocation_id="cursor-bind",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )

    async def _open_success_scenario(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        return await self._open_scenario(
            invocation_id="cursor-success",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )

    async def _open_receipt_scenario(self) -> _Scenario:
        fake, handler = self._fake_and_handler()
        return await self._open_scenario(
            invocation_id="cursor-receipt",
            handler=handler,
            provider=fake,
            host_cut="after_terminal_receipt",
            secrets={self.secret_handle: CANARY},
        )

    async def recover_live_activity(self) -> RecoveryOutcome:
        raise AssertionError("Cursor recovery_profile does not claim durable live adoption")

    async def recover_from_dead_host(self) -> RecoveryOutcome:
        fake, handler = self._fake_and_handler(cut="after_bind", status="running")
        scenario = await self._open_scenario(
            invocation_id="cursor-dead",
            handler=handler,
            provider=fake,
            secrets={self.secret_handle: CANARY},
        )
        await self._drive_wave(scenario, allow_incomplete=True)
        fake.alive = False
        result = await self._drive_recover(scenario)
        status = result.reconcile_status or "indeterminate"
        return RecoveryOutcome(status=status, attempt=result.attempt, reason=None)
