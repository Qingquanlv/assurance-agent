from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import copy
from types import MappingProxyType
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, ValidationError

from agent_runtime_contracts import (
    AgentExecutionContract,
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeBinding,
    AgentRuntimePolicy,
    RawAgentRuntimeBindingProjectionV1,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
)
from agent_runtime_contracts.executor.phases import phase_task_id
from agent_runtime_contracts.ops import AgentOpFinalizeInputV1
from graph_engine.attempts import (
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    PermanentTaskFailure,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostExecuteCall,
    current_bound_identity,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import CapabilityBindingEntry, FrozenComposition, TaskHandlerEntry
from graph_engine.frozen_json import thaw_json as thaw_frozen
from graph_engine.plugin_api import (
    InvocationMetadata,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from assurance_product.agent_contracts import (
    all_feature_agent_contracts,
    all_feature_task_contracts,
)
from assurance_product.models import (
    ADAPTER_BINDING_RESOURCE_ID,
    CONFIGURATION_PLUGIN_ID,
    PLUGIN_ID,
    OpenCodeBindingV1,
)

_RUNTIME_HANDLER_ID = "runtime.opencode.execute"
_PROVIDER = "opencode"
_ACTIVITY_RECOVERY = "adopt-observe-reconcile-v1"
_CAPABILITY_CATALOG_RESOURCE_ID = "assurance.product.configuration.capability-catalog"
# Each prepared JSON instruction is its own document; its version tag is not a business field.
_UNMERGED_PREPARED_FIELDS = frozenset({"agent_result", "prepare", "schema_version"})
_FinalOutputT = TypeVar("_FinalOutputT", bound=BaseModel)


def _contract_digest(contract: AgentExecutionContract[Any, Any, Any]) -> str:
    return canonical_digest(cast(JSONValue, contract.canonical_projection()))


def _contract_owner(contract_id: str) -> str:
    body = contract_id.removeprefix("assurance.").removesuffix(".v1")
    feature, marker, _base = body.partition(".agent.")
    if marker != ".agent." or not feature:
        raise ValueError(f"wrong-owner: invalid agent contract id {contract_id!r}")
    return f"assurance.{feature}"


def _runtime_handler_owner(runtime_handler_id: str) -> str:
    owner, separator, leaf = runtime_handler_id.rpartition(".")
    if separator != "." or not owner or not leaf:
        raise ValueError(f"invalid runtime handler id: {runtime_handler_id!r}")
    return owner


def project_raw_agent_runtime_binding(
    binding: AgentRuntimeBinding,
    contract: AgentExecutionContract[Any, Any, Any],
) -> RawAgentRuntimeBindingProjectionV1:
    return RawAgentRuntimeBindingProjectionV1(
        schema_version="raw-agent-runtime-binding-v1",
        contract_id=binding.contract_id,
        contract_digest=_contract_digest(contract),
        runtime_handler_id=binding.runtime_handler_id,
        adapter=_PROVIDER,
        provider=binding.provider,
        model=binding.model,
        policy=binding.policy,
        secret_handles=binding.secret_handles,
        activity_recovery=_ACTIVITY_RECOVERY,
    )


def authenticate_raw_agent_runtime_bindings(
    rows: Sequence[RawAgentRuntimeBindingProjectionV1],
    contracts: Mapping[str, AgentExecutionContract[Any, Any, Any]],
    *,
    adapter: str = _PROVIDER,
) -> tuple[RawAgentRuntimeBindingProjectionV1, ...]:
    seen: dict[str, RawAgentRuntimeBindingProjectionV1] = {}
    for row in rows:
        if row.contract_id in seen:
            raise ValueError(f"duplicate raw agent runtime binding: {row.contract_id}")
        seen[row.contract_id] = row
    extra = sorted(set(seen) - set(contracts))
    if extra:
        raise ValueError(f"extra raw agent runtime binding: {extra}")
    missing = sorted(set(contracts) - set(seen))
    if missing:
        raise ValueError(f"missing raw agent runtime binding: {missing}")
    for contract_id, row in seen.items():
        contract = contracts[contract_id]
        if row.adapter != adapter or row.provider != adapter:
            raise ValueError(f"wrong-adapter: {contract_id}")
        if (
            _runtime_handler_owner(row.runtime_handler_id) != f"runtime.{adapter}"
            or contract.owner_id != _contract_owner(contract_id)
            or row.runtime_handler_id != f"runtime.{adapter}.execute"
        ):
            raise ValueError(f"wrong-owner: {contract_id}")
        if row.contract_digest != _contract_digest(contract):
            raise ValueError(f"contract-digest-drift: {contract_id}")
        if row.schema_version != "raw-agent-runtime-binding-v1":
            raise ValueError(f"raw-agent-runtime-binding schema_version drifted: {contract_id}")
        if row.activity_recovery != _ACTIVITY_RECOVERY:
            raise ValueError(f"activity recovery drifted: {contract_id}")
        if not row.model or row.model != row.model.strip():
            raise ValueError(f"missing authenticated model: {contract_id}")
    return tuple(seen[contract_id] for contract_id in sorted(contracts))


def _capability_bindings(composition: FrozenComposition) -> dict[str, CapabilityBindingEntry]:
    return {
        key: value
        for key, value in composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }


def _binding_from_entry(
    entry: CapabilityBindingEntry,
    contract: AgentExecutionContract[Any, Any, Any],
) -> AgentRuntimeBinding:
    data = thaw_frozen(entry.data)
    if not isinstance(data, Mapping):
        raise ValueError(f"binding data is not a mapping: {entry.capability_id}")
    execution = data.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError(f"binding execution is not a mapping: {entry.capability_id}")
    model = execution.get("provider_model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"missing authenticated model: {entry.capability_id}")
    resource_ids = list(entry.resource_ids)
    if len(resource_ids) != 2:
        raise ValueError(f"binding resource ids drifted: {entry.capability_id}")
    permission_id, request_policy_id = resource_ids[0], resource_ids[1]
    if entry.contract_id != contract.contract_id:
        raise ValueError(f"wrong-owner: {entry.capability_id}")
    if entry.target_capability_id != _RUNTIME_HANDLER_ID:
        raise ValueError(f"wrong-adapter: {entry.capability_id}")
    return AgentRuntimeBinding(
        contract_id=contract.contract_id,
        runtime_handler_id=entry.target_capability_id,
        provider=_PROVIDER,
        model=model,
        policy=AgentRuntimePolicy(
            request_policy_handle=request_policy_id,
            request_config_handle=permission_id,
        ),
        secret_handles=entry.secret_handles,
    )


def _require_opencode_composition(composition: FrozenComposition) -> None:
    source = composition.manifest.source
    if source is None or source.entrypoint_name != "assurance-opencode":
        raise ValueError("composition is not the OpenCode product")


def runtime_bindings_from_composition(
    composition: FrozenComposition,
) -> Mapping[str, AgentRuntimeBinding]:
    _require_opencode_composition(composition)
    contracts = all_feature_agent_contracts()
    entries = _capability_bindings(composition)
    extra = sorted(set(entries) - set(contracts))
    if extra:
        raise ValueError(f"extra raw agent runtime binding: {extra}")
    missing = sorted(set(contracts) - set(entries))
    if missing:
        raise ValueError(f"missing raw agent runtime binding: {missing}")
    resolved = {
        contract_id: _binding_from_entry(entries[contract_id], contract)
        for contract_id, contract in contracts.items()
    }
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(binding, contracts[contract_id])
            for contract_id, binding in sorted(resolved.items())
        ),
        contracts,
        adapter=_PROVIDER,
    )
    if set(resolved) != set(contracts):
        raise ValueError("composition runtime binding set is not the exact Agent contract catalog")
    return MappingProxyType(resolved)


def _adapter_binding_from_composition(composition: FrozenComposition) -> OpenCodeBindingV1:
    entry = composition.registries.resources.entries.get(ADAPTER_BINDING_RESOURCE_ID)
    if entry is None:
        raise ValueError("authenticated OpenCode adapter binding is missing")
    if entry.owner_id != PLUGIN_ID or entry.media_type != "application/json":
        raise ValueError("authenticated OpenCode adapter binding identity drifted")
    return OpenCodeBindingV1.model_validate_json(entry.content)


def _attempt_validation_context(composition: FrozenComposition) -> Mapping[str, object]:
    entry = composition.registries.resources.entries.get(_CAPABILITY_CATALOG_RESOURCE_ID)
    if entry is None:
        raise ValueError("authenticated capability catalog is missing")
    if entry.owner_id != CONFIGURATION_PLUGIN_ID or entry.media_type != "application/json":
        raise ValueError("authenticated capability catalog identity drifted")
    try:
        document = json.loads(entry.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("authenticated capability catalog is not canonical JSON") from error
    if not isinstance(document, Mapping) or set(document) != {"schema_version", "typed_leafs"}:
        raise ValueError("authenticated capability catalog shape drifted")
    if document.get("schema_version") != "1":
        raise ValueError("authenticated capability catalog schema_version drifted")
    leafs = document.get("typed_leafs")
    if not isinstance(leafs, list) or any(
        not isinstance(item, str) or not item or item != item.strip() or "." not in item for item in leafs
    ):
        raise ValueError("authenticated capability catalog typed_leafs are invalid")
    if leafs != sorted(set(leafs)):
        raise ValueError("authenticated capability catalog typed_leafs are not canonical")
    return MappingProxyType({"capability_leafs": frozenset(leafs)})


def raw_agent_runtime_binding_rows(
    composition: FrozenComposition,
) -> tuple[RawAgentRuntimeBindingProjectionV1, ...]:
    contracts = all_feature_agent_contracts()
    bindings = runtime_bindings_from_composition(composition)
    return authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(bindings[contract_id], contracts[contract_id])
            for contract_id in sorted(contracts)
        ),
        contracts,
        adapter=_PROVIDER,
    )


def _authenticate_runtime_binding(
    composition: FrozenComposition,
    binding: AgentRuntimeBinding,
) -> None:
    from assurance_product.product import AssuranceCompositionError

    owner = _runtime_handler_owner(binding.runtime_handler_id)
    closure = set(composition.manifest.required_plugin_ids)
    if owner not in closure:
        raise AssuranceCompositionError(
            f"runtime handler owner is outside authenticated Product dependency closure: {owner}"
        )
    handlers = composition.registries.capabilities.task_handlers
    if binding.runtime_handler_id not in handlers:
        raise AssuranceCompositionError(
            f"runtime handler is outside authenticated Product dependency closure: "
            f"{binding.runtime_handler_id}"
        )


def _task_context(scope: AuthorizedAttemptScope) -> TaskContext:
    digest = scope.workspace.identity.identity_digest
    invocation = InvocationMetadata(
        invocation_id=scope.execution.invocation_id,
        lock_digest=digest,
        composition_digest=digest,
        entrypoint=scope.execution.public_entrypoint,
    )
    return TaskContext(
        project_root=scope.workspace.project_root,
        write_root=scope.workspace.write_root,
        workspace_identity=scope.workspace.identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=invocation,
    )


def _task_request(
    *,
    capability_id: str,
    payload: Mapping[str, object] | None,
    scope: AuthorizedAttemptScope,
    binding_data: object = None,
    task_id: str,
    lock_digest: str | None = None,
) -> TaskRequest:
    digest = lock_digest or scope.workspace.identity.identity_digest
    invocation = InvocationMetadata(
        invocation_id=scope.execution.invocation_id,
        lock_digest=digest,
        composition_digest=digest,
        entrypoint=scope.execution.public_entrypoint,
    )
    return TaskRequest(
        invocation_id=scope.execution.invocation_id,
        task_id=task_id,
        graph_instance_id=scope.execution.invocation_id,
        node_id=scope.execution.semantic_node_id,
        capability_id=capability_id,
        binding_data=cast(Any, binding_data),
        invocation=invocation,
        attempt=1,
        input=cast(JSONValue, payload),
    )


def _attempt_root(scope: AuthorizedAttemptScope) -> AttemptRootDescriptor:
    workspace = scope.workspace
    return AttemptRootDescriptor(
        workspace_identity=workspace.identity,
        project_root_identity=workspace.project_root_identity,
        write_root_identity=workspace.write_root_identity,
        project_root_digest=workspace.identity.project_digest,
        write_root_digest=workspace.identity.write_root_digest,
        baseline_digest=canonical_digest(
            [item.model_dump(mode="json") for item in workspace.identity.baseline_files]
        ),
    )


class _HostBackedInstalledPhase:
    def __init__(
        self,
        handler_id: str,
        handler: object,
        *,
        phase: str,
        callable_path: str | None = None,
        secret_handles: tuple[str, ...] = (),
        timeout_seconds: float = 30.0,
    ) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._phase = phase
        self._callable_path = callable_path or (
            f"{type(handler).__module__}:{type(handler).__qualname__}.execute"
        )
        self._secret_handles = tuple(sorted(secret_handles))
        self._timeout_seconds = timeout_seconds
        self._host: object | None = None
        self._graph_revision = ""
        self._product_lock_digest = ""

    def with_host(
        self,
        host: object,
        *,
        graph_revision: str,
        product_lock_digest: str,
    ) -> _HostBackedInstalledPhase:
        bound = copy(self)
        bound._host = host
        bound._graph_revision = graph_revision
        bound._product_lock_digest = product_lock_digest
        return bound

    @property
    def lock_digest(self) -> str | None:
        return self._product_lock_digest or None

    async def _invoke(self, request: TaskRequest, scope: AuthorizedAttemptScope) -> TaskOutcome:
        if self._host is None:
            return await self._handler.execute(request, _task_context(scope))  # type: ignore[attr-defined]
        authorization_id = scope.execution.authorization_id
        if authorization_id is None:
            raise ValueError("host-backed phase requires an authorized Attempt")
        request_digest = canonical_digest(cast(JSONValue, request.model_dump(mode="json")))
        activity_id = scope.execution.attempt_key.digest if self._phase == "runtime" else None
        bound = current_bound_identity(
            attempt_key_digest=scope.execution.attempt_key.digest,
            authorization_id=authorization_id,
            workspace_identity_digest=scope.workspace.identity.identity_digest,
            request_digest=request_digest,
            graph_revision=self._graph_revision,
            product_lock_digest=self._product_lock_digest,
            handler_id=self.handler_id,
            fencing_token=scope.execution.fencing_token,
            phase=cast(Any, self._phase),
        )
        identity = TaskHostCallIdentity(
            invocation_id=scope.execution.invocation_id,
            task_id=request.task_id,
            activation_id=scope.execution.semantic_node_id,
            attempt=scope.workspace.identity.attempt,
            activity_id=activity_id,
            operation="execute",
            **bound,  # type: ignore[arg-type]
        )
        call = TaskHostExecuteCall(
            identity=identity,
            capability_id=self.handler_id,
            capability_entrypoint=self._callable_path,
            request=request,
            attempt_root=_attempt_root(scope),
            activity_rpc=TaskActivityRpcIdentity(
                invocation_id=identity.invocation_id,
                task_id=identity.task_id,
                activation_id=identity.activation_id,
                attempt=identity.attempt,
                activity_id=identity.activity_id,
                **bound,  # type: ignore[arg-type]
            ),
            authorized_secret_handles=(self._secret_handles if self._phase == "runtime" else ()),
            timeout_seconds=self._timeout_seconds,
        )
        result = await self._host.execute(call)  # type: ignore[attr-defined]
        if result.operation != "execute" or result.outcome is None:
            raise ValueError("task execution host returned an invalid execute result")
        return result.outcome


def _outcome_failure(outcome: TaskOutcome) -> PermanentTaskFailure | None:
    if outcome.failure is None:
        return None
    kind = outcome.failure.kind
    if kind not in {
        "transient",
        "timeout",
        "invalid_input",
        "invalid_output",
        "external_effect",
        "internal",
        "configuration",
    }:
        kind = "internal"
    return PermanentTaskFailure(
        kind=kind,
        message=outcome.failure.message,
        retryable=outcome.failure.retryable,
    )


class InstalledPreparePhase(_HostBackedInstalledPhase):
    def __init__(
        self,
        handler_id: str,
        handler: object,
        binding_data: object,
        *,
        callable_path: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            handler_id,
            handler,
            phase="prepare",
            callable_path=callable_path,
            timeout_seconds=timeout_seconds,
        )
        self._binding_data = binding_data

    async def execute(
        self,
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope,
    ) -> AgentRunRequest | PermanentTaskFailure:
        request = _task_request(
            capability_id=self.handler_id,
            payload=validated_input.model_dump(mode="json"),
            scope=scope,
            binding_data=self._binding_data,
            task_id=phase_task_id(scope.execution.attempt_key, "prepare", self.handler_id),
            lock_digest=self.lock_digest,
        )
        outcome = await self._invoke(request, scope)
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        return AgentRunRequest.model_validate(outcome.output)


class InstalledRuntimePhase(_HostBackedInstalledPhase):
    def __init__(
        self,
        handler_id: str,
        handler: object,
        binding_data: object,
        *,
        callable_path: str | None = None,
        secret_handles: tuple[str, ...] = (),
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            handler_id,
            handler,
            phase="runtime",
            callable_path=callable_path,
            secret_handles=secret_handles,
            timeout_seconds=timeout_seconds,
        )
        self._binding_data = binding_data

    async def execute(
        self,
        prepared: AgentRunRequest,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        request = _task_request(
            capability_id=self.handler_id,
            payload=prepared.model_dump(mode="json"),
            scope=scope,
            binding_data=self._binding_data,
            task_id=phase_task_id(scope.execution.attempt_key, "runtime", self.handler_id),
            lock_digest=self.lock_digest,
        )
        outcome = await self._invoke(request, scope)
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        run_result = AgentRunResult.model_validate(outcome.output)
        return RawAgentRuntimeOutcome(
            run_result=run_result,
            raw_workspace=ReadOnlyRawWorkspace(
                scope.workspace.write_root,
                identity_digest=scope.workspace.identity.identity_digest,
            ),
        )


class InstalledFinalizePhase(_HostBackedInstalledPhase, Generic[_FinalOutputT]):
    def __init__(
        self,
        handler_id: str,
        handler: object,
        output_model: type[_FinalOutputT],
        *,
        callable_path: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            handler_id,
            handler,
            phase="finalize",
            callable_path=callable_path,
            timeout_seconds=timeout_seconds,
        )
        del output_model

    async def execute(
        self,
        bundle: RawFinalizeBundle[Any, Any, Any],
        scope: AuthorizedAttemptScope,
    ) -> _FinalOutputT | PermanentTaskFailure:
        input_model = getattr(self._handler, "input_model", None)
        if not isinstance(input_model, type) or not issubclass(input_model, BaseModel):
            return PermanentTaskFailure(
                kind="configuration",
                message=f"finalize handler does not declare an input model: {self.handler_id}",
            )
        locked_input = bundle.validated_input.model_dump(mode="json")
        prepared_business: dict[str, object] = {}
        whole_business = issubclass(input_model, AgentOpFinalizeInputV1)
        if isinstance(bundle.prepared, AgentRunRequest):
            for instruction in bundle.prepared.instructions:
                raw = thaw_frozen(instruction.json_content)
                if not isinstance(raw, Mapping):
                    continue
                for name in tuple(raw) if whole_business else tuple(input_model.model_fields):
                    if name in _UNMERGED_PREPARED_FIELDS or name not in raw:
                        continue
                    value = raw[name]
                    if name in prepared_business and prepared_business[name] != value:
                        return PermanentTaskFailure(
                            kind="invalid_input",
                            message=f"prepared business field is ambiguous: {name}",
                        )
                    prepared_business[name] = value
        projected = {
            name: value
            for name, value in locked_input.items()
            if name in input_model.model_fields and name != "agent_result"
        }
        if not whole_business:
            projected.update(prepared_business)
        projected["agent_result"] = bundle.run_evidence.model_dump(mode="json")
        if "prepare" in input_model.model_fields:
            projected["prepare"] = {**locked_input, **prepared_business}
        try:
            payload = input_model.model_validate(projected).model_dump(mode="json")
        except ValidationError as error:
            return PermanentTaskFailure(kind="invalid_input", message=str(error))
        request = _task_request(
            capability_id=self.handler_id,
            payload=payload,
            scope=scope,
            task_id=phase_task_id(scope.execution.attempt_key, "finalize", self.handler_id),
            lock_digest=self.lock_digest,
        )
        outcome = await self._invoke(request, scope)
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        return cast(_FinalOutputT, outcome.output)


class DeterministicTaskExecutor:
    def __init__(self, handler_id: str, handler: object, output_model: type[BaseModel]) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._output_model = output_model
        self.dispatch_count = 0

    async def execute(
        self,
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[Any] | PermanentTaskFailure:
        self.dispatch_count += 1
        request = _task_request(
            capability_id=self.handler_id,
            payload=validated_input.model_dump(mode="json", exclude_computed_fields=True),
            scope=scope,
            task_id=scope.execution.attempt_key.digest,
        )
        outcome = await self._handler.execute(request, _task_context(scope))  # type: ignore[attr-defined]
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        return ExecutedAttemptResult(
            output=self._output_model.model_validate(outcome.output),
            effects=tuple(outcome.effects),
        )


def _installed_handler(
    composition: FrozenComposition,
    handler_id: str,
) -> tuple[object, str]:
    entry = composition.registries.capabilities.entries.get(handler_id)
    if not isinstance(entry, TaskHandlerEntry):
        raise ValueError(f"installed task handler entry is missing: {handler_id}")
    return entry.handler, entry.provenance.callable_path


def _binding_timeout(binding_data: object) -> float:
    if not isinstance(binding_data, Mapping):
        raise ValueError("binding data is not a mapping")
    execution = binding_data.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("binding execution is not a mapping")
    limits = execution.get("limits")
    if not isinstance(limits, Mapping):
        raise ValueError("binding execution limits are not a mapping")
    value = limits.get("max_seconds")
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError("binding execution max_seconds is invalid")
    timeout = float(value)
    if timeout <= 0 or timeout > 3600:
        raise ValueError("binding execution max_seconds is outside the host limit")
    return timeout


def _resolve_agent_contract(
    contract: AgentExecutionContract[Any, Any, Any],
    binding: AgentRuntimeBinding,
    composition: FrozenComposition,
    adapter_binding: OpenCodeBindingV1,
    validation_context: Mapping[str, object],
) -> ResolvedAttemptContract[Any, Any]:
    entries = _capability_bindings(composition)
    binding_data = thaw_frozen(entries[contract.contract_id].data)
    timeout_seconds = _binding_timeout(binding_data)
    prepare_handler, prepare_callable = _installed_handler(composition, contract.prepare_handler_id)
    runtime_handler, runtime_callable = _installed_handler(composition, binding.runtime_handler_id)
    finalize_handler, finalize_callable = _installed_handler(composition, contract.finalize_handler_id)
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=InstalledPreparePhase(
            contract.prepare_handler_id,
            prepare_handler,
            binding_data,
            callable_path=prepare_callable,
            timeout_seconds=timeout_seconds,
        ),
        runtime=InstalledRuntimePhase(
            binding.runtime_handler_id,
            runtime_handler,
            adapter_binding.model_dump(mode="json"),
            callable_path=runtime_callable,
            secret_handles=binding.secret_handles,
            timeout_seconds=timeout_seconds,
        ),
        finalize=InstalledFinalizePhase(
            contract.finalize_handler_id,
            finalize_handler,
            contract.output_model,
            callable_path=finalize_callable,
            timeout_seconds=timeout_seconds,
        ),
        result_context=validation_context,
    )
    return resolve_contract(
        executor.to_task_contract(),
        executor=executor,
        validation_context=validation_context,
    )


def _resolve_task_contract(
    contract: TaskAttemptContract[Any, Any],
    composition: FrozenComposition,
    validation_context: Mapping[str, object],
) -> ResolvedAttemptContract[Any, Any]:
    handler = composition.registries.capabilities.task_handlers[contract.handler_id]
    return resolve_contract(
        contract,
        executor=DeterministicTaskExecutor(contract.handler_id, handler, contract.output_model),
        validation_context=validation_context,
    )


def boot_semantic_attempt_contracts(
    composition: FrozenComposition,
    bindings: Mapping[str, AgentRuntimeBinding] | None = None,
) -> Mapping[str, ResolvedAttemptContract[Any, Any]]:
    catalog = dict(runtime_bindings_from_composition(composition))
    if bindings is not None:
        catalog.update(bindings)
    for binding in catalog.values():
        _authenticate_runtime_binding(composition, binding)
    _require_opencode_composition(composition)
    adapter_binding = _adapter_binding_from_composition(composition)
    validation_context = _attempt_validation_context(composition)
    agents = all_feature_agent_contracts()
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(binding, agents[binding.contract_id])
            for binding in catalog.values()
        ),
        agents,
        adapter=_PROVIDER,
    )
    resolved: dict[str, ResolvedAttemptContract[Any, Any]] = {}
    for contract_id, binding in catalog.items():
        resolved[contract_id] = _resolve_agent_contract(
            agents[contract_id], binding, composition, adapter_binding, validation_context
        )
    for contract in all_feature_task_contracts().values():
        resolved[contract.contract_id] = _resolve_task_contract(
            contract,
            composition,
            validation_context,
        )
    if len(resolved) != 45:
        raise ValueError(f"semantic attempt registry must contain 45 contracts, got {len(resolved)}")
    return MappingProxyType(resolved)


__all__ = [
    "DeterministicTaskExecutor",
    "authenticate_raw_agent_runtime_bindings",
    "boot_semantic_attempt_contracts",
    "project_raw_agent_runtime_binding",
    "raw_agent_runtime_binding_rows",
    "runtime_bindings_from_composition",
]
