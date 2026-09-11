from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn, cast

import click
from pydantic import ValidationError

from graph_engine.composition import (
    CapabilityBindingEntry,
    ConfigTreePluginSource,
    FrozenComposition,
    WheelPluginSource,
)
from graph_engine.errors import GraphEngineError
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    RuntimeAuthorizationError,
    SecretSourceBinding,
    authorize_binding_secret_handles,
    runtime_authorization_digest,
)

from assurance_product.application import AssuranceProductApplication, SimpleRun
from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import PRODUCT_ENTRYPOINTS
from assurance_product.product import (
    AssuranceCompositionError,
    AssuranceCompositionRequest,
    prepare_change_workspace,
    reopen_change_workspace,
    resolve_assurance_composition,
)
from assurance_product.application import RuntimeSelectionError, SelectionCrash

_SOURCE_FLAGS = (
    "product",
    "binding_dist",
    "binding_entrypoint",
    "binding_declaration",
    "config_tree",
)
_START_FLAGS = (
    "project_dir",
    "change",
    "invocation_id",
    *_SOURCE_FLAGS,
    "entrypoint",
    "input",
)
_EXISTING_FLAGS = ("project_dir", "change", "invocation_id", *_SOURCE_FLAGS)
_RUN_EXIT = {
    "succeeded": (0, "completed"),
    "stopped": (20, "stopped"),
    "interrupted": (30, "interrupted"),
    "failed": (40, "failed"),
}
_STATUS_EXIT = {
    "completed": 0,
    "succeeded": 0,
    "running": 20,
    "blocked": 20,
    "stopped": 20,
    "interrupted": 30,
    "failed": 40,
}


class CommandError(Exception):
    def __init__(self, message: str, code: int = 40) -> None:
        super().__init__(message)
        self.code = code


def main() -> None:
    app.main(prog_name="aa")


@click.group(context_settings={"help_option_names": ["--help"]})
def app() -> None:
    """aa — authenticated Assurance graph product."""


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
        from assurance_product.product import reject_organization_overrides

        reject_organization_overrides(Path(cast(str, config_tree)))
        composition, _audit = _resolve_and_audit(
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
        )
        artifacts = AssuranceProductApplication().compile(
            composition,
            product=cast(str, product),
            config_tree=cast(str, config_tree),
        )
        document = artifacts.model_dump()
    except CommandError as error:
        _fail(str(error), error.code)
    except Exception as error:
        _fail(str(error), 40)
    _emit(document)


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
@click.option("--change")
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
    change: str | None,
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
            "change": change,
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
            change_id=cast(str, change),
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
@click.option("--change")
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
    change: str | None,
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
            "change": change,
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
            project_dir=Path(cast(str, project_dir)),
            change_id=cast(str, change),
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
    exit_spec = _RUN_EXIT.get(result.status)
    raise SystemExit(exit_spec[0] if exit_spec is not None else _STATUS_EXIT[mapped])


@app.command("status")
@click.option("--project-dir", type=click.Path())
@click.option("--change")
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def status_command(
    project_dir: str | None,
    change: str | None,
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
            "project_dir": project_dir,
            "change": change,
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
        composition, _audit = _resolve_and_audit(
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
        )
        authorization = _authorize_secrets(composition, secrets)
        workspace = _bind_workspace(Path(cast(str, project_dir)), cast(str, change), create=False)
        document = (
            AssuranceProductApplication()
            .status(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=cast(str, invocation_id),
                change_id=cast(str, change),
            )
            .model_dump(mode="json")
        )
    except CommandError as error:
        _fail(str(error), error.code)
    except Exception as error:
        _fail(str(error), 40)
    _emit(document)


@app.command("resume")
@click.option("--project-dir", type=click.Path())
@click.option("--change")
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--action")
@click.option("--reason")
@click.option("--resume-file", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def resume_command(
    project_dir: str | None,
    change: str | None,
    invocation_id: str | None,
    product: str | None,
    binding_dist: str | None,
    binding_entrypoint: str | None,
    binding_declaration: str | None,
    config_tree: str | None,
    secrets: tuple[str, ...],
    action: str | None,
    reason: str | None,
    resume_file: str | None,
    as_json: bool,
) -> None:
    del as_json
    if resume_file and (action or reason):
        raise click.UsageError("--resume-file is mutually exclusive with --action/--reason")
    required = {
        "project_dir": project_dir,
        "change": change,
        "invocation_id": invocation_id,
        "product": product,
        "binding_dist": binding_dist,
        "binding_entrypoint": binding_entrypoint,
        "binding_declaration": binding_declaration,
        "config_tree": config_tree,
    }
    if resume_file:
        _require_options(required, _EXISTING_FLAGS)
    else:
        _require_options(
            {**required, "action": action, "reason": reason},
            (*_EXISTING_FLAGS, "action", "reason"),
        )
    try:
        result, mapped, code = _resume_invocation(
            project_dir=Path(cast(str, project_dir)),
            change_id=cast(str, change),
            invocation_id=cast(str, invocation_id),
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
            secrets=secrets,
            action=action,
            reason=reason,
            resume_file=None if resume_file is None else Path(resume_file),
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
    raise SystemExit(code)


@app.group("lock")
def lock() -> None:
    """Authenticated invocation lock commands."""


@lock.command("show")
@click.option("--project-dir", type=click.Path())
@click.option("--change")
@click.option("--invocation-id")
@click.option("--product")
@click.option("--binding-dist")
@click.option("--binding-entrypoint")
@click.option("--binding-declaration")
@click.option("--config-tree", type=click.Path())
@click.option("--secret", "secrets", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def lock_show(
    project_dir: str | None,
    change: str | None,
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
            "project_dir": project_dir,
            "change": change,
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
        composition, _audit = _resolve_and_audit(
            product=cast(str, product),
            binding_dist=cast(str, binding_dist),
            binding_entrypoint=cast(str, binding_entrypoint),
            binding_declaration=cast(str, binding_declaration),
            config_tree=cast(str, config_tree),
        )
        authorization = _authorize_secrets(composition, secrets)
        workspace = _bind_workspace(Path(cast(str, project_dir)), cast(str, change), create=False)
        document = AssuranceProductApplication().lock_show(
            workspace=workspace,
            composition=composition,
            authorization=authorization,
            invocation_id=cast(str, invocation_id),
        )
    except CommandError as error:
        _fail(str(error), error.code)
    except Exception as error:
        _fail(str(error), 40)
    _emit(document)


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
) -> tuple[FrozenComposition, None]:
    if binding_entrypoint != "deployment":
        raise CommandError("binding entrypoint must be deployment")
    if product != "assurance-opencode":
        raise CommandError(f"unknown product: {product}")
    try:
        composition = resolve_assurance_composition(
            AssuranceCompositionRequest(
                product_entrypoint="assurance-opencode",
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
    return composition, None


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


def _bind_workspace(project_dir: Path, change_id: str, *, create: bool) -> ChangeWorkspace:
    try:
        if create:
            return prepare_change_workspace(project_dir, change_id)
        return reopen_change_workspace(project_dir, change_id)
    except ValueError as error:
        raise CommandError(str(error)) from error


@contextmanager
def _engine_failures() -> Iterator[None]:
    try:
        yield
    except (
        GraphEngineError,
        OSError,
        ValidationError,
        RuntimeSelectionError,
        SelectionCrash,
        RuntimeError,
        ValueError,
    ) as error:
        raise CommandError(f"{type(error).__name__}: {error}") from error


def _start_invocation(
    *,
    project_dir: Path,
    change_id: str,
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
    authorization = _authorize_secrets(composition, secrets)
    workspace = _bind_workspace(project_dir, change_id, create=True)
    with _engine_failures():
        return AssuranceProductApplication().start(
            project_dir=project_dir,
            change_id=change_id,
            invocation_id=invocation_id,
            composition=composition,
            authorization=authorization,
            entrypoint=entrypoint,
            input_path=input_path,
            workspace=workspace,
        )


def _run_invocation(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    entrypoint: str | None,
    input_path: Path | None,
    secrets: Sequence[str],
) -> tuple[SimpleRun, str]:
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    authorization = _authorize_secrets(composition, secrets)
    workspace = _bind_workspace(project_dir, change_id, create=True)
    with _engine_failures():
        result, mapped, _code = AssuranceProductApplication().run(
            project_dir=project_dir,
            change_id=change_id,
            invocation_id=invocation_id,
            composition=composition,
            authorization=authorization,
            entrypoint=entrypoint,
            input_path=input_path,
            workspace=workspace,
            secrets=secrets,
        )
    return cast(SimpleRun, result), mapped


def _resume_invocation(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    product: str,
    binding_dist: str,
    binding_entrypoint: str,
    binding_declaration: str,
    config_tree: str,
    secrets: Sequence[str],
    action: str | None,
    reason: str | None,
    resume_file: Path | None = None,
) -> tuple[SimpleRun, str, int]:
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    authorization = _authorize_secrets(composition, secrets)
    workspace = _bind_workspace(project_dir, change_id, create=False)
    with _engine_failures():
        return cast(
            tuple[SimpleRun, str, int],
            AssuranceProductApplication().resume(
                workspace=workspace,
                composition=composition,
                authorization=authorization,
                invocation_id=invocation_id,
                action=action,
                reason=reason,
                resume_file=resume_file,
            ),
        )
