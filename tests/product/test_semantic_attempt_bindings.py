from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

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
    assert len(contracts) == 33
    assert set(bindings) == set(contracts)
    assert len(bindings) == 33
    assert all(
        binding.model != "fixture-model" or binding.provider == "opencode" for binding in bindings.values()
    )


def test_runtime_registry_contains_exact_semantic_contracts(runtime_registry) -> None:
    from assurance_product.agent_contracts import is_agent_contract

    assert len(runtime_registry) == 41
    assert sum(is_agent_contract(item.contract) for item in runtime_registry.values()) == 33
    assert not any(type(item.executor).__name__.startswith("_Deferred") for item in runtime_registry.values())


def test_semantic_bindings_are_the_only_live_agent_ids(opencode_composition) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

    composition = opencode_composition
    assert len(composition.semantic_attempt_contracts) == 41
    assert len(AGENT_EXECUTION_CONTRACTS) == 33
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
    assert len(resolved) == 41
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
    assert len(rows) == 33
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
    assert len(agents) == 33
    assert len(tasks) == 8
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


class _FailingHandler:
    def __init__(self, message: str = "phase rejected") -> None:
        self.message = message

    async def execute(self, request: object, context: object) -> object:
        del request, context
        from graph_engine.plugin_api import TaskOutcome

        return TaskOutcome.failed("invalid_output", self.message)


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


def _runtime_binding():
    from agent_runtime_contracts import AgentRuntimeBinding, AgentRuntimePolicy

    return AgentRuntimeBinding(
        contract_id="assurance.intake.agent.case-design.v1",
        runtime_handler_id="runtime.opencode.execute",
        provider="opencode",
        model="provider_default",
        policy=AgentRuntimePolicy(
            request_policy_handle="assurance.policy.v1",
            request_config_handle="assurance.config.v1",
        ),
        secret_handles=(),
    )


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

    phase = InstalledRuntimePhase("runtime.opencode.execute", _FailingHandler(), _runtime_binding())
    result = asyncio.run(phase.execute(frozen_run_request(), _phase_scope(tmp_path)))
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "phase rejected"


def test_installed_finalize_returns_permanent_failure_on_invalid_outcome(tmp_path: Path) -> None:
    from graph_engine.attempts import PermanentTaskFailure

    from assurance_product.runtime_bindings import InstalledFinalizePhase

    phase = InstalledFinalizePhase(
        "assurance.intake.case-design.finalize",
        _FailingHandler(),
        _PhaseOutput,
    )
    result = asyncio.run(phase.execute(_finalize_bundle(tmp_path), _phase_scope(tmp_path)))
    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert result.message == "phase rejected"
