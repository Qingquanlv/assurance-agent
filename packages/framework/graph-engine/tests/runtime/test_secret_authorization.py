from __future__ import annotations

import importlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    InvocationWorkspaceBinding,
    TaskOutcome,
    TaskRequest,
)
from bootstrap_fixtures import Engine, InvocationDrift
from graph_engine.attempts.activity import PlannedTask
from bootstrap_fixtures import Scheduler, SchedulerStateError
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    empty_runtime_authorization,
    runtime_authorization_digest,
)
from bootstrap_fixtures import empty_invocation_seed


class _Handler:
    async def execute(self, _request: TaskRequest, _context: object) -> TaskOutcome:
        return TaskOutcome.succeeded()


_handler = _Handler()


def _toy_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrozenComposition:
    source = tmp_path / "source"
    shutil.copytree(
        Path(__file__).resolve().parent / "fixtures" / "leftover-toy-a",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    declaration_path = source / "graph_engine_toy_a" / "product-declaration.json"
    declaration = json.loads(declaration_path.read_bytes())
    declaration["manifest"]["entrypoints"] = {"full": "root"}
    declaration["manifest"]["workflow"]["entrypoints"] = {"full": "root"}
    declaration_path.write_bytes(
        __import__("graph_engine.canonical", fromlist=["canonical_json_bytes"]).canonical_json_bytes(
            declaration
        )
    )
    provider_path = source / "graph_engine_toy_a" / "product.py"
    provider_path.write_text(
        provider_path.read_text(encoding="utf-8")
        .replace('entrypoints={"hello": "root"}', 'entrypoints={"full": "root"}')
        .replace('"entrypoints": {"hello": "root"}', '"entrypoints": {"full": "root"}'),
        encoding="utf-8",
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == "graph_engine_toy_a" or module_name.startswith("graph_engine_toy_a."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    common: dict[str, object] = {
        "distribution": "graph-engine-toy-a",
        "entrypoint_name": "toy-a",
        "source_root": source,
        "source_files": source_files,
    }
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                **common,
                declaration_path="graph_engine_toy_a/product-declaration.json",
            ),
            plugins=(
                EditableWheelPluginSource(
                    **common,
                    declaration_path="graph_engine_toy_a/plugin-declaration.json",
                ),
            ),
        )
    )


def _registry_with_binding(binding: CapabilityBindingContribution) -> SimpleNamespace:
    entry = SimpleNamespace(
        target_capability_id=binding.target_capability_id,
        secret_handles=tuple(binding.secret_handles),
        data=binding.data,
        resource_ids=tuple(binding.resource_ids),
        handler=_handler,
    )
    return SimpleNamespace(
        bindings={binding.capability_id: entry},
        task_handlers={binding.capability_id: entry.handler},
        commit_validators={},
    )


def _workspace_binding(tmp_path: Path) -> InvocationWorkspaceBinding:
    project_root = tmp_path / "project"
    attempts_root = tmp_path / "attempts"
    receipts_root = tmp_path / "promotion-receipts"
    for path in (project_root, attempts_root, receipts_root):
        path.mkdir(exist_ok=True)
    return InvocationWorkspaceBinding(
        project_root=project_root,
        attempts_root=attempts_root,
        receipts_root=receipts_root,
    )


def _planned_task(capability_id: str) -> PlannedTask:
    return PlannedTask(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id=capability_id,
        attempt=1,
        activation_id="activation-run",
        input={},
        timeout_seconds=30.0,
        validators=(),
        topology_rank=0,
        declaration_index=0,
        resources=__import__("graph_engine.plugin_api", fromlist=["ResourceClaims"]).ResourceClaims(),
    )


@dataclass(frozen=True, slots=True)
class _SchedulerProbe:
    scheduler: Scheduler
    capability_id: str

    def authorized_handles(self) -> tuple[str, ...]:
        return self.scheduler._authorized_secret_handles(_planned_task(self.capability_id))  # noqa: SLF001


@pytest.fixture
def scheduler_fixture() -> object:
    def factory(
        *,
        binding: CapabilityBindingContribution,
        authorization: InvocationRuntimeAuthorization | None = None,
    ) -> _SchedulerProbe:
        runtime_authorization = authorization if authorization is not None else empty_runtime_authorization()
        scheduler = Scheduler(
            _registry_with_binding(binding),
            store=object(),  # type: ignore[arg-type]
            ledger=object(),  # type: ignore[arg-type]
            host=object(),  # type: ignore[arg-type]
            owner_id="owner",
            runtime_authorization=runtime_authorization,
        )
        return _SchedulerProbe(scheduler=scheduler, capability_id=binding.capability_id)

    return factory


@pytest.fixture
def authorization(tmp_path: Path) -> InvocationRuntimeAuthorization:
    secret_path = tmp_path / "secret.txt"
    secret_path.write_bytes(b"actual-secret")
    binding = SecretSourceBinding(
        handle="opencode.token",
        source_kind="file",
        source_locator=str(secret_path.resolve()),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=(binding,),
        digest=runtime_authorization_digest((binding,)),
    )


@pytest.fixture
def make_engine(tmp_path: Path):
    def factory() -> Engine:
        return Engine(tmp_path / "engine")

    return factory


def test_task_receives_only_binding_authorized_handles(scheduler_fixture) -> None:
    binding = CapabilityBindingContribution(
        capability_id="fixture.binding",
        target_capability_id="runtime.opencode.execute",
        secret_handles=("opencode.token",),
    )
    authorization = InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=(
            SecretSourceBinding(
                handle="opencode.token",
                source_kind="environment",
                source_locator="OPENCODE_TOKEN",
            ),
        ),
        digest=runtime_authorization_digest(
            (
                SecretSourceBinding(
                    handle="opencode.token",
                    source_kind="environment",
                    source_locator="OPENCODE_TOKEN",
                ),
            )
        ),
    )
    handles = scheduler_fixture(binding=binding, authorization=authorization).authorized_handles()
    assert handles == ("opencode.token",)


def test_secret_bytes_never_serialize(
    tmp_path: Path,
    make_engine,
    authorization: InvocationRuntimeAuthorization,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_composition(tmp_path, monkeypatch)
    engine = make_engine()
    engine.start(
        composition,
        entrypoint="full",
        invocation_id="inv",
        seed=empty_invocation_seed(),
        authorization=authorization,
        workspace_binding=_workspace_binding(tmp_path),
    )
    engine_root = tmp_path / "engine"
    assert b"actual-secret" not in b"".join(
        path.read_bytes() for path in engine_root.rglob("*") if path.is_file() and not path.is_symlink()
    )


def test_runtime_authorization_rejects_duplicate_handles() -> None:
    binding = SecretSourceBinding(
        handle="opencode.token",
        source_kind="environment",
        source_locator="OPENCODE_TOKEN",
    )
    with pytest.raises(ValueError, match="unique"):
        InvocationRuntimeAuthorization(
            schema_version="1",
            secret_sources=(binding, binding),
            digest=runtime_authorization_digest((binding,)),
        )


def test_runtime_authorization_rejects_digest_mismatch() -> None:
    binding = SecretSourceBinding(
        handle="opencode.token",
        source_kind="environment",
        source_locator="OPENCODE_TOKEN",
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        InvocationRuntimeAuthorization(
            schema_version="1",
            secret_sources=(binding,),
            digest="0" * 64,
        )


def test_open_rejects_runtime_authorization_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_composition(tmp_path, monkeypatch)
    engine = Engine(tmp_path / "engine")
    workspace_binding = _workspace_binding(tmp_path)
    engine.start(
        composition,
        entrypoint="full",
        invocation_id="inv-open",
        seed=empty_invocation_seed(),
        authorization=empty_runtime_authorization(),
        workspace_binding=workspace_binding,
    ).close()
    drifted = InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=(
            SecretSourceBinding(
                handle="opencode.token",
                source_kind="environment",
                source_locator="OTHER_TOKEN",
            ),
        ),
        digest=runtime_authorization_digest(
            (
                SecretSourceBinding(
                    handle="opencode.token",
                    source_kind="environment",
                    source_locator="OTHER_TOKEN",
                ),
            )
        ),
    )
    with pytest.raises(InvocationDrift, match="authorization"):
        engine.open(
            "inv-open",
            composition,
            authorization=drifted,
            workspace_binding=workspace_binding,
        )


def test_dispatch_rejects_missing_authorized_handle(scheduler_fixture) -> None:
    binding = CapabilityBindingContribution(
        capability_id="fixture.binding",
        target_capability_id="runtime.opencode.execute",
        secret_handles=("opencode.token",),
    )
    with pytest.raises(SchedulerStateError, match="missing authorized secret handle"):
        scheduler_fixture(binding=binding).authorized_handles()


def test_serialized_artifacts_contain_handles_not_secret_bytes(
    authorization: InvocationRuntimeAuthorization,
) -> None:
    encoded = repr(
        {
            "schema_version": authorization.schema_version,
            "secret_sources": [
                {
                    "handle": item.handle,
                    "source_kind": item.source_kind,
                    "source_locator": item.source_locator,
                }
                for item in authorization.secret_sources
            ],
            "digest": authorization.digest,
        }
    ).encode()
    assert b"actual-secret" not in encoded
    assert b"opencode.token" in encoded
