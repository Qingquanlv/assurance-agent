"""Test-only six-wheel composition and OpenCode/Cursor rebinding harness."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, TypeVar, cast

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from graph_engine import Engine
from graph_engine.canonical import JSONValue
from graph_engine.composition import (
    ConfigTreePluginSource,
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)
from graph_engine.runtime.engine import RunResult
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
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
    (
        "agent-runtime-cursor",
        "agent_runtime_cursor",
        "cursor",
        "agent_runtime_cursor/plugin-declaration.json",
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
    PRODUCT_PACKAGE / "workflow.yaml",
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
        workflow_raw = __import__("yaml").safe_load(
            (package_root / "workflow.yaml").read_text(encoding="utf-8")
        )
        for cls, filename in (
            (module.Phase4OpenCodeProduct, "product-opencode-declaration.json"),
            (module.Phase4CursorProduct, "product-cursor-declaration.json"),
        ):
            manifest = cls.manifest()
            source = manifest.source
            if source is None:
                raise ValueError("phase4 product source must be declared")
            dumped = manifest.model_dump(mode="json", by_alias=True, exclude_none=True)
            dumped["workflow"] = workflow_raw
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


class _OverlayMetadata:
    def __init__(self, overlay: Mapping[str, metadata.Distribution]) -> None:
        self._overlay = dict(overlay)

    def distribution(self, distribution_name: str) -> metadata.Distribution:
        try:
            return self._overlay[distribution_name]
        except KeyError:
            return metadata.distribution(distribution_name)


class SixWheelTaskHost:
    """Substitutes deterministic adapter fakes at the engine host seam."""

    def __init__(self, *, adapter_id: str, provider_state_dir: Path) -> None:
        self.adapter_id = adapter_id
        self.provider_state_dir = provider_state_dir
        self.recorded_request_bytes: bytes | None = None
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: Any = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: object,
        receipts: object | None = None,
    ) -> None:
        del receipts
        self._handlers = handlers
        self._store = store

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        request = call.request.model_copy(update={"input": _unwrap_engine_input(call)})
        workspace_root = Path(self._store.root) / "attempts" / call.attempt_root.attempt_directory_id
        workspace_root.mkdir(parents=True, exist_ok=True)
        if call.capability_id in RUNTIME_EXECUTE.values():
            outcome = self._execute_fake(request.input)
            return TaskHostCallResult(operation="execute", outcome=outcome)
        handler = self._handlers[call.request.capability_id]
        outcome = await handler.execute(
            request,
            TaskContext(
                workspace_root=workspace_root,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)

    def _execute_fake(self, payload: JSONValue) -> TaskOutcome:
        agent_request = AgentRunRequest.model_validate(payload)
        self.recorded_request_bytes = agent_request.canonical_bytes()
        self.provider_state_dir.mkdir(parents=True, exist_ok=True)
        (self.provider_state_dir / "session.json").write_bytes(
            json.dumps({"adapter_id": self.adapter_id, "state": "open"}).encode("utf-8")
        )
        result = FakeAgentAdapter(
            CASE_REVIEW_STRUCTURED,
            adapter_id=self.adapter_id,
            adapter_version="1.0.0",
        ).execute_request(agent_request)
        return TaskOutcome.succeeded(result.model_dump(mode="json"))

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(status="indeterminate", reason="test host"),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        del call
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(status="indeterminate", reason="test host"),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


def resolve_fixture(product_name: str) -> SixWheelComposition:
    require_six_wheel_fixtures()
    if product_name not in PRODUCT_NAMES:
        raise ValueError(f"unknown phase4 product: {product_name}")
    workspace = Path(tempfile.mkdtemp(prefix="phase4-six-wheel-")).resolve()
    _KEEP.append(workspace)
    product_root, fixtures, wheel = _materialize_fixture_distribution(workspace)
    bindings_root = _bindings_for(product_name, fixtures)
    plugins = _editable_wheel_plugins(workspace)
    _scrub_generated(product_root)
    overlay = _product_metadata(workspace)
    if product_name == "phase4-opencode":
        plugins = tuple(plugin for plugin in plugins if plugin.distribution != "agent-runtime-cursor")
    else:
        plugins = tuple(plugin for plugin in plugins if plugin.distribution != "agent-runtime-opencode")
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


def _engine_call(fn: Callable[..., _T], *args: object) -> _T:
    # Engine.run_until_blocked uses asyncio.run; keep it off the pytest loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


def _start_until_blocked(
    engine_root: Path,
    host: SixWheelTaskHost,
    composition: FrozenComposition,
    invocation_id: str,
) -> tuple[RunResult, Path]:
    with Engine(engine_root, host=host) as engine:
        with engine.start(composition, entrypoint="fixture", invocation_id=invocation_id) as handle:
            result = engine.run_until_blocked(handle)
            return result, handle.invocation_root


def _open_until_blocked(
    engine_root: Path,
    host: SixWheelTaskHost,
    composition: FrozenComposition,
    invocation_id: str,
) -> RunResult:
    with Engine(engine_root, host=host) as engine:
        with engine.open(invocation_id, composition) as handle:
            return engine.run_until_blocked(handle)


async def run_fixture(product_name: str) -> SixWheelRun:
    resolved = resolve_fixture(product_name)
    provider_state = resolved.product_root.parent / f"{product_name}-provider-state"
    host = SixWheelTaskHost(adapter_id=ADAPTER_IDS[product_name], provider_state_dir=provider_state)
    engine_root = resolved.product_root.parent / f"{product_name}-engine"
    invocation_id = f"{product_name}-inv"
    with _import_activation(resolved.product_root, resolved.workspace):
        result, invocation_root = _engine_call(
            _start_until_blocked,
            engine_root,
            host,
            resolved.composition,
            invocation_id,
        )
    if result.status != "succeeded":
        raise AssertionError(f"{product_name} fixture run failed: {result}")
    if host.recorded_request_bytes is None:
        raise AssertionError(f"{product_name} host did not observe a pre-adapter AgentRunRequest")
    return SixWheelRun(
        product_name=product_name,
        composition=resolved.composition,
        agent_request_bytes=host.recorded_request_bytes,
        business_output_bytes=canonical_json_bytes(cast(JSONValue, thaw_json(result.output))),
        adapter_id=host.adapter_id,
        lock_digest=resolved.lock_digest,
        composition_digest=resolved.digest,
        invocation_root=invocation_root,
        engine_root=engine_root,
        provider_state_dir=provider_state,
        fixture_wheel=resolved.fixture_wheel,
        product_root=resolved.product_root,
        workspace=resolved.workspace,
    )


def replay_after_deleting_provider_state(run: SixWheelRun) -> None:
    if run.provider_state_dir.exists():
        shutil.rmtree(run.provider_state_dir)
    host = SixWheelTaskHost(adapter_id=run.adapter_id, provider_state_dir=run.provider_state_dir)
    with _import_activation(run.product_root, run.workspace):
        result = _engine_call(
            _open_until_blocked,
            run.engine_root,
            host,
            run.composition,
            f"{run.product_name}-inv",
        )
    if result.status != "succeeded":
        raise AssertionError(f"{run.product_name} replay failed: {result}")
    if host.recorded_request_bytes is not None:
        raise AssertionError("replay must not re-enter the fake provider")
    assert canonical_json_bytes(cast(JSONValue, thaw_json(result.output))) == run.business_output_bytes


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


def _unwrap_engine_input(call: TaskHostExecuteCall) -> JSONValue:
    raw = thaw_json(call.request.input)
    if not isinstance(raw, dict) or "config" not in raw or "tokens" not in raw:
        return raw
    config = raw["config"]
    tokens = raw["tokens"]
    prior = tokens[0] if isinstance(tokens, list) and tokens else None
    if call.capability_id in RUNTIME_EXECUTE.values():
        return prior if prior is not None else config
    if str(call.capability_id).endswith(".finalize"):
        merged = dict(config) if isinstance(config, dict) else {}
        merged["agent_result"] = prior
        return merged
    return config if config is not None else {}


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
        source = REPO_ROOT / "packages" / distribution / package
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
            if name not in module_snapshot:
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
