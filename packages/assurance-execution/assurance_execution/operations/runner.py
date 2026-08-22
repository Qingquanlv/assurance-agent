"""Closed-mapping test execution behind a private process host."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.agent import RunTestsInputV1
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    leafs_of,
    validate_input,
)
from assurance_execution.operations.normalize import normalize_evidence
from assurance_execution.operations.paths import resolve_selected_file


@dataclass(frozen=True, slots=True)
class ProcessReceipt:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    report: Mapping[str, object] | None


@runtime_checkable
class ExecutionProcessHost(Protocol):
    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt: ...


_JSON_REPORT_FILE = ".assurance-execution-report.json"
_REPORT_REASON = "pytest report path must be a regular file under the workspace"


class ConfinedExecutionProcessHost:
    """Argv-only spawn. Production host; tests inject a fake instead."""

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt:
        if not argv or any("\x00" in item for item in argv):
            raise InputError("execution argv must be a confined non-empty command")
        report_path = _confined_report_path(argv, cwd)
        if (
            report_path is not None
            and report_path.exists()
            and (report_path.is_symlink() or not report_path.is_file())
        ):
            raise InputError(_REPORT_REASON)
        completed = subprocess.run(  # noqa: S603
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            env=_scrubbed_env(),
        )
        report = _load_confined_report(report_path, cwd)
        return ProcessReceipt(
            command=argv,
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            report=report,
        )


def _scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _confined_report_path(argv: tuple[str, ...], cwd: Path) -> Path | None:
    raw: str | None = None
    for item in argv:
        prefix = "--json-report-file="
        if item.startswith(prefix):
            raw = item[len(prefix) :]
            break
    if raw is None:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != raw:
        raise InputError(_REPORT_REASON)
    path = cwd.joinpath(*candidate.parts)
    try:
        path.resolve().relative_to(cwd.resolve())
    except ValueError as error:
        raise InputError(_REPORT_REASON) from error
    return path


def _load_confined_report(report_path: Path | None, cwd: Path) -> Mapping[str, object] | None:
    if report_path is None:
        return None
    if report_path.is_symlink() or not report_path.is_file():
        raise InputError(_REPORT_REASON)
    try:
        report_path.resolve().relative_to(cwd.resolve())
    except ValueError as error:
        raise InputError(_REPORT_REASON) from error
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputError(_REPORT_REASON) from error
    if not isinstance(payload, Mapping):
        raise InputError(_REPORT_REASON)
    return payload


def _closed_mapping(payload: RunTestsInputV1) -> ClosedMappingV1:
    leafs = leafs_of(payload.capability_leafs)
    case_ids = leafs_of(payload.case_ids)
    try:
        return ClosedMappingV1.model_validate(
            payload.mapping.model_dump(mode="json"),
            context={"capability_leafs": leafs, "case_ids": case_ids},
        )
    except ValidationError as error:
        raise InputError(str(error)) from error


def _authenticate_selected(workspace: Path, mapping: ClosedMappingV1) -> tuple[str, ...]:
    selected = tuple(mapping.selected)
    resolved: list[str] = []
    for relative in selected:
        resolve_selected_file(workspace, relative)
        resolved.append(relative)
    if frozenset(resolved) != frozenset(mapping.selected):
        raise InputError("mapping must equal selected tests")
    return selected


def build_pytest_argv(selected: tuple[str, ...]) -> tuple[str, ...]:
    return (
        "pytest",
        *selected,
        "-p",
        "no:cacheprovider",
        "--json-report",
        f"--json-report-file={_JSON_REPORT_FILE}",
    )


def _pr_metric_input(
    *,
    change_id: str,
    batch_id: str,
    selected: tuple[str, ...],
    mapping_digest_value: str,
    receipt_digest: str,
    results: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "change_id": change_id,
        "batch_id": batch_id,
        "selected": list(selected),
        "mapping_digest": mapping_digest_value,
        "receipt_digest": receipt_digest,
        "results": results,
    }


def run_closed_mapping(
    payload: RunTestsInputV1,
    workspace: Path,
    process_host: ExecutionProcessHost,
    *,
    include_pr_metrics: bool,
) -> dict[str, object]:
    mapping = _closed_mapping(payload)
    selected = _authenticate_selected(workspace, mapping)
    if not selected:
        evidence = normalize_evidence(
            change_id=payload.change_id,
            batch_id=payload.batch_id,
            selected_targets=payload.selected_targets,
            mapping=mapping,
            capability_leafs=leafs_of(payload.capability_leafs),
            case_ids=leafs_of(payload.case_ids),
            baseline_tree_id=payload.baseline_tree_id,
            runner_profile_digest=payload.runner_profile_digest,
            command=(),
            exit_code=0,
            report={},
        )
        return _run_output(payload, selected, evidence, include_pr_metrics=include_pr_metrics)
    argv = build_pytest_argv(selected)
    receipt = process_host.spawn(argv, workspace)
    report = receipt.report or {}
    evidence = normalize_evidence(
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        selected_targets=payload.selected_targets,
        mapping=mapping,
        capability_leafs=leafs_of(payload.capability_leafs),
        case_ids=leafs_of(payload.case_ids),
        baseline_tree_id=payload.baseline_tree_id,
        runner_profile_digest=payload.runner_profile_digest,
        command=receipt.command,
        exit_code=receipt.exit_code,
        report=report,
    )
    return _run_output(payload, selected, evidence, include_pr_metrics=include_pr_metrics)


def _run_output(
    payload: RunTestsInputV1,
    selected: tuple[str, ...],
    evidence: ExecutionEvidenceV1,
    *,
    include_pr_metrics: bool,
) -> dict[str, object]:
    output: dict[str, object] = {
        "executed": list(selected),
        "receipt": evidence.receipt.model_dump(mode="json"),
        "results": [item.model_dump(mode="json") for item in evidence.results],
        "mapping_digest": evidence.mapping_digest,
        "receipt_digest": evidence.receipt_digest,
        "evidence": evidence.model_dump(mode="json"),
    }
    if include_pr_metrics:
        output["pr_metric_input"] = _pr_metric_input(
            change_id=payload.change_id,
            batch_id=payload.batch_id,
            selected=selected,
            mapping_digest_value=evidence.mapping_digest,
            receipt_digest=evidence.receipt_digest,
            results=cast(list[dict[str, object]], output["results"]),
        )
    return output


class RunTestsHandler:
    def __init__(self, *, process_host: ExecutionProcessHost) -> None:
        self._process_host = process_host

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(RunTestsInputV1, request.input)
            output = run_closed_mapping(
                payload,
                context.workspace_root,
                self._process_host,
                include_pr_metrics=False,
            )
            return TaskOutcome.succeeded(cast(JSONValue, output))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)


class RunTestsAndCollectPrMetricsHandler:
    def __init__(self, *, process_host: ExecutionProcessHost) -> None:
        self._process_host = process_host

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            payload = validate_input(RunTestsInputV1, request.input)
            output = run_closed_mapping(
                payload,
                context.workspace_root,
                self._process_host,
                include_pr_metrics=True,
            )
            return TaskOutcome.succeeded(cast(JSONValue, output))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)


def classify_exit(exit_code: int, *, failed: int, collected: int) -> str:
    if failed > 0 or exit_code not in {0, 5}:
        return "failed"
    if collected == 0 or exit_code == 5:
        return "skipped"
    return "passed"
