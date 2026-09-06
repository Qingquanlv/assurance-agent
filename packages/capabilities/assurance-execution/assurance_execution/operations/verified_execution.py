"""Parent-owned action execution and authenticated recoverable evidence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import ctypes
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from assurance_execution.contracts.verification import VerificationManifestV1
from assurance_execution.operations.sqlite_oracle import observe_user
from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1


# The formal task adapter deliberately shares the prepare authentication seam.
from collections.abc import Callable, Mapping
from typing import Protocol

from pydantic import ValidationError

from graph_engine.attempts.activity import TaskActivityIndeterminate
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import (
    FrozenModel,
    TaskContext,
    TaskRequest,
    TaskOutcome,
    TaskActivitySnapshot,
    TaskActivityReconcileResult,
    TaskActivityCancelResult,
)
from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1
from assurance_execution.contracts.verification import (
    ManagedSutAuthorityV1,
    ObservationState,
    ObservationV1,
    VerificationEvidenceV1,
    EvidenceCompletionV1,
)
from assurance_execution.execution_view import ExecutionView, authenticate_execution_view
from assurance_execution.operations.managed_sut import (
    authenticate_managed_sut_receipts,
    managed_sut_ownership_token,
)
from assurance_execution.operations.verified_process import (
    ActionControl,
    DockerVerificationHost,
    ProcessLimits,
    VerifiedProcessReceiptV1,
)
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _publish_exclusive(source: Path, destination: Path) -> None:
    """Atomic no-replace rename; final records never have a partial or two-link state."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform == "linux":
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise OSError("atomic exclusive journal publication requires Linux or macOS")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


class ActionJournal:
    """Exclusive, fsynced host records, authenticated by the retained SUT authority."""

    _NAMES = frozenset(
        {"action_started", "action_terminal", "process_terminal", "cleanup_terminal", "outcome"}
    )

    def __init__(self, root: Path, manifest: VerificationManifestV1, key: bytes) -> None:
        self.root = root
        self.manifest = manifest
        self._key = key
        self._binding = hashlib.sha256(_bytes(manifest.model_dump(mode="json"))).hexdigest()
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise ValueError("journal path must not contain symlinks")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, name: str) -> Path:
        if name not in self._NAMES:
            raise ValueError("unknown journal record")
        return self.root / f"{name}.json"

    def write(self, name: str, payload: dict[str, Any]) -> None:
        document = {"manifest_digest": self._binding, "record": name, "payload": payload}
        document["seal"] = hmac.new(self._key, _bytes(document), hashlib.sha256).hexdigest()
        data = _bytes(document)
        destination = self._path(name)
        if destination.exists() or destination.is_symlink():
            if name != "action_started" and self.read(name) == payload and destination.read_bytes() == data:
                return
            raise FileExistsError("journal claim already exists")
        fd, temporary = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o400)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("journal write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
            try:
                _publish_exclusive(Path(temporary), destination)
            except FileExistsError:
                if name == "action_started" or self.read(name) != payload or destination.read_bytes() != data:
                    raise
            directory = os.open(self.root, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.close(fd)
            Path(temporary).unlink(missing_ok=True)

    def read(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("journal authentication failed: nonregular record")
        if path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("journal authentication failed: oversized record")
        document = json.loads(path.read_bytes())
        seal = document.pop("seal", None)
        expected = hmac.new(self._key, _bytes(document), hashlib.sha256).hexdigest()
        if (
            not isinstance(seal, str)
            or not hmac.compare_digest(seal, expected)
            or document.get("manifest_digest") != self._binding
            or document.get("record") != name
        ):
            raise ValueError("journal authentication failed")
        return document["payload"]


async def _post(
    plan: CaseExecutionPlanV1, manifest: VerificationManifestV1, credential: bytes
) -> dict[str, Any]:
    async with asyncio.timeout(10):
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=10, trust_env=False, transport=httpx.AsyncHTTPTransport(retries=0)
        ) as client:
            async with client.stream(
                "POST",
                manifest.sut.base_url + plan.action.path,
                headers={"token": _credentials(credential)["token"]},
                json={
                    **manifest.inputs.model_dump(mode="json"),
                    "password": _credentials(credential)["user_password"],
                },
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 256 * 1024:
                        return {
                            "state": "error",
                            "status": response.status_code,
                            "reason": "response_too_large",
                        }
                try:
                    document = json.loads(body)
                except (UnicodeError, ValueError):
                    document = None
                return {
                    "state": "observed",
                    "status": response.status_code,
                    "code": document.get("code") if isinstance(document, dict) else None,
                }


async def _supervised_post(
    plan: CaseExecutionPlanV1, manifest: VerificationManifestV1, credential: bytes, control: ActionControl
) -> dict[str, Any]:
    async def watch() -> None:
        while not control.stopped:
            await asyncio.sleep(0.01)
        raise TimeoutError("action_cancelled_or_expired")

    request = asyncio.create_task(_post(plan, manifest, credential))
    cancellation = asyncio.create_task(watch())
    try:
        async with asyncio.timeout(min(10.0, control.remaining)):
            done, _ = await asyncio.wait({request, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done:
                await cancellation
            return await request
    finally:
        request.cancel()
        cancellation.cancel()
        await asyncio.gather(request, cancellation, return_exceptions=True)


def execute_frozen_action(
    plan: CaseExecutionPlanV1,
    manifest: VerificationManifestV1,
    journal: ActionJournal,
    credential: bytes,
    control: ActionControl | None = None,
) -> None:
    """Execute once; no terminal record after a crash ever authorizes another POST."""
    _credentials(credential)
    if journal.read("action_started") is not None:
        raise ValueError("action already started; recovery must not resend POST")
    control = control or ActionControl(time.monotonic() + 60)
    control.require(14)  # Initial observer + frozen HTTP bound + post-action observer.
    journal.write("action_started", {"execution_id": manifest.execution_id, "state": "started"})
    args = (Path(manifest.sqlite.path), manifest.inputs.username, manifest.inputs.email)
    initial = observe_user(*args, timeout_s=min(2.0, control.remaining), expected_identity=manifest.sqlite)
    if initial["state"] != "observed" or initial["rows"]:
        journal.write(
            "action_terminal",
            {
                "initial": initial,
                "http": {"state": "skipped", "reason": "initial_state_not_absent"},
                "oracle": {"state": "skipped", "reason": "action_not_dispatched", "rows": []},
            },
        )
        return
    try:
        control.require(12)  # Do not dispatch if HTTP and observer bounds cannot fit.
        http = asyncio.run(_supervised_post(plan, manifest, credential, control))
    except (httpx.HTTPError, TimeoutError):
        http = {"state": "timeout", "reason": "http_terminal_unknown"}
    # A fresh independent read-only connection observes committed post-action state.
    oracle = (
        {"state": "skipped", "reason": "action_budget_cancelled_or_expired", "rows": []}
        if control.stopped
        else observe_user(*args, timeout_s=min(2.0, control.remaining), expected_identity=manifest.sqlite)
    )
    journal.write("action_terminal", {"initial": initial, "http": http, "oracle": oracle})


class VerifiedExecutionInputV1(FrozenModel):
    manifest_ref: EvidenceArtifactRefV1
    verification: VerifiedExecutionPrepareV1
    view: ExecutionView


class VerifiedProcessHost(Protocol):
    def preflight(self) -> dict[str, Any]: ...
    def run(
        self,
        *,
        view: Path,
        nodeid: str,
        case_id: str,
        container_name: str,
        execute: Callable[[str, ActionControl], None],
        cancel_requested: Callable[[], bool],
    ) -> VerifiedProcessReceiptV1: ...
    def stop(self, container_name: str) -> bool: ...


def _read_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    path = root / ref.path
    path.resolve(strict=True).relative_to(root.resolve(strict=True))
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("nonregular authenticated input")
    value = path.read_bytes()
    if hashlib.sha256(value).hexdigest() != ref.digest:
        raise ValueError("authenticated input digest mismatch")
    return value


def _authenticate(
    request: TaskRequest, context: TaskContext
) -> tuple[VerifiedExecutionInputV1, VerificationManifestV1, CaseExecutionPlanV1, ActionJournal]:
    payload = VerifiedExecutionInputV1.model_validate(request.input)
    manifest = VerificationManifestV1.model_validate_json(
        _read_ref(context.project_root, payload.manifest_ref)
    )
    profile = payload.verification
    scope = context.workspace_identity.identity_digest
    activity = canonical_digest(
        {
            "attempt_key": context.workspace_identity.task_id,
            "invocation_id": request.invocation_id,
            "task_id": request.task_id,
            "graph_instance_id": request.graph_instance_id,
            "node_id": request.node_id,
            "workspace_identity_digest": scope,
        }
    )
    _, _, identity, _ = authenticate_managed_sut_receipts(
        context.project_root,
        profile,
        secret_port=context.secrets,
        authorization_scope_digest=scope,
        activity_receipt_digest=activity,
    )
    if (
        manifest.authorization_scope_digest != scope
        or manifest.activity_receipt_digest != activity
        or manifest.attempt_key.digest != context.workspace_identity.task_id
        or manifest.invocation_id != request.invocation_id
        or manifest.task_id != request.task_id
        or manifest.graph_instance_id != request.graph_instance_id
        or manifest.sqlite != identity
        or manifest.inputs != profile.user_inputs
        or manifest.nodeid != profile.nodeid
        or manifest.sut.base_url != profile.sut_base_url
        or manifest.sut.instance_id != profile.sut_instance_id
        or manifest.business_activation != profile.business_activation
        or manifest.case_execution_plan_ref != profile.case_execution_plan_ref.path
        or manifest.case_execution_plan_digest != profile.case_execution_plan_ref.digest
    ):
        raise ValueError("manifest does not match authenticated host authorization")
    plans = CaseExecutionPlanSetV1.model_validate_json(
        _read_ref(context.project_root, profile.case_execution_plan_ref)
    )
    if len(plans.cases) != 1:
        raise ValueError("exactly one frozen case is required")
    plan = plans.cases[0]
    if (
        plan.case_id != manifest.case_id
        or plan.change_id != manifest.change_id
        or plan.plan_digest != manifest.plan_digest
        or plan.plan_ref.path != manifest.plan_ref
        or plan.spec_digest != manifest.spec_digest
        or plan.sut_digest != manifest.sut_digest
        or plan.technical_config_digest != manifest.technical_config_digest
        or plan.validation_profile != manifest.validation_profile
        or profile.validation_profile != manifest.validation_profile
        or plan.coverage_epoch != manifest.coverage_epoch
    ):
        raise ValueError("formal plan does not match manifest")
    if (
        payload.view.mode != "verified"
        or payload.view.execution_id != manifest.execution_id
        or payload.view.selected_targets != (manifest.nodeid,)
    ):
        raise ValueError("execution view does not match manifest")
    # Recovery after promotion may have consumed the disposable view; execution rechecks it before dispatch.
    assert context.secrets is not None
    authority = ManagedSutAuthorityV1.model_validate_json(
        context.secrets.resolve(profile.managed_sut_authority_handle)
    )
    key = managed_sut_ownership_token(Path(authority.run_root), authority)
    journal = ActionJournal(context.write_root / manifest.evidence_root, manifest, key)
    return payload, manifest, plan, journal


def _observation(
    journal: ActionJournal,
    obligation: str,
    state: ObservationState,
    actual: Any = None,
    reason: str | None = None,
) -> ObservationV1:
    path = journal.root / "action_terminal.json"
    ref = (
        EvidenceArtifactRefV1(
            path=f"{journal.manifest.evidence_root}/action_terminal.json",
            digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        if path.is_file()
        else None
    )
    return ObservationV1(
        execution_id=journal.manifest.execution_id,
        obligation_id=obligation,
        state=state,
        actual=actual,
        evidence_ref=ref,
        reason=reason,
    )


def collect_facts(journal: ActionJournal, plan: CaseExecutionPlanV1) -> list[ObservationV1]:
    terminal = journal.read("action_terminal")
    if terminal is None:
        return [
            _observation(journal, key, "missing", reason="action_terminal_unknown") for key in plan.required
        ]
    initial, http, oracle = terminal["initial"], terminal["http"], terminal["oracle"]
    values: dict[str, Any] = {}
    if initial["state"] == "observed":
        values["initial.user_absent"] = len(initial["rows"])
    if http["state"] == "observed":
        values.update({"action.finished": True, "api.http_status": http["status"]})
        if http.get("code") is not None:
            values["api.code"] = http["code"]
    if oracle["state"] == "observed":
        values["oracle.executed"] = True
        values["user.row_count"] = len(oracle["rows"])
        if len(oracle["rows"]) == 1:
            values.update({f"user.{key}": value for key, value in oracle["rows"][0].items()})
    return [
        _observation(journal, key, "observed", values[key])
        if key in values
        else _observation(journal, key, "missing", reason="runtime_fact_unavailable")
        for key in plan.required
    ]


def _outcome(journal: ActionJournal, plan: CaseExecutionPlanV1, reason: str | None = None) -> TaskOutcome:
    process = journal.read("process_terminal")
    if process and not process["cleanup_confirmed"] and journal.read("cleanup_terminal") is None:
        raise TaskActivityIndeterminate("container_cleanup_unconfirmed")
    existing = journal.read("outcome")
    if existing is not None:
        return TaskOutcome.model_validate(existing)
    process = journal.read("process_terminal")
    observations = collect_facts(journal, plan)
    host_reason = reason or (process.get("reason") if process else "runner_terminal_unknown")
    if host_reason == "container_cleanup_unconfirmed" and journal.read("cleanup_terminal") is not None:
        host_reason = None
    if process and process.get("exit_code") != 0:
        host_reason = host_reason or "runner_exit_nonzero"
    if any(item.state != "observed" for item in observations):
        host_reason = host_reason or "required_facts_missing"
    if process is None:
        # A host recovery fact, never a manufactured process exit status.
        journal.write(
            "process_terminal",
            VerifiedProcessReceiptV1(
                command=(),
                limits=ProcessLimits(),
                exit_code=None,
                report=None,
                reason=host_reason,
                request_count=int(journal.read("action_started") is not None),
                stderr="",
                cleanup_confirmed=reason != "container_cleanup_unconfirmed",
            ).model_dump(mode="json"),
        )
    receipt_path = journal.root / "process_terminal.json"
    evidence = VerificationEvidenceV1(
        execution_id=journal.manifest.execution_id,
        manifest_digest=journal._binding,
        receipt_ref=EvidenceArtifactRefV1(
            path=f"{journal.manifest.evidence_root}/process_terminal.json",
            digest=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        ),
        observations=tuple(observations),
        host_completion=EvidenceCompletionV1(state="error", reason=host_reason)
        if host_reason
        else EvidenceCompletionV1(state="complete"),
        collector_completion=EvidenceCompletionV1(state="not_required"),
        state="incomplete" if host_reason else "collected",
    )
    outcome = TaskOutcome.succeeded(evidence.model_dump(mode="json"))
    journal.write("outcome", outcome.model_dump(mode="json"))
    return outcome


class VerifiedExecutionHandler:
    """Recoverable production handler: dispatch history never permits a replayed action."""

    def __init__(self, *, process_host: VerifiedProcessHost | None = None) -> None:
        self._host = process_host

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await asyncio.to_thread(self._execute, request, context)

    def _process_host(self, request: TaskRequest) -> VerifiedProcessHost:
        if self._host is not None:
            return self._host
        binding = request.binding_data
        config = binding.get("verification_runner") if isinstance(binding, Mapping) else None
        if not isinstance(config, Mapping) or set(config) != {"source_root", "qualification_path"}:
            raise ValueError("NOT_READY: qualified OCI runner configuration is required")
        if not all(isinstance(item, str) for item in config.values()):
            raise ValueError("NOT_READY: runner paths must be strings")
        return DockerVerificationHost(
            source_root=Path(str(config["source_root"])),
            qualification_path=Path(str(config["qualification_path"])),
        )

    def _execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        payload, manifest, plan, journal = _authenticate(request, context)
        port = context.activity
        if port is None or port.snapshot.workspace_identity != context.workspace_identity:
            raise ValueError("authenticated production activity port is required")
        if port.snapshot.state != "prepared" or journal.read("action_started") is not None:
            raise ValueError("dispatch has already started; use reconciliation")
        try:
            host = self._process_host(request)
            config = host.preflight()
        except ValueError as error:
            return TaskOutcome.failed("configuration", str(error), retryable=False)
        authenticate_execution_view(context.write_root, payload.view)
        assert context.secrets is not None
        credential = context.secrets.resolve(plan.action.credential_ref)
        _credentials(credential)
        fingerprint = {
            "execution_id": manifest.execution_id,
            "manifest_digest": journal._binding,
            "view_digest": payload.view.digest,
            "runner": config,
        }
        port.mark_dispatch_started(fingerprint)
        name = "aa-verify-" + manifest.execution_id
        port.bind(
            {
                "execution_id": manifest.execution_id,
                "manifest_digest": journal._binding,
                "container_name": name,
            }
        )
        receipt = host.run(
            view=context.write_root / payload.view.root,
            nodeid=manifest.nodeid,
            case_id=manifest.case_id,
            container_name=name,
            execute=lambda _, control: execute_frozen_action(plan, manifest, journal, credential, control),
            cancel_requested=context.cancel_requested,
        )
        journal.write("process_terminal", receipt.model_dump(mode="json"))
        return _outcome(journal, plan)

    async def reconcile(
        self, request: TaskRequest, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityReconcileResult:
        try:
            _, manifest, plan, journal = _authenticate(request, context)
            if context.activity is None or activity != context.activity.snapshot:
                raise ValueError("activity snapshot does not match live production activity")
            if activity.state == "prepared":
                if journal.read("action_started") is not None:
                    raise ValueError("action history exists without authenticated dispatch")
                return TaskActivityReconcileResult(status="not_dispatched")
            fingerprint = activity.dispatch_fingerprint
            if (
                not isinstance(fingerprint, dict | Mapping)
                or fingerprint.get("manifest_digest") != journal._binding
            ):
                raise ValueError("dispatch fingerprint does not authenticate retained history")
            reference: JSONValue = {
                "execution_id": manifest.execution_id,
                "manifest_digest": journal._binding,
                "container_name": "aa-verify-" + manifest.execution_id,
            }
            if activity.reference is not None and dict(activity.reference) != reference:  # type: ignore[arg-type]
                raise ValueError("activity reference differs from authenticated execution")
            if activity.reference is None:
                context.activity.bind(reference)
            if not await asyncio.to_thread(
                self._process_host(request).stop, "aa-verify-" + manifest.execution_id
            ):
                return TaskActivityReconcileResult(
                    status="indeterminate", reason="container_cleanup_unconfirmed"
                )
            process = journal.read("process_terminal")
            if process is not None and not process["cleanup_confirmed"]:
                journal.write(
                    "cleanup_terminal",
                    {"container_name": "aa-verify-" + manifest.execution_id, "confirmed": True},
                )
            return TaskActivityReconcileResult(
                status="terminal", reference=reference, outcome=_outcome(journal, plan)
            )
        except (ValueError, OSError, ValidationError) as error:
            return TaskActivityReconcileResult(status="indeterminate", reason=str(error))

    async def cancel(
        self, request: TaskRequest, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityCancelResult:
        result = await self.reconcile(request, context, activity)
        if result.status == "terminal":
            return TaskActivityCancelResult(status="terminal", outcome=result.outcome)
        if result.status == "not_dispatched":
            return TaskActivityCancelResult(status="acknowledged")
        return TaskActivityCancelResult(
            status="indeterminate", reason=result.reason or "cancellation not confirmed"
        )


def _credentials(value: bytes) -> dict[str, str]:
    document = json.loads(value)
    if (
        not isinstance(document, dict)
        or set(document) != {"token", "user_password"}
        or any(
            not isinstance(item, str) or not item or "\n" in item or "\r" in item
            for item in document.values()
        )
    ):
        raise ValueError("parent credential must contain token and user_password")
    return document
