from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import FrozenComposition
from graph_engine.plugin_api import TaskActivitySnapshot, TaskHandler, TaskOutcome
from graph_engine.runtime.activity import LedgerTaskActivityPort
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostExecuteCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.host_receipts import TerminalReceiptStore, prove_call_quiescent
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    SecretSourceBinding,
    runtime_authorization_digest,
)
from graph_engine.runtime.seed import empty_invocation_seed

from tests.phase5.composition_harness import InstalledSources, request_for
from tests.phase5.product_runner import _ScriptedTaskHost
from tests.phase5.test_product_input import valid_product_input

SECRET_HANDLE = "opencode.token"
SECRET_ENV = "AA_NEXT_OPENCODE_TOKEN"
SECRET_VALUE = "runtime-only-token"
_DISPATCH_FINGERPRINT: JSONValue = {"endpoint": "https://127.0.0.1:1", "profile": "aa-next-test"}
_ACTIVITY_REFERENCE: JSONValue = {"id": "scripted-host"}


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def command_names(help_text: str) -> set[str]:
    names: set[str] = set()
    in_commands = False
    for line in help_text.splitlines():
        stripped = line.strip()
        if stripped == "Commands:":
            in_commands = True
            continue
        if in_commands:
            if not stripped:
                break
            names.add(stripped.split()[0])
    return names


def nested_command_names(cli_runner: CliRunner, app: Any, group: str) -> set[str]:
    result = cli_runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0, result.output
    return command_names(result.stdout)


def source_args(installed_sources: InstalledSources, adapter: str = "opencode") -> list[str]:
    deployment = installed_sources.deployments[adapter]
    return [
        "--product",
        f"assurance-{adapter}",
        "--binding-dist",
        deployment.distribution,
        "--binding-entrypoint",
        "deployment",
        "--binding-declaration",
        deployment.declaration_path,
        "--config-tree",
        str(installed_sources.configuration_tree.path),
    ]


def ref_from_composition(composition: FrozenComposition, resource_id: str) -> dict[str, str]:
    entry = composition.registries.resources.entries[resource_id]
    return {"resource_id": resource_id, "sha256": entry.sha256}


def _first_resource_ref(composition: FrozenComposition, *resource_ids: str) -> dict[str, str]:
    for resource_id in resource_ids:
        if resource_id in composition.registries.resources.entries:
            return ref_from_composition(composition, resource_id)
    raise KeyError(resource_ids)


def write_product_input(
    path: Path,
    composition: FrozenComposition,
    **overrides: object,
) -> Path:
    payload = valid_product_input(
        capability_catalog=ref_from_composition(
            composition,
            "assurance.product.configuration.capability-catalog",
        ),
        product_policy=_first_resource_ref(
            composition,
            "assurance.product.configuration.product-policy",
        ),
        data_knowledge=_first_resource_ref(
            composition,
            "assurance.product.configuration.data-knowledge",
        ),
        **overrides,
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_project_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("minimal SUT seed\n", encoding="utf-8")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return path


def parse_json_output(output: str) -> dict[str, Any]:
    text = output.strip()
    return json.loads(text)


class _CompletingScriptedHost(_ScriptedTaskHost):
    """Scripted host that also finalizes recoverable runtime execute activities."""

    def __init__(
        self,
        *,
        execution_sequence: tuple[str, ...],
        coverage_sequence: tuple[float, ...],
        threshold: float,
        coverage_rounds: int,
        review_decision: str,
        healing_decision: str,
    ) -> None:
        super().__init__(
            execution_sequence=execution_sequence,
            coverage_sequence=coverage_sequence,
            threshold=threshold,
            coverage_rounds=coverage_rounds,
            review_decision=review_decision,
            healing_decision=healing_decision,
        )
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: object | None = None
        self._receipts: TerminalReceiptStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: object,
        receipts: TerminalReceiptStore | None = None,
        handler_import_roots: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        del handler_import_roots
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    def _invocation_ledger(self, invocation_id: str) -> Ledger | None:
        store = self._store
        if store is None:
            return None
        snapshot_root = getattr(store, "root", None)
        if snapshot_root is not None:
            return Ledger(Path(snapshot_root).parent / "ledger")
        receipts_root = getattr(store, "receipts_root", None)
        if receipts_root is not None:
            return Ledger(Path(receipts_root).parent / "invocations" / invocation_id / "ledger")
        return None

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        outcome = self._outcome(call.request.capability_id)
        if call.request.capability_id.startswith(
            "assurance.product.agent."
        ) and call.request.capability_id.endswith(".prepare"):
            input_value = call.request.input
            if not isinstance(input_value, Mapping) or not isinstance(input_value.get("change_id"), str):
                raise AssertionError("scripted agent prepare requires a change_id")
            if not isinstance(outcome.output, Mapping):
                raise AssertionError("scripted agent prepare requires an object output")
            outcome = outcome.model_copy(
                update={
                    "output": {
                        **outcome.output,
                        "workspace": {"scope_id": input_value["change_id"]},
                    }
                }
            )
        ledger = self._invocation_ledger(call.identity.invocation_id)
        if call.activity_rpc.activity_id is not None and ledger is not None:
            port = LedgerTaskActivityPort(ledger=ledger, identity=call.activity_rpc)
            port.mark_dispatch_started(_DISPATCH_FINGERPRINT)
            port.bind(_ACTIVITY_REFERENCE)
            self._install_receipt(call.identity, port.snapshot, outcome)
        return TaskHostCallResult(operation="execute", outcome=outcome)

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._receipts is None:
            return ()
        return self._receipts.authenticate(identity)

    def _install_receipt(
        self,
        identity: TaskHostCallIdentity,
        activity: TaskActivitySnapshot,
        outcome: TaskOutcome,
    ) -> None:
        if self._receipts is None or identity.activity_id is None or self._store is None:
            return
        seal = getattr(self._store, "seal", None)
        if seal is None:
            return
        try:
            staged_digest = seal(activity.workspace_identity).staged_digest
        except Exception:
            return
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
                baseline_digest=canonical_digest(
                    [item.model_dump(mode="json") for item in activity.workspace_identity.baseline_files]
                ),
                staged_write_set_digest=staged_digest,
                dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                reference_digest=activity.reference_digest,
                outcome=outcome,
                outcome_digest=canonical_digest(cast(JSONValue, outcome.model_dump(mode="json"))),
                terminal_proof_digest=None,
                quiescence_proof_digest=prove_call_quiescent(),
                host_call_id=sink.host_call_id,
            )
        )


def scripted_engine_factory(
    *,
    review_decision: str = "pass",
    healing_decision: str = "allowed",
):
    def factory(root: Path, authorization: InvocationRuntimeAuthorization) -> Engine:
        del authorization
        return Engine(
            root,
            host=_CompletingScriptedHost(
                execution_sequence=(),
                coverage_sequence=(),
                threshold=0.90,
                coverage_rounds=1,
                review_decision=review_decision,
                healing_decision=healing_decision,
            ),
        )

    return factory


@dataclass
class LifecycleInvocation:
    engine: Engine
    id: str
    authorization: InvocationRuntimeAuthorization
    lock_digest: str
    composition: FrozenComposition
    project_dir: Path
    change_id: str
    engine_root: Path


def lifecycle_authorization() -> InvocationRuntimeAuthorization:
    sources = (
        SecretSourceBinding(
            handle=SECRET_HANDLE,
            source_kind="environment",
            source_locator=SECRET_ENV,
        ),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=sources,
        digest=runtime_authorization_digest(sources),
    )


def start_lifecycle_invocation(
    tmp_path: Path,
    installed_sources: InstalledSources,
    *,
    invocation_id: str,
    drive: bool = False,
    require_succeeded: bool = True,
    entrypoint: str = "intake",
    change_id: str = "CH-DEMO-001",
    families: tuple[str, ...] = (),
    extra_project_files: Mapping[str, str] | None = None,
    host_factory=None,
) -> LifecycleInvocation:
    from assurance_product.models import ProductInputV1
    from assurance_product.product import prepare_change_workspace, resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    project_dir = write_project_dir(tmp_path / "project")
    for relative, content in dict(extra_project_files or {}).items():
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    input_path = write_product_input(
        tmp_path / "input.json",
        composition,
        selected_test_families=families,
        change_id=change_id,
    )
    product_input = ProductInputV1.model_validate_json(input_path.read_text(encoding="utf-8"))
    root_input = cast(JSONValue, product_input.model_dump(mode="json"))
    seed = empty_invocation_seed(root_input=root_input)
    authorization = lifecycle_authorization()
    workspace = prepare_change_workspace(project_dir, change_id)
    factory = scripted_engine_factory() if host_factory is None else host_factory
    engine = factory(workspace.paths.runtime_root, authorization)
    handle = engine.start(
        composition,
        entrypoint=entrypoint,
        invocation_id=invocation_id,
        seed=seed,
        authorization=authorization,
        workspace_binding=workspace.runtime_binding(),
    )
    try:
        if drive:
            result = engine.run_until_blocked(handle)
            if require_succeeded and result.status != "succeeded":
                raise AssertionError(f"expected succeeded lifecycle, got {result.status}")
    finally:
        handle.close()
    return LifecycleInvocation(
        engine=engine,
        id=invocation_id,
        authorization=authorization,
        lock_digest=composition.lock_digest,
        composition=composition,
        project_dir=project_dir,
        change_id=change_id,
        engine_root=workspace.paths.runtime_root,
    )


@pytest.fixture
def completed_invocation(
    tmp_path: Path,
    installed_sources: InstalledSources,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LifecycleInvocation]:
    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    invocation = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-export-completed",
        drive=True,
    )
    try:
        yield invocation
    finally:
        invocation.engine.close()


def common_lifecycle_args(
    *,
    tmp_path: Path,
    installed_sources: InstalledSources,
    composition: FrozenComposition,
    invocation_id: str,
    entrypoint: str = "intake",
    change_id: str = "CH-DEMO-001",
    families: tuple[str, ...] = (),
    extra: Mapping[str, object] | None = None,
) -> tuple[list[str], Path, str]:
    project_dir = write_project_dir(tmp_path / "project")
    overrides = {"selected_test_families": families, "change_id": change_id, **dict(extra or {})}
    input_path = write_product_input(
        tmp_path / "input.json",
        composition,
        **overrides,
    )
    args = [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args(installed_sources),
        "--entrypoint",
        entrypoint,
        "--input",
        str(input_path),
        "--secret",
        f"{SECRET_HANDLE}=env:{SECRET_ENV}",
        "--json",
    ]
    return args, project_dir, change_id
