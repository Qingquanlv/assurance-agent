from __future__ import annotations

import json
import os
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

from assurance_improvement.operations.knowledge_promote import (
    KnowledgePromoteError,
    PromoteOutcome,
    load_promotable_delta,
    load_proposal_file,
    promote_knowledge,
)
from assurance_improvement.operations.retro_dashboard import build_retro_dashboard

from assurance_product.worker_entrypoints import exclusive_cli
from assurance_product.application import AssuranceProductApplication, SimpleRun
from assurance_product.binding_builder import BindingBuildError, build_deployment_wheel
from assurance_product.bootstrap.driver import resume_bootstrap, run_bootstrap, stop_bootstrap
from assurance_product.operator import AssuranceOperator, OperatorError
from assurance_product.bootstrap.opencode import OpenCodeLaunchError
from assurance_product.bootstrap.preflight import BootstrapPreflightError
from assurance_product.bootstrap.spec import SpecOverrideError, load_run_spec
from assurance_product.bootstrap.status import read_bootstrap_status
from assurance_product.change_workspace import ChangeWorkspace, require_real_directory
from assurance_product.models import PRODUCT_ENTRYPOINTS
from assurance_product.product import (
    AssuranceCompositionError,
    AssuranceCompositionRequest,
    prepare_change_workspace,
    reopen_change_workspace,
    resolve_assurance_composition,
)
from assurance_product.sut_worktree import ensure_run_worktree
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


@app.command("stop")
@click.option("--project-dir", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--invocation-id", required=True)
@click.option(
    "--force",
    is_flag=True,
    help="Terminate the verified foreground owner and confirm owned activity termination.",
)
def stop_command(project_dir: str, invocation_id: str, force: bool) -> None:
    """Stop a foreground execution in its actual workspace directory."""
    result = AssuranceProductApplication().stop(
        project_dir=Path(project_dir),
        invocation_id=invocation_id,
        force=force,
    )
    _emit({"invocation_id": invocation_id, "status": result})
    raise SystemExit({"stopped": 0, "stopping": 20, "unconfirmed": 40}[result])


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


@app.group("retro")
def retro() -> None:
    """Read-only retro run dashboard commands."""


@retro.command("show")
@click.option("--project-dir", type=click.Path())
@click.option("--change")
@click.option("--json", "as_json", is_flag=True)
def retro_show(project_dir: str | None, change: str | None, as_json: bool) -> None:
    del as_json
    _require_options({"project_dir": project_dir}, ("project_dir",))
    try:
        root = require_real_directory(Path(cast(str, project_dir)))
        dashboard = build_retro_dashboard(root, change_id=change)
    except CommandError as error:
        _fail(str(error), error.code)
    except Exception as error:
        _fail(str(error), 40)
    _emit(dashboard.model_dump(mode="json"))


def _promote_payload(outcome: PromoteOutcome) -> dict[str, object]:
    return {
        "written": outcome.written,
        "merged_keys": list(outcome.merged_keys),
        "conflicts_path": outcome.conflicts_path,
        "conflicts": [item.key for item in outcome.conflicts],
    }


@app.group("knowledge")
def knowledge() -> None:
    """Promote a domain-knowledge delta into .aa/data-knowledge.yaml."""


@knowledge.command("promote")
@click.option("--project-dir", type=click.Path())
@click.option("--improvement")
@click.option("--from", "proposal_path", type=click.Path())
@click.option("--yes", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def knowledge_promote(
    project_dir: str | None,
    improvement: str | None,
    proposal_path: str | None,
    yes: bool,
    force: bool,
    as_json: bool,
) -> None:
    del as_json
    _require_options({"project_dir": project_dir}, ("project_dir",))
    if (improvement is None) == (proposal_path is None):
        _fail("exactly one of --improvement or --from is required", 40)
    try:
        root = require_real_directory(Path(cast(str, project_dir)))
        proposal = (
            load_promotable_delta(root, improvement)
            if improvement is not None
            else load_proposal_file(Path(cast(str, proposal_path)))
        )
        outcome = promote_knowledge(root, proposal, yes=yes, force=force)
    except KnowledgePromoteError as error:
        if error.outcome is not None:
            _emit(_promote_payload(error.outcome))
        _fail(str(error), error.code)
    except CommandError as error:
        _fail(str(error), error.code)
    except Exception as error:
        _fail(str(error), 40)
    _emit(_promote_payload(outcome))


@app.group("operator")
def operator() -> None:
    """Exact-run lifecycle shared by the QA Panel and /assure."""


def _operator_result(payload: Mapping[str, object]) -> None:
    _emit(payload)


def _operator_fail(error: OperatorError) -> NoReturn:
    _emit({"kind": error.kind, "error": str(error)})
    raise SystemExit(40)


@operator.command("start")
@click.option("--project-dir", type=click.Path())
@click.option("--requirement")
@click.option("--family", "families", multiple=True)
@click.option("--opencode-endpoint")
@click.option("--origin-session-id")
@click.option("--origin-parent-session-id")
@click.option("--task-directory", type=click.Path())
@click.option("--request-id")
@click.option("--json", "as_json", is_flag=True)
def operator_start(
    project_dir: str | None,
    requirement: str | None,
    families: tuple[str, ...],
    opencode_endpoint: str | None,
    origin_session_id: str | None,
    origin_parent_session_id: str | None,
    task_directory: str | None,
    request_id: str | None,
    as_json: bool,
) -> None:
    _require_options(
        {
            "project_dir": project_dir,
            "requirement": requirement,
            "family": families,
            "opencode_endpoint": opencode_endpoint,
            "json": as_json,
        },
        ("project_dir", "requirement", "family", "opencode_endpoint", "json"),
    )
    try:
        payload = AssuranceOperator().start(
            project_dir=Path(cast(str, project_dir)),
            requirement=cast(str, requirement),
            families=families,
            opencode_endpoint=cast(str, opencode_endpoint),
            origin_session_id=origin_session_id,
            origin_parent_session_id=origin_parent_session_id,
            task_directory=Path(task_directory) if task_directory else None,
            request_id=request_id,
            environ=os.environ,
        )
    except OperatorError as error:
        _operator_fail(error)
    _operator_result(payload)


@operator.command("task-define")
@click.option("--project-dir", type=click.Path())
@click.option("--task-directory", type=click.Path())
@click.option("--name")
@click.option("--base-ref")
@click.option("--requirement")
@click.option("--family", "families", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def operator_task_define(
    project_dir: str | None,
    task_directory: str | None,
    name: str | None,
    base_ref: str | None,
    requirement: str | None,
    families: tuple[str, ...],
    as_json: bool,
) -> None:
    from assurance_product.task_records import TaskRecordError, define_task

    _require_options(
        {
            "project_dir": project_dir,
            "task_directory": task_directory,
            "name": name,
            "base_ref": base_ref,
            "requirement": requirement,
            "family": families,
            "json": as_json,
        },
        ("project_dir", "task_directory", "name", "base_ref", "requirement", "family", "json"),
    )
    try:
        definition = define_task(
            project_dir=Path(cast(str, project_dir)),
            task_directory=Path(cast(str, task_directory)),
            name=cast(str, name),
            base_ref=cast(str, base_ref),
            requirement=cast(str, requirement),
            families=families,
        )
    except TaskRecordError as error:
        _operator_fail(OperatorError(error.kind, str(error)))
    _operator_result(definition.model_dump(mode="json"))


@operator.command("task")
@click.option("--project-dir", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def operator_task(project_dir: str | None, as_json: bool) -> None:
    from assurance_product.task_records import TaskRecordError, read_task

    _require_options({"project_dir": project_dir, "json": as_json}, ("project_dir", "json"))
    try:
        definition = read_task(Path(cast(str, project_dir)))
    except TaskRecordError as error:
        _operator_fail(OperatorError(error.kind, str(error)))
    if definition is None:
        _operator_fail(OperatorError("not_configured", "task is not configured"))
    _operator_result(definition.model_dump(mode="json"))


@operator.command("preflight")
@click.option("--project-dir", type=click.Path())
@click.option("--requirement")
@click.option("--family", "families", multiple=True)
@click.option("--opencode-endpoint")
@click.option("--task-directory", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def operator_preflight(
    project_dir: str | None,
    requirement: str | None,
    families: tuple[str, ...],
    opencode_endpoint: str | None,
    task_directory: str | None,
    as_json: bool,
) -> None:
    _require_options(
        {
            "project_dir": project_dir,
            "requirement": requirement,
            "family": families,
            "opencode_endpoint": opencode_endpoint,
            "json": as_json,
        },
        ("project_dir", "requirement", "family", "opencode_endpoint", "json"),
    )
    try:
        payload = AssuranceOperator().preflight(
            project_dir=Path(cast(str, project_dir)),
            requirement=cast(str, requirement),
            families=families,
            opencode_endpoint=cast(str, opencode_endpoint),
            task_directory=Path(task_directory) if task_directory else None,
        )
    except OperatorError as error:
        _operator_fail(error)
    _operator_result(payload)


@operator.command("capabilities")
@click.option("--json", "as_json", is_flag=True)
def operator_capabilities(as_json: bool) -> None:
    from assurance_product.operator_views import capabilities

    _require_options({"json": as_json}, ("json",))
    _operator_result(capabilities())


@operator.command("history")
@click.option("--project-dir", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def operator_history(project_dir: str | None, as_json: bool) -> None:
    from assurance_product.operator_views import ProjectionError, read_task_history

    _require_options({"project_dir": project_dir, "json": as_json}, ("project_dir", "json"))
    try:
        view = read_task_history(Path(cast(str, project_dir)))
    except ProjectionError as error:
        _operator_fail(OperatorError(error.kind, str(error)))
    _operator_result(view.model_dump(mode="json"))


@operator.command("run")
@click.option("--project-dir", type=click.Path())
@click.option("--run-id")
@click.option("--json", "as_json", is_flag=True)
def operator_run(project_dir: str | None, run_id: str | None, as_json: bool) -> None:
    from assurance_product.operator_views import ProjectionError, read_run_view

    _require_options(
        {"project_dir": project_dir, "run_id": run_id, "json": as_json},
        ("project_dir", "run_id", "json"),
    )
    try:
        view = read_run_view(Path(cast(str, project_dir)), cast(str, run_id))
    except ProjectionError as error:
        _operator_fail(OperatorError(error.kind, str(error)))
    _operator_result(view.model_dump(mode="json"))


@operator.command("output")
@click.option("--project-dir", type=click.Path())
@click.option("--run-id")
@click.option("--output-id")
@click.option("--preview", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def operator_output(
    project_dir: str | None,
    run_id: str | None,
    output_id: str | None,
    preview: bool,
    as_json: bool,
) -> None:
    from assurance_product.operator_views import ProjectionError, read_run_view
    from assurance_product.run_history import RunHistoryError, read_output_ref, read_preserved_output

    _require_options(
        {
            "project_dir": project_dir,
            "run_id": run_id,
            "output_id": output_id,
            "preview": preview,
            "json": as_json,
        },
        ("project_dir", "run_id", "output_id", "json"),
    )
    task = Path(cast(str, project_dir))
    try:
        view = read_run_view(task, cast(str, run_id))
        known = {ref.output_id for node in view.nodes for ref in node.outputs}
        if cast(str, output_id) not in known:
            raise RunHistoryError("unavailable", "preserved output is missing")
        ref = read_output_ref(task / ".aa" / "runs" / view.change_id, cast(str, output_id))
        content = read_preserved_output(task / ".aa" / "runs" / view.change_id, ref)
    except ProjectionError as error:
        _operator_fail(OperatorError(error.kind, str(error)))
    except RunHistoryError as error:
        _operator_fail(OperatorError(error.kind, str(error)))
    if preview:
        limit = 64_000
        sample = content[:limit]
        _operator_result(
            {
                "output": ref.model_dump(mode="json"),
                "preview": None if b"\0" in sample else sample.decode("utf-8", errors="replace"),
                "truncated": len(content) > limit,
            }
        )
        return
    _operator_result(ref.model_dump(mode="json"))


@operator.command("current-output")
@click.option("--project-dir", type=click.Path())
@click.option("--node-id")
@click.option("--output-id")
@click.option("--json", "as_json", is_flag=True)
def operator_current_output(
    project_dir: str | None,
    node_id: str | None,
    output_id: str | None,
    as_json: bool,
) -> None:
    from assurance_product.current_node_output import (
        CurrentNodeOutputError,
        list_current_node_outputs,
        preview_current_node_output,
    )

    _require_options(
        {"project_dir": project_dir, "node_id": node_id, "json": as_json},
        ("project_dir", "node_id", "json"),
    )
    task = Path(cast(str, project_dir))
    node = cast(str, node_id)
    try:
        payload = (
            preview_current_node_output(task, node, output_id)
            if output_id
            else list_current_node_outputs(task, node)
        )
    except CurrentNodeOutputError as error:
        _operator_fail(OperatorError("unavailable", str(error)))
    _operator_result(payload)


@operator.command("status")
@click.option("--project-dir", type=click.Path())
@click.option("--run-id")
@click.option("--json", "as_json", is_flag=True)
def operator_status(project_dir: str | None, run_id: str | None, as_json: bool) -> None:
    _require_options(
        {"project_dir": project_dir, "run_id": run_id, "json": as_json},
        ("project_dir", "run_id", "json"),
    )
    try:
        payload = AssuranceOperator().status(
            project_dir=Path(cast(str, project_dir)),
            run_id=cast(str, run_id),
        )
    except OperatorError as error:
        _operator_fail(error)
    _operator_result(payload)


@operator.command("stop")
@click.option("--project-dir", type=click.Path())
@click.option("--run-id")
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--force", is_flag=True, help="Terminate the verified owned worker and confirm its activity stopped."
)
def operator_stop(project_dir: str | None, run_id: str | None, as_json: bool, force: bool) -> None:
    _require_options(
        {"project_dir": project_dir, "run_id": run_id, "json": as_json},
        ("project_dir", "run_id", "json"),
    )
    try:
        payload = AssuranceOperator().stop(
            project_dir=Path(cast(str, project_dir)),
            run_id=cast(str, run_id),
            force=force,
        )
    except OperatorError as error:
        _operator_fail(error)
    _operator_result(payload)


@operator.command("resume")
@click.option("--project-dir", type=click.Path())
@click.option("--run-id")
@click.option("--mode", type=click.Choice(("restart_terminal", "resolve_interrupt")))
@click.option("--action")
@click.option("--reason")
@click.option("--json", "as_json", is_flag=True)
def operator_resume(
    project_dir: str | None,
    run_id: str | None,
    mode: str | None,
    action: str | None,
    reason: str | None,
    as_json: bool,
) -> None:
    _require_options(
        {
            "project_dir": project_dir,
            "run_id": run_id,
            "mode": mode,
            "json": as_json,
        },
        ("project_dir", "run_id", "mode", "json"),
    )
    try:
        payload = AssuranceOperator().resume(
            project_dir=Path(cast(str, project_dir)),
            run_id=cast(str, run_id),
            mode=cast(str, mode),
            action=action,
            reason=reason,
            environ=os.environ,
        )
    except OperatorError as error:
        _operator_fail(error)
    _operator_result(payload)


@operator.command("assessment")
@click.option("--project-dir", type=click.Path())
@click.option("--change-id")
@click.option("--plan-digest")
@click.option("--coverage-epoch")
@click.option("--repair-round")
@click.option("--json", "as_json", is_flag=True)
def operator_assessment(
    project_dir: str | None,
    change_id: str | None,
    plan_digest: str | None,
    coverage_epoch: str | None,
    repair_round: str | None,
    as_json: bool,
) -> None:
    _require_options(
        {
            "project_dir": project_dir,
            "change_id": change_id,
            "plan_digest": plan_digest,
            "coverage_epoch": coverage_epoch,
            "repair_round": repair_round,
            "json": as_json,
        },
        ("project_dir", "change_id", "plan_digest", "coverage_epoch", "repair_round", "json"),
    )
    payload = AssuranceOperator().assessment(
        project_dir=Path(cast(str, project_dir)),
        change_id=cast(str, change_id),
        plan_digest=cast(str, plan_digest),
        coverage_epoch=cast(str, coverage_epoch),
        repair_round=cast(str, repair_round),
    )
    _operator_result(payload)


@app.group("bootstrap")
def bootstrap() -> None:
    """Operator wrapper that prepares composition and drives an entrypoint."""


@bootstrap.command("run")
@click.option("--project-dir", type=click.Path())
@click.option("--spec", type=click.Path())
@click.option("--runs-root", type=click.Path())
@click.option("--change")
@click.option("--json", "as_json", is_flag=True)
def bootstrap_run(
    project_dir: str | None,
    spec: str | None,
    runs_root: str | None,
    change: str | None,
    as_json: bool,
) -> None:
    _require_options(
        {
            "project_dir": project_dir,
            "spec": spec,
            "runs_root": runs_root,
            "json": as_json,
        },
        ("project_dir", "spec", "runs_root", "json"),
    )
    status_path: Path | None = None
    previous_status_bytes: bytes | None = None
    if (
        as_json
        and runs_root is not None
        and change is not None
        and change not in {"", ".", ".."}
        and not any(character in change for character in ("/", "\\", " ", "\x00"))
    ):
        status_path = Path(runs_root) / change / "bootstrap-status.json"
        try:
            previous_status_bytes = status_path.read_bytes()
        except OSError:
            pass
    try:
        loaded = load_run_spec(Path(cast(str, spec)))
        status = run_bootstrap(
            project_dir=Path(cast(str, project_dir)),
            spec=loaded,
            runs_root=Path(cast(str, runs_root)),
            change_id=change,
            environ=os.environ,
        )
    except Exception as error:
        if status_path is not None:
            try:
                current_status_bytes = status_path.read_bytes()
            except (OSError, ValueError):
                pass
            else:
                if current_status_bytes != previous_status_bytes:
                    try:
                        persisted = read_bootstrap_status(status_path.parent)
                    except (OSError, ValueError):
                        pass
                    else:
                        if persisted.phase == "terminal" and persisted.change_id == change:
                            _emit(persisted.model_dump(mode="json"))
        _fail(str(error), error.code if isinstance(error, CommandError) else 40)
    _emit(status.model_dump(mode="json"))
    raise SystemExit(status.exit_code or 0)


@bootstrap.command("status")
@click.option("--run-dir", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def bootstrap_status(run_dir: str | None, as_json: bool) -> None:
    _require_options({"run_dir": run_dir, "json": as_json}, ("run_dir", "json"))
    try:
        status = read_bootstrap_status(Path(cast(str, run_dir)))
    except Exception as error:
        _fail(str(error), 40)
    _emit(status.model_dump(mode="json"))


@bootstrap.command("stop")
@click.option("--run-dir", type=click.Path())
@click.option(
    "--force", is_flag=True, help="Terminate the verified owned worker and confirm its activity stopped."
)
def bootstrap_stop(run_dir: str | None, force: bool) -> None:
    _require_options({"run_dir": run_dir}, ("run_dir",))
    try:
        destination = Path(cast(str, run_dir))
        before = read_bootstrap_status(destination)
        status = stop_bootstrap(destination, force=force)
    except Exception as error:
        _fail(str(error), 40)
    _emit(status.model_dump(mode="json"))
    raise SystemExit(0 if force or before.phase == "terminal" else 20)


@bootstrap.command("resume")
@click.option("--run-dir", type=click.Path())
@click.option("--json", "as_json", is_flag=True)
def bootstrap_resume(run_dir: str | None, as_json: bool) -> None:
    _require_options({"run_dir": run_dir, "json": as_json}, ("run_dir", "json"))
    try:
        status = resume_bootstrap(Path(cast(str, run_dir)), environ=os.environ)
    except (BootstrapPreflightError, SpecOverrideError, OpenCodeLaunchError) as error:
        _fail(str(error), 40)
    except Exception as error:
        _fail(str(error), 40)
    _emit(status.model_dump(mode="json"))
    raise SystemExit(status.exit_code or 0)


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


def _project_for_run(project_dir: Path, change_id: str) -> Path:
    try:
        return ensure_run_worktree(require_real_directory(project_dir), change_id)
    except ValueError as error:
        raise CommandError(str(error)) from error


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


@exclusive_cli
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
    reuse_directory: bool = False,
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
    if not reuse_directory:
        project_dir = _project_for_run(project_dir, change_id)
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


@exclusive_cli
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
    reuse_directory: bool = False,
    stop_file: Path | None = None,
) -> tuple[SimpleRun, str]:
    composition, _audit = _resolve_and_audit(
        product=product,
        binding_dist=binding_dist,
        binding_entrypoint=binding_entrypoint,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    authorization = _authorize_secrets(composition, secrets)
    if not reuse_directory:
        project_dir = _project_for_run(project_dir, change_id)
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
            stop_file=stop_file,
        )
    return cast(SimpleRun, result), mapped


@exclusive_cli
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
    stop_file: Path | None = None,
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
                stop_file=stop_file,
            ),
        )
