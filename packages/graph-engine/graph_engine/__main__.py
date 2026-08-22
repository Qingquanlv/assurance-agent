from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import sys
from collections.abc import Mapping, Sequence
from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import (
    FrozenComposition,
    PluginSource,
    ProductSource,
    RegistryPlatform,
    ResolutionRequest,
    WheelPluginSource,
    WheelProductSource,
)
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    RecoverableTaskHandler,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.engine import Engine, RunResult
from graph_engine.runtime.host_protocol import (
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.ledger import Ledger


class _CliSourceError(GraphEngineError):
    """Raised when explicit distribution coordinates do not select one source."""


class _TrustedWheelPluginHost:
    """Run explicitly selected, trusted wheel plugins in-process.

    This command-boundary adapter is deliberately not an OS sandbox and is not
    a production host. Engine itself remains fail-closed when no execution host
    is supplied.
    """

    def __init__(self) -> None:
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: object | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: object,
    ) -> None:
        if any(isinstance(handler, RecoverableTaskHandler) for handler in handlers.values()):
            raise GraphEngineError("CLI wheel host refuses recoverable handlers")
        self._handlers = handlers
        self._store = store

    def _refuse_recoverable(self, handler: TaskHandler, operation: str) -> TaskHostCallResult | None:
        if not isinstance(handler, RecoverableTaskHandler):
            return None
        if operation == "execute":
            return TaskHostCallResult(
                operation="execute",
                outcome=TaskOutcome.failed("internal", "CLI wheel host refuses recoverable handlers"),
            )
        if operation == "reconcile":
            return TaskHostCallResult(
                operation="reconcile",
                reconcile_result=TaskActivityReconcileResult(
                    status="indeterminate",
                    reason="CLI wheel host refuses recoverable handlers",
                ),
            )
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(
                status="indeterminate",
                reason="CLI wheel host refuses recoverable handlers",
            ),
        )

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        refused = self._refuse_recoverable(handler, "execute")
        if refused is not None:
            return refused
        workspace_root = Path(self._store.root) / "attempts" / call.attempt_root.attempt_directory_id  # type: ignore[attr-defined]
        outcome = await handler.execute(
            call.request,
            TaskContext(
                workspace_root=workspace_root,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        handler = self._handlers.get(call.request.capability_id)
        if handler is not None:
            refused = self._refuse_recoverable(handler, "reconcile")
            if refused is not None:
                return refused
        return TaskHostCallResult(
            operation="reconcile",
            reconcile_result=TaskActivityReconcileResult(
                status="indeterminate",
                reason="CLI wheel host does not reconcile",
            ),
        )

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        handler = self._handlers.get(call.request.capability_id)
        if handler is not None:
            refused = self._refuse_recoverable(handler, "cancel")
            if refused is not None:
                return refused
        return TaskHostCallResult(
            operation="cancel",
            cancel_result=TaskActivityCancelResult(
                status="indeterminate",
                reason="CLI wheel host does not cancel",
            ),
        )

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        del identity
        return ()


def _selected_entrypoint(
    distribution_name: str,
    group: str,
    entrypoint_name: str,
) -> tuple[metadata.Distribution, metadata.EntryPoint]:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as error:
        raise _CliSourceError(f"selected distribution is not installed: {distribution_name}") from error
    selected = tuple(
        entrypoint
        for entrypoint in distribution.entry_points
        if entrypoint.group == group and entrypoint.name == entrypoint_name
    )
    if len(selected) != 1:
        raise _CliSourceError(
            f"distribution {distribution_name!r} does not expose exactly one "
            f"{group} entry point named {entrypoint_name!r}"
        )
    return distribution, selected[0]


def _declaration_path(entrypoint: metadata.EntryPoint, kind: str) -> str:
    top_level = entrypoint.module.partition(".")[0]
    if not top_level or not top_level.isidentifier():
        raise _CliSourceError("selected entry point has no canonical top-level package")
    return f"{top_level}/{kind}-declaration.json"


def _require_installed_wheel(distribution: metadata.Distribution) -> None:
    """Reject ambient editable inference at the CLI boundary.

    Task 8's explicit distribution/entry-point flags can describe an installed
    wheel source.  An editable source additionally requires the physical root
    and exact closed file tuple, neither of which these flags can express.
    """

    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        return
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as error:
        raise _CliSourceError("selected distribution has malformed direct_url.json") from error
    if not isinstance(direct_url, dict):
        raise _CliSourceError("selected distribution has malformed direct_url.json")
    directory_info = direct_url.get("dir_info")
    if directory_info is not None and not isinstance(directory_info, dict):
        raise _CliSourceError("selected distribution has malformed direct_url.json")
    if isinstance(directory_info, dict) and directory_info.get("editable") is True:
        raise _CliSourceError("editable distribution requires an explicit source root and file tuple")


def _product_source(
    distribution_name: str,
    entrypoint_name: str,
) -> ProductSource:
    distribution, entrypoint = _selected_entrypoint(
        distribution_name,
        "graph_engine.products",
        entrypoint_name,
    )
    _require_installed_wheel(distribution)
    declaration_path = _declaration_path(entrypoint, "product")
    return WheelProductSource(
        distribution=distribution_name,
        entrypoint_name=entrypoint_name,
        declaration_path=declaration_path,
    )


def _plugin_source(
    distribution_name: str,
    entrypoint_name: str,
) -> PluginSource:
    distribution, entrypoint = _selected_entrypoint(
        distribution_name,
        "graph_engine.plugins",
        entrypoint_name,
    )
    _require_installed_wheel(distribution)
    declaration_path = _declaration_path(entrypoint, "plugin")
    return WheelPluginSource(
        distribution=distribution_name,
        entrypoint_name=entrypoint_name,
        declaration_path=declaration_path,
    )


def _resolve_bundle(
    *,
    product_distribution: str,
    product_entrypoint: str,
    plugin_distributions: Sequence[str],
    plugin_entrypoints: Sequence[str],
) -> FrozenComposition:
    product = _product_source(product_distribution, product_entrypoint)
    plugins = tuple(
        _plugin_source(distribution, entrypoint)
        for distribution, entrypoint in zip(
            plugin_distributions,
            plugin_entrypoints,
            strict=True,
        )
    )
    request = ResolutionRequest(product=product, plugins=plugins)
    return RegistryPlatform().resolve(request)


def _compile_document(
    composition: FrozenComposition,
    *,
    product_distribution: str,
    product_entrypoint: str,
    plugin_distributions: Sequence[str],
    plugin_entrypoints: Sequence[str],
) -> dict[str, JSONValue]:
    return {
        "product_distribution": product_distribution,
        "product_entrypoint": product_entrypoint,
        "plugin_distributions": list(plugin_distributions),
        "plugin_entrypoints": list(plugin_entrypoints),
        "product_id": composition.manifest.product_id,
        "product_version": composition.manifest.product_version,
        "lock_digest": composition.lock_digest,
        "composition_digest": composition.digest,
        "compiled_digest": composition.workflow.digest,
    }


def _run_document(
    composition: FrozenComposition,
    *,
    product_distribution: str,
    product_entrypoint: str,
    plugin_distributions: Sequence[str],
    plugin_entrypoints: Sequence[str],
    entrypoint: str,
    invocation_id: str,
    root: Path,
) -> tuple[RunResult, dict[str, JSONValue]]:
    with Engine(root, host=_TrustedWheelPluginHost()) as engine:
        with engine.start(
            composition,
            entrypoint=entrypoint,
            invocation_id=invocation_id,
            seed=empty_invocation_seed(), authorization=empty_runtime_authorization(),
        ) as handle:
            result = engine.run_until_blocked(handle)
            envelopes = Ledger(handle.invocation_root / "ledger").read_all()
            ledger_document = cast(
                JSONValue,
                [envelope.model_dump(mode="json") for envelope in envelopes],
            )
            with handle.workspace as workspace:
                final_tree_id = workspace.head_tree_id()

    document = _compile_document(
        composition,
        product_distribution=product_distribution,
        product_entrypoint=product_entrypoint,
        plugin_distributions=plugin_distributions,
        plugin_entrypoints=plugin_entrypoints,
    )
    document.update(
        {
            "invocation_id": invocation_id,
            "status": result.status,
            "terminal_reason": result.terminal_reason,
            "actions": list(result.actions),
            "output": cast(JSONValue, result.model_dump(mode="json")["output"]),
            "ledger_digest": canonical_digest(ledger_document),
            "final_tree_id": final_tree_id,
        }
    )
    return result, document


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--product-dist")
    parser.add_argument("--product-entrypoint")
    parser.add_argument("--plugin-dist", action="append")
    parser.add_argument("--plugin-entrypoint", action="append")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m graph_engine", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    compile_parser = commands.add_parser(
        "compile",
        help="compile one explicit product bundle",
        allow_abbrev=False,
    )
    _add_source_arguments(compile_parser)

    run_parser = commands.add_parser(
        "run",
        help="run one explicit product bundle",
        allow_abbrev=False,
    )
    _add_source_arguments(run_parser)
    run_parser.add_argument("--entrypoint")
    run_parser.add_argument("--invocation-id")
    run_parser.add_argument("--root", type=Path)
    return parser


def _required(parser: argparse.ArgumentParser, value: object, message: str) -> None:
    if value is None:
        parser.error(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    _required(parser, arguments.product_dist, "product distribution is required")
    _required(parser, arguments.product_entrypoint, "product entrypoint is required")
    _required(parser, arguments.plugin_dist, "at least one plugin distribution is required")
    _required(parser, arguments.plugin_entrypoint, "at least one plugin entrypoint is required")
    if len(arguments.plugin_dist) != len(arguments.plugin_entrypoint):
        parser.error("plugin distribution and entrypoint counts must match")
    if arguments.command == "run":
        _required(parser, arguments.entrypoint, "entrypoint is required")
        _required(parser, arguments.invocation_id, "invocation id is required")
        _required(parser, arguments.root, "root is required")

    try:
        composition = _resolve_bundle(
            product_distribution=arguments.product_dist,
            product_entrypoint=arguments.product_entrypoint,
            plugin_distributions=arguments.plugin_dist,
            plugin_entrypoints=arguments.plugin_entrypoint,
        )
        if arguments.command == "compile":
            result: RunResult | None = None
            document = _compile_document(
                composition,
                product_distribution=arguments.product_dist,
                product_entrypoint=arguments.product_entrypoint,
                plugin_distributions=arguments.plugin_dist,
                plugin_entrypoints=arguments.plugin_entrypoint,
            )
        else:
            result, document = _run_document(
                composition,
                product_distribution=arguments.product_dist,
                product_entrypoint=arguments.product_entrypoint,
                plugin_distributions=arguments.plugin_dist,
                plugin_entrypoints=arguments.plugin_entrypoint,
                entrypoint=arguments.entrypoint,
                invocation_id=arguments.invocation_id,
                root=arguments.root,
            )
    except (GraphEngineError, OSError, ValueError) as error:
        print(f"graph-engine: {error}", file=sys.stderr)
        return 1

    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    if result is None or result.status == "succeeded":
        return 0
    if result.status == "interrupted":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
