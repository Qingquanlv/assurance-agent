"""Test-only six-wheel composition and OpenCode rebinding harness."""

from __future__ import annotations

import pydantic.root_model  # noqa: F401  # keep RootModel[list[...]] reimportable
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
import importlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, TypeVar, cast

import asyncio

from agent_runtime_contracts import AgentRunRequest, InstructionPart
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from graph_engine.application import AssuranceApplication, InvocationBoundExecutionFactory
from graph_engine.application.status import InvocationStatus
from graph_engine.attempts.context import AuthorizedAttemptScope
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.boot.generic import (
    CatalogContractResolver,
    MemoryCheckpointer,
    boot_factory_product,
    factory_application,
    invocation_values,
    workspace_provider_for,
)
from graph_engine.canonical import JSONValue
from graph_engine.composition import (
    ConfigTreePluginSource,
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.boot.graph_revision import BootArtifact
from graph_engine.composition.lock import ProductLock
from graph_engine.plugin_api import ResourceClaims
from pydantic import BaseModel
from tests.phase4.agent_harness import FakeAgentAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
PRODUCT_ROOT = FIXTURE_ROOT / "six-wheel-product"
PRODUCT_PACKAGE = PRODUCT_ROOT / "test_assurance_phase4_product"
BINDINGS_ROOTS = {
    "phase4-opencode": FIXTURE_ROOT / "bindings-opencode",
    "phase4-cursor": FIXTURE_ROOT / "bindings-cursor",
}
PRODUCT_NAMES = frozenset(BINDINGS_ROOTS)
PRODUCT_DISTRIBUTION = "test-assurance-phase4-product"
PRODUCT_ENTRYPOINTS = {
    "phase4-opencode": "test_assurance_phase4_product.product:Phase4OpenCodeProduct",
    "phase4-cursor": "test_assurance_phase4_product.product:Phase4CursorProduct",
}
PRODUCT_DECLARATIONS = {
    "phase4-opencode": "test_assurance_phase4_product/product-opencode-declaration.json",
    "phase4-cursor": "test_assurance_phase4_product/product-cursor-declaration.json",
}
RUNTIME_EXECUTE = {
    "phase4-opencode": "runtime.opencode.execute",
    "phase4-cursor": "runtime.cursor.execute",
}
ADAPTER_IDS = {
    "phase4-opencode": "runtime.opencode",
    "phase4-cursor": "runtime.cursor",
}
WHEEL_PLUGINS: tuple[tuple[str, str, str, str], ...] = (
    ("assurance-intake", "assurance_intake", "intake", "assurance_intake/plugin-declaration.json"),
    (
        "assurance-generation",
        "assurance_generation",
        "generation",
        "assurance_generation/plugin-declaration.json",
    ),
    (
        "assurance-execution",
        "assurance_execution",
        "execution",
        "assurance_execution/plugin-declaration.json",
    ),
    ("assurance-healing", "assurance_healing", "healing", "assurance_healing/plugin-declaration.json"),
    ("assurance-quality", "assurance_quality", "quality", "assurance_quality/plugin-declaration.json"),
    (
        "assurance-improvement",
        "assurance_improvement",
        "improvement",
        "assurance_improvement/plugin-declaration.json",
    ),
    (
        "agent-runtime-opencode",
        "agent_runtime_opencode",
        "opencode",
        "agent_runtime_opencode/plugin-declaration.json",
    ),
)
FIXTURE_PERMISSION_BYTES = b'{"profile":"fixture-v1","writes":["review.json"]}\n'
CASE_REVIEW_STRUCTURED: dict[str, JSONValue] = {
    "schema_version": "1.0",
    "review_type": "case",
    "change_id": "CH-DEMO-001",
    "decision": "pass",
    "findings": [],
    "auto_fix_plan": [],
    "next_action": "continue",
    "auto_fix_allowed": False,
    "human_review_required": False,
    "risk_level": "low",
    "minimum_coverage": {
        "total_required": 2,
        "covered": 2,
        "skipped_by_scope": 0,
        "missing": [],
    },
    "source_verification": {
        "independent": True,
        "reviewed_source_files": ["src/app.py"],
        "verified_claims": [
            {"claim": "create item persists a menu record", "evidence_files": ["src/app.py"]}
        ],
    },
}

_REQUIRED_PATHS = (
    PRODUCT_ROOT / "pyproject.toml",
    PRODUCT_PACKAGE / "product.py",
    PRODUCT_PACKAGE / "product-opencode-declaration.json",
    PRODUCT_PACKAGE / "product-cursor-declaration.json",
    FIXTURE_ROOT / "bindings-opencode" / "plugin.yaml",
    FIXTURE_ROOT / "bindings-opencode" / "model-policy.json",
    FIXTURE_ROOT / "bindings-cursor" / "plugin.yaml",
    FIXTURE_ROOT / "bindings-cursor" / "model-policy.json",
)
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.egg-info")
_KEEP: list[Any] = []


def require_six_wheel_fixtures() -> None:
    missing = [path for path in _REQUIRED_PATHS if not path.is_file()]
    if missing:
        names = ", ".join(path.as_posix() for path in missing)
        raise FileNotFoundError(f"phase4 six-wheel product/config/workflow fixtures are absent: {names}")


def binding_data_template() -> dict[str, JSONValue]:
    policy_bytes = (FIXTURE_ROOT / "bindings-opencode" / "model-policy.json").read_bytes()
    execution: dict[str, JSONValue] = {
        "provider_model": "fixture/model-v1",
        "worker_profile": "fixture-profile",
        "permission_profile_digest": hashlib_sha256(FIXTURE_PERMISSION_BYTES),
        "limits": {"max_seconds": 30},
    }
    policy_digest = hashlib_sha256(policy_bytes)
    return {
        "agent_profile": "aa-doc-author",
        "execution": execution,
        "request_policy_digest": policy_digest,
        "request_config_digest": canonical_digest(
            {"execution": execution, "request_policy_digest": policy_digest}
        ),
    }


def hashlib_sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def refresh_product_declarations(product_root: Path) -> None:
    package_root = product_root / "test_assurance_phase4_product"
    inserted = str(product_root)
    sys.path.insert(0, inserted)
    try:
        for name in tuple(sys.modules):
            if name == "test_assurance_phase4_product" or name.startswith("test_assurance_phase4_product."):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()
        module = importlib.import_module("test_assurance_phase4_product.product")
        for cls, filename in (
            (module.Phase4OpenCodeProduct, "product-opencode-declaration.json"),
            (module.Phase4CursorProduct, "product-cursor-declaration.json"),
        ):
            manifest = cls.manifest()
            source = manifest.source
            if source is None:
                raise ValueError("phase4 product source must be declared")
            dumped = manifest.model_dump(mode="json", by_alias=True, exclude_none=True)
            document = {
                "kind": "product",
                "manifest": dumped,
                "schema_version": "1",
                "source": source.model_dump(mode="json", by_alias=True),
            }
            (package_root / filename).write_bytes(canonical_json_bytes(cast(JSONValue, document)))
    finally:
        if sys.path and sys.path[0] == inserted:
            sys.path.pop(0)


@dataclass(frozen=True, slots=True)
class _InvocationLockView:
    lock: ProductLock

    def canonical_bytes(self) -> bytes:
        return self.lock.canonical_bytes


@dataclass(frozen=True, slots=True)
class SixWheelComposition:
    composition: FrozenComposition
    product_name: str
    fixture_wheel: Path
    product_root: Path
    bindings_root: Path
    workspace: Path

    @property
    def dependency_order(self) -> tuple[str, ...]:
        return self.composition.lock.dependency_order

    @property
    def digest(self) -> str:
        return self.composition.digest

    @property
    def lock_digest(self) -> str:
        return self.composition.lock_digest

    @property
    def invocation_lock(self) -> _InvocationLockView:
        return _InvocationLockView(self.composition.lock)


@dataclass
class SixWheelRun:
    product_name: str
    composition: FrozenComposition
    agent_request_bytes: bytes
    business_output_bytes: bytes
    adapter_id: str
    lock_digest: str
    composition_digest: str
    invocation_root: Path
    engine_root: Path
    provider_state_dir: Path
    fixture_wheel: Path
    product_root: Path
    workspace: Path
    application: AssuranceApplication | None = None
    artifact: object = None
    runtime_context: object = None
    checkpointer: MemoryCheckpointer | None = None
    invocation_id: str = ""


class _OverlayMetadata:
    def __init__(self, overlay: Mapping[str, metadata.Distribution]) -> None:
        self._overlay = dict(overlay)

    def distribution(self, distribution_name: str) -> metadata.Distribution:
        try:
            return self._overlay[distribution_name]
        except KeyError:
            return metadata.distribution(distribution_name)


class SixWheelTaskHost:
    """Records fixture AgentRunRequest bytes for the Application execute contract."""

    def __init__(self, *, adapter_id: str, provider_state_dir: Path) -> None:
        self.adapter_id = adapter_id
        self.provider_state_dir = provider_state_dir
        self.recorded_request_bytes: bytes | None = None


def resolve_fixture(
    product_name: str,
    *,
    mutate: Callable[[Path, Path, Path], None] | None = None,
) -> SixWheelComposition:
    require_six_wheel_fixtures()
    if product_name not in PRODUCT_NAMES:
        raise ValueError(f"unknown phase4 product: {product_name}")
    workspace = Path(tempfile.mkdtemp(prefix="phase4-six-wheel-")).resolve()
    _KEEP.append(workspace)
    product_root, fixtures, wheel = _materialize_fixture_distribution(workspace)
    bindings_root = _bindings_for(product_name, fixtures)
    plugins = _editable_wheel_plugins(workspace)
    _scrub_generated(product_root)
    if mutate is not None:
        mutate(workspace, product_root, fixtures)
        plugins = _editable_wheel_plugins(workspace)
        refresh_product_declarations(product_root)
        _scrub_generated(product_root)
        bindings_root = _bindings_for(product_name, fixtures)
    overlay = _product_metadata(workspace)
    if product_name != "phase4-opencode":
        raise ValueError(f"unsupported phase4 product: {product_name}")
    with _import_activation(product_root, workspace):
        composition = RegistryPlatform(metadata_provider=overlay).resolve(
            ResolutionRequest(
                product=EditableWheelProductSource(
                    distribution=PRODUCT_DISTRIBUTION,
                    entrypoint_name=product_name,
                    declaration_path=PRODUCT_DECLARATIONS[product_name],
                    source_root=product_root,
                    source_files=_source_files(product_root),
                ),
                plugins=(
                    *plugins,
                    ConfigTreePluginSource(path=bindings_root),
                ),
            )
        )
    return SixWheelComposition(
        composition=composition,
        product_name=product_name,
        fixture_wheel=wheel,
        product_root=product_root,
        bindings_root=bindings_root,
        workspace=workspace,
    )


_T = TypeVar("_T")
_PREPARE_ID = "test.assurance.bindings.prepare"
_EXECUTE_ID = "test.assurance.bindings.execute"
_FINALIZE_ID = "test.assurance.bindings.finalize"


class _Phase4NodeInput(BaseModel):
    model_config = {"extra": "allow"}


class _Phase4NodeOutput(BaseModel):
    ok: bool = True


class _RecordingExecuteExecutor:
    def __init__(self, host: "SixWheelTaskHost") -> None:
        self.host = host

    async def execute(
        self, validated_input: _Phase4NodeInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_Phase4NodeOutput]:
        del validated_input, scope
        request = _fixture_agent_request()
        self.host.recorded_request_bytes = request.canonical_bytes()
        self.host.provider_state_dir.mkdir(parents=True, exist_ok=True)
        (self.host.provider_state_dir / "session.json").write_bytes(
            json.dumps({"adapter_id": self.host.adapter_id, "state": "open"}).encode("utf-8")
        )
        FakeAgentAdapter(
            CASE_REVIEW_STRUCTURED,
            adapter_id=self.host.adapter_id,
            adapter_version="1.0.0",
        ).execute_request(request)
        return ExecutedAttemptResult(output=_Phase4NodeOutput())


class _PassExecutor:
    async def execute(
        self, validated_input: _Phase4NodeInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_Phase4NodeOutput]:
        del validated_input, scope
        return ExecutedAttemptResult(output=_Phase4NodeOutput())


class _CallableExecutor:
    """Wrap a leftover-style execute function as an AttemptExecutor."""

    def __init__(self, fn: object) -> None:
        self._fn = fn

    async def execute(
        self, validated_input: _Phase4NodeInput, scope: AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[_Phase4NodeOutput]:
        result = self._fn(validated_input, scope)  # type: ignore[operator]
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        if isinstance(result, ExecutedAttemptResult):
            return result
        return ExecutedAttemptResult(output=cast(_Phase4NodeOutput, result))


def _as_executor(value: object | None, default: object) -> object:
    if value is None:
        return default
    if inspect.isfunction(value) or inspect.ismethod(value):
        return _CallableExecutor(value)
    return value


def _fixture_agent_request() -> AgentRunRequest:
    template = binding_data_template()
    execution = dict(template["execution"])  # type: ignore[arg-type]
    workspace_payload = {
        "schema_version": "1",
        "agent_profile": "assurance-v1-doc-author",
        "scope_id": "CH-DEMO-001",
        "write_root": "qa/changes/CH-DEMO-001/.staging/attempt-1",
        "allowed_outputs": ["review.json"],
    }
    workspace_payload["identity_digest"] = canonical_digest(workspace_payload)
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "review"),),
        result_contract={
            "schema_id": "fixture.result.v1",
            "schema_digest": hashlib_sha256(b'{"type":"object"}'),
            "delivery_mode": "assistant_json_local_v1",
        },  # type: ignore[arg-type]
        execution=execution,  # type: ignore[arg-type]
        workspace=workspace_payload,  # type: ignore[arg-type]
        request_policy_digest=str(template["request_policy_digest"]),
        request_config_digest=str(template["request_config_digest"]),
    )


def _phase4_contract(contract_id: str) -> TaskAttemptContract[_Phase4NodeInput, _Phase4NodeOutput]:
    return TaskAttemptContract(
        contract_id=contract_id,
        owner_id="test.assurance.bindings",
        handler_id=contract_id,
        input_model=_Phase4NodeInput,
        output_model=_Phase4NodeOutput,
        resources=ResourceClaims(writes=()),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )


def _phase4_resolver(
    host: "SixWheelTaskHost",
    *,
    execute: object | None = None,
    finalize: object | None = None,
) -> CatalogContractResolver:
    contracts = {
        _PREPARE_ID: _phase4_contract(_PREPARE_ID),
        _EXECUTE_ID: _phase4_contract(_EXECUTE_ID),
        _FINALIZE_ID: _phase4_contract(_FINALIZE_ID),
    }
    executors: dict[str, ResolvedAttemptContract[Any, Any]] = {
        _PREPARE_ID: resolve_contract(contracts[_PREPARE_ID], executor=_PassExecutor()),
        _EXECUTE_ID: resolve_contract(
            contracts[_EXECUTE_ID],
            executor=_as_executor(execute, _RecordingExecuteExecutor(host)),  # type: ignore[arg-type]
        ),
        _FINALIZE_ID: resolve_contract(
            contracts[_FINALIZE_ID],
            executor=_as_executor(finalize, _PassExecutor()),  # type: ignore[arg-type]
        ),
    }
    return CatalogContractResolver(contracts, executors)


def _application_call(fn: Callable[..., _T], *args: object, **kwargs: object) -> _T:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


_engine_call = _application_call


def _seed_fixture_project(project_root: Path) -> None:
    matrix = [
        {
            "mrc_id": "MRC-API-001",
            "key": "auth.session.create",
            "required": True,
            "covered_by_cases": ["TC_MENU_001"],
            "status": "covered",
            "skip_reason": None,
            "category": "api",
            "layer": "api",
        },
        {
            "mrc_id": "MRC-API-002",
            "key": "entities.item.create",
            "required": True,
            "covered_by_cases": ["TC_MENU_002"],
            "status": "covered",
            "skip_reason": None,
            "category": "api",
            "layer": "api",
        },
    ]
    change_root = project_root / "qa/changes/CH-DEMO-001"
    fixture_inputs = {
        change_root / ".qa.yaml": "schema_version: '1.0'\nchange_id: CH-DEMO-001\n",
        change_root / "proposal.md": "# Fixture case proposal\n",
        change_root / "cases/system/menu/case.yaml": (
            "schema_version: '1.0'\n"
            "added:\n"
            "  - case_id: TC_MENU_001\n"
            "    title: Create menu item\n"
            "    type: API\n"
            "modified: []\n"
            "removed: []\n"
        ),
    }
    for path, content in fixture_inputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    matrix_path = change_root / "trace/minimum-coverage-matrix.json"
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    if not matrix_path.exists():
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    source = project_root / "src" / "app.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.write_text("def create_item():\n    return None\n", encoding="utf-8")


def _workspace_binding(engine_root: Path):
    engine_root.mkdir(parents=True, exist_ok=True)
    workspace, project_root = workspace_provider_for(engine_root)
    _seed_fixture_project(project_root)
    return workspace, project_root


def _boot_application(
    composition: FrozenComposition,
    host: SixWheelTaskHost,
    engine_root: Path,
    *,
    checkpointer: MemoryCheckpointer | None = None,
    execute: object | None = None,
    finalize: object | None = None,
) -> tuple[AssuranceApplication, BootArtifact, InvocationBoundExecutionFactory, MemoryCheckpointer, Path]:
    workspace, project_root = _workspace_binding(engine_root)
    saver = MemoryCheckpointer() if checkpointer is None else checkpointer
    artifact, kernel = boot_factory_product(
        composition,
        workspace=workspace,
        contract_resolver=_phase4_resolver(host, execute=execute, finalize=finalize),
        checkpointer=saver,
    )
    application, factory = factory_application(
        artifact,
        kernel=kernel,
        workspace=workspace,
        lease_root=engine_root / "leases",
    )
    return application, artifact, factory, saver, project_root


def _start_until_blocked(
    engine_root: Path,
    host: SixWheelTaskHost,
    composition: FrozenComposition,
    invocation_id: str,
    *,
    checkpointer: MemoryCheckpointer | None = None,
    execute: object | None = None,
    finalize: object | None = None,
) -> tuple[InvocationStatus, Path, AssuranceApplication, object, object, MemoryCheckpointer]:
    application, artifact, context, saver, _project = _boot_application(
        composition,
        host,
        engine_root,
        checkpointer=checkpointer,
        execute=execute,
        finalize=finalize,
    )

    async def _run() -> InvocationStatus:
        return await application.start_and_run(
            invocation_id=invocation_id,
            entrypoint="fixture",
            graph_input={"change_id": "CH-DEMO-001"},
            execution_factory=context,
        )

    result = asyncio.run(_run())
    return result, engine_root / "leases", application, artifact, context, saver


def _open_until_blocked(
    engine_root: Path,
    host: SixWheelTaskHost,
    composition: FrozenComposition,
    invocation_id: str,
    *,
    application: AssuranceApplication,
    artifact: object,
    runtime_context: object,
) -> InvocationStatus:
    del engine_root, host, composition

    async def _run() -> InvocationStatus:
        return await application.run(
            invocation_id=invocation_id,
            execution_factory=runtime_context,  # type: ignore[arg-type]
        )

    return asyncio.run(_run())


async def run_fixture(product_name: str) -> SixWheelRun:
    resolved = resolve_fixture(product_name)
    provider_state = resolved.product_root.parent / f"{product_name}-provider-state"
    host = SixWheelTaskHost(adapter_id=ADAPTER_IDS[product_name], provider_state_dir=provider_state)
    engine_root = resolved.product_root.parent / f"{product_name}-engine"
    invocation_id = f"{product_name}-inv"
    with _import_activation(resolved.product_root, resolved.workspace):
        result, invocation_root, application, artifact, context, saver = _application_call(
            _start_until_blocked,
            engine_root,
            host,
            resolved.composition,
            invocation_id,
        )
    if result.status != "completed":
        raise AssertionError(f"{product_name} fixture run failed: {result}")
    if host.recorded_request_bytes is None:
        raise AssertionError(f"{product_name} host did not observe a pre-adapter AgentRunRequest")
    values = await invocation_values(artifact, invocation_id, "fixture")  # type: ignore[arg-type]
    return SixWheelRun(
        product_name=product_name,
        composition=resolved.composition,
        agent_request_bytes=host.recorded_request_bytes,
        business_output_bytes=canonical_json_bytes(cast(JSONValue, dict(values))),
        adapter_id=host.adapter_id,
        lock_digest=resolved.lock_digest,
        composition_digest=resolved.digest,
        invocation_root=invocation_root,
        engine_root=engine_root,
        provider_state_dir=provider_state,
        fixture_wheel=resolved.fixture_wheel,
        product_root=resolved.product_root,
        workspace=resolved.workspace,
        application=application,
        artifact=artifact,
        runtime_context=context,
        checkpointer=saver,
        invocation_id=invocation_id,
    )


def replay_after_deleting_provider_state(run: SixWheelRun) -> None:
    if run.provider_state_dir.exists():
        shutil.rmtree(run.provider_state_dir)
    host = SixWheelTaskHost(adapter_id=run.adapter_id, provider_state_dir=run.provider_state_dir)
    if run.application is None or run.artifact is None or run.runtime_context is None:
        raise AssertionError("replay requires a started Application")
    with _import_activation(run.product_root, run.workspace):
        result = _application_call(
            _open_until_blocked,
            run.engine_root,
            host,
            run.composition,
            run.invocation_id,
            application=run.application,
            artifact=run.artifact,
            runtime_context=run.runtime_context,
        )
    if result.status != "completed":
        raise AssertionError(f"{run.product_name} replay failed: {result}")
    if host.recorded_request_bytes is not None:
        raise AssertionError("replay must not re-enter the fake provider")
    values = _application_call(
        lambda: asyncio.run(invocation_values(run.artifact, run.invocation_id, "fixture"))  # type: ignore[arg-type]
    )
    assert canonical_json_bytes(cast(JSONValue, dict(values))) == run.business_output_bytes


def invocation_files_contain_provider_transcript(invocation_root: Path) -> bool:
    markers = (b"SessionEvent", b"session_event", b"provider_message", b'"transcript"')
    for path in invocation_root.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if any(marker in payload for marker in markers):
            return True
    return False


def production_product_entrypoints() -> frozenset[str]:
    return frozenset(entry.name for entry in metadata.entry_points(group="graph_engine.products"))


def production_aa_products() -> frozenset[str]:
    return frozenset(entry.name for entry in metadata.entry_points(group="assurance_agent.products"))


def _materialize_fixture_distribution(workspace: Path) -> tuple[Path, Path, Path]:
    fixtures = workspace / "fixtures"
    shutil.copytree(FIXTURE_ROOT, fixtures, ignore=_IGNORE)
    product_root = fixtures / "six-wheel-product"
    refresh_product_declarations(product_root)
    dist_dir = workspace / "dist"
    dist_dir.mkdir()
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=product_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        raise AssertionError(f"fixture wheel build failed:\n{built.stdout}\n{built.stderr}")
    wheels = tuple(sorted(dist_dir.glob("test_assurance_phase4_product-*.whl")))
    if len(wheels) != 1:
        raise AssertionError(f"expected one fixture wheel in {dist_dir}, found {wheels}")
    _scrub_generated(product_root)
    return product_root, fixtures, wheels[0]


def _bindings_for(product_name: str, fixtures: Path) -> Path:
    return fixtures / ("bindings-opencode" if product_name == "phase4-opencode" else "bindings-cursor")


def _activate_product_imports(product_root: Path) -> None:
    sys.path.insert(0, str(product_root))
    for name in tuple(sys.modules):
        if name == "test_assurance_phase4_product" or name.startswith("test_assurance_phase4_product."):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _product_metadata(workspace: Path) -> _OverlayMetadata:
    dist_info = workspace / "meta" / "test_assurance_phase4_product-1.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: test-assurance-phase4-product\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[graph_engine.products]\n"
        "phase4-opencode = test_assurance_phase4_product.product:Phase4OpenCodeProduct\n"
        "phase4-cursor = test_assurance_phase4_product.product:Phase4CursorProduct\n",
        encoding="utf-8",
    )
    return _OverlayMetadata({PRODUCT_DISTRIBUTION: metadata.Distribution.at(dist_info)})


def _activate_wheel_imports(workspace: Path) -> None:
    prefixes = tuple(package for _distribution, package, _entrypoint, _declaration in WHEEL_PLUGINS)
    for distribution, _package, _entrypoint, _declaration in reversed(WHEEL_PLUGINS):
        sys.path.insert(0, str(workspace / "wheels" / distribution))
    for name in tuple(sys.modules):
        if name in prefixes or name.startswith(tuple(f"{prefix}." for prefix in prefixes)):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _editable_wheel_plugins(workspace: Path) -> tuple[EditableWheelPluginSource, ...]:
    plugins: list[EditableWheelPluginSource] = []
    wheels = workspace / "wheels"
    wheels.mkdir(exist_ok=True)
    for distribution, package, entrypoint_name, declaration_path in WHEEL_PLUGINS:
        if distribution.startswith("agent-runtime-"):
            group = "adapters"
        elif distribution.startswith("assurance-"):
            group = "capabilities"
        else:
            group = None
        source = (
            REPO_ROOT / "packages" / group / distribution / package
            if group is not None
            else REPO_ROOT / "packages" / distribution / package
        )
        dest = wheels / distribution
        if not dest.exists():
            shutil.copytree(source, dest / package, ignore=_IGNORE)
        plugins.append(
            EditableWheelPluginSource(
                distribution=distribution,
                entrypoint_name=entrypoint_name,
                declaration_path=declaration_path,
                source_root=dest,
                source_files=_source_files(dest),
            )
        )
    return tuple(plugins)


@contextmanager
def _import_activation(product_root: Path, workspace: Path) -> Iterator[None]:
    path_snapshot = list(sys.path)
    module_snapshot = sys.modules.copy()
    try:
        _activate_product_imports(product_root)
        _activate_wheel_imports(workspace)
        yield
    finally:
        sys.path[:] = path_snapshot
        for name in tuple(sys.modules):
            if name not in module_snapshot and not name.startswith("pydantic"):
                del sys.modules[name]
        sys.modules.update(module_snapshot)
        importlib.invalidate_caches()


def _scrub_generated(root: Path) -> None:
    generated_dirs = [
        path
        for path in root.rglob("*")
        if path.is_dir()
        and (path.name == "__pycache__" or path.name.endswith(".egg-info") or path.name in {"dist", "build"})
    ]
    for path in generated_dirs:
        shutil.rmtree(path, ignore_errors=True)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".pyc", ".pyo"}:
            path.unlink(missing_ok=True)


def _source_files(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()))
