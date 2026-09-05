from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.fixture
def runtime_registry(opencode_composition):
    return opencode_composition.semantic_attempt_contracts


_PURE_FUNCTION_IDS = (
    "assurance.generation.complete",
    "assurance.generation.review-round.advance",
    "assurance.healing.repair-round.advance",
    "assurance.intake.review-round.advance",
)


def test_product_has_exactly_one_runtime_binding_per_agent_contract(opencode_composition) -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import runtime_bindings_from_composition

    contracts = all_feature_agent_contracts()
    composition = opencode_composition
    bindings = runtime_bindings_from_composition(composition)
    assert len(contracts) == 34
    assert set(bindings) == set(contracts)
    assert len(bindings) == 34
    assert all(
        binding.model != "fixture-model" or binding.provider == "opencode" for binding in bindings.values()
    )


def test_runtime_registry_contains_exact_semantic_contracts(runtime_registry) -> None:
    from assurance_product.agent_contracts import is_agent_contract

    # The two Intake plan Tasks enter the live registry when Task 11 wires their
    # semantic nodes into the public graphs.
    assert len(runtime_registry) == 48
    assert sum(is_agent_contract(item.contract) for item in runtime_registry.values()) == 34
    assert not any(type(item.executor).__name__.startswith("_Deferred") for item in runtime_registry.values())


def test_generation_runtime_binds_the_authenticated_capability_validation_context(
    opencode_composition,
) -> None:
    import json

    from agent_runtime_contracts import ResolvedRawAgentExecutor

    catalog = opencode_composition.registries.resources.entries[
        "assurance.product.configuration.capability-catalog"
    ]
    document = json.loads(catalog.content)
    expected = {"capability_leafs": frozenset(document["typed_leafs"])}
    resolved = opencode_composition.semantic_attempt_contracts["assurance.generation.agent.api.plan.v1"]

    assert resolved.validation_context == expected
    assert isinstance(resolved.executor, ResolvedRawAgentExecutor)
    assert resolved.executor._result_context == expected


def test_semantic_bindings_are_the_only_live_agent_ids(opencode_composition) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

    composition = opencode_composition
    assert len(composition.semantic_attempt_contracts) == 48
    assert len(AGENT_EXECUTION_CONTRACTS) == 34
    assert not any(item.startswith("assurance.product.agent.") for item in AGENT_EXECUTION_CONTRACTS)


@pytest.mark.parametrize(
    "extra",
    [
        {"validators": ()},
        {"resources": {"reads": ("qa",)}},
        {"output_model": "CaseDesignOutput"},
    ],
)
def test_product_runtime_binding_rejects_feature_authority(
    extra: dict[str, object], opencode_composition
) -> None:
    from agent_runtime_contracts import AgentRuntimeBinding

    from assurance_product.runtime_bindings import runtime_bindings_from_composition

    composition = opencode_composition
    payload = next(iter(runtime_bindings_from_composition(composition).values())).model_dump()
    with pytest.raises(ValidationError, match="extra"):
        AgentRuntimeBinding.model_validate({**payload, **extra})


def test_boot_rejects_runtime_handler_outside_product_closure(opencode_composition) -> None:
    from agent_runtime_contracts import AgentRuntimeBinding

    from assurance_product.product import AssuranceCompositionError
    from assurance_product.runtime_bindings import (
        boot_semantic_attempt_contracts,
        runtime_bindings_from_composition,
    )

    composition = opencode_composition
    catalog = next(iter(runtime_bindings_from_composition(composition).values()))
    foreign = AgentRuntimeBinding.model_validate(
        {
            **catalog.model_dump(),
            "runtime_handler_id": "runtime.unauthenticated.execute",
        }
    )
    with pytest.raises(AssuranceCompositionError, match="dependency closure"):
        boot_semantic_attempt_contracts(composition, {foreign.contract_id: foreign})


def test_semantic_registry_omits_pure_functions_and_keeps_validators_unbound(
    opencode_composition,
) -> None:
    from graph_engine.attempts import ResolvedAttemptContract

    composition = opencode_composition
    resolved = composition.semantic_attempt_contracts
    assert len(resolved) == 48
    assert all(isinstance(item, ResolvedAttemptContract) for item in resolved.values())
    assert all(item.contract.validators == () for item in resolved.values())
    assert all(pure_id not in resolved for pure_id in _PURE_FUNCTION_IDS)


def test_raw_runtime_rows_are_canonical_and_digest_locked(opencode_composition) -> None:
    from agent_runtime_contracts import RawAgentRuntimeBindingProjectionV1
    from graph_engine.canonical import canonical_digest

    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import raw_agent_runtime_binding_rows

    contracts = all_feature_agent_contracts()
    composition = opencode_composition
    rows = raw_agent_runtime_binding_rows(composition)
    assert len(rows) == 34
    assert tuple(row.contract_id for row in rows) == tuple(sorted(contracts))
    assert all(isinstance(row, RawAgentRuntimeBindingProjectionV1) for row in rows)
    assert all(row.schema_version == "raw-agent-runtime-binding-v1" for row in rows)
    assert all(row.activity_recovery == "adopt-observe-reconcile-v1" for row in rows)
    for row in rows:
        contract = contracts[row.contract_id]
        assert row.contract_digest == canonical_digest(contract.canonical_projection())  # type: ignore[arg-type]
        assert row.adapter == row.provider
        assert row.runtime_handler_id == f"runtime.{row.adapter}.execute"


def test_raw_bindings_do_not_resolve_through_legacy_aliases() -> None:
    from assurance_product import runtime_bindings

    source = inspect.getsource(runtime_bindings.runtime_bindings_from_composition)
    assert "alias_ids_for_prepare" not in source
    assert "LEGACY_AGENT_PHASE_ALIASES" not in source
    assert "bindings[" not in source


def test_boot_uses_resolved_raw_executor_for_every_agent_occurrence(opencode_composition) -> None:
    from agent_runtime_contracts import ResolvedRawAgentExecutor

    from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts

    composition = opencode_composition
    agents = all_feature_agent_contracts()
    tasks = all_feature_task_contracts()
    resolved = composition.semantic_attempt_contracts
    assert len(agents) == 34
    assert len(tasks) == 14
    task_ids = {contract.contract_id for contract in tasks.values()}
    assert set(agents) | task_ids == set(resolved)
    for contract_id in agents:
        assert isinstance(resolved[contract_id].executor, ResolvedRawAgentExecutor)
    for contract_id in task_ids:
        assert not isinstance(resolved[contract_id].executor, ResolvedRawAgentExecutor)
        assert type(resolved[contract_id].executor).__name__ == "DeterministicTaskExecutor"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ("missing", "missing"),
        ("duplicate", "duplicate"),
        ("extra", "extra"),
        ("wrong-owner", "wrong-owner"),
        ("wrong-adapter", "wrong-adapter"),
        ("contract-digest-drift", "contract-digest-drift"),
    ],
)
def test_raw_binding_rows_reject_invalid_catalog(mutate: str, match: str, opencode_composition) -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import (
        authenticate_raw_agent_runtime_bindings,
        raw_agent_runtime_binding_rows,
    )

    contracts = all_feature_agent_contracts()
    composition = opencode_composition
    rows = list(raw_agent_runtime_binding_rows(composition))
    if mutate == "missing":
        rows = rows[1:]
    elif mutate == "duplicate":
        rows.append(rows[0])
    elif mutate == "extra":
        extra = rows[0].model_copy(update={"contract_id": "assurance.intake.agent.forged.v1"})
        rows.append(extra)
    elif mutate == "wrong-owner":
        rows[0] = rows[0].model_copy(update={"runtime_handler_id": "assurance.generation.forged.execute"})
    elif mutate == "wrong-adapter":
        rows[0] = rows[0].model_copy(
            update={"adapter": "cursor", "provider": "cursor", "runtime_handler_id": "runtime.cursor.execute"}
        )
    else:
        rows[0] = rows[0].model_copy(update={"contract_digest": "c" * 64})

    with pytest.raises(ValueError, match=match):
        authenticate_raw_agent_runtime_bindings(rows, contracts, adapter="opencode")


class _PhaseInput(BaseModel):
    change_id: str = "CH-1"


class _PhasePrepared(BaseModel):
    prompt: str = "ok"


class _PhaseOutput(BaseModel):
    status: str = "ok"


class _PhaseFinalizeInput(BaseModel):
    agent_result: dict[str, object]


class _PreparedBusinessFinalizeInput(BaseModel):
    agent_result: dict[str, object]
    change_id: str
    batch_id: str
    execution_view_root: str


class _FailingHandler:
    def __init__(self, message: str = "phase rejected") -> None:
        self.message = message

    async def execute(self, request: object, context: object) -> object:
        del request, context
        from graph_engine.plugin_api import TaskOutcome

        return TaskOutcome.failed("invalid_output", self.message)


class _CapturingFailingHandler(_FailingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.binding_data: object = None

    async def execute(self, request: object, context: object) -> object:
        self.binding_data = request.binding_data  # type: ignore[attr-defined]
        return await super().execute(request, context)


class _FailingFinalizeHandler(_FailingHandler):
    input_model = _PhaseFinalizeInput


class _CapturingFinalizeHandler:
    def __init__(self, input_model: type[BaseModel]) -> None:
        self.input_model = input_model
        self.input: object = None

    async def execute(self, request: object, context: object) -> object:
        del context
        from graph_engine.plugin_api import TaskOutcome

        self.input = request.input  # type: ignore[attr-defined]
        return TaskOutcome.succeeded({"status": "ok"})


class _RecordingTaskHost:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def execute(self, call: object) -> object:
        from graph_engine.attempts.host_protocol import TaskHostCallResult
        from graph_engine.plugin_api import TaskOutcome

        self.calls.append(call)
        return TaskHostCallResult(
            operation="execute",
            outcome=TaskOutcome.failed("invalid_output", "stop-after-host", retryable=False),
        )


def _phase_scope(tmp_path: Path):
    from graph_engine.attempts import AttemptExecutionContext, AttemptKey, AuthorizedAttemptScope
    from graph_engine.plugin_api import DirectoryIdentity, TaskWorkspaceBinding, TaskWorkspaceIdentity

    project = tmp_path / "project"
    write = tmp_path / "write"
    project.mkdir()
    write.mkdir()
    digest = "a" * 64
    identity = TaskWorkspaceIdentity.model_construct(
        task_id="task",
        attempt=1,
        attempt_id="attempt-1",
        output_paths=(),
        baseline_files=(),
        project_digest=digest,
        write_root_digest=digest,
        identity_digest=digest,
        layout_schema_version="1",
    )
    directory = DirectoryIdentity.model_construct(
        path_digest=digest,
        device=1,
        inode=1,
        identity_digest=digest,
    )
    return AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv-1",
            public_entrypoint="intake",
            semantic_node_id="case-design",
            attempt_key=AttemptKey(digest=digest),
            fencing_token=1,
        ),
        workspace=TaskWorkspaceBinding(
            identity=identity,
            project_root=project,
            write_root=write,
            project_root_identity=directory,
            write_root_identity=directory,
        ),
    )


def _adapter_binding() -> dict[str, object]:
    return {
        "schema_version": "1",
        "endpoint": "http://127.0.0.1:4096",
        "tls_identity_digest": "a" * 64,
        "secret_handle": "opencode.token",
        "protocol_profile": "opencode-http-v1",
        "project_scope": "fixture-project",
        "request_timeout_seconds": 5,
        "observation_horizon_seconds": 30,
        "progress_timeout_seconds": 10,
        "poll_interval_seconds": 0.5,
        "cancel_timeout_seconds": 5,
        "max_response_bytes": 65536,
        "adapter_configuration_digest": "b" * 64,
    }


def _finalize_bundle(tmp_path: Path):
    from agent_runtime_contracts import (
        AgentRunResult,
        RawFinalizeBundle,
        ReadOnlyRawWorkspace,
        canonical_digest,
    )

    payload = {"status": "ok"}
    run_result = AgentRunResult.model_validate(
        {
            "result_payload": payload,
            "result_digest": canonical_digest(payload),
            "evidence_digest": "b" * 64,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    raw = tmp_path / "raw"
    raw.mkdir()
    return RawFinalizeBundle(
        validated_input=_PhaseInput(),
        prepared=_PhasePrepared(),
        agent_result=_PhaseOutput(),
        run_evidence=run_result,
        raw_workspace=ReadOnlyRawWorkspace(raw),
    )


def test_installed_prepare_returns_permanent_failure_on_invalid_outcome(tmp_path: Path) -> None:
    from graph_engine.attempts import PermanentTaskFailure

    from assurance_product.runtime_bindings import InstalledPreparePhase

    phase = InstalledPreparePhase("assurance.intake.case-design.prepare", _FailingHandler(), None)
    result = asyncio.run(phase.execute(_PhaseInput(), _phase_scope(tmp_path)))
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "phase rejected"


def test_installed_runtime_returns_permanent_failure_on_invalid_outcome(tmp_path: Path) -> None:
    from graph_engine.attempts import PermanentTaskFailure

    from assurance_product.runtime_bindings import InstalledRuntimePhase

    from agent_runtime_fixture.contracts import frozen_run_request

    phase = InstalledRuntimePhase("runtime.opencode.execute", _FailingHandler(), _adapter_binding())
    result = asyncio.run(phase.execute(frozen_run_request(), _phase_scope(tmp_path)))
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "phase rejected"


def test_installed_runtime_passes_complete_authenticated_adapter_binding(
    tmp_path: Path,
    opencode_composition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_runtime_opencode import OpenCodeAdapterConfig

    from agent_runtime_fixture.contracts import frozen_run_request

    expected = _adapter_binding()
    handler = _CapturingFailingHandler()
    resolved = opencode_composition.semantic_attempt_contracts["assurance.intake.agent.case-design.v1"]
    phase = resolved.executor._runtime  # type: ignore[attr-defined]
    monkeypatch.setattr(phase, "_handler", handler)

    asyncio.run(phase.execute(frozen_run_request(), _phase_scope(tmp_path)))

    assert OpenCodeAdapterConfig.model_validate(handler.binding_data).model_dump(mode="json") == (
        OpenCodeAdapterConfig.model_validate(expected).model_dump(mode="json")
    )


def test_bound_agent_runtime_executes_through_host_with_runtime_authority(
    tmp_path: Path,
    opencode_composition,
) -> None:
    from graph_engine.attempts import (
        AttemptExecutionContext,
        AttemptKey,
        AuthorizedAttemptScope,
        PermanentTaskFailure,
    )
    from graph_engine.attempts.workspace import TaskWorkspaceStore

    from agent_runtime_fixture.contracts import frozen_run_request

    graph_revision = "c" * 64
    product_lock_digest = "d" * 64
    host = _RecordingTaskHost()
    resolved = opencode_composition.semantic_attempt_contracts["assurance.intake.agent.case-design.v1"]
    executor = resolved.executor.with_host(
        host,
        graph_revision=graph_revision,
        product_lock_digest=product_lock_digest,
    )
    project = tmp_path / "host-project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "host-attempts", tmp_path / "host-receipts")
    try:
        binding = store.begin(task_id="task", attempt=1, output_paths=())
        scope = AuthorizedAttemptScope(
            execution=AttemptExecutionContext(
                invocation_id="inv-1",
                public_entrypoint="intake",
                semantic_node_id="case-design",
                attempt_key=AttemptKey(digest="a" * 64),
                fencing_token=1,
                authorization_id="b" * 64,
            ),
            workspace=binding,
        )
        result = asyncio.run(  # type: ignore[attr-defined]
            executor._runtime.execute(frozen_run_request(), scope)
        )
    finally:
        store.close()

    assert isinstance(result, PermanentTaskFailure)
    assert result.message == "stop-after-host"
    assert len(host.calls) == 1
    call = host.calls[0]
    assert call.identity.phase == "runtime"  # type: ignore[attr-defined]
    assert call.identity.activity_id == scope.execution.attempt_key.digest  # type: ignore[attr-defined]
    assert call.request.invocation.lock_digest == product_lock_digest  # type: ignore[attr-defined]
    assert call.authorized_secret_handles == ("opencode.token",)  # type: ignore[attr-defined]
    assert call.capability_entrypoint == (  # type: ignore[attr-defined]
        "agent_runtime_opencode.handler:OpenCodeHandler.execute"
    )


def test_installed_finalize_returns_permanent_failure_on_invalid_outcome(tmp_path: Path) -> None:
    from graph_engine.attempts import PermanentTaskFailure

    from assurance_product.runtime_bindings import InstalledFinalizePhase

    phase = InstalledFinalizePhase(
        "assurance.intake.case-design.finalize",
        _FailingFinalizeHandler(),
        _PhaseOutput,
    )
    result = asyncio.run(phase.execute(_finalize_bundle(tmp_path), _phase_scope(tmp_path)))
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "phase rejected"


def test_every_installed_agent_finalizer_declares_its_input_model(opencode_composition) -> None:
    from graph_engine.composition import TaskHandlerEntry

    from assurance_product.agent_contracts import all_feature_agent_contracts

    entries = opencode_composition.registries.capabilities.entries
    for contract in all_feature_agent_contracts().values():
        entry = entries[contract.finalize_handler_id]
        assert isinstance(entry, TaskHandlerEntry)
        input_model = getattr(entry.handler, "input_model", None)
        assert isinstance(input_model, type), contract.finalize_handler_id
        assert issubclass(input_model, BaseModel), contract.finalize_handler_id
        assert "agent_result" in input_model.model_fields, contract.finalize_handler_id


def test_installed_finalize_projects_the_bundle_to_the_feature_input_model(tmp_path: Path) -> None:
    from agent_runtime_contracts import (
        AgentRunResult,
        RawFinalizeBundle,
        ReadOnlyRawWorkspace,
        canonical_digest,
    )
    from assurance_intake.contracts.agent import (
        AgentFinalizeInputV1,
        ArtifactListResultV1,
        IntakeInputV1,
    )
    from assurance_product.runtime_bindings import InstalledFinalizePhase

    result_payload = {"output_files": ["qa/changes/CH-1/requirement.md"]}
    run_evidence = AgentRunResult.model_validate(
        {
            "result_payload": result_payload,
            "result_digest": canonical_digest(result_payload),
            "evidence_digest": "b" * 64,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    validated_input = IntakeInputV1(
        change_id="CH-1",
        capability_leafs=("intake",),
        artifact_paths=("qa/changes/CH-1/requirement.md",),
        requirement="must not leak into finalize",
    )
    raw = tmp_path / "raw-finalize"
    raw.mkdir()
    bundle = RawFinalizeBundle(
        validated_input=validated_input,
        prepared=_PhasePrepared(),
        agent_result=ArtifactListResultV1.model_validate(result_payload),
        run_evidence=run_evidence,
        raw_workspace=ReadOnlyRawWorkspace(raw),
    )
    handler = _CapturingFinalizeHandler(AgentFinalizeInputV1)
    phase = InstalledFinalizePhase(
        "assurance.intake.intake.finalize",
        handler,
        _PhaseOutput,
    )

    result = asyncio.run(phase.execute(bundle, _phase_scope(tmp_path)))

    assert result == {"status": "ok"}
    projected = AgentFinalizeInputV1.model_validate(handler.input)
    assert projected.agent_result == run_evidence
    assert projected.change_id == "CH-1"
    assert projected.capability_leafs == ("intake",)
    assert projected.artifact_paths == ("qa/changes/CH-1/requirement.md",)
    assert isinstance(handler.input, dict)
    assert "prepared" not in handler.input
    assert "validated_input" not in handler.input
    assert "requirement" not in handler.input


def test_installed_proposal_finalize_uses_current_input_without_approval_envelope(tmp_path: Path) -> None:
    from dataclasses import replace

    from assurance_healing.contracts.agent import FixProposalFinalizeInputV1, FixProposalInputV1
    from assurance_product.runtime_bindings import InstalledFinalizePhase

    business = FixProposalInputV1(
        change_id="CH-1",
        plan_digest="e" * 64,
        plan_ref=cast(
            Any,
            {
                "path": f"qa/changes/CH-1/plan/{'e' * 64}/resolved-assurance-plan.json",
                "digest": "f" * 64,
            },
        ),
        owner_id="assurance.healing",
        capability_leafs=("api.users",),
        allowed_paths=("tests/api/test_users.py",),
        allowed_roots=("tests/api",),
        baseline_digest="a" * 64,
        candidate_digest="b" * 64,
        policy_digest="c" * 64,
        mapping_paths=("qa/changes/CH-1/generated/mapping.json",),
        require_approval=True,
        execution_evidence_digest="d" * 64,
    )
    bundle = replace(_finalize_bundle(tmp_path), validated_input=business)
    handler = _CapturingFinalizeHandler(FixProposalFinalizeInputV1)
    phase = InstalledFinalizePhase("assurance.healing.fix-proposal.finalize", handler, _PhaseOutput)

    result = asyncio.run(phase.execute(bundle, _phase_scope(tmp_path)))

    assert result == {"status": "ok"}
    projected = FixProposalFinalizeInputV1.model_validate(handler.input)
    assert projected.prepare == business
    assert projected.agent_result == bundle.run_evidence
    assert projected.require_approval is True
    assert isinstance(handler.input, dict)
    assert not {"validated_input", "prepared", "approval", "mapping", "artifact_paths"} & handler.input.keys()


def test_installed_finalize_projects_trusted_prepared_business_fields(tmp_path: Path) -> None:
    from agent_runtime_contracts import (
        AgentRunRequest,
        AgentRunResult,
        AgentWorkspaceV1,
        FrozenExecutionSelection,
        InstructionPart,
        RawFinalizeBundle,
        ReadOnlyRawWorkspace,
        ResultContract,
        canonical_digest,
    )
    from agent_runtime_contracts.models import ExecutionLimits
    from assurance_product.runtime_bindings import InstalledFinalizePhase

    result_payload = {"status": "ok"}
    run_evidence = AgentRunResult.model_validate(
        {
            "result_payload": result_payload,
            "result_digest": canonical_digest(result_payload),
            "evidence_digest": "b" * 64,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )
    workspace_payload = {
        "schema_version": "1",
        "agent_profile": "assurance-v1-executor",
        "scope_id": "CH-1",
        "write_root": "qa/changes/CH-1/.staging/task/attempt-1",
        "allowed_outputs": ["qa/changes/CH-1/execution/execute-result.json"],
        "read_roots": ["qa/changes/CH-1/.staging/task/attempt-1/qa/changes/CH-1/.staging/execution/batch-1"],
    }
    workspace = AgentWorkspaceV1.model_validate(
        {**workspace_payload, "identity_digest": canonical_digest(workspace_payload)}
    )
    prepared = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", "execute"),
            InstructionPart.from_json(
                {
                    "change_id": "CH-1",
                    "batch_id": "batch-1",
                    "execution_view_root": (
                        "qa/changes/CH-1/.staging/task/attempt-1/qa/changes/CH-1/.staging/execution/batch-1"
                    ),
                }
            ),
        ),
        result_contract=ResultContract(
            schema_id="test.result.v1",
            schema_digest=canonical_digest({"type": "object"}),
            delivery_mode="assistant_json_local_v1",
            schema_document={"type": "object"},
        ),
        execution=FrozenExecutionSelection(
            provider_model="test/model",
            worker_profile="worker",
            permission_profile_digest="c" * 64,
            limits=ExecutionLimits(max_seconds=5),
        ),
        workspace=workspace,
        request_policy_digest="d" * 64,
        request_config_digest="e" * 64,
    )
    raw = tmp_path / "prepared-raw"
    raw.mkdir()
    bundle = RawFinalizeBundle(
        validated_input=_PhaseInput(change_id="CH-1"),
        prepared=prepared,
        agent_result=_PhaseOutput(),
        run_evidence=run_evidence,
        raw_workspace=ReadOnlyRawWorkspace(raw),
    )
    handler = _CapturingFinalizeHandler(_PreparedBusinessFinalizeInput)
    phase = InstalledFinalizePhase("assurance.execution.execute.finalize", handler, _PhaseOutput)

    result = asyncio.run(phase.execute(bundle, _phase_scope(tmp_path)))

    assert result == {"status": "ok"}
    projected = _PreparedBusinessFinalizeInput.model_validate(handler.input)
    assert projected.batch_id == "batch-1"
    assert projected.execution_view_root.endswith("/.staging/execution/batch-1")
