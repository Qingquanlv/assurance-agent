from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.product.cli_support import write_project_dir
from tests.product.composition_harness import request_for

pytestmark = pytest.mark.usefixtures("installed_sources")


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
        artifact = await ports.compile_roots()
        assert artifact.checkpointer_backend_id
        assert set(artifact.entrypoints) >= {"archive", "intake"}


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
        order = ports.shutdown_order()
        assert order[0] == "observer_outbox_recovery"
        assert order[-1] == "sqlite"
        assert "attempt_journal" in order
        assert "kernel" in order
