from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import PluginProvider, TaskContext, TaskHandler, TaskOutcome, TaskRequest
from graph_engine.product import (
    ProductManifest,
    ProductProvider,
    ProductResolutionError,
    ResolvedProduct,
    load_plugin_entrypoint,
    load_product_entrypoint,
    resolve_product,
)
from graph_engine.runtime.engine import Engine, RunResult
from graph_engine.runtime.ledger import Ledger


@dataclass(frozen=True, slots=True)
class _ManifestSnapshot:
    value: ProductManifest

    def manifest(self) -> ProductManifest:
        return self.value


class _TrustedWheelPluginHost:
    """Run explicitly selected, trusted Phase 1 wheel plugins in-process.

    This command-boundary adapter is deliberately not an OS sandbox and is not a
    production host.  It exists so the Phase 1 demonstration CLI can execute
    reviewed wheel plugins while ``Engine`` itself remains fail-closed when no
    ``TaskExecutionHost`` is supplied.
    """

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        return await handler.execute(
            request,
            TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
        )


def _plugin_map(entrypoint_names: Sequence[str]) -> dict[str, PluginProvider]:
    providers: dict[str, PluginProvider] = {}
    selected_entrypoints: dict[str, str] = {}
    for entrypoint_name in entrypoint_names:
        provider = load_plugin_entrypoint(entrypoint_name)
        plugin_id = provider.descriptor().plugin_id
        if plugin_id in providers:
            first = selected_entrypoints[plugin_id]
            raise ProductResolutionError(
                f"plugin entry points {first!r} and {entrypoint_name!r} both provide {plugin_id}"
            )
        providers[plugin_id] = provider
        selected_entrypoints[plugin_id] = entrypoint_name
    return providers


def _resolve_bundle(product_entrypoint: str, plugin_entrypoints: Sequence[str]) -> ResolvedProduct:
    provider: ProductProvider = load_product_entrypoint(product_entrypoint)
    manifest = provider.manifest()
    plugins = _plugin_map(plugin_entrypoints)
    expected = {requirement.plugin_id for requirement in manifest.plugins}
    selected = set(plugins)
    if selected != expected:
        missing = sorted(expected - selected)
        extra = sorted(selected - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ProductResolutionError(
            "explicit plugin set does not match product manifest: " + "; ".join(details)
        )
    return resolve_product(_ManifestSnapshot(manifest), plugins)


def _compile_document(
    product_entrypoint: str,
    plugin_entrypoints: Sequence[str],
) -> tuple[ResolvedProduct, dict[str, JSONValue]]:
    resolved = _resolve_bundle(product_entrypoint, plugin_entrypoints)
    document: dict[str, JSONValue] = {
        "product_entrypoint": product_entrypoint,
        "product_id": resolved.manifest.product_id,
        "product_version": resolved.manifest.product_version,
        "product_digest": resolved.digest,
        "compiled_digest": resolved.workflow.digest,
    }
    return resolved, document


def _run_document(
    resolved: ResolvedProduct,
    *,
    product_entrypoint: str,
    plugin_entrypoints: Sequence[str],
    entrypoint: str,
    invocation_id: str,
    root: Path,
) -> tuple[RunResult, dict[str, JSONValue]]:
    # Host construction is intentionally local to this trusted demonstration
    # command. Engine(root) still uses its fail-closed unavailable host.
    with Engine(root, host=_TrustedWheelPluginHost()) as engine:
        with engine.start(
            resolved,
            entrypoint=entrypoint,
            invocation_id=invocation_id,
        ) as handle:
            result = engine.run_until_blocked(handle)
            envelopes = Ledger(handle.invocation_root / "ledger").read_all()
            ledger_document = cast(
                JSONValue,
                [envelope.model_dump(mode="json") for envelope in envelopes],
            )
            with handle.workspace as workspace:
                final_tree_id = workspace.head_tree_id()

    document: dict[str, JSONValue] = {
        "product_entrypoint": product_entrypoint,
        "plugin_entrypoints": list(plugin_entrypoints),
        "product_id": resolved.manifest.product_id,
        "product_version": resolved.manifest.product_version,
        "product_digest": resolved.digest,
        "compiled_digest": resolved.workflow.digest,
        "invocation_id": invocation_id,
        "status": result.status,
        "terminal_reason": result.terminal_reason,
        "actions": list(result.actions),
        "output": cast(JSONValue, result.model_dump(mode="json")["output"]),
        "ledger_digest": canonical_digest(ledger_document),
        "final_tree_id": final_tree_id,
    }
    return result, document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m graph_engine")
    commands = parser.add_subparsers(dest="command", required=True)

    compile_parser = commands.add_parser("compile", help="compile one explicit product bundle")
    compile_parser.add_argument("--product")

    run_parser = commands.add_parser("run", help="run one explicit product bundle")
    run_parser.add_argument("--product")
    run_parser.add_argument("--plugin", action="append")
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
    _required(parser, arguments.product, "product is required")

    try:
        if arguments.command == "compile":
            _resolved, document = _compile_document(
                arguments.product,
                # Phase 1 toy distributions deliberately use the same explicit
                # entry-point name for their product and sole plugin.
                (arguments.product,),
            )
            result: RunResult | None = None
        else:
            _required(parser, arguments.plugin, "at least one plugin is required")
            _required(parser, arguments.entrypoint, "entrypoint is required")
            _required(parser, arguments.invocation_id, "invocation id is required")
            _required(parser, arguments.root, "root is required")
            resolved, _compile = _compile_document(arguments.product, arguments.plugin)
            result, document = _run_document(
                resolved,
                product_entrypoint=arguments.product,
                plugin_entrypoints=arguments.plugin,
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
