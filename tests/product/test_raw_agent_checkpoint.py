from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from agent_runtime_contracts import (
    AgentRunRequest,
    AgentRunResult,
    AgentWorkspaceV1,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.discovery import OpenCodeActivityReference, OpenCodeDispatchIncomplete
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.observation import prompt_admission_body
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    TaskWorkspaceIdentity,
)

from tests.product.composition_harness import request_for
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS, _build_owner

_T5A_LANGGRAPH = frozenset(
    {
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)
_T5B_LANGGRAPH = frozenset(
    {
        "intake",
        "case",
        "archive",
        "retro",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
        "improvement-review",
    }
)
_LANGGRAPH = _T5A_LANGGRAPH | _T5B_LANGGRAPH
_LEFTOVER = frozenset({"execute", "full"})
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_EVALUATE_ID = "assurance.improvement.task.evaluate-memory-improvement"
_STRUCTURED_ARTIFACT_MARKERS = (
    "class ArtifactContract",
    "materialization_receipt",
    "typed artifact slot",
    "StructuredArtifact",
)
_PRODUCTION_ROOTS = (
    "packages/adapters/agent-runtime-opencode/agent_runtime_opencode",
    "packages/adapters/agent-runtime-contracts/agent_runtime_contracts",
    "packages/products/assurance-product/assurance_product",
    "packages/capabilities",
    "packages/framework/graph-engine/graph_engine",
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _feature_bound_ids() -> tuple[str, ...]:
    bound: list[str] = []
    for owner_id in PUBLIC_BUNDLE_FIELDS:
        _bundle, context, _digest = _build_owner(owner_id)
        bound.extend(context.bound_contract_ids)
    return tuple(bound)


def _candidate_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        text=True,
    ).strip()


def test_checkpoint_r_inventory_is_33_33_34_41_43() -> None:
    from assurance_product.agent_contracts import (
        LEGACY_AGENT_PHASE_ALIASES,
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS, RAW_AGENT_RUNTIME_BINDING_ROWS

    contracts = all_feature_agent_contracts()
    tasks = {contract.contract_id: contract for contract in all_feature_task_contracts().values()}
    bound = _feature_bound_ids()
    agent_bound = tuple(item for item in bound if item in contracts)
    task_bound = tuple(item for item in bound if item in tasks)

    assert len(contracts) == 33
    assert len(AGENT_RUNTIME_BINDINGS) == 33
    assert set(AGENT_RUNTIME_BINDINGS) == set(contracts)
    assert len(RAW_AGENT_RUNTIME_BINDING_ROWS) == 33
    assert len(contracts) + len(tasks) == 41
    assert len(LEGACY_AGENT_PHASE_ALIASES) == 99
    assert all("alias" not in row.schema_version for row in RAW_AGENT_RUNTIME_BINDING_ROWS)

    assert set(agent_bound) == set(contracts)
    assert set(task_bound) == set(tasks)
    assert agent_bound.count(_CASE_DESIGN_ID) == 2
    assert task_bound.count(_EVALUATE_ID) == 2

    agent_occurrences = len(set(agent_bound)) + (agent_bound.count(_CASE_DESIGN_ID) - 1)
    attempt_occurrences = (
        len(set(agent_bound) | set(task_bound))
        + (agent_bound.count(_CASE_DESIGN_ID) - 1)
        + (task_bound.count(_EVALUATE_ID) - 1)
    )
    assert agent_occurrences == 34
    assert attempt_occurrences == 43
    assert len(contracts) + len(tasks) + 2 == 43


def test_twelve_roots_stay_langgraph_and_execute_full_stay_legacy() -> None:
    from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS
    from assurance_product.runtime_selection import ENTRYPOINT_AGENT_CONTRACT_IDS, select_runtime

    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    flipped = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "langgraph-v1"}
    leftover = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "legacy-v2"}
    assert flipped == set(_LANGGRAPH)
    assert leftover == set(_LEFTOVER)
    for name in _T5A_LANGGRAPH:
        assert select_runtime(name) == "langgraph-v1"
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name] == ()
    for name in _T5B_LANGGRAPH:
        assert select_runtime(name) == "langgraph-v1"
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name]
    for name in leftover:
        assert select_runtime(name) == "legacy-v2"
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name]


def test_checkpoint_r_records_candidate_lock_and_revision(installed_sources) -> None:
    from assurance_product.product import (
        coexistence_graph_manifest,
        product_lock_from_composition,
        resolve_assurance_composition,
    )
    from assurance_product.runtime_bindings import RAW_AGENT_RUNTIME_BINDING_ROWS

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    product_lock = product_lock_from_composition(composition)
    manifest = coexistence_graph_manifest(composition, product_lock)
    rows = RAW_AGENT_RUNTIME_BINDING_ROWS
    sha = _candidate_sha()

    assert len(sha) == 40
    assert len(product_lock.digest) == 64
    assert len(manifest.revision.revision_id) == 64
    assert manifest.revision.product_lock_digest == product_lock.digest
    assert composition.lock.digest
    assert {row.adapter for row in rows} == {"opencode"}
    assert {row.provider for row in rows} == {"opencode"}
    assert {row.model for row in rows} == {"fixture-model"}
    assert len(composition.semantic_attempt_contracts) == 41
    print(
        "checkpoint-r "
        f"candidate_sha={sha} "
        f"product_lock={product_lock.digest} "
        f"graph_revision={manifest.revision.revision_id} "
        "adapter=opencode provider=opencode model=fixture-model"
    )


def test_checkpoint_r_proves_no_structured_artifact_pipeline() -> None:
    hits: list[str] = []
    for root in _PRODUCTION_ROOTS:
        path = _REPO_ROOT / root
        files: Iterator[Path] = path.rglob("*.py") if path.is_dir() else iter(())
        for file in files:
            if "tests" in file.parts:
                continue
            text = file.read_text(encoding="utf-8")
            for marker in _STRUCTURED_ARTIFACT_MARKERS:
                if marker in text:
                    hits.append(f"{file.relative_to(_REPO_ROOT)}:{marker}")
    assert hits == []
    assert not (_REPO_ROOT / "scripts" / "opencode_structured_output_eligibility_probe.py").exists()
    assert not (
        _REPO_ROOT / "tests" / "agent_runtime" / "test_opencode_structured_output_eligibility_probe.py"
    ).exists()
    opencode = _REPO_ROOT / "packages" / "adapters" / "agent-runtime-opencode" / "agent_runtime_opencode"
    adapter_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(opencode.rglob("*.py")))
    assert "format.type" not in adapter_text
    assert 'format": "json_schema"' not in adapter_text
    assert "json_schema" not in adapter_text


_LIVE_RESULT_SCHEMA = {
    "additionalProperties": False,
    "properties": {"ok": {"const": True, "type": "boolean"}},
    "required": ["ok"],
    "type": "object",
}
_SHA = "a" * 64
_WRITE_ROOT = "qa/changes/CH-1/.staging/task-1/attempt-1"
_ALLOWED_OUTPUTS = ("qa/changes/CH-1/result.json",)


def _live_opencode_ready() -> bool:
    if shutil.which("opencode") is None:
        return False
    if os.environ.get("AA_CHECKPOINT_R_LIVE") != "1":
        return False
    return bool(os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENCODE_SERVER_PASSWORD"))


def _live_secret() -> bytes:
    token = os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
    return token.encode("utf-8")


# Sessions never reach a closed terminal on older builds: the loop keeps emitting empty
# assistant turns and the session stays busy, so the adapter only ever sees the horizon
# expire. Verified broken on 1.18.4 and 1.18.11, verified closed on 1.18.26.
_MIN_LIVE_OPENCODE_VERSION = (1, 18, 26)


def _require_live_opencode_version(binary: str) -> tuple[int, ...]:
    raw = subprocess.check_output([binary, "--version"], text=True).strip().splitlines()[-1]
    version = tuple(int(part) for part in raw.strip().lstrip("v").split(".")[:3])
    assert version >= _MIN_LIVE_OPENCODE_VERSION, (
        f"live Checkpoint R needs OpenCode >= "
        f"{'.'.join(str(part) for part in _MIN_LIVE_OPENCODE_VERSION)}; "
        f"{binary} reports {raw}. Older builds never close the session loop and the row "
        f"can only time out."
    )
    return version


def _require_operator_file(variable: str, purpose: str) -> Path:
    raw = os.environ.get(variable)
    assert raw, (
        f"live Checkpoint R needs {variable} to point at {purpose}; the loopback server runs "
        f"under an isolated HOME/XDG root and cannot see the operator's own OpenCode config."
    )
    path = Path(raw).expanduser()
    assert path.is_file(), f"{variable} does not point at a readable file: {path}"
    return path


def _operator_provider_config() -> dict[str, Any]:
    path = _require_operator_file(
        "AA_CHECKPOINT_R_PROVIDER_CONFIG",
        "a JSON file holding the operator's OpenCode `provider` block",
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict) and isinstance(document.get("provider"), dict), (
        f"{path} must be a JSON object carrying a `provider` object"
    )
    config: dict[str, Any] = {"provider": document["provider"]}
    if isinstance(document.get("model"), str):
        config["model"] = document["model"]
    return config


def _live_provider_model(*, provider: str, model: str) -> str:
    override = os.environ.get("OPENCODE_MODEL")
    assert override, (
        f"live Checkpoint R needs OPENCODE_MODEL to name a model the operator's provider really "
        f"serves. The binding row carries the fixture placeholder {provider}/{model}, which no "
        f"provider resolves, and an unresolvable model surfaces only as the observation horizon "
        f"expiring several minutes later."
    )
    return override


class _ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


class _LiveActivityPort:
    def __init__(self, snapshot: TaskActivitySnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        return self._snapshot

    def mark_dispatch_started(self, fingerprint: object) -> TaskActivitySnapshot:
        digest = canonical_digest(fingerprint)
        current = self._snapshot.dispatch_fingerprint_digest
        if current is not None:
            if current != digest:
                raise ValueError("dispatch fingerprint drifted from the durable activity")
            return self._snapshot
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "dispatch_started",
                "dispatch_fingerprint": fingerprint,
                "dispatch_fingerprint_digest": digest,
            }
        )
        return self._snapshot

    def bind(self, reference: object) -> TaskActivitySnapshot:
        digest = canonical_digest(reference)
        current = self._snapshot.reference_digest
        if current is not None:
            if current != digest:
                raise ValueError("activity reference changed after bind")
            return self._snapshot
        self._snapshot = self._snapshot.model_copy(
            update={
                "state": "bound",
                "reference": reference,
                "reference_digest": digest,
            }
        )
        return self._snapshot


def _live_agent_run(*, provider_model: str) -> AgentRunRequest:
    schema = _LIVE_RESULT_SCHEMA
    workspace_payload = {
        "schema_version": "1",
        "agent_profile": "assurance-v1-doc-author",
        "scope_id": "CH-1",
        "write_root": _WRITE_ROOT,
        "allowed_outputs": list(_ALLOWED_OUTPUTS),
    }
    return AgentRunRequest(
        schema_version="1",
        instructions=(
            InstructionPart.text(
                "text/plain",
                'Reply with exactly one JSON object and nothing else: {"ok": true}',
            ),
        ),
        result_contract=ResultContract(
            schema_id="checkpoint-r.live.result.v1",
            schema_digest=canonical_digest(schema),
            delivery_mode="assistant_json_local_v1",
            schema_document=schema,
        ),
        execution=FrozenExecutionSelection(
            provider_model=provider_model,
            worker_profile="checkpoint-r-live",
            permission_profile_digest=_SHA,
            limits={"max_seconds": 180},  # type: ignore[arg-type]
        ),
        workspace=AgentWorkspaceV1.model_validate(
            {**workspace_payload, "identity_digest": canonical_digest(workspace_payload)}
        ),
        request_policy_digest=_SHA,
        request_config_digest=_SHA,
    )


def _live_task_request(agent_run: AgentRunRequest, config: OpenCodeAdapterConfig) -> TaskRequest:
    return TaskRequest.model_validate(
        {
            "invocation_id": "inv-checkpoint-r-live",
            "task_id": "task-1",
            "graph_instance_id": "graph-1",
            "node_id": "run",
            "capability_id": "runtime.opencode.execute",
            "invocation": InvocationMetadata(
                invocation_id="inv-checkpoint-r-live",
                lock_digest=_SHA,
                composition_digest="b" * 64,
                entrypoint="runtime.opencode.execute",
            ),
            "attempt": 1,
            "input": agent_run.model_dump(mode="json"),
            "binding_data": config.model_dump(mode="json"),
        }
    )


def _live_workspace_identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": list(_ALLOWED_OUTPUTS),
        "baseline_files": [],
        "project_digest": _SHA,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


def _assert_admission_is_raw(body: dict[str, Any]) -> None:
    encoded = json.dumps(body)
    assert "format" not in body
    assert "json_schema" not in encoded
    assert "opencode_structured_output" not in encoded


@contextmanager
def _direct_loopback_opencode_server(project_root: Path, state_root: Path) -> Iterator[str]:
    binary = shutil.which("opencode")
    assert binary is not None
    _require_live_opencode_version(binary)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    # The server's own database and logs must stay out of the scanned project, otherwise
    # OpenCode folds them back into the prompt context and the exchange balloons past any
    # sane response bound.
    home = state_root / "home"
    home.mkdir(parents=True)
    xdg_config = state_root / "xdg-config"
    xdg_data = state_root / "xdg-data"
    opencode_config = xdg_config / "opencode"
    opencode_config.mkdir(parents=True)
    agents = {
        "agent": {
            "assurance-v1-doc-author": {
                "description": "Checkpoint R live raw-agent probe",
                "mode": "all",
                "prompt": "Execute the supplied instructions and return exactly one JSON object.",
            }
        }
    }
    (opencode_config / "opencode.json").write_text(
        json.dumps({**agents, **_operator_provider_config()}), encoding="utf-8"
    )
    (project_root / "opencode.json").write_text(json.dumps(agents), encoding="utf-8")
    auth_target = xdg_data / "opencode" / "auth.json"
    auth_target.parent.mkdir(parents=True)
    shutil.copyfile(
        _require_operator_file(
            "AA_CHECKPOINT_R_AUTH_FILE",
            "the operator's OpenCode auth.json holding provider credentials",
        ),
        auth_target,
    )
    # Without a repository boundary OpenCode resolves the project root to "/" and walks the
    # whole filesystem for context.
    subprocess.run(["git", "init", "-q"], cwd=str(project_root), check=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(xdg_config)
    env["XDG_DATA_HOME"] = str(xdg_data)
    env.pop("OPENCODE_SERVER_PASSWORD", None)
    # Keep the server's own log readable after a failure, and off a pipe that nothing drains.
    log_path = state_root / "serve.log"
    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        cwd=str(project_root),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                proc.communicate()
                raise AssertionError(
                    f"official OpenCode serve exited before health: {log_path.read_text(encoding='utf-8')!r}"
                )
            try:
                import urllib.request

                with urllib.request.urlopen(f"{endpoint}/global/health", timeout=1) as response:
                    if response.status == 200:
                        ready = True
                        break
            except OSError:
                time.sleep(0.1)
        assert ready, f"official OpenCode serve did not become healthy at {endpoint}"
        yield endpoint
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
        log_handle.close()


def _execute_live_cutover(
    *,
    endpoint: str,
    project_root: Path,
    agent_run: AgentRunRequest,
    secret: bytes,
) -> tuple[TaskOutcome, TaskActivitySnapshot]:
    write_root = project_root / _WRITE_ROOT
    write_root.mkdir(parents=True)
    config = OpenCodeAdapterConfig.model_validate(
        {
            "schema_version": "1",
            "endpoint": endpoint,
            "tls_identity_digest": _SHA,
            "secret_handle": "opencode.token",
            "protocol_profile": "opencode-http-v1",
            "project_scope": str(project_root.resolve()),
            "request_timeout_seconds": 60,
            "observation_horizon_seconds": 180,
            "poll_interval_seconds": 1,
            "cancel_timeout_seconds": 15,
            "max_response_bytes": 262_144,
            "adapter_configuration_digest": _SHA,
        }
    )
    request = _live_task_request(agent_run, config)
    snapshot = TaskActivitySnapshot(
        activity_id="activity-checkpoint-r-live",
        request_digest=canonical_digest(request.model_dump(mode="json")),
        workspace_identity=_live_workspace_identity(),
        state="prepared",
    )
    port = _LiveActivityPort(snapshot)
    secrets: SecretPort = _ExactSecretPort({"opencode.token": secret})
    context = TaskContext(
        project_root=project_root,
        write_root=write_root,
        workspace_identity=snapshot.workspace_identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=request.invocation,
        activity=port,
        secrets=secrets,
    )
    outcome = asyncio.run(OpenCodeHandler().execute(request, context))
    return outcome, port.snapshot


@pytest.mark.skipif(
    not _live_opencode_ready(),
    reason="official OpenCode binary plus AA_CHECKPOINT_R_LIVE=1 and operator credentials are required",
)
def test_live_opencode_cutover_binding_records_checkpoint_r(
    installed_sources,
    tmp_path: Path,
) -> None:
    from assurance_product.product import (
        coexistence_graph_manifest,
        product_lock_from_composition,
        resolve_assurance_composition,
    )
    from assurance_product.runtime_bindings import RAW_AGENT_RUNTIME_BINDING_ROWS

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    product_lock = product_lock_from_composition(composition)
    manifest = coexistence_graph_manifest(composition, product_lock)
    rows = RAW_AGENT_RUNTIME_BINDING_ROWS
    assert {row.adapter for row in rows} == {"opencode"}
    assert {row.provider for row in rows} == {"opencode"}
    assert {row.model for row in rows} == {"fixture-model"}
    row = rows[0]
    provider_model = _live_provider_model(provider=row.provider, model=row.model)
    agent_run = _live_agent_run(provider_model=provider_model)
    admission = prompt_admission_body(agent_run, "msg_checkpoint_r_live")
    _assert_admission_is_raw(admission)

    project_root = tmp_path / "isolated"
    project_root.mkdir()
    with _direct_loopback_opencode_server(project_root, tmp_path / "opencode-state") as endpoint:
        assert endpoint.startswith("http://127.0.0.1:")
        try:
            outcome, snapshot = _execute_live_cutover(
                endpoint=endpoint,
                project_root=project_root,
                agent_run=agent_run,
                secret=_live_secret(),
            )
        except OpenCodeDispatchIncomplete as error:
            raise AssertionError(
                f"live OpenCode cutover row did not observe a closed terminal: {error}"
            ) from error

    assert snapshot.reference is not None
    reference = OpenCodeActivityReference.model_validate(thaw_json(snapshot.reference))
    assert reference.session_id
    assert outcome.status == "succeeded", (
        f"live OpenCode cutover row failed: status={outcome.status} failure={outcome.failure}"
    )
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"ok": True}
    assert result.adapter_id == "runtime.opencode"
    print(
        "checkpoint-r-live "
        f"candidate_sha={_candidate_sha()} "
        f"product_lock={product_lock.digest} "
        f"graph_revision={manifest.revision.revision_id} "
        f"adapter={row.adapter} provider={row.provider} model={row.model} "
        f"live_provider_model={provider_model} "
        f"session_id={reference.session_id} "
        f"result_digest={result.result_digest}"
    )
