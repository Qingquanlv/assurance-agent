from __future__ import annotations

import json
from collections.abc import Mapping
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
from graph_engine.runtime.secret_sources import InvocationRuntimeAuthorization
from graph_engine.runtime.workspace import SnapshotStore

from tests.phase5.composition_harness import InstalledSources
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
        capability_catalog=_first_resource_ref(
            composition,
            "assurance.product.configuration.capability-catalog",
            "assurance.product.configuration.project-config",
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
        self._store: SnapshotStore | None = None
        self._receipts: TerminalReceiptStore | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: SnapshotStore,
        receipts: TerminalReceiptStore | None = None,
    ) -> None:
        self._handlers = handlers
        self._store = store
        if receipts is not None:
            self._receipts = receipts

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        outcome = self._outcome(call.request.capability_id)
        if call.activity_rpc.activity_id is not None and self._store is not None:
            ledger = Ledger(self._store.root.parent / "ledger")
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
        if self._receipts is None or identity.activity_id is None:
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
                workspace_identity_digest=activity.workspace_identity.attempt_identity_digest,
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


def common_lifecycle_args(
    *,
    tmp_path: Path,
    installed_sources: InstalledSources,
    composition: FrozenComposition,
    invocation_id: str,
    entrypoint: str = "intake",
    families: tuple[str, ...] = (),
    extra: Mapping[str, object] | None = None,
) -> tuple[list[str], Path, Path]:
    project_dir = write_project_dir(tmp_path / "project")
    engine_root = tmp_path / "engine-root"
    engine_root.mkdir()
    overrides = {"selected_test_families": families, **dict(extra or {})}
    input_path = write_product_input(
        tmp_path / "input.json",
        composition,
        **overrides,
    )
    args = [
        "--project-dir",
        str(project_dir),
        "--engine-root",
        str(engine_root),
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
    return args, project_dir, engine_root
