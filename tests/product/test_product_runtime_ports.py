from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pydantic import BaseModel

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ResolvedAttemptContract
from graph_engine.attempts.keys import AttemptKey
from tests.product.cli_support import lifecycle_authorization, write_project_dir
from tests.product.composition_harness import request_for

if TYPE_CHECKING:
    from assurance_product.runtime_ports import ProductRuntimePorts

pytestmark = pytest.mark.usefixtures("installed_sources")


@dataclass
class _ProductPortsFixture:
    ports: ProductRuntimePorts
    kernel_call_count: int = 0

    async def bind_without_runner_lease(self) -> object:
        factory = self.ports.execution_factory(
            invocation_id="inv-missing-fence",
            entrypoint="improvement-apply",
            root_input_digest="c" * 64,
        )
        return factory.bind(None)  # type: ignore[arg-type]


@pytest.fixture
def product_ports_fixture(installed_sources, tmp_path: Path):
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product.product import prepare_change_workspace, resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-FENCE")

    async def _open() -> _ProductPortsFixture:
        async with ProductRuntimePorts.open(
            workspace,
            composition,
            invocation="inv-missing-fence",
            authorization=lifecycle_authorization(),
            reachable_contract_ids=ENTRYPOINT_AGENT_CONTRACT_IDS["improvement-apply"],
        ) as ports:
            fixture = _ProductPortsFixture(ports=ports)
            original = ports.kernel.execute_or_recover

            async def _count(
                attempt_key: AttemptKey,
                contract: ResolvedAttemptContract[Any, Any],
                validated_input: BaseModel,
                context: AttemptExecutionContext,
            ) -> object:
                fixture.kernel_call_count += 1
                return await original(attempt_key, contract, validated_input, context)

            ports.kernel.execute_or_recover = _count  # type: ignore[method-assign]
            return fixture

    return asyncio.run(_open())


def test_missing_fence_fails_before_kernel(product_ports_fixture) -> None:
    with pytest.raises(ValueError, match="fencing token"):
        asyncio.run(product_ports_fixture.bind_without_runner_lease())
    assert product_ports_fixture.kernel_call_count == 0


def test_product_runtime_ports_register_observer_before_boot(installed_sources, tmp_path: Path) -> None:
    asyncio.run(_register_observer(installed_sources, tmp_path))


async def _register_observer(installed_sources, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace, resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts
    from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
    from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-001")
    async with ProductRuntimePorts.open(workspace, composition) as ports:
        assert ports.kernel is not None
        assert isinstance(ports.observer, AttemptCheckpointObserver)
        assert ports.attempt_journal is ports.observer._journal
        checkpointer = ports.checkpointer_for(
            invocation_id="inv-ports-001",
            revision_id="a" * 64,
            product_lock_digest="b" * 64,
            root_input_digest="c" * 64,
            fencing_token=1,
        )
        assert isinstance(checkpointer, AnchoredCheckpointer)
        assert ports.observer in checkpointer._observers
        assert ports.observer_registered_before_compile is True
        lease = await ports.backend.lease.acquire("inv-ports-001", owner_id="ports-test")
        try:
            bound = ports.execution_factory(
                invocation_id="inv-ports-001",
                entrypoint="intake",
                root_input_digest="c" * 64,
            ).bind(lease)
            assert bound.artifact.checkpointer_backend_id
            assert set(bound.artifact.entrypoints) >= {"archive", "intake"}
            assert bound.runtime_context.fencing_token == lease.fencing_token
        finally:
            await ports.backend.lease.release(lease)
        assert ports.observer_registered_before_compile is True


def test_production_observer_registry_rejects_empty_and_fake_only(installed_sources, tmp_path: Path) -> None:
    asyncio.run(_reject_observers(installed_sources, tmp_path))


async def _reject_observers(installed_sources, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace, resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts, ProductionObserverError

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-002")
    with pytest.raises(ProductionObserverError):
        async with ProductRuntimePorts.open(
            workspace,
            composition,
            observers=(),
        ):
            raise AssertionError("empty observer registry must not enter")
    with pytest.raises(ProductionObserverError):
        async with ProductRuntimePorts.open(
            workspace,
            composition,
            observers=(object(),),
        ):
            raise AssertionError("fake-only observer registry must not enter")


def test_ports_shutdown_closes_in_reverse_after_recovery(installed_sources, tmp_path: Path) -> None:
    asyncio.run(_shutdown_order(installed_sources, tmp_path))


async def _shutdown_order(installed_sources, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace, resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-003")
    async with ProductRuntimePorts.open(workspace, composition) as ports:
        assert ports.shutdown_order() == ()
        held = ports
    order = held.shutdown_order()
    assert order[0] == "observer_outbox_recovery"
    assert order[-1] == "sqlite"
    assert "attempt_journal" in order
    assert "kernel" in order


def test_aa_compile_does_not_construct_invocation_runtime(cli_runner, installed_sources, monkeypatch) -> None:
    from assurance_product.cli import app
    from assurance_product.runtime_ports import AuthorizedSecretResolver, ProductRuntimePorts
    from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease
    from tests.product.cli_support import source_args

    opened = {"sqlite": 0, "secret": 0, "host": 0, "lease": 0, "opencode": 0}

    def _count(name: str):
        def _blocked(*args: object, **kwargs: object) -> object:
            del args, kwargs
            opened[name] += 1
            raise AssertionError(f"aa compile must not {name}")

        return _blocked

    monkeypatch.setattr(ProductRuntimePorts, "open", classmethod(_count("sqlite")))
    monkeypatch.setattr(AuthorizedSecretResolver, "resolve", _count("secret"))
    monkeypatch.setattr(
        "graph_engine.attempts.production_host.create_production_task_execution_host",
        _count("host"),
    )
    monkeypatch.setattr(LocalInvocationRunnerLease, "acquire", _count("lease"))
    try:
        from agent_runtime_opencode.protocol import OpenCodeHttpClient

        monkeypatch.setattr(OpenCodeHttpClient, "create_session", _count("opencode"))
    except ImportError:
        pass

    result = cli_runner.invoke(app, ["compile", "--json", *source_args(installed_sources)])
    assert result.exit_code == 0, result.output
    assert opened == {"sqlite": 0, "secret": 0, "host": 0, "lease": 0, "opencode": 0}


def test_bound_agent_executors_carry_the_production_host(installed_sources, tmp_path: Path) -> None:
    asyncio.run(_bound_agent_executors_carry_host(installed_sources, tmp_path))


async def _bound_agent_executors_carry_host(installed_sources, tmp_path: Path) -> None:
    from agent_runtime_contracts import ResolvedRawAgentExecutor
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product.product import prepare_change_workspace, resolve_assurance_composition
    from assurance_product.runtime_ports import ProductRuntimePorts

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-HOST")
    async with ProductRuntimePorts.open(
        workspace,
        composition,
        invocation="inv-host-bind",
        authorization=lifecycle_authorization(),
        reachable_contract_ids=ENTRYPOINT_AGENT_CONTRACT_IDS["intake"],
    ) as ports:
        lease = await ports.backend.lease.acquire("inv-host-bind", owner_id="ports-host")
        try:
            bound = ports.execution_factory(
                invocation_id="inv-host-bind",
                entrypoint="intake",
                root_input_digest="c" * 64,
            ).bind(lease)
        finally:
            await ports.backend.lease.release(lease)
        agents = [
            resolved.executor
            for resolved in bound.artifact.attempt_contracts.values()
            if isinstance(resolved.executor, ResolvedRawAgentExecutor)
        ]
        assert agents
        assert all(executor._host is ports.host for executor in agents)
