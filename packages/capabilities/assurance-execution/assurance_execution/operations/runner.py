"""Closed-mapping test execution behind a private process host."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from assurance_execution.contracts.agent import ExecutionPrepareInputV1, RunTestsInputV1
from assurance_execution.contracts.evidence import ExecutionEvidenceV1, FamilyExecutionOutcomeV1
from assurance_execution.contracts.execution import EXECUTION_FAMILIES, ExecutionFamily
from assurance_execution.contracts.observations import (
    CollectorDocumentV1,
    ExecutionIdentityV1,
    ObservationBundleV1,
    ObservationRunContextV1,
    RuntimeObservationV1,
    SubjectBindingV1,
)
from assurance_generation.contracts.plans import ObligationMethodPlanV1
from assurance_intake.contracts.obligations import VerificationRequirementV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.observation_run import (
    OBSERVE_CONTEXT_FLAG,
    OBSERVE_OUTPUT_FLAG,
    RunnerUnsupported,
    build_family_argv,
    normalize_collector_report,
)
from assurance_execution.operations.common import (
    InputError,
    OutputError,
    leafs_of,
    mapping_digest,
    validate_input,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_execution.operations.normalize import normalize_evidence
from assurance_execution.operations.paths import (
    resolve_canonical_evidence,
    resolve_selected_file,
)


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
_MAX_RESPONSE_BYTES = 65536
_REPORT_REASON = "pytest report path must be a regular file under the workspace"


class ConfinedExecutionProcessHost:
    """Argv-only spawn. Production host; tests inject a fake instead."""

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt:
        if not argv or any("\x00" in item for item in argv):
            raise InputError("execution argv must be a confined non-empty command")
        public_argv = _public_pytest_argv(argv)
        report_path = _confined_report_path(public_argv, cwd)
        if (
            report_path is not None
            and report_path.exists()
            and (report_path.is_symlink() or not report_path.is_file())
        ):
            raise InputError(_REPORT_REASON)
        completed = subprocess.run(  # noqa: S603
            list(public_argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            env=_scrubbed_env(argv, cwd),
        )
        report = _load_confined_report(report_path, cwd)
        return ProcessReceipt(
            command=argv,
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            report=report,
        )


def runner_environment(batch_id: str) -> dict[str, str]:
    return {
        "HYPOTHESIS_STORAGE_DIRECTORY": f"/tmp/aa-hypothesis-{batch_id}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


_BATCH_FLAG = "--assurance-batch-id="


def _batch_id_from_argv(argv: tuple[str, ...]) -> str | None:
    for item in argv:
        if item.startswith(_BATCH_FLAG):
            return item[len(_BATCH_FLAG) :]
    return None


_HOST_FLAGS = (_BATCH_FLAG, OBSERVE_CONTEXT_FLAG, OBSERVE_OUTPUT_FLAG)


def _public_pytest_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in argv if not item.startswith(_HOST_FLAGS))


def _flag_value(argv: tuple[str, ...], flag: str) -> str | None:
    for item in argv:
        if item.startswith(flag):
            return item[len(flag) :]
    return None


def _confined_observe_path(raw: str, cwd: Path) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != raw:
        raise InputError("collector paths must be workspace-relative regular paths")
    path = cwd.joinpath(*candidate.parts)
    try:
        path.resolve().relative_to(cwd.resolve())
    except ValueError as error:
        raise InputError("collector paths must be workspace-relative regular paths") from error
    return path


def _scrubbed_env(argv: tuple[str, ...] | None = None, cwd: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("AA_OBSERVE_CONTEXT", None)
    env.pop("AA_OBSERVE_OUTPUT", None)
    batch_id = _batch_id_from_argv(argv or ())
    if batch_id:
        env.update(runner_environment(batch_id))
    else:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cwd is not None:
        for flag, name in (
            (OBSERVE_CONTEXT_FLAG, "AA_OBSERVE_CONTEXT"),
            (OBSERVE_OUTPUT_FLAG, "AA_OBSERVE_OUTPUT"),
        ):
            raw = _flag_value(argv or (), flag)
            if raw:
                env[name] = str(_confined_observe_path(raw, cwd))
        if batch_id:
            env["AA_OBSERVE_TOKEN"] = batch_id
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


def _project_config(project_root: Path) -> Path | None:
    for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"):
        candidate = project_root / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def build_pytest_argv(
    selected: tuple[str, ...],
    *,
    rootdir: Path | None = None,
    project_root: Path | None = None,
    batch_id: str | None = None,
    config: Path | None = None,
) -> tuple[str, ...]:
    argv: list[str] = ["pytest", *selected, "-p", "no:cacheprovider"]
    if rootdir is not None:
        argv.append(f"--rootdir={rootdir}")
        argv.append(f"--confcutdir={rootdir}")
    if project_root is not None:
        argv.append(f"-o=pythonpath={str((project_root / 'qa').resolve())}")
        resolved_config = config or _project_config(project_root)
        if resolved_config is not None:
            argv.append(f"-c={resolved_config}")
        argv.append("-o=asyncio_mode=auto")
    if batch_id is not None:
        argv.append(f"--assurance-batch-id={batch_id}")
    argv.extend(("--json-report", f"--json-report-file={_JSON_REPORT_FILE}"))
    return tuple(argv)


def write_canonical_evidence(
    project: Path,
    evidence: ExecutionEvidenceV1,
    *,
    filename: str = "execute-result.json",
) -> Path:
    if filename not in {"execute-result.json", "run-result.json"}:
        raise InputError("canonical evidence filename is not a closed execution result")
    path = resolve_canonical_evidence(project, evidence.change_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


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
    selected = tuple(mapping.selected)
    if not selected:
        raise InputError("execution mapping must contain at least one selected test")
    selected = _authenticate_selected(workspace, mapping)
    argv = build_pytest_argv(
        selected,
        project_root=workspace,
        batch_id=payload.batch_id,
    )
    receipt = process_host.spawn(argv, workspace)
    report = receipt.report or {}
    evidence = normalize_evidence(
        change_id=payload.change_id,
        plan_digest=payload.plan_digest,
        plan_ref=payload.plan_ref,
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
    write_canonical_evidence(workspace, evidence)
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


def authenticate_execution_output(
    expected: RunTestsInputV1,
    document: CollectorDocumentV1,
) -> None:
    if (
        document.identity.plan_digest != expected.plan_digest
        or document.identity.batch_id != expected.batch_id
    ):
        raise OutputError("collector identity does not match the locked execution input")
    if not document.complete:
        raise OutputError("collector document is incomplete")


class RunTestsHandler:
    def __init__(self, *, process_host: ExecutionProcessHost) -> None:
        self._process_host = process_host

    def _payload(self, request: TaskRequest, context: TaskContext) -> RunTestsInputV1:
        raw = request.input
        if isinstance(raw, Mapping) and "mapping" in raw:
            return validate_input(RunTestsInputV1, raw)
        from assurance_execution.operations.agent_skills import assemble_execution_input

        prepared = validate_input(ExecutionPrepareInputV1, raw)
        return assemble_execution_input(
            prepared,
            workspace=context.project_root,
            write_root=context.write_root,
        )

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            raw = request.input
            assembled = isinstance(raw, Mapping) and "mapping" in raw
            payload = self._payload(request, context)
            if payload.timeout_seconds <= 30:
                raise InputError("execution timeout must reserve 30 seconds for cleanup")
            if context.activity is not None:
                context.activity.mark_dispatch_started(
                    {
                        "handler": "assurance.execution.run-tests",
                        "batch_id": payload.batch_id,
                        "baseline_tree_id": payload.baseline_tree_id,
                        "runner_profile_digest": payload.runner_profile_digest,
                    }
                )
            if assembled:
                output = run_closed_mapping(
                    payload,
                    context.project_root,
                    self._process_host,
                    include_pr_metrics=False,
                )
                evidence = ExecutionEvidenceV1.model_validate(output["evidence"])
                observations_ref = None
            else:
                evidence, bundle = run_observed_mapping(payload, context.project_root, self._process_host)
                filename = "run-result.json" if payload.execution_kind == "run" else "execute-result.json"
                write_canonical_evidence(context.project_root, evidence, filename=filename)
                observations_ref = (
                    None
                    if bundle is None
                    else write_observation_bundle(context.project_root, payload, bundle)
                )
            if context.activity is not None:
                context.activity.bind({"batch_id": payload.batch_id})
            result = evidence.model_dump(mode="json")
            if observations_ref is not None:
                result["observations_ref"] = observations_ref.model_dump(mode="json")
            return TaskOutcome.succeeded(cast(JSONValue, result))
        except InputError as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        except OutputError as error:
            return TaskOutcome.failed("invalid_output", str(error), retryable=False)
        except ValidationError as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context
        if activity.state == "terminal_observed" and activity.terminal is not None:
            return TaskActivityReconcileResult(status="terminal", outcome=activity.terminal)
        if activity.dispatch_fingerprint is None:
            return TaskActivityReconcileResult(status="not_dispatched")
        return TaskActivityReconcileResult(status="indeterminate", reason="execution_interrupted")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context
        reference = activity.reference
        if isinstance(reference, Mapping):
            pid = reference.get("pid")
            if isinstance(pid, int) and pid > 0:
                try:
                    os.killpg(pid, 15)
                except OSError:
                    try:
                        os.kill(pid, 15)
                    except OSError:
                        pass
        return TaskActivityCancelResult(status="acknowledged")


def _observation_batch_root(payload: RunTestsInputV1) -> str:
    return f"qa/results/execution/epochs/{payload.coverage_epoch}/batches/{payload.batch_id}"


def load_observation_method(
    payload: RunTestsInputV1,
    workspace: Path,
) -> tuple[tuple[VerificationRequirementV1, ...], tuple[ObligationMethodPlanV1, ...]]:
    """Read the frozen observation method the generation cycle committed."""

    requirements: list[VerificationRequirementV1] = []
    plans: list[ObligationMethodPlanV1] = []
    for ref in payload.method_plan_refs:
        path = resolve_selected_file(workspace, ref.path)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.digest:
            raise InputError(f"observation method digest changed: {ref.path}")
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise InputError(f"observation method is not JSON: {ref.path}") from error
        if not isinstance(document, Mapping):
            raise InputError(f"observation method is not a document: {ref.path}")
        try:
            requirements.extend(
                VerificationRequirementV1.model_validate(item) for item in document.get("requirements") or ()
            )
            plans.extend(
                ObligationMethodPlanV1.model_validate(item) for item in document.get("method_plans") or ()
            )
        except ValidationError as error:
            raise InputError(f"invalid observation method: {error}") from error
    return tuple(requirements), tuple(plans)


def _write_observe_context(
    payload: RunTestsInputV1,
    workspace: Path,
    *,
    family: str,
    mapping: ClosedMappingV1,
    requirements: tuple[VerificationRequirementV1, ...],
    plans: tuple[ObligationMethodPlanV1, ...],
) -> tuple[str, str, ExecutionIdentityV1]:
    identity = ExecutionIdentityV1(
        plan_digest=payload.plan_digest,
        method_plan_refs=payload.method_plan_refs,
        mapping_digest=mapping_digest(mapping),
        batch_id=payload.batch_id,
        baseline_tree_id=payload.baseline_tree_id,
        runner_profile_digest=payload.runner_profile_digest,
    )
    context = ObservationRunContextV1(
        identity=identity,
        requirements=requirements,
        method_plans=plans,
        allowed_origins=payload.allowed_origins,
        timeout_seconds=payload.timeout_seconds,
        max_response_bytes=_MAX_RESPONSE_BYTES,
    )
    root = _observation_batch_root(payload)
    context_relative = f"{root}/collector/{family}-context.json"
    output_relative = f"{root}/collector/{family}-collector.json"
    context_path = workspace.joinpath(*context_relative.split("/"))
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_bytes(json.dumps(context.model_dump(mode="json"), sort_keys=True).encode("utf-8"))
    output_path = workspace.joinpath(*output_relative.split("/"))
    if output_path.exists() or output_path.is_symlink():
        output_path.unlink()
    return context_relative, output_relative, identity


def _read_collector_document(
    workspace: Path,
    relative: str,
    payload: RunTestsInputV1,
) -> CollectorDocumentV1:
    path = workspace.joinpath(*relative.split("/"))
    if path.is_symlink() or not path.is_file():
        raise OutputError("collector did not produce a document")
    try:
        document = CollectorDocumentV1.model_validate(json.loads(path.read_bytes().decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise OutputError(f"invalid collector document: {error}") from error
    authenticate_execution_output(payload, document)
    return document


def run_observed_mapping(
    payload: RunTestsInputV1,
    workspace: Path,
    process_host: ExecutionProcessHost,
) -> tuple[ExecutionEvidenceV1, ObservationBundleV1 | None]:
    mapping = _closed_mapping(payload)
    if not mapping.selected:
        raise InputError("execution mapping must contain at least one selected test")
    selected_families = tuple(
        family for family in EXECUTION_FAMILIES if getattr(payload.selected_targets, family)
    )
    requirements, plans = load_observation_method(payload, workspace)
    observed = bool(plans)
    outcomes: list[FamilyExecutionOutcomeV1] = []
    commands: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    observations: list[RuntimeObservationV1] = []
    collection_errors: list[str] = []
    identity: ExecutionIdentityV1 | None = None
    failed = False
    for family in selected_families:
        family_selected = tuple(entry.test for entry in mapping.mappings if entry.layer == family)
        context_relative: str | None = None
        output_relative: str | None = None
        try:
            if observed:
                family_mapping_for_context = ClosedMappingV1.model_validate(
                    {
                        "selected": list(family_selected),
                        "mappings": [
                            item.model_dump(mode="json") for item in mapping.mappings if item.layer == family
                        ],
                    },
                    context={
                        "capability_leafs": leafs_of(payload.capability_leafs),
                        "case_ids": leafs_of(payload.case_ids),
                    },
                )
                context_relative, output_relative, identity = _write_observe_context(
                    payload,
                    workspace,
                    family=family,
                    mapping=family_mapping_for_context,
                    requirements=requirements,
                    plans=plans,
                )
            argv = build_family_argv(
                cast(ExecutionFamily, family),
                family_selected,
                batch_id=payload.batch_id,
                observe_context=context_relative,
                observe_output=output_relative,
            )
        except RunnerUnsupported:
            relative = (
                f"qa/results/execution/epochs/{payload.coverage_epoch}/"
                f"batches/{payload.batch_id}/diagnostics/{family}.json"
            )
            diagnostic = workspace.joinpath(*relative.split("/"))
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            payload_bytes = (
                json.dumps({"family": family, "reason_code": "runner_unsupported"}) + "\n"
            ).encode()
            diagnostic.write_bytes(payload_bytes)
            outcomes.append(
                FamilyExecutionOutcomeV1(
                    family=cast(ExecutionFamily, family),
                    state="blocked",
                    reason_code="runner_unsupported",
                    diagnostic_refs=(
                        EvidenceArtifactRefV1(
                            path=relative,
                            digest=hashlib.sha256(payload_bytes).hexdigest(),
                        ),
                    ),
                )
            )
            failed = True
            continue
        _authenticate_selected(
            workspace,
            ClosedMappingV1.model_validate(
                {
                    "selected": list(family_selected),
                    "mappings": [
                        item.model_dump(mode="json") for item in mapping.mappings if item.layer == family
                    ],
                },
                context={
                    "capability_leafs": leafs_of(payload.capability_leafs),
                    "case_ids": leafs_of(payload.case_ids),
                },
            ),
        )
        receipt = process_host.spawn(argv, workspace)
        document = (
            None if output_relative is None else _read_collector_document(workspace, output_relative, payload)
        )
        # The collector owns the run report. Falling back to the host's report
        # would leave the evidence and the observations describing two runs.
        report = (
            receipt.report or {}
            if document is None
            else normalize_collector_report(document).model_dump(mode="json")
        )
        family_mapping = ClosedMappingV1.model_validate(
            {
                "selected": list(family_selected),
                "mappings": [
                    item.model_dump(mode="json") for item in mapping.mappings if item.layer == family
                ],
            },
            context={
                "capability_leafs": leafs_of(payload.capability_leafs),
                "case_ids": leafs_of(payload.case_ids),
            },
        )
        family_evidence = normalize_evidence(
            change_id=payload.change_id,
            plan_digest=payload.plan_digest,
            plan_ref=payload.plan_ref,
            batch_id=payload.batch_id,
            selected_targets={name: name == family for name in ("api", "e2e", "fuzz", "performance")},
            mapping=family_mapping,
            capability_leafs=leafs_of(payload.capability_leafs),
            case_ids=leafs_of(payload.case_ids),
            baseline_tree_id=payload.baseline_tree_id,
            runner_profile_digest=payload.runner_profile_digest,
            command=receipt.command,
            exit_code=receipt.exit_code,
            report=report,
        )
        commands.extend(item.model_dump(mode="json") for item in family_evidence.receipt.commands)
        results.extend(item.model_dump(mode="json") for item in family_evidence.results)
        if document is not None:
            observations.extend(document.observations)
            collection_errors.extend(document.collection_errors)
        outcomes.append(FamilyExecutionOutcomeV1(family=cast(ExecutionFamily, family), state="executed"))
        if family_evidence.status == "failed":
            failed = True
    evidence = ExecutionEvidenceV1.model_validate(
        {
            "schema_version": "1",
            "status": "failed" if failed else "passed",
            "change_id": payload.change_id,
            "plan_digest": payload.plan_digest,
            "plan_ref": payload.plan_ref.model_dump(mode="json"),
            "batch_id": payload.batch_id,
            "selected_targets": payload.selected_targets.model_dump(mode="json"),
            "mapping": mapping.model_dump(mode="json"),
            "baseline_tree_id": payload.baseline_tree_id,
            "runner_profile_digest": payload.runner_profile_digest,
            "receipt": {"commands": commands},
            "results": results,
            "family_outcomes": [item.model_dump(mode="json") for item in outcomes],
            "mapping_digest": mapping_digest(mapping),
            "receipt_digest": mapping_digest(mapping)
            if not commands
            else hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest(),
        }
    )
    if identity is None:
        return evidence, None
    bundle = ObservationBundleV1(
        schema_version="1",
        identity=identity,
        subject=SubjectBindingV1(
            kind="local",
            expected_identity=payload.baseline_tree_id,
            observed_identity=payload.baseline_tree_id,
            evidence_ref=None,
            status="matched",
        ),
        observations=tuple(observations),
        collection_errors=tuple(collection_errors),
    )
    return evidence, bundle


def write_observation_bundle(
    workspace: Path,
    payload: RunTestsInputV1,
    bundle: ObservationBundleV1,
) -> EvidenceArtifactRefV1:
    relative = f"{_observation_batch_root(payload)}/runtime-observations.json"
    path = workspace.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(bundle.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(encoded)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(encoded).hexdigest())


def classify_exit(exit_code: int, *, failed: int, collected: int) -> str:
    if failed > 0 or exit_code not in {0, 5}:
        return "failed"
    if collected == 0 or exit_code == 5:
        return "skipped"
    return "passed"
