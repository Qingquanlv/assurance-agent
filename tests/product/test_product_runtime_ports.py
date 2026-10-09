from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pydantic import BaseModel

from graph_engine.attempts.models.context import AttemptExecutionContext
from graph_engine.attempts.models.contracts import ResolvedAttemptContract
from graph_engine.attempts.models.keys import AttemptKey
from tests.product.cli_support import lifecycle_authorization, write_project_dir

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
            entrypoint="init",
            root_input_digest="c" * 64,
        )
        return factory.bind(None)  # type: ignore[arg-type]


@pytest.fixture
def product_ports_fixture(opencode_composition, tmp_path: Path):
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts

    composition = opencode_composition
    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-FENCE")

    async def _open() -> _ProductPortsFixture:
        async with ProductRuntimePorts.open(
            workspace,
            composition,
            invocation="inv-missing-fence",
            authorization=lifecycle_authorization(),
            reachable_contract_ids=ENTRYPOINT_AGENT_CONTRACT_IDS["init"],
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


def test_product_runtime_ports_register_observer_before_boot(opencode_composition, tmp_path: Path) -> None:
    asyncio.run(_register_observer(opencode_composition, tmp_path))


def test_managed_run_uses_output_capture_workspace(opencode_composition, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace
    from assurance_product.run_history import RunOutputWorkspaceProvider
    from assurance_product.runtime_ports import ProductRuntimePorts

    project = write_project_dir(tmp_path / "project")
    change_id = "CH-OUTPUT-CAPTURE"
    workspace = prepare_change_workspace(project, change_id)
    (project / ".aa" / "runs" / change_id).mkdir(parents=True, exist_ok=True)

    async def inspect() -> None:
        async with ProductRuntimePorts.open(workspace, opencode_composition, invocation=change_id) as ports:
            assert isinstance(ports.workspace_provider, RunOutputWorkspaceProvider)

    asyncio.run(inspect())


async def _register_observer(composition, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts
    from graph_engine.attempts.orchestration.checkpoint_bridge import AttemptCheckpointObserver
    from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer

    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-001")
    async with ProductRuntimePorts.open(workspace, composition) as ports:
        assert ports.kernel is not None
        assert isinstance(ports.observer, AttemptCheckpointObserver)
        assert ports.attempt_checkpoints is ports.observer._checkpoints
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
            assert set(bound.artifact.entrypoints) >= {"init", "intake"}
            assert bound.runtime_context.fencing_token == lease.fencing_token
        finally:
            await ports.backend.lease.release(lease)
        assert ports.observer_registered_before_compile is True


def test_production_observer_registry_rejects_empty_and_fake_only(
    opencode_composition, tmp_path: Path
) -> None:
    asyncio.run(_reject_observers(opencode_composition, tmp_path))


async def _reject_observers(composition, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts, ProductionObserverError

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


def test_ports_shutdown_closes_in_reverse_after_recovery(opencode_composition, tmp_path: Path) -> None:
    asyncio.run(_shutdown_order(opencode_composition, tmp_path))


def test_projection_failure_preserves_result_and_closes_workspace(
    opencode_composition, tmp_path: Path, monkeypatch, caplog
) -> None:
    import os

    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts

    project = write_project_dir(tmp_path / "project")
    change_id = "CH-PROJECTION-ERROR"
    workspace = prepare_change_workspace(project, change_id)
    (project / ".aa/runs" / change_id).mkdir(parents=True)
    descriptors: list[int] = []

    def broken_projection(*args, **kwargs):
        raise OSError("display filesystem unavailable")

    monkeypatch.setattr("assurance_product.operator_views.write_attempt_projection", broken_projection)

    async def run() -> str:
        async with ProductRuntimePorts.open(workspace, opencode_composition, invocation=change_id) as ports:
            descriptors.append(ports.workspace_provider.store._project_fd)
            return "completed"

    assert asyncio.run(run()) == "completed"
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert "display filesystem unavailable" in caplog.text


def test_checkpoint_recovery_failure_is_reported_after_workspace_close(
    opencode_composition, tmp_path: Path, monkeypatch
) -> None:
    import os

    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts

    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-RECOVERY-ERROR")
    descriptors: list[int] = []

    async def run() -> None:
        async with ProductRuntimePorts.open(workspace, opencode_composition) as ports:
            descriptors.append(ports.workspace_provider.store._project_fd)

            async def broken_recovery(_backend, _invocation_id: str):
                raise RuntimeError("checkpoint recovery failed")

            monkeypatch.setattr(type(ports.backend), "recover_handshake", broken_recovery)

    with pytest.raises(RuntimeError, match="checkpoint recovery failed"):
        asyncio.run(run())
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


async def _shutdown_order(composition, tmp_path: Path) -> None:
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts

    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-PORTS-003")
    async with ProductRuntimePorts.open(workspace, composition) as ports:
        assert ports.shutdown_order() == ()
        held = ports
    order = held.shutdown_order()
    assert order[0] == "observer_outbox_recovery"
    assert order[-1] == "sqlite"
    assert "attempt_checkpoints" in order
    assert "kernel" in order


def test_bound_agent_executors_carry_the_production_host(opencode_composition, tmp_path: Path) -> None:
    asyncio.run(_bound_agent_executors_carry_host(opencode_composition, tmp_path))


def test_post_factory_full_manifest_rejects_newly_broken_schema(
    opencode_composition, tmp_path: Path, monkeypatch
) -> None:
    from assurance_product.graphs import factory as graph_factory
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts

    project = write_project_dir(tmp_path / "project")
    workspace = prepare_change_workspace(project, "CH-POST-FACTORY-SCHEMA")
    resolved = next(iter(opencode_composition.semantic_attempt_contracts.values()))
    real_build = graph_factory.build_product_graphs
    factory_calls: list[str] = []

    def build_then_change_model(*args: Any, **kwargs: Any) -> Any:
        graphs = real_build(*args, **kwargs)
        factory_calls.append("built")

        def broken_schema(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("post-factory schema drift")

        monkeypatch.setattr(resolved.contract.input_model, "model_json_schema", classmethod(broken_schema))
        return graphs

    async def compile_after_factory() -> None:
        async with ProductRuntimePorts.open(workspace, opencode_composition) as ports:
            monkeypatch.setattr(graph_factory, "build_product_graphs", build_then_change_model)
            lease = await ports.backend.lease.acquire("inv-schema-drift", owner_id="test-post-factory")
            try:
                with pytest.raises(RuntimeError, match="post-factory schema drift"):
                    ports.execution_factory(
                        invocation_id="inv-schema-drift",
                        entrypoint="improvement-apply",
                        root_input_digest="c" * 64,
                    ).bind(lease)
                assert factory_calls == ["built"]
                assert ports._artifact is None
            finally:
                await ports.backend.lease.release(lease)

    asyncio.run(compile_after_factory())


async def _bound_agent_executors_carry_host(composition, tmp_path: Path) -> None:
    from agent_runtime_contracts import ResolvedRawAgentExecutor
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_ports import ProductRuntimePorts

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
