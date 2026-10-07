"""Product operator for one exact Assurance run.

OpenChamber and `/assure` call this interface. It allocates the run and writes
the effective spec. It does not choose graph nodes or seal evidence.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from assurance_product.bootstrap.contracts import BootstrapStatusV1, RunSpecV1
from assurance_product.bootstrap.driver import resume_bootstrap, utc_stamp
from assurance_product.bootstrap.status import (
    derive_bootstrap_change_id,
    read_bootstrap_status,
    read_run_manifest,
    run_dir_for,
    write_bootstrap_status,
    write_effective_spec,
    write_run_manifest,
)
from assurance_product.models import TEST_FAMILY_ORDER
from assurance_product.sut_worktree import ensure_run_worktree
from assurance_product.worker_lifecycle import ExecutionConflict

_FAMILIES = tuple(TEST_FAMILY_ORDER)
_LOOPBACK = frozenset({"127.0.0.1", "localhost"})


class OperatorError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _stamp() -> str:
    return utc_stamp()


def _nonce() -> str:
    return secrets.token_hex(4)


def launch_worker(*, run_dir: Path, change_id: str, environ: Mapping[str, str]) -> None:
    import sys
    from assurance_product.worker_lifecycle import acquire_execution, launch_reserved
    from assurance_product.worker_entrypoints import run_workspace

    workspace, invocation = run_workspace(run_dir)
    if invocation != change_id:
        raise OperatorError("invalid_input", "worker invocation identity differs")
    with acquire_execution(workspace, invocation, run_dir=run_dir) as owner:
        launch_reserved(
            owner, [sys.executable, "-m", "assurance_product.operator_worker", str(run_dir)], environ
        )


def serve_run(run_dir: Path, *, environ: Mapping[str, str] | None = None) -> BootstrapStatusV1:
    """Drive one operator-created run against its recorded OpenCode endpoint."""
    from assurance_product.bootstrap.driver import run_bootstrap
    from assurance_product.bootstrap.spec import load_run_spec

    manifest = read_run_manifest(run_dir)
    source = manifest.get("source_project_dir")
    task_value = manifest.get("task_directory")
    task_directory = Path(task_value) if isinstance(task_value, str) and task_value else None
    if isinstance(source, str) and source:
        provenance: Path = Path(source)
    elif task_directory is not None:
        provenance = task_directory
    else:
        raise OperatorError("invalid_input", "operator run is missing source_project_dir")
    endpoint = manifest.get("requested_opencode_endpoint")
    shared_endpoint = endpoint if isinstance(endpoint, str) and endpoint else None
    if manifest.get("ownership") != "shared":
        shared_endpoint = None
    return run_bootstrap(
        project_dir=provenance,
        spec=load_run_spec(run_dir / "run-spec.effective.yaml"),
        runs_root=run_dir.parent,
        change_id=str(manifest["change_id"]),
        environ=dict(environ or os.environ),
        shared_endpoint=shared_endpoint,
        task_directory=task_directory,
    )


def drive_stop(run_dir: Path) -> None:
    if read_bootstrap_status(run_dir).phase != "terminal":
        raise OperatorError("stop_pending", "stop requested; waiting for a durable graph pause")


def resume_terminal_run(*, run_dir: Path, environ: Mapping[str, str]) -> BootstrapStatusV1:
    return resume_bootstrap(run_dir, environ=environ)


def resolve_graph_interrupt(
    *,
    run_dir: Path,
    action: str,
    reason: str,
    environ: Mapping[str, str],
) -> BootstrapStatusV1:
    return resume_bootstrap(run_dir, environ=environ, action=action, reason=reason)


def build_effective_spec(
    config: Mapping[str, object],
    *,
    requirement: str,
    families: tuple[str, ...],
) -> RunSpecV1:
    text = requirement.strip()
    if not text:
        raise OperatorError("invalid_input", "requirement is required")
    if not families:
        raise OperatorError("invalid_input", "candidate_test_families is required")
    unknown = [name for name in families if name not in _FAMILIES]
    if unknown:
        raise OperatorError("invalid_input", "invalid test family: " + ", ".join(unknown))
    if len(set(families)) != len(families):
        raise OperatorError("invalid_input", "candidate_test_families must be unique")
    ordered = tuple(name for name in _FAMILIES if name in families)
    project = config.get("project")
    urls = config.get("urls")
    if not isinstance(project, Mapping) or not isinstance(urls, Mapping):
        raise OperatorError("invalid_input", "project config is missing project or urls")
    project_type = str(project.get("type") or "backend")
    base_url = urls.get("frontend") if project_type == "frontend" else urls.get("backend")
    if not isinstance(base_url, str) or not base_url:
        raise OperatorError("invalid_input", "project config is missing the SUT URL")
    readiness = base_url if project_type == "frontend" else base_url.rstrip("/") + "/openapi.json"
    env = {"BASE_URL": base_url}
    frontend = urls.get("frontend")
    if isinstance(frontend, str) and frontend:
        env["FRONTEND_URL"] = frontend
    execution = config.get("execution")
    routing = execution.get("model_routing") if isinstance(execution, Mapping) else None
    model = routing.get("default") if isinstance(routing, Mapping) else None
    if not isinstance(model, str) or not model.strip():
        raise OperatorError("invalid_input", "project config is missing execution.model_routing.default")
    try:
        sut: dict[str, object] = {
            "base_url": base_url,
            "readiness_url": readiness,
            "env": env,
            "env_from_node": ["QA_ADMIN_PASSWORD"],
        }
        backend = urls.get("backend")
        if isinstance(backend, str) and backend:
            sut["api_base_url"] = backend
        if isinstance(frontend, str) and frontend:
            sut["ui_base_url"] = frontend
        return RunSpecV1.model_validate(
            {
                "schema_version": "1",
                "product": "assurance-opencode",
                "entrypoint": "full",
                "requirement": text,
                "candidate_test_families": list(ordered),
                "case_modules": [],
                "sut": sut,
                "routes": {
                    "provider_model": model.strip(),
                    "worker_profile": "max",
                },
                "budgets": {
                    "review_rounds": 4,
                    "coverage_rounds": 2,
                    "healing_rounds": 2,
                    "execution_retries": 2,
                },
                "opencode_token_env": "AA_NEXT_OPENCODE_TOKEN",
                "timeout_seconds": 28800,
            }
        )
    except Exception as error:
        raise OperatorError("invalid_input", str(error)) from error


def _require_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if (
        parsed.scheme != "http"
        or host not in _LOOPBACK
        or parsed.port is None
        or parsed.username
        or parsed.password
    ):
        raise OperatorError("invalid_input", "opencode_endpoint must be a loopback HTTP URL")
    return endpoint


def _execution_config(
    project_dir: Path,
    task_directory: Path | None,
) -> tuple[Mapping[str, object], str]:
    """Read task config when it exists. A present but invalid task config does not fall back."""
    if task_directory is not None and (task_directory / ".aa" / "config.yaml").exists():
        source = task_directory / ".aa" / "config.yaml"
        return _load_config(task_directory), str(source.resolve())
    source = project_dir / ".aa" / "config.yaml"
    return _load_config(project_dir), str(source.resolve())


def _load_config(project_dir: Path) -> Mapping[str, object]:
    path = project_dir / ".aa" / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise OperatorError("invalid_input", f"config.yaml is unreadable: {error}") from error
    if not isinstance(raw, Mapping):
        raise OperatorError("invalid_input", "config.yaml must be a mapping")
    return raw


def _runs_root(project_dir: Path) -> Path:
    return project_dir / ".aa" / "runs"


def _run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.is_dir():
        return []
    return sorted(path for path in runs_root.iterdir() if path.is_dir())


def _require_run(project_dir: Path, run_id: str) -> Path:
    runs_root = _runs_root(project_dir)
    found = _run_dirs(runs_root)
    destination = runs_root / run_id
    if not found:
        raise OperatorError("no_runs", "project has no runs")
    if destination not in found or not (destination / "bootstrap-status.json").is_file():
        raise OperatorError("unknown_run", f"unknown run: {run_id}")
    return destination


def _active_run(project_dir: Path) -> Path | None:
    expected = project_dir.resolve()
    for candidate in _run_dirs(_runs_root(project_dir)):
        try:
            status = read_bootstrap_status(candidate)
            manifest = read_run_manifest(candidate)
        except (OSError, ValueError):
            continue
        if status.phase == "terminal":
            continue
        source = manifest.get("source_project_dir") or manifest.get("project_dir")
        if isinstance(source, str) and Path(source).resolve() == expected:
            return candidate
    return None


class AssuranceOperator:
    def start(
        self,
        *,
        project_dir: Path,
        requirement: str,
        families: tuple[str, ...],
        opencode_endpoint: str,
        origin_session_id: str | None = None,
        origin_parent_session_id: str | None = None,
        task_directory: Path | None = None,
        request_id: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        if origin_parent_session_id:
            raise OperatorError(
                "invalid_input",
                "a child session cannot start a run; start from the operator session",
            )
        root = project_dir.resolve()
        selected_task: Path | None = None
        if task_directory is not None:
            selected_task = task_directory.resolve()
            if not selected_task.is_dir():
                raise OperatorError("invalid_input", "task directory does not exist")
        config, config_source = _execution_config(root, selected_task)
        spec = build_effective_spec(config, requirement=requirement, families=families)
        endpoint = _require_endpoint(opencode_endpoint)
        if selected_task is not None:
            return self._start_managed(
                root=root,
                task_directory=selected_task,
                requirement=requirement,
                families=families,
                endpoint=endpoint,
                config_source=config_source,
                spec=spec,
                request_id=request_id,
                origin_session_id=origin_session_id,
                environ=environ,
            )
        change_id = derive_bootstrap_change_id(stamp=_stamp(), nonce=_nonce())
        active = _active_run(root)
        if active is not None:
            raise OperatorError("conflict", f"project already has an active run: {active.name}")
        worktree = ensure_run_worktree(root, change_id)
        runs_root = _runs_root(root)
        run_dir = run_dir_for(runs_root, change_id)
        write_effective_spec(run_dir, spec)
        manifest: dict[str, object] = {
            "source_project_dir": str(root),
            "project_dir": str(worktree),
            "worktree": str(worktree),
            "change_id": change_id,
            "invocation_id": change_id,
            "origin_session_id": origin_session_id,
            "ownership": "shared",
            "requested_opencode_endpoint": endpoint,
            "config_source": config_source,
        }
        write_run_manifest(run_dir, manifest)
        write_bootstrap_status(
            run_dir,
            BootstrapStatusV1(phase="preparing", change_id=change_id),
        )
        child_env = dict(environ or os.environ)
        try:
            launch_worker(run_dir=run_dir, change_id=change_id, environ=child_env)
        except ExecutionConflict as error:
            raise OperatorError("conflict", str(error)) from error
        except OperatorError:
            raise
        except Exception as error:
            write_bootstrap_status(
                run_dir,
                BootstrapStatusV1(
                    phase="terminal",
                    change_id=change_id,
                    exit_code=40,
                    error=str(error),
                ),
            )
            raise OperatorError("worker_failed", str(error)) from error
        status = read_bootstrap_status(run_dir)
        return {
            "run_id": change_id,
            "change_id": change_id,
            "project_dir": str(root),
            "worktree": str(worktree),
            "phase": status.phase,
        }

    def _start_managed(
        self,
        *,
        root: Path,
        task_directory: Path,
        requirement: str,
        families: tuple[str, ...],
        endpoint: str,
        config_source: str,
        spec: RunSpecV1,
        request_id: str | None,
        origin_session_id: str | None,
        environ: Mapping[str, str] | None,
    ) -> dict[str, str]:
        from assurance_product.task_records import (
            TaskRecordError,
            active_manifest,
            allocation_lock,
            input_digest,
            managed_baseline,
            read_task,
            run_manifests,
            write_next_run_number,
        )

        if not request_id:
            raise OperatorError("invalid_input", "request_id is required")
        try:
            definition = read_task(task_directory)
        except TaskRecordError as error:
            raise OperatorError(error.kind, str(error)) from error
        if definition is None:
            raise OperatorError("not_configured", "task is not configured")
        if definition.project_dir != str(root):
            raise OperatorError("invalid_input", "invalid task identity")
        if (
            requirement.strip() != definition.requirement
            or tuple(name for name in _FAMILIES if name in families) != definition.test_families
        ):
            raise OperatorError("conflict", "request overrides the saved task inputs")
        digest = input_digest(
            task_id=definition.task_id,
            requirement=definition.requirement,
            families=definition.test_families,
            config_source=config_source,
            endpoint=endpoint,
        )
        from contextlib import ExitStack
        from assurance_product.worker_lifecycle import acquire_execution

        with ExitStack() as ownership:
            try:
                with allocation_lock(task_directory, definition.task_id):
                    manifests = run_manifests(task_directory)
                    matches = [row for row in manifests if row.get("request_id") == request_id]
                    if matches:
                        if matches[0].get("input_digest") != digest:
                            raise OperatorError("conflict", "request ID already belongs to different inputs")
                        allocated = matches[0]
                        change_id = str(allocated["change_id"])
                        run_dir = Path(str(allocated["run_dir"]))
                        launch = False
                    else:
                        if active_manifest(manifests) is not None:
                            raise OperatorError("conflict", "task already has an active run")
                        change_id = derive_bootstrap_change_id(stamp=_stamp(), nonce=_nonce())
                        ownership.enter_context(
                            acquire_execution(
                                task_directory,
                                change_id,
                                run_dir=run_dir_for(task_directory / ".aa" / "runs", change_id),
                            )
                        )
                        number = definition.next_run_number
                        definition = write_next_run_number(task_directory, definition, number + 1)
                        run_dir = run_dir_for(task_directory / ".aa" / "runs", change_id)
                        baseline = list(
                            managed_baseline(
                                (
                                    {"path": ".aa/task.json"},
                                    {"path": f".aa/runs/{change_id}/bootstrap-status.json"},
                                )
                            )
                        )
                        write_effective_spec(run_dir, spec)
                        write_run_manifest(
                            run_dir,
                            {
                                "source_project_dir": str(root),
                                "project_dir": str(task_directory),
                                "worktree": str(task_directory),
                                "task_directory": str(task_directory),
                                "task_id": definition.task_id,
                                "change_id": change_id,
                                "invocation_id": change_id,
                                "run_number": number,
                                "request_id": request_id,
                                "input_digest": digest,
                                "baseline": baseline,
                                "origin_session_id": origin_session_id,
                                "ownership": "shared",
                                "requested_opencode_endpoint": endpoint,
                                "config_source": config_source,
                            },
                        )
                        write_bootstrap_status(
                            run_dir,
                            BootstrapStatusV1(phase="preparing", change_id=change_id),
                        )
                        launch = True
            except ExecutionConflict as error:
                raise OperatorError("conflict", str(error)) from error
            except TaskRecordError as error:
                raise OperatorError(error.kind, str(error)) from error
            if launch:
                self._launch_or_fail(run_dir=run_dir, change_id=change_id, environ=environ)
            status = read_bootstrap_status(run_dir)
            recorded = read_run_manifest(run_dir)
            number = recorded.get("run_number")
            return {
                "run_id": change_id,
                "change_id": change_id,
                "project_dir": str(root),
                "worktree": str(task_directory),
                "task_directory": str(task_directory),
                "run_number": "" if number is None else str(number),
                "phase": status.phase,
            }

    def _launch_or_fail(
        self,
        *,
        run_dir: Path,
        change_id: str,
        environ: Mapping[str, str] | None,
    ) -> None:
        try:
            from assurance_product.worker_lifecycle import acquire_execution
            from assurance_product.worker_entrypoints import run_workspace

            workspace, invocation = run_workspace(run_dir)
            with acquire_execution(workspace, invocation, run_dir=run_dir):
                recorded = read_run_manifest(run_dir)
                task_value = recorded.get("task_directory")
                if isinstance(task_value, str) and task_value:
                    from assurance_product.task_records import prepare_managed_workspace

                    prepare_managed_workspace(Path(task_value), change_id)
                launch_worker(run_dir=run_dir, change_id=change_id, environ=dict(environ or os.environ))
        except ExecutionConflict as error:
            raise OperatorError("conflict", str(error)) from error
        except OperatorError:
            raise
        except Exception as error:
            write_bootstrap_status(
                run_dir,
                BootstrapStatusV1(
                    phase="terminal",
                    change_id=change_id,
                    exit_code=40,
                    error=str(error),
                ),
            )
            raise OperatorError("worker_failed", str(error)) from error

    def preflight(
        self,
        *,
        project_dir: Path,
        requirement: str,
        families: tuple[str, ...],
        opencode_endpoint: str,
        task_directory: Path | None = None,
    ) -> dict[str, object]:
        root = project_dir.resolve()
        selected = task_directory.resolve() if task_directory is not None else None
        if selected is not None and not selected.is_dir():
            raise OperatorError("invalid_input", "task directory does not exist")
        _config, config_source = _execution_config(root, selected)
        spec = build_effective_spec(_config, requirement=requirement, families=families)
        endpoint = _require_endpoint(opencode_endpoint)
        return {
            "project_dir": str(root),
            "task_directory": str(selected) if selected is not None else None,
            "config_source": config_source,
            "requirement": spec.requirement,
            "test_families": list(spec.candidate_test_families),
            "opencode_endpoint": endpoint,
        }

    def status(self, *, project_dir: Path, run_id: str) -> dict[str, object]:
        run_dir = _require_run(project_dir.resolve(), run_id)
        return read_bootstrap_status(run_dir).model_dump(mode="json")

    def stop(self, *, project_dir: Path, run_id: str, force: bool = False) -> dict[str, object]:
        run_dir = _require_run(project_dir.resolve(), run_id)
        current = read_bootstrap_status(run_dir)
        if force:
            from assurance_product.bootstrap.driver import stop_bootstrap

            try:
                return stop_bootstrap(run_dir, force=True).model_dump(mode="json")
            except (ValueError, RuntimeError) as error:
                raise OperatorError("stop_unconfirmed", str(error)) from error
        if current.phase == "terminal":
            return current.model_dump(mode="json")
        from assurance_product.application import AssuranceProductApplication

        AssuranceProductApplication().request_stop(run_dir, change_id=run_id)
        try:
            drive_stop(run_dir)
        except ExecutionConflict as error:
            raise OperatorError("conflict", str(error)) from error
        except OperatorError:
            raise
        resolved = read_bootstrap_status(run_dir)
        if resolved.phase != "terminal" or resolved.change_id != run_id:
            raise OperatorError("stop_pending", "cancellation has not resolved")
        return resolved.model_dump(mode="json")

    def resume(
        self,
        *,
        project_dir: Path,
        run_id: str,
        mode: str,
        action: str | None = None,
        reason: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        run_dir = _require_run(project_dir.resolve(), run_id)
        child_env = dict(environ or os.environ)
        if mode == "restart_terminal":
            if action or reason:
                raise OperatorError("invalid_resume", "restart_terminal does not take action or reason")
            status = resume_terminal_run(run_dir=run_dir, environ=child_env)
        elif mode == "resolve_interrupt":
            if not action or not reason:
                raise OperatorError("invalid_resume", "resolve_interrupt requires action and reason")
            status = resolve_graph_interrupt(
                run_dir=run_dir,
                action=action,
                reason=reason,
                environ=child_env,
            )
        else:
            raise OperatorError("invalid_resume", f"unknown resume mode: {mode}")
        if status.change_id != run_id:
            raise OperatorError("unknown_run", "resume returned a different run")
        return status.model_dump(mode="json")

    def assessment(
        self,
        *,
        project_dir: Path,
        change_id: str,
        plan_digest: str,
        coverage_epoch: str,
        repair_round: str,
    ) -> dict[str, object]:
        from assurance_product.assessment_read import read_committed_assessment

        return read_committed_assessment(
            project_dir=project_dir.resolve(),
            change_id=change_id,
            plan_digest=plan_digest,
            coverage_epoch=coverage_epoch,
            repair_round=repair_round,
        )


__all__ = [
    "AssuranceOperator",
    "OperatorError",
    "build_effective_spec",
    "drive_stop",
    "launch_worker",
    "resolve_graph_interrupt",
    "resume_terminal_run",
    "serve_run",
]
