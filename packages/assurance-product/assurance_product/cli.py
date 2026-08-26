from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, NoReturn, cast

import click
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import (
    CapabilityBindingEntry,
    ConfigTreePluginSource,
    FrozenComposition,
    WheelPluginSource,
)
from graph_engine.errors import GraphEngineError
from graph_engine.runtime.engine import Engine, EngineConflictError, EngineError, RunResult
from graph_engine.runtime.events import InvocationStarted
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    RuntimeAuthorizationError,
    SecretSourceBinding,
    authorize_binding_secret_handles,
    runtime_authorization_digest,
)
from graph_engine.runtime.seed import InvocationSeed
from graph_engine.runtime.tree_io import (
    SeedCapturePolicy,
    capture_workspace_seed,
)

from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel
from assurance_product.export import PublishError, publish_achieved, select_publish_change
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1
from assurance_product.product import (
    AssuranceCompositionError,
    AssuranceCompositionRequest,
    GraphAuditResult,
    audit_full_graph,
    resolve_assurance_composition,
)
from assurance_product.status import render_status

_SOURCE_FLAGS = (
    "product",
    "binding_dist",
    "binding_entrypoint",
    "binding_declaration",
    "config_tree",
)
_START_FLAGS = (
    "project_dir",
    "engine_root",
    "invocation_id",
    *_SOURCE_FLAGS,
    "entrypoint",
    "input",
)
_EXISTING_FLAGS = ("engine_root", "invocation_id", *_SOURCE_FLAGS)
_RUN_EXIT = {
    "succeeded": (0, "completed"),
    "stopped": (20, "stopped"),
    "interrupted": (30, "interrupted"),
    "failed": (40, "failed"),
}


class CommandError(Exception):
    def __init__(self, message: str, code: int = 40) -> None:
        super().__init__(message)
        self.code = code


def create_engine(root: Path, authorization: InvocationRuntimeAuthorization) -> Engine:
    return Engine.production(root, authorization=authorization)


def main() -> None:
    app.main(prog_name="aa-next")


@click.group(context_settings={"help_option_names": ["--help"]})
def app() -> None:
    """aa-next — Phase 5 product composition command."""


@app.command("compile")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--engine-root", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def compile_command(
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    engine_root: str | None,
    as_json: bool,
) -> None:
    del engine_root, as_json
    _require_options(
        {
            "product": product,
            "binding_dist": binding_dist,
            "binding_entrypoint": binding_entrypoint,
            "binding_declaration": binding_declaration,
            "config_tree": config_tree,
        },
        _SOURCE_FLAGS,
    )
    try:
        composition, audit = _resolve_and_audit(
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
        )
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(
        {
            "lock_digest": composition.lock_digest,
            "composition_digest": composition.digest,
            "workflow_digest": composition.workflow.digest,
            "product": product,
            "engine_api": composition.lock.engine_api,
            "entrypoints": sorted(composition.workflow.entrypoints),
            "audit": audit.model_dump(mode="json"),
        }
    )


@app.group("bindings")
def bindings() -> None:
    """Deployment binding wheel commands."""


@bindings.command("build")
@click.option("--manifest", type=click.Path())
@click.option("--output-dir", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def bindings_build(manifest: str | None, output_dir: str | None, as_json: bool) -> None:
    del as_json
    _require_options({"manifest": manifest, "output_dir": output_dir}, ("manifest", "output_dir"))
    try:
        built = build_deployment_wheel(Path(cast(str, manifest)), Path(cast(str, output_dir)))
    except (BindingBuildError, ValidationError, OSError) as error:
        _fail(str(error), 40)
    _emit(
        {
            "wheel": str(built.wheel),
            "distribution": built.distribution,
            "declaration_path": built.declaration_path,
            "manifest_digest": built.manifest_digest,
            "wheel_digest": built.wheel_digest,
            "entry_point_value": built.entry_point_value,
        }
    )


@app.command("start")
@click.option("--project-dir", type=click.Path())
@click.option("--engine-root", type=click.Path())
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--entrypoint")
@click.option("--input", "input_path", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def start_command(
    project_dir: str | None,
    engine_root: str | None,
    invocation_id: str | None,
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    entrypoint: str | None,
    input_path: str | None,
    secrets: tuple[str, ...],
    as_json: bool,
) -> None:
    del as_json
    _require_options(
        {
            "project_dir": project_dir,
            "engine_root": engine_root,
            "invocation_id": invocation_id,
            "product": product,
            "binding_dist": binding_dist,
            "binding_entrypoint": binding_entrypoint,
            "binding_declaration": binding_declaration,
            "config_tree": config_tree,
            "entrypoint": entrypoint,
            "input": input_path,
        },
        _START_FLAGS,
    )
    try:
        document = _start_invocation(
            project_dir=Path(cast(str, project_dir)),
            engine_root=Path(cast(str, engine_root)),
            invocation_id=cast(str, invocation_id),
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
            entrypoint=cast(str, entrypoint),
            input_path=Path(cast(str, input_path)),
            secrets=secrets,
        )
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(document)


@app.command("run")
@click.option("--project-dir", type=click.Path())
@click.option("--engine-root", type=click.Path())
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--entrypoint")
@click.option("--input", "input_path", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def run_command(
    project_dir: str | None,
    engine_root: str | None,
    invocation_id: str | None,
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    entrypoint: str | None,
    input_path: str | None,
    secrets: tuple[str, ...],
    as_json: bool,
) -> None:
    del as_json
    _require_options(
        {
            "engine_root": engine_root,
            "invocation_id": invocation_id,
            "product": product,
            "binding_dist": binding_dist,
            "binding_entrypoint": binding_entrypoint,
            "binding_declaration": binding_declaration,
            "config_tree": config_tree,
        },
        _EXISTING_FLAGS,
    )
    try:
        result, mapped = _run_invocation(
            project_dir=None if project_dir is None else Path(project_dir),
            engine_root=Path(cast(str, engine_root)),
            invocation_id=cast(str, invocation_id),
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
            entrypoint=entrypoint,
            input_path=None if input_path is None else Path(input_path),
            secrets=secrets,
        )
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(
        {
            "invocation_id": invocation_id,
            "status": mapped,
            "terminal_reason": result.terminal_reason,
            "actions": list(result.actions),
        }
    )
    raise SystemExit(_RUN_EXIT[result.status][0])


@app.command("status")
@click.option("--engine-root", type=click.Path())
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def status_command(
    engine_root: str | None,
    invocation_id: str | None,
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    secrets: tuple[str, ...],
    as_json: bool,
) -> None:
    del as_json
    _require_options(
        {
            "engine_root": engine_root,
            "invocation_id": invocation_id,
            "product": product,
            "binding_dist": binding_dist,
            "binding_entrypoint": binding_entrypoint,
            "binding_declaration": binding_declaration,
            "config_tree": config_tree,
        },
        _EXISTING_FLAGS,
    )
    try:
        projection, identity = _read_authenticated_projection(
            engine_root=Path(cast(str, engine_root)),
            invocation_id=cast(str, invocation_id),
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
            secrets=secrets,
        )
        document = render_status(
            projection,
            root_input_digest=identity["root_input_digest"],
            initial_tree_id=identity["initial_tree_id"],
        ).model_dump(mode="json")
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(document)


@app.command("resume")
@click.option("--engine-root", type=click.Path())
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--action")
@click.option("--reason")
@click.option("--json", "as_json", is_flag=True)
def resume_command(
    engine_root: str | None,
    invocation_id: str | None,
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    secrets: tuple[str, ...],
    action: str | None,
    reason: str | None,
    as_json: bool,
) -> None:
    del as_json
    _require_options(
        {
            "engine_root": engine_root,
            "invocation_id": invocation_id,
            "product": product,
            "binding_dist": binding_dist,
            "binding_entrypoint": binding_entrypoint,
            "binding_declaration": binding_declaration,
            "config_tree": config_tree,
            "action": action,
            "reason": reason,
        },
        (*_EXISTING_FLAGS, "action", "reason"),
    )
    try:
        result, mapped = _resume_invocation(
            engine_root=Path(cast(str, engine_root)),
            invocation_id=cast(str, invocation_id),
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
            secrets=secrets,
            action=cast(str, action),
            reason=cast(str, reason),
        )
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(
        {
            "invocation_id": invocation_id,
            "status": mapped,
            "terminal_reason": result.terminal_reason,
            "action": action,
        }
    )
    raise SystemExit(_RUN_EXIT[result.status][0])


@app.command("export")
@click.option("--project-dir", type=click.Path())
@click.option("--change")
@click.option("--json", "as_json", is_flag=True)
def export_command(
    project_dir: str | None,
    change: str | None,
    as_json: bool,
) -> None:
    del as_json
    _require_options({"project_dir": project_dir}, ("project_dir",))
    try:
        document = _export_change(project_dir=Path(cast(str, project_dir)), change_id=change)
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(document)


@app.group("lock")
def lock() -> None:
    """Authenticated invocation lock commands."""


@lock.command("show")
@click.option("--engine-root", type=click.Path())
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def lock_show(
    engine_root: str | None,
    invocation_id: str | None,
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    secrets: tuple[str, ...],
    as_json: bool,
) -> None:
    del as_json
    _require_options(
        {
            "engine_root": engine_root,
            "invocation_id": invocation_id,
            "product": product,
            "binding_dist": binding_dist,
            "binding_entrypoint": binding_entrypoint,
            "binding_declaration": binding_declaration,
            "config_tree": config_tree,
        },
        _EXISTING_FLAGS,
    )
    try:
        composition, _projection, _identity = _open_authenticated(
            engine_root=Path(cast(str, engine_root)),
            invocation_id=cast(str, invocation_id),
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
            secrets=secrets,
        )
    except CommandError as error:
        _fail(str(error), error.code)
    _emit(
        {
            "lock_digest": composition.lock_digest,
            "engine_api": composition.lock.engine_api,
            "lock": composition.lock.model_dump(mode="json"),
        }
    )


def _require_options(values: Mapping[str, object], names: Sequence[str]) -> None:
    missing = [f"--{name.replace('_', '-')}" for name in names if not values.get(name)]
    if missing:
        raise click.UsageError("missing required options: " + " ".join(missing))


def _emit(document: Mapping[str, object]) -> None:
    click.echo(json.dumps(document, sort_keys=True, separators=(",", ":")))


def _fail(message: str, code: int) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(code)


def _resolve_and_audit(
    *,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
) -> tuple[FrozenComposition, GraphAuditResult]:
    if binding_entrypoint != "deployment":
        raise CommandError("binding entrypoint must be deployment")
    if product not in {"assurance-opencode", "assurance-cursor"}:
        raise CommandError(f"unknown product: {product}")
    try:
        composition = resolve_assurance_composition(
            AssuranceCompositionRequest(
                product_entrypoint=cast(Literal["assurance-opencode", "assurance-cursor"], product),
                deployment_source=WheelPluginSource(
                    distribution=binding_dist,
                    entrypoint_name=binding_entrypoint,
                    declaration_path=binding_declaration,
                ),
                configuration_tree=ConfigTreePluginSource(path=Path(config_tree).resolve()),
            )
        )
    except (
        AssuranceCompositionError,
        ValidationError,
        GraphEngineError,
        ValueError,
        OSError,
    ) as error:
        raise CommandError(str(error)) from error
    audit = audit_full_graph(composition.workflow, composition)
    if any(
        (
            audit.unreachable_nodes,
            audit.dead_ends,
            audit.forbidden_direct_targets,
            audit.missing_bindings,
            audit.uninventoried_nodes,
        )
    ):
        raise CommandError(f"graph audit failed: {audit.model_dump(mode='json')}")
    return composition, audit


def _authorization(secrets: Sequence[str]) -> InvocationRuntimeAuthorization:
    bindings: list[SecretSourceBinding] = []
    for raw in secrets:
        handle, separator, locator = raw.partition("=")
        if not separator or not handle or not locator:
            raise CommandError("secret mapping must be HANDLE=env:NAME or HANDLE=file:/absolute/path")
        if locator.startswith("env:"):
            bindings.append(
                SecretSourceBinding(
                    handle=handle,
                    source_kind="environment",
                    source_locator=locator.removeprefix("env:"),
                )
            )
            continue
        if locator.startswith("file:"):
            bindings.append(
                SecretSourceBinding(
                    handle=handle,
                    source_kind="file",
                    source_locator=locator.removeprefix("file:"),
                )
            )
            continue
        raise CommandError("secret mapping must be HANDLE=env:NAME or HANDLE=file:/absolute/path")
    try:
        sources = tuple(bindings)
        return InvocationRuntimeAuthorization(
            schema_version="1",
            secret_sources=sources,
            digest=runtime_authorization_digest(sources),
        )
    except (ValueError, RuntimeAuthorizationError) as error:
        raise CommandError(str(error)) from error


def _required_handles(composition: FrozenComposition) -> tuple[str, ...]:
    handles: set[str] = set()
    for entry in composition.registries.capabilities.entries.values():
        if isinstance(entry, CapabilityBindingEntry):
            handles.update(entry.secret_handles)
    return tuple(sorted(handles))


def _authorize_secrets(
    composition: FrozenComposition, secrets: Sequence[str]
) -> InvocationRuntimeAuthorization:
    authorization = _authorization(secrets)
    try:
        authorize_binding_secret_handles(_required_handles(composition), authorization)
    except RuntimeAuthorizationError as error:
        raise CommandError(str(error)) from error
    return authorization


def _load_product_input(path: Path, *, entrypoint: str, composition: FrozenComposition) -> ProductInputV1:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = ProductInputV1.model_validate(payload)
        return value.validate_for_entrypoint(entrypoint).authenticate_against(composition)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise CommandError(str(error)) from error


def _capture_seed(project_dir: Path, product_input: ProductInputV1) -> InvocationSeed:
    try:
        workspace = capture_workspace_seed(project_dir, policy=SeedCapturePolicy())
    except (GraphEngineError, OSError, ValidationError) as error:
        raise CommandError(str(error)) from error
    root_input = cast(JSONValue, product_input.model_dump(mode="json"))
    return InvocationSeed(
        schema_version="1",
        root_input=root_input,
        root_input_digest=canonical_digest(root_input),
        workspace=workspace,
    )


def _start_invocation(
    *,
    project_dir: Path,
    engine_root: Path,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    entrypoint: str,
    input_path: Path,
    secrets: Sequence[str],
) -> dict[str, object]:
    if entrypoint not in PRODUCT_ENTRYPOINTS:
        raise CommandError(f"unknown product entrypoint: {entrypoint}")
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    product_input = _load_product_input(input_path, entrypoint=entrypoint, composition=composition)
    authorization = _authorize_secrets(composition, secrets)
    seed = _capture_seed(project_dir, product_input)
    try:
        with create_engine(engine_root, authorization) as engine:
            with engine.start(
                composition,
                entrypoint=entrypoint,
                invocation_id=invocation_id,
                seed=seed,
                authorization=authorization,
            ):
                pass
    except (EngineError, GraphEngineError, OSError, ValidationError) as error:
        raise CommandError(str(error)) from error
    return {
        "invocation_id": invocation_id,
        "lock_digest": composition.lock_digest,
        "composition_digest": composition.digest,
        "seed_tree_id": seed.workspace.tree_id,
        "root_input_digest": seed.root_input_digest,
    }


def _run_invocation(
    *,
    project_dir: Path | None,
    engine_root: Path,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    entrypoint: str | None,
    input_path: Path | None,
    secrets: Sequence[str],
) -> tuple[RunResult, str]:
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    authorization = _authorize_secrets(composition, secrets)
    exists = (engine_root / "invocations" / invocation_id).is_dir()
    try:
        with create_engine(engine_root, authorization) as engine:
            if exists:
                with engine.open(invocation_id, composition, authorization=authorization) as handle:
                    result = engine.run_until_blocked(handle)
            else:
                if project_dir is None or entrypoint is None or input_path is None:
                    raise CommandError("first run requires --project-dir, --entrypoint, and --input")
                if entrypoint not in PRODUCT_ENTRYPOINTS:
                    raise CommandError(f"unknown product entrypoint: {entrypoint}")
                product_input = _load_product_input(
                    input_path, entrypoint=entrypoint, composition=composition
                )
                seed = _capture_seed(project_dir, product_input)
                with engine.start(
                    composition,
                    entrypoint=entrypoint,
                    invocation_id=invocation_id,
                    seed=seed,
                    authorization=authorization,
                ) as handle:
                    result = engine.run_until_blocked(handle)
    except EngineConflictError as error:
        raise CommandError(str(error)) from error
    except (EngineError, GraphEngineError, OSError, ValidationError) as error:
        raise CommandError(str(error)) from error
    return result, _RUN_EXIT[result.status][1]


def _resume_invocation(
    *,
    engine_root: Path,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    secrets: Sequence[str],
    action: str,
    reason: str,
) -> tuple[RunResult, str]:
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    authorization = _authorize_secrets(composition, secrets)
    try:
        with create_engine(engine_root, authorization) as engine:
            opened = engine.open(invocation_id, composition, authorization=authorization)
            try:
                resumed = engine.resume(opened, action=action, payload={"reason": reason})
                try:
                    result = engine.run_until_blocked(resumed)
                finally:
                    resumed.close()
            finally:
                opened.close()
    except EngineConflictError as error:
        raise CommandError(str(error)) from error
    except (EngineError, GraphEngineError, OSError, ValidationError) as error:
        raise CommandError(str(error)) from error
    return result, _RUN_EXIT[result.status][1]


def _export_change(*, project_dir: Path, change_id: str | None) -> dict[str, object]:
    try:
        project = Path(project_dir).resolve()
        selected = select_publish_change(project, change_id)
        receipt = publish_achieved(project, selected)
    except (PublishError, ValueError, OSError) as error:
        raise CommandError(str(error)) from error
    return receipt.model_dump(mode="json")


def _read_authenticated_projection(
    *,
    engine_root: Path,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    secrets: Sequence[str],
) -> tuple[InvocationProjection, dict[str, str]]:
    _composition, projection, identity = _open_authenticated(
        engine_root=engine_root,
        invocation_id=invocation_id,
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
        secrets=secrets,
    )
    return projection, identity


def _open_authenticated(
    *,
    engine_root: Path,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    secrets: Sequence[str],
) -> tuple[FrozenComposition, InvocationProjection, dict[str, str]]:
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    authorization = _authorize_secrets(composition, secrets)
    if not (engine_root / "invocations" / invocation_id).is_dir():
        raise CommandError(f"invocation is missing: {invocation_id}")
    try:
        with create_engine(engine_root, authorization) as engine:
            with engine.open(invocation_id, composition, authorization=authorization) as handle:
                envelopes = Ledger(handle.invocation_root / "ledger").read_all()
                projection = fold_events(envelopes)
                identity = _start_identity(envelopes)
    except EngineConflictError as error:
        raise CommandError(str(error)) from error
    except (EngineError, GraphEngineError, OSError, ValidationError) as error:
        raise CommandError(str(error)) from error
    return composition, projection, identity


def _start_identity(envelopes: Sequence[object]) -> dict[str, str]:
    if not envelopes:
        raise CommandError("invocation has no ledger bootstrap")
    event = getattr(envelopes[0], "event", None)
    if not isinstance(event, InvocationStarted):
        raise CommandError("invocation ledger lacks its canonical bootstrap")
    return {
        "root_input_digest": event.root_input_digest,
        "initial_tree_id": event.initial_tree_id,
    }
