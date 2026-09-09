from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest, ResolvedRawAgentExecutor
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptKey,
    AuthorizedAttemptScope,
    BusinessActivation,
    PermanentTaskFailure,
)
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import (
    InvocationMetadata,
    ResourceClaimTemplate,
    SecretHandleUnauthorized,
    SecretPort,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from assurance_execution.contracts import ExecutionAgentResultV1
from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_execution.contracts.verification import ManagedSutAuthorityV1
from assurance_execution.operations import agent_skills
from assurance_execution.operations import build_managed_sut_authority
from assurance_execution.operations.agent_skills import (
    ExecuteFinalizeHandler,
    ExecutePrepareHandler,
    RunFinalizeHandler,
    RunPrepareHandler,
)
from assurance_execution.resource_loader import resource_bytes
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1, CasePlanContextV1
from assurance_generation.operations.execution_plan import compile_case_plan
from assurance_intake.contracts.verification import AssertionSourcesV1
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    VALID_LEAFS,
    as_object,
    closed_mapping,
    execute_task,
    fake_agent_result,
    run_request,
    reviewed_cases,
)
from tests.acg_plan_fixture import install_plan
from tests.verification_support import read_fixture

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_execution" / "resources"
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "workflow-state.json",
    "aa risk",
    "claude code",
    "codex",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|workflow-state\.json|aa risk|claude code|\bcodex\b",
    re.IGNORECASE,
)


class _VerifiedPayload(dict[str, Any]):
    def __init__(self, payload: dict[str, Any], *, authority_handle: str, authority_bytes: bytes) -> None:
        super().__init__(payload)
        self.host_authority_handle = authority_handle
        self.host_authority_bytes = authority_bytes


class _ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


def _skill_input() -> dict[str, Any]:
    payload = run_request(selected=["tests/generated_test.py"])
    payload["artifact_paths"] = []
    return payload


def _prepare_input(project: Path) -> dict[str, Any]:
    change_id = "CH-DEMO-001"
    target = "tests/api/test_generated.py"
    content = b"def test_tc_a_001__ok():\n    assert True\n"
    generated = project / "qa" / "changes" / change_id / "generated" / "api" / "files" / target
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(content)
    manifest = project / "qa" / "changes" / change_id / "codegen" / "api-generated-files.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "layer": "api",
                "files": [
                    {
                        "repo_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": ["TC_A"],
                    }
                ],
                "mapping": {
                    "schema_version": "1",
                    "layer": "api",
                    "entries": [
                        {
                            "case_id": "TC_A",
                            "symbol": "test_tc_a_001__ok",
                            "target_file": target,
                        }
                    ],
                },
                "required_capabilities": [],
            }
        ),
        encoding="utf-8",
    )
    cases = project / "qa" / "changes" / change_id / "cases" / "items" / "case.yaml"
    cases.parent.mkdir(parents=True, exist_ok=True)
    cases.write_text(yaml.safe_dump(reviewed_cases(), sort_keys=False), encoding="utf-8")
    plan, plan_ref = install_plan(
        project,
        change_id,
        capability_leafs=tuple(sorted(VALID_LEAFS)),
    )
    return {
        "change_id": change_id,
        "plan_digest": plan.plan_digest,
        "plan_ref": plan_ref,
        "selected_test_families": ["api"],
        "capability_leafs": list(VALID_LEAFS),
    }


def _business_payload(request: AgentRunRequest) -> dict[str, object]:
    value = request.instructions[-1].json_content
    assert isinstance(value, Mapping)
    thawed = thaw_json(value)
    assert isinstance(thawed, dict)
    return thawed


def _seal_test_receipt(document: Mapping[str, Any], ownership_token: bytes) -> dict[str, Any]:
    unsigned = {key: value for key, value in document.items() if key != "receipt_digest"}
    encoded = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return {
        **unsigned,
        "receipt_digest": "hmac-sha256:" + hmac.new(ownership_token, encoded, hashlib.sha256).hexdigest(),
    }


def _verified_prepare_input(
    project: Path, db: Path, *, runtime_source: bytes = b"def create_user(): pass\n"
) -> _VerifiedPayload:
    payload = _prepare_input(project)
    case = read_fixture("user-case.json")
    case["case_id"] = "TC_A"
    sources_payload = read_fixture("user-sources.json")
    sources_payload["case_id"] = "TC_A"
    source = AssertionSourcesV1.model_validate(sources_payload)
    raw_plan = read_fixture("user-plan.json")
    context_payload = cast(dict[str, Any], raw_plan["context"])
    context_payload["change_id"] = payload["change_id"]
    context_payload["coverage_epoch"] = 0
    context_payload["plan_digest"] = payload["plan_digest"]
    context_payload["plan_ref"] = payload["plan_ref"]
    reviewed = cast(dict[str, Any], context_payload["reviewed_case"])
    reviewed["change_id"] = payload["change_id"]
    reviewed["coverage_epoch"] = 0
    reviewed["plan_digest"] = payload["plan_digest"]
    reviewed["plan_ref"] = payload["plan_ref"]
    reviewed["preparation_refs"] = [payload["plan_ref"]]
    reviewed["case_refs"] = [
        {
            "path": "qa/changes/CH-DEMO-001/cases/items/case.yaml",
            "digest": "b" * 64,
        }
    ]
    reviewed["review_ref"] = {
        "path": "qa/changes/CH-DEMO-001/review/case-review.json",
        "digest": "c" * 64,
    }
    reviewed_source = project / "app/user.py"
    reviewed_source.parent.mkdir(parents=True, exist_ok=True)
    reviewed_source.write_bytes(b"def create_user(): pass\n")
    source_ref = {"path": "app/user.py", "digest": hashlib.sha256(reviewed_source.read_bytes()).hexdigest()}
    reviewed["preparation_refs"].append(source_ref)
    reviewed["preparation_refs"].sort(key=lambda ref: ref["path"])
    review_path = project / reviewed["review_ref"]["path"]
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({"source_verification": {"reviewed_source_files": ["app/user.py"]}}))
    reviewed["review_ref"]["digest"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    context_payload["sut_digest"] = agent_skills.canonical_digest([source_ref])
    context = CasePlanContextV1.model_validate(context_payload)
    formal = compile_case_plan(
        case,
        source,
        cast(dict[str, object], raw_plan["bindings"]),
        "api_db.v1",
        context=context,
    )
    plan_set = CaseExecutionPlanSetV1(change_id=cast(str, payload["change_id"]), cases=(formal,))
    plan_relative = "qa/changes/CH-DEMO-001/plans/api-case-execution-plan.json"
    plan_path = project / plan_relative
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_bytes = canonical_json_bytes(cast(JSONValue, plan_set.model_dump(mode="json")))
    plan_path.write_bytes(plan_bytes)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as connection:
        connection.execute(
            'CREATE TABLE "user" (username TEXT, email TEXT, is_active INTEGER, '
            "is_superuser INTEGER, dept_id INTEGER)"
        )
    run_root = db.parent.parent
    prepare_receipt = run_root / "harness-prepare.json"
    start_receipt = run_root / "owned-process.json"
    ownership_token = b"task-3-managed-sut-owner-token!!"
    assert len(ownership_token) == 32
    token_path = run_root / ".ownership-token"
    token_path.write_bytes(ownership_token)
    token_path.chmod(0o400)
    file_stat = db.stat()
    sqlite_identity = {
        "path": str(db.resolve()),
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
    }
    qualification = {
        "schema_version": "1",
        "python_version": "3.11.14",
        "python_executable": str(run_root / "runtime/bin/python"),
        "distributions": {"fastapi": "0.111.0"},
        "app_module": str(db.parent / "app/__init__.py"),
        "sqlite_engine": "tortoise.backends.sqlite",
        "sqlite_path": str(db.resolve()),
    }
    qualification_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(qualification, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
    frozen = project / ".aa/user-oracle/sut-source/app/user.py"
    runtime = db.parent / "app/user.py"
    for member in (frozen, runtime):
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(runtime_source)
    source_files = {"sut-source/app/user.py": "sha256:" + hashlib.sha256(runtime_source).hexdigest()}
    source_digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(sorted(source_files.items()), separators=(",", ":")).encode()).hexdigest()
    )
    bootstrap = project / ".aa/user-oracle/bootstrap.py"
    bootstrap.write_bytes(b"# fixed lifecycle helper\n")
    source_files["bootstrap.py"] = "sha256:" + hashlib.sha256(bootstrap.read_bytes()).hexdigest()
    runtime_digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(sorted(source_files.items()), separators=(",", ":")).encode()).hexdigest()
    )
    frozen_lock = project / ".aa/user-oracle/runtime-lock.json"
    frozen_lock.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "fault": "none",
                "files": source_files,
                "source_digest": source_digest,
                "runtime_digest": runtime_digest,
            }
        )
    )
    prepare_unsigned = {
        "schema_version": "1",
        "state": "prepared",
        "fault": "none",
        "frozen_artifact": str(project / ".aa/user-oracle"),
        "frozen_artifact_digest": "sha256:" + hashlib.sha256(frozen_lock.read_bytes()).hexdigest(),
        "source_files": source_files,
        "source_file_mapping": {
            "app/user.py": {"frozen_path": "sut-source/app/user.py", "runtime_path": "app/user.py"}
        },
        "workspace_root": str(project.resolve()),
        "run_root": str(run_root.resolve()),
        "sut_dir": str(db.parent.resolve()),
        "sqlite_path": str(db.resolve()),
        "sqlite_identity": sqlite_identity,
        "runtime_qualification": qualification,
        "runtime_qualification_digest": qualification_digest,
        "source_digest": source_digest,
        "runtime_digest": runtime_digest,
    }
    prepare_document = _seal_test_receipt(prepare_unsigned, ownership_token)
    prepare_bytes = (json.dumps(prepare_document, indent=2, sort_keys=True) + "\n").encode()
    prepare_receipt.write_bytes(prepare_bytes)
    start_unsigned = {
        "schema_version": "1",
        "state": "started",
        "workspace_root": str(project.resolve()),
        "run_root": str(run_root.resolve()),
        "sut_dir": str(db.parent.resolve()),
        "sqlite_path": str(db.resolve()),
        "sqlite_identity": sqlite_identity,
        "prepare_receipt": str(prepare_receipt.resolve()),
        "prepare_receipt_digest": prepare_document["receipt_digest"],
        "prepare_receipt_sha256": hashlib.sha256(prepare_bytes).hexdigest(),
        "instance_id": "managed-sut-1",
        "base_url": "http://127.0.0.1:32123",
        "source_digest": source_digest,
        "runtime_digest": runtime_digest,
    }
    start_document = _seal_test_receipt(start_unsigned, ownership_token)
    start_bytes = (json.dumps(start_document, indent=2, sort_keys=True) + "\n").encode()
    start_receipt.write_bytes(start_bytes)
    token_stat = token_path.stat()
    authority = build_managed_sut_authority(
        run_root=run_root.resolve(),
        ownership_token_path=token_path.resolve(),
        ownership_token_device=token_stat.st_dev,
        ownership_token_inode=token_stat.st_ino,
        ownership_token_digest="sha256:" + hashlib.sha256(ownership_token).hexdigest(),
        prepare_receipt_digest=hashlib.sha256(prepare_bytes).hexdigest(),
        start_receipt_digest=hashlib.sha256(start_bytes).hexdigest(),
        authorization_scope_digest="0" * 64,
        activity_receipt_digest="0" * 64,
    )
    authority_handle = "managed-sut-authority.test"
    payload["verification"] = {
        "validation_profile": "api_db.v1",
        "case_execution_plan_ref": {
            "path": plan_relative,
            "digest": hashlib.sha256(plan_bytes).hexdigest(),
        },
        "nodeid": "tests/api/test_generated.py::test_tc_a_001__ok",
        "business_activation": BusinessActivation.for_trigger("coverage.0.execute").model_dump(mode="json"),
        "sut_instance_id": "managed-sut-1",
        "sut_base_url": "http://127.0.0.1:32123",
        "managed_sqlite_path": str(db.resolve()),
        "observer_sqlite_path": str(db.resolve()),
        "user_inputs": {"username": "qa_t3", "email": "qa_t3@example.test"},
        "managed_sut_prepare_receipt_ref": {
            "path": prepare_receipt.relative_to(project).as_posix(),
            "digest": hashlib.sha256(prepare_bytes).hexdigest(),
        },
        "managed_sut_start_receipt_ref": {
            "path": start_receipt.relative_to(project).as_posix(),
            "digest": hashlib.sha256(start_bytes).hexdigest(),
        },
        "managed_sut_authority_handle": authority_handle,
    }
    return _VerifiedPayload(
        payload,
        authority_handle=authority_handle,
        authority_bytes=canonical_json_bytes(cast(JSONValue, authority.model_dump(mode="json"))),
    )


class _ActivityPort:
    def __init__(self, snapshot: TaskActivitySnapshot) -> None:
        self.snapshot = snapshot


def _bind_test_managed_sut_authority(
    payload: dict[str, Any],
    *,
    attempt_key: str,
    invocation_id: str,
    task_id: str,
    graph_instance_id: str,
    node_id: str,
    workspace_identity_digest: str,
) -> None:
    if not isinstance(payload, _VerifiedPayload):
        return
    authority = ManagedSutAuthorityV1.model_validate_json(payload.host_authority_bytes)
    activity_digest = agent_skills.canonical_digest(
        {
            "attempt_key": attempt_key,
            "invocation_id": invocation_id,
            "task_id": task_id,
            "graph_instance_id": graph_instance_id,
            "node_id": node_id,
            "workspace_identity_digest": workspace_identity_digest,
        }
    )
    bound = authority.model_copy(
        update={
            "authorization_scope_digest": workspace_identity_digest,
            "activity_receipt_digest": activity_digest,
        }
    )
    payload.host_authority_bytes = canonical_json_bytes(cast(JSONValue, bound.model_dump(mode="json")))


async def _execute_verified(
    project: Path,
    payload: dict[str, Any],
    *,
    attempt_key: str,
    expose_authority: bool = True,
    bind_authority: bool = True,
) -> TaskOutcome:
    attempt_token = attempt_key[:12]
    write_root = project / "qa/changes/CH-DEMO-001/.staging" / attempt_token
    write_root.mkdir(parents=True, exist_ok=True)
    identity_payload = {
        "task_id": attempt_key,
        "attempt": 1,
        "attempt_id": f"attempt-{attempt_token}",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "a" * 64,
        "layout_schema_version": "1",
    }
    identity = TaskWorkspaceIdentity(
        **identity_payload,
        identity_digest=agent_skills.canonical_digest(identity_payload),
    )
    invocation = InvocationMetadata(
        invocation_id="verified-invocation",
        lock_digest="a" * 64,
        composition_digest="b" * 64,
        entrypoint="full",
    )
    if bind_authority:
        _bind_test_managed_sut_authority(
            payload,
            attempt_key=attempt_key,
            invocation_id=invocation.invocation_id,
            task_id="verified-task",
            graph_instance_id="verified-graph",
            node_id="execution.execute",
            workspace_identity_digest=identity.identity_digest,
        )
    request = TaskRequest(
        invocation_id=invocation.invocation_id,
        task_id="verified-task",
        graph_instance_id="verified-graph",
        node_id="execution.execute",
        capability_id="assurance.execution.agent.execute.v1",
        binding_data=BINDING,
        invocation=invocation,
        attempt=1,
        input=cast(JSONValue, payload),
    )
    snapshot = TaskActivitySnapshot(
        activity_id=attempt_key,
        request_digest="c" * 64,
        workspace_identity=identity,
        state="prepared",
    )
    secrets: SecretPort | None = None
    if expose_authority and isinstance(payload, _VerifiedPayload):
        secrets = _ExactSecretPort({payload.host_authority_handle: payload.host_authority_bytes})
    return await ExecutePrepareHandler().execute(
        request,
        TaskContext(
            project_root=project,
            write_root=write_root,
            workspace_identity=identity,
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=invocation,
            activity=cast(Any, _ActivityPort(snapshot)),
            secrets=secrets,
        ),
    )


class _ExecutorVerifiedPrepare:
    def __init__(self, secrets: SecretPort) -> None:
        self._secrets = secrets

    async def execute(
        self,
        validated_input: ExecutionPrepareInputV1,
        scope: AuthorizedAttemptScope,
    ) -> AgentRunRequest | PermanentTaskFailure:
        digest = scope.workspace.identity.identity_digest
        invocation = InvocationMetadata(
            invocation_id=scope.execution.invocation_id,
            lock_digest=digest,
            composition_digest=digest,
            entrypoint=scope.execution.public_entrypoint,
        )
        task_id = agent_skills.canonical_digest(
            {
                "attempt_key": scope.execution.attempt_key.digest,
                "handler_id": "assurance.execution.execute.prepare",
                "phase": "prepare",
            }
        )
        request = TaskRequest(
            invocation_id=scope.execution.invocation_id,
            task_id=task_id,
            graph_instance_id=scope.execution.invocation_id,
            node_id=scope.execution.semantic_node_id,
            capability_id="assurance.execution.execute.prepare",
            binding_data=BINDING,
            invocation=invocation,
            attempt=1,
            input=cast(JSONValue, validated_input.model_dump(mode="json")),
        )
        outcome = await ExecutePrepareHandler().execute(
            request,
            TaskContext(
                project_root=scope.workspace.project_root,
                write_root=scope.workspace.write_root,
                workspace_identity=scope.workspace.identity,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=invocation,
                secrets=self._secrets,
            ),
        )
        if outcome.failure is not None:
            return PermanentTaskFailure(
                kind=cast(Any, outcome.failure.kind),
                message=outcome.failure.message,
            )
        return AgentRunRequest.model_validate(outcome.output)


class _StopAfterPrepare:
    handler_id = "assurance.execution.test.runtime"

    async def execute(
        self,
        prepared: AgentRunRequest,
        scope: AuthorizedAttemptScope,
    ) -> PermanentTaskFailure:
        del prepared, scope
        return PermanentTaskFailure(kind="internal", message="stop after prepare")


class _UnusedFinalize:
    async def execute(self, bundle: object, scope: AuthorizedAttemptScope) -> None:
        del bundle, scope
        raise AssertionError("finalize must not run")


class _UndeclaredWritingPrepare(_ExecutorVerifiedPrepare):
    async def execute(
        self,
        validated_input: ExecutionPrepareInputV1,
        scope: AuthorizedAttemptScope,
    ) -> AgentRunRequest | PermanentTaskFailure:
        result = await super().execute(validated_input, scope)
        (scope.workspace.write_root / "undeclared.txt").write_text("escape\n", encoding="utf-8")
        return result


def _verified_executor_scope(
    project: Path,
    payload: _VerifiedPayload,
) -> tuple[TaskWorkspaceStore, TaskWorkspaceProvider, AuthorizedAttemptScope, SecretPort]:
    change_root = project / "qa/changes/CH-DEMO-001"
    store = TaskWorkspaceStore(
        project,
        change_root / ".staging",
        change_root / ".runtime/receipts",
    )
    provider = TaskWorkspaceProvider(store)
    contract = AGENT_JOB_CONTRACTS["execute"]
    resources = contract.resources
    assert isinstance(resources, ResourceClaimTemplate)
    claims = resources.resolve(cast(JSONValue, payload))
    attempt_key = AttemptKey(digest="f" * 64)
    binding = store.begin(
        task_id=attempt_key.digest,
        attempt=1,
        output_paths=claims.writes,
    )
    scope = AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="verified-executor-invocation",
            public_entrypoint="full",
            semantic_node_id="execution.execute",
            attempt_key=attempt_key,
            fencing_token=1,
        ),
        workspace=binding,
    )
    prepare_task_id = agent_skills.canonical_digest(
        {
            "attempt_key": attempt_key.digest,
            "handler_id": "assurance.execution.execute.prepare",
            "phase": "prepare",
        }
    )
    _bind_test_managed_sut_authority(
        payload,
        attempt_key=attempt_key.digest,
        invocation_id=scope.execution.invocation_id,
        task_id=prepare_task_id,
        graph_instance_id=scope.execution.invocation_id,
        node_id=scope.execution.semantic_node_id,
        workspace_identity_digest=binding.identity.identity_digest,
    )
    secrets = _ExactSecretPort({payload.host_authority_handle: payload.host_authority_bytes})
    return store, provider, scope, secrets


async def _prepared_finalize_payload(project: Path) -> tuple[dict[str, object], dict[str, Any]]:
    prepared = await execute_task(
        ExecutePrepareHandler(),
        _prepare_input(project),
        project,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    business = _business_payload(AgentRunRequest.model_validate(prepared.output))
    mapping = cast(dict[str, Any], business["mapping"])
    selected = cast(list[str], mapping["selected"])
    assert len(selected) == 1
    evidence = _valid_evidence()
    receipt = cast(dict[str, object], evidence["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    evidence.update(
        {
            "change_id": business["change_id"],
            "batch_id": business["batch_id"],
            "selected_targets": business["selected_targets"],
            "mapping": mapping,
            "baseline_tree_id": business["baseline_tree_id"],
            "runner_profile_digest": business["runner_profile_digest"],
            "receipt": {
                "commands": [
                    {
                        **command,
                        "command": ["pytest", selected[0]],
                    }
                ]
            },
            "results": [
                {
                    "test": selected[0],
                    "status": "passed",
                    "duration_ms": 1,
                    "message": "",
                }
            ],
        }
    )
    agent_result = cast(dict[str, object], fake_agent_result(evidence)["agent_result"])
    return {**business, "agent_result": agent_result}, evidence


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def _valid_evidence() -> dict[str, Any]:
    return {
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": closed_mapping(["tests/generated_test.py"]),
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "receipt": {
            "commands": [
                {
                    "family": "api",
                    "command": ["pytest", "tests/generated_test.py"],
                    "exit_code": 0,
                    "collected": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                }
            ]
        },
        "results": [{"test": "tests/generated_test.py", "status": "passed", "duration_ms": 1, "message": ""}],
    }


@pytest.mark.asyncio
async def test_execute_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    second = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    assert AgentRunRequest.model_validate(first.output).canonical_bytes() == (
        AgentRunRequest.model_validate(second.output).canonical_bytes()
    )


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    prepared = await execute_task(
        RunPrepareHandler(),
        _prepare_input(tmp_path),
        tmp_path,
        binding_data=BINDING,
    )
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 3
    skill, persona, business = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned run skill" in (skill.text_content or "")
    assert "Executor persona" in (persona.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    assert payload["change_id"] == "CH-DEMO-001"
    assert payload["runner_profile_digest"] == (
        "b7a0e4a77bf9eeb220b6094c28389aed65e2f9e94f055a60e4e8231dfa5bcd61"
    )
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_execute_prepare_materializes_an_authenticated_attempt_local_view(
    tmp_path: Path,
) -> None:
    payload = _prepare_input(tmp_path)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = _business_payload(request)
    view_root = business["execution_view_root"]
    view_digest = business["execution_view_digest"]
    assert isinstance(view_root, str)
    assert isinstance(view_digest, str) and len(view_digest) == 64
    executed_at = datetime.fromisoformat(cast(str, business["executed_at"]).replace("Z", "+00:00"))
    assert executed_at.utcoffset() is not None
    assert view_root.startswith("qa/changes/CH-DEMO-001/.staging/attempt-1/")
    assert "/qa/changes/CH-DEMO-001/.staging/execution/" in view_root
    materialized = tmp_path.joinpath(*view_root.split("/"), "tests/api/test_generated.py")
    assert materialized.read_bytes() == b"def test_tc_a_001__ok():\n    assert True\n"
    assert request.workspace.allowed_outputs == ()
    assert request.workspace.read_roots == (view_root,)


@pytest.mark.asyncio
async def test_execute_prepare_replay_authenticates_the_existing_attempt_view(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)

    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    second = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert first.status == second.status == "succeeded"
    first_request = AgentRunRequest.model_validate(first.output)
    second_request = AgentRunRequest.model_validate(second.output)
    assert "execution_view_root" in _business_payload(first_request)
    assert _business_payload(first_request) == _business_payload(second_request)
    assert first_request.canonical_bytes() == second_request.canonical_bytes()


@pytest.mark.asyncio
async def test_verified_prepare_freezes_manifest_and_uses_execution_scoped_view(
    tmp_path: Path,
) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")

    first = await _execute_verified(tmp_path, payload, attempt_key="1" * 64)
    second = await _execute_verified(tmp_path, payload, attempt_key="1" * 64)

    assert first.status == second.status == "succeeded"
    first_request = AgentRunRequest.model_validate(first.output)
    first_business = _business_payload(first_request)
    second_business = _business_payload(AgentRunRequest.model_validate(second.output))
    assert first_business == second_business
    execution_id = cast(str, first_business["execution_id"])
    assert execution_id
    assert execution_id in cast(str, first_business["execution_view_root"])
    manifest_ref = cast(dict[str, str], first_business["verification_manifest_ref"])
    assert "/.staging/execution/_verification/" in manifest_ref["path"]
    manifest = json.loads((tmp_path / manifest_ref["path"]).read_text())
    independent_receipt = (tmp_path / manifest_ref["path"]).with_name("verification-prepare-receipt.json")
    emitted = first_request.canonical_bytes() + (tmp_path / manifest_ref["path"]).read_bytes()
    emitted += independent_receipt.read_bytes()
    assert payload.host_authority_bytes not in emitted
    assert b"task-3-managed-sut-owner-token!!" not in emitted
    assert b'"ownership_token"' not in emitted
    assert manifest["execution_id"] == execution_id
    assert manifest["attempt_key"] == {"digest": "1" * 64}
    assert manifest["nodeid"] == "tests/api/test_generated.py::test_tc_a_001__ok"
    assert manifest["inputs"] == {
        "dept_id": None,
        "email": "qa_t3@example.test",
        "is_active": True,
        "is_superuser": False,
        "username": "qa_t3",
    }


@pytest.mark.asyncio
async def test_verified_prepare_rejects_fabricated_self_consistent_sut_bundle(
    tmp_path: Path,
) -> None:
    trusted = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    payload = _verified_prepare_input(tmp_path, tmp_path / ".fabricated/sut/db.sqlite3")
    fabricated_profile = cast(dict[str, Any], payload["verification"])
    fabricated_profile["managed_sut_authority_handle"] = trusted.host_authority_handle
    payload.host_authority_handle = trusted.host_authority_handle
    payload.host_authority_bytes = trusted.host_authority_bytes

    outcome = await _execute_verified(tmp_path, payload, attempt_key="d" * 64)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "independent managed SUT authority" in outcome.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["absent", "unknown", "malformed", "incorrect"])
async def test_verified_prepare_requires_host_secret_authority(tmp_path: Path, mode: str) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    profile = cast(dict[str, Any], payload["verification"])
    expose_authority = True
    bind_authority = True
    if mode == "absent":
        expose_authority = False
    elif mode == "unknown":
        profile["managed_sut_authority_handle"] = "managed-sut-authority.unknown"
    elif mode == "malformed":
        payload.host_authority_bytes = b"{}"
        bind_authority = False
    else:
        bind_authority = False

    outcome = await _execute_verified(
        tmp_path,
        payload,
        attempt_key="0" * 64,
        expose_authority=expose_authority,
        bind_authority=bind_authority,
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "independent managed SUT authority" in outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_passes_raw_executor_claim_validation_and_promotion(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload = _verified_prepare_input(project, project / ".managed/sut/db.sqlite3")
    store, provider, scope, secrets = _verified_executor_scope(project, payload)
    contract = AGENT_JOB_CONTRACTS["execute"]
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=cast(Any, _ExecutorVerifiedPrepare(secrets)),
        runtime=cast(Any, _StopAfterPrepare()),
        finalize=cast(Any, _UnusedFinalize()),
    )
    try:
        result = await executor.execute(ExecutionPrepareInputV1.model_validate(payload), scope)

        assert isinstance(result, PermanentTaskFailure)
        assert result.message == "stop after prepare"
        assert executor.phase_deltas["prepare"]
        claim = "qa/changes/CH-DEMO-001/.staging/execution"
        assert contract.phase_write_claims.prepare == ("qa/changes/{change_id}/.staging/execution",)
        assert all(path.startswith(f"{claim}/") for path in executor.phase_deltas["prepare"])
        assert any(path.endswith("/verification-manifest.json") for path in executor.phase_deltas["prepare"])
        assert any(
            path.endswith("/verification-prepare-receipt.json") for path in executor.phase_deltas["prepare"]
        )

        sealed = await provider.seal(scope.workspace)
        prepared = await provider.prepare(scope.workspace, sealed)
        await provider.promote(prepared)
    finally:
        store.close()

    for relative in executor.phase_deltas["prepare"]:
        assert (project / relative).is_file()


@pytest.mark.asyncio
async def test_raw_executor_rejects_verified_prepare_undeclared_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload = _verified_prepare_input(project, project / ".managed/sut/db.sqlite3")
    store, _, scope, secrets = _verified_executor_scope(project, payload)
    executor = ResolvedRawAgentExecutor(
        AGENT_JOB_CONTRACTS["execute"],
        prepare=cast(Any, _UndeclaredWritingPrepare(secrets)),
        runtime=cast(Any, _StopAfterPrepare()),
        finalize=cast(Any, _UnusedFinalize()),
    )
    try:
        result = await executor.execute(ExecutionPrepareInputV1.model_validate(payload), scope)
    finally:
        store.close()

    assert isinstance(result, PermanentTaskFailure)
    assert result.kind == "invalid_output"
    assert "undeclared staging paths: ['undeclared.txt']" in result.message


@pytest.mark.asyncio
async def test_verified_prepare_rejects_unrelated_database_even_when_profile_paths_agree(
    tmp_path: Path,
) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    unrelated = tmp_path / ".unrelated/db.sqlite3"
    unrelated.parent.mkdir()
    with sqlite3.connect(unrelated) as connection:
        connection.execute(
            'CREATE TABLE "user" (username TEXT, email TEXT, is_active INTEGER, '
            "is_superuser INTEGER, dept_id INTEGER)"
        )
    profile = cast(dict[str, Any], payload["verification"])
    profile["managed_sqlite_path"] = str(unrelated.resolve())
    profile["observer_sqlite_path"] = str(unrelated.resolve())

    outcome = await _execute_verified(tmp_path, payload, attempt_key="7" * 64)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "managed SUT receipt" in outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_rejects_tampered_managed_sut_receipt(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    profile = cast(dict[str, Any], payload["verification"])
    start_ref = cast(dict[str, str], profile["managed_sut_start_receipt_ref"])
    (tmp_path / start_ref["path"]).write_text("{}\n", encoding="utf-8")

    outcome = await _execute_verified(tmp_path, payload, attempt_key="8" * 64)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "managed SUT receipt digest changed" in outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_rejects_tampered_receipt_with_recomputed_reference_digest(
    tmp_path: Path,
) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    profile = cast(dict[str, Any], payload["verification"])
    start_ref = cast(dict[str, str], profile["managed_sut_start_receipt_ref"])
    start_path = tmp_path / start_ref["path"]
    started = json.loads(start_path.read_text())
    started["base_url"] = "http://127.0.0.1:32124"
    start_bytes = (json.dumps(started, indent=2, sort_keys=True) + "\n").encode()
    start_path.write_bytes(start_bytes)
    start_ref["digest"] = hashlib.sha256(start_bytes).hexdigest()
    profile["sut_base_url"] = started["base_url"]

    outcome = await _execute_verified(tmp_path, payload, attempt_key="e" * 64)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "independent managed SUT authority" in outcome.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["sut_instance_id", "sut_base_url"])
async def test_verified_prepare_rejects_managed_sut_identity_mismatch(tmp_path: Path, field: str) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    profile = cast(dict[str, Any], payload["verification"])
    profile[field] = "managed-sut-elsewhere" if field == "sut_instance_id" else "http://127.0.0.1:32124"

    outcome = await _execute_verified(tmp_path, payload, attempt_key="9" * 64)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "managed SUT receipt identity" in outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_rejects_authenticated_non_sqlite_sut_config(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    profile = cast(dict[str, Any], payload["verification"])
    prepare_ref = cast(dict[str, str], profile["managed_sut_prepare_receipt_ref"])
    start_ref = cast(dict[str, str], profile["managed_sut_start_receipt_ref"])
    prepare_path = tmp_path / prepare_ref["path"]
    start_path = tmp_path / start_ref["path"]
    prepared = json.loads(prepare_path.read_text())
    prepared["runtime_qualification"]["sqlite_engine"] = "tortoise.backends.postgres"
    prepared["runtime_qualification_digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(prepared["runtime_qualification"], separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
    ownership_token = (tmp_path / ".managed/.ownership-token").read_bytes()
    prepared = _seal_test_receipt(prepared, ownership_token)
    prepare_bytes = (json.dumps(prepared, indent=2, sort_keys=True) + "\n").encode()
    prepare_path.write_bytes(prepare_bytes)
    prepare_ref["digest"] = hashlib.sha256(prepare_bytes).hexdigest()
    started = json.loads(start_path.read_text())
    started["prepare_receipt_sha256"] = prepare_ref["digest"]
    started["prepare_receipt_digest"] = prepared["receipt_digest"]
    started = _seal_test_receipt(started, ownership_token)
    start_bytes = (json.dumps(started, indent=2, sort_keys=True) + "\n").encode()
    start_path.write_bytes(start_bytes)
    start_ref["digest"] = hashlib.sha256(start_bytes).hexdigest()
    authority = ManagedSutAuthorityV1.model_validate_json(payload.host_authority_bytes)
    payload.host_authority_bytes = canonical_json_bytes(
        cast(
            JSONValue,
            authority.model_copy(
                update={
                    "prepare_receipt_digest": prepare_ref["digest"],
                    "start_receipt_digest": start_ref["digest"],
                }
            ).model_dump(mode="json"),
        )
    )

    outcome = await _execute_verified(tmp_path, payload, attempt_key="a" * 64)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "effective SQLite config" in outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_recovery_rejects_unfrozen_manifest(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    first = await _execute_verified(tmp_path, payload, attempt_key="6" * 64)
    assert first.status == "succeeded"
    business = _business_payload(AgentRunRequest.model_validate(first.output))
    manifest_ref = cast(dict[str, str], business["verification_manifest_ref"])
    manifest_path = tmp_path / manifest_ref["path"]
    manifest_path.chmod(0o600)

    recovery = await _execute_verified(tmp_path, payload, attempt_key="6" * 64)

    assert recovery.status == "failed"
    assert recovery.failure is not None
    assert "not a frozen single-link file" in recovery.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_recovery_rejects_replaced_manifest_inputs(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    first = await _execute_verified(tmp_path, payload, attempt_key="b" * 64)
    assert first.status == "succeeded"
    business = _business_payload(AgentRunRequest.model_validate(first.output))
    manifest_ref = cast(dict[str, str], business["verification_manifest_ref"])
    manifest_path = tmp_path / manifest_ref["path"]
    manifest = json.loads(manifest_path.read_text())
    manifest["inputs"] = {"username": "qa_changed", "email": "changed@example.test"}
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o400)

    recovery = await _execute_verified(tmp_path, payload, attempt_key="b" * 64)

    assert recovery.status == "failed"
    assert recovery.failure is not None
    assert "independent prepare receipt" in recovery.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [False, True])
async def test_verified_prepare_recovery_requires_independent_receipt(tmp_path: Path, tamper: bool) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    first = await _execute_verified(tmp_path, payload, attempt_key="c" * 64)
    assert first.status == "succeeded"
    business = _business_payload(AgentRunRequest.model_validate(first.output))
    manifest_ref = cast(dict[str, str], business["verification_manifest_ref"])
    receipt_path = (tmp_path / manifest_ref["path"]).with_name("verification-prepare-receipt.json")
    if tamper:
        receipt_path.chmod(0o600)
        receipt_path.write_text("{}\n", encoding="utf-8")
        receipt_path.chmod(0o400)
    else:
        receipt_path.unlink()

    recovery = await _execute_verified(tmp_path, payload, attempt_key="c" * 64)

    assert recovery.status == "failed"
    assert recovery.failure is not None
    assert "independent prepare receipt" in recovery.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_rejects_partial_or_mismatched_profile(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    partial = dict(payload)
    partial["verification"] = {"validation_profile": "api_db.v1"}

    partial_outcome = await _execute_verified(tmp_path, partial, attempt_key="2" * 64)

    assert partial_outcome.status == "failed"
    assert partial_outcome.failure is not None
    assert partial_outcome.failure.kind == "invalid_input"
    mismatched = cast(dict[str, Any], payload["verification"])
    mismatched["validation_profile"] = "api_db_trace.v1"
    mismatch_outcome = await _execute_verified(tmp_path, payload, attempt_key="3" * 64)
    assert mismatch_outcome.status == "failed"
    assert mismatch_outcome.failure is not None
    assert "frozen machine plan" in mismatch_outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_real_rerun_gets_new_execution_id(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")

    first = await _execute_verified(tmp_path, payload, attempt_key="4" * 64)
    rerun = await _execute_verified(tmp_path, payload, attempt_key="5" * 64)

    assert first.status == rerun.status == "succeeded"
    first_id = _business_payload(AgentRunRequest.model_validate(first.output))["execution_id"]
    rerun_id = _business_payload(AgentRunRequest.model_validate(rerun.output))["execution_id"]
    assert first_id != rerun_id


@pytest.mark.asyncio
async def test_prepare_baseline_ignores_dependency_and_runtime_noise(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    first_request = AgentRunRequest.model_validate(first.output)

    dependency = tmp_path / "dependency-cache"
    dependency.mkdir()
    (dependency / "package.js").write_text("runtime dependency\n", encoding="utf-8")
    (tmp_path / "node_modules").symlink_to(dependency, target_is_directory=True)
    (tmp_path / "db.sqlite3").write_bytes(b"runtime database\n")
    app_logs = tmp_path / "app" / "logs"
    app_logs.mkdir(parents=True)
    (app_logs / "server.log").write_text("runtime log\n", encoding="utf-8")
    (tmp_path / "app" / "runtime.sqlite3").write_bytes(b"runtime database\n")
    runtime = tmp_path / "qa" / "changes" / "CH-DEMO-001" / ".runtime" / "langgraph"
    runtime.mkdir(parents=True)
    (runtime / "checkpoints.sqlite3").write_bytes(b"runtime checkpoint\n")
    evaluation = tmp_path / "eval-out"
    evaluation.mkdir()
    (evaluation / "result.json").write_text("{}\n", encoding="utf-8")

    second = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert second.status == "succeeded"
    second_request = AgentRunRequest.model_validate(second.output)
    assert (
        _business_payload(first_request)["baseline_tree_id"]
        == _business_payload(second_request)["baseline_tree_id"]
    )
    assert first_request.canonical_bytes() == second_request.canonical_bytes()


@pytest.mark.asyncio
async def test_prepare_baseline_binds_closed_source_and_execution_inputs(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    tracked = (
        tmp_path / "app" / "main.py",
        tmp_path / "migrations" / "001_initial.py",
        tmp_path / "web" / "src" / "App.vue",
        tmp_path / "web" / "build" / "proxy.ts",
        tmp_path / "web" / ".env.test",
        tmp_path / "pyproject.toml",
        tmp_path / "uv.lock",
        tmp_path / "web" / "package.json",
        tmp_path / "tests" / "config.py",
        tmp_path
        / "qa"
        / "changes"
        / "CH-DEMO-001"
        / "generated"
        / "api"
        / "files"
        / "tests"
        / "api"
        / "test_generated.py",
    )
    baselines: list[object] = []
    initial = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    assert initial.status == "succeeded"
    baselines.append(_business_payload(AgentRunRequest.model_validate(initial.output))["baseline_tree_id"])

    for index, path in enumerate(tracked):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"tracked-{index}\n", encoding="utf-8")
        prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
        assert prepared.status == "succeeded"
        baselines.append(
            _business_payload(AgentRunRequest.model_validate(prepared.output))["baseline_tree_id"]
        )

    assert len(set(baselines)) == len(baselines)


@pytest.mark.asyncio
async def test_prepare_baseline_rejects_a_symlink_in_an_included_source_root(
    tmp_path: Path,
) -> None:
    payload = _prepare_input(tmp_path)
    target = tmp_path / "outside.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir()
    source.symlink_to(target)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "symbolic link" in prepared.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
async def test_prepare_baseline_rejects_non_regular_included_source(
    tmp_path: Path,
    kind: str,
) -> None:
    payload = _prepare_input(tmp_path)
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir()
    if kind == "hardlink":
        target = tmp_path / "outside.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        os.link(target, source)
    else:
        os.mkfifo(source)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "non-regular file" in prepared.failure.message


@pytest.mark.asyncio
async def test_prepare_baseline_enforces_file_count_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _prepare_input(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (app / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    monkeypatch.setattr(agent_skills, "_MAX_BASELINE_FILES", 1)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "file count limit" in prepared.failure.message


@pytest.mark.asyncio
async def test_execute_prepare_replay_rejects_a_drifted_execution_view(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    business = _business_payload(AgentRunRequest.model_validate(first.output))
    view = tmp_path.joinpath(*cast(str, business["execution_view_root"]).split("/"))
    (view / "tests/api/test_generated.py").write_text("def test_drift(): pass\n", encoding="utf-8")

    replay = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert replay.status == "failed"
    assert replay.failure is not None
    assert replay.failure.kind == "invalid_input"
    assert "execution view digest drifted" in replay.failure.message


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **BINDING["execution"],  # type: ignore[arg-type]
            "provider_model": "primary,fallback",
        },
    }
    prepared = await execute_task(ExecutePrepareHandler(), _skill_input(), tmp_path, binding_data=binding)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_finalize_rejects_malformed_input(tmp_path: Path) -> None:
    executed = await execute_task(RunFinalizeHandler(), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


@pytest.mark.asyncio
async def test_run_finalize_rejects_result_outside_mapping(tmp_path: Path) -> None:
    payload, raw = await _prepared_finalize_payload(tmp_path)
    raw["results"] = [{"test": "tests/legacy_test.py", "status": "passed", "duration_ms": 1, "message": ""}]
    payload["agent_result"] = fake_agent_result(raw)["agent_result"]
    outcome = await execute_task(RunFinalizeHandler(), payload, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True
    assert "outside the closed mapping" in outcome.failure.message


@pytest.mark.asyncio
async def test_finalize_rejects_receipt_failure_that_results_claim_passed(tmp_path: Path) -> None:
    payload, raw = await _prepared_finalize_payload(tmp_path)
    receipt = cast(dict[str, object], raw["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    command.update({"passed": 0, "failed": 1})
    payload["agent_result"] = fake_agent_result(raw)["agent_result"]

    outcome = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "receipt failure contradicts" in outcome.failure.message


@pytest.mark.asyncio
async def test_finalize_rejects_command_receipt_for_an_unselected_family(tmp_path: Path) -> None:
    payload, raw = await _prepared_finalize_payload(tmp_path)
    receipt = cast(dict[str, object], raw["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    command["family"] = "e2e"
    payload["agent_result"] = fake_agent_result(raw)["agent_result"]

    outcome = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "command receipts must exactly cover selected targets" in outcome.failure.message


@pytest.mark.asyncio
async def test_execute_finalize_accepts_typed_evidence(tmp_path: Path) -> None:
    payload, _ = await _prepared_finalize_payload(tmp_path)
    view_root = cast(str, payload["execution_view_root"])
    outcome = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)
    assert outcome.status == "succeeded"
    results = as_object(outcome.output)["results"]
    assert as_object(results[0])["test"] == "tests/api/test_generated.py::test_tc_a_001__ok"
    assert as_object(outcome.output)["executed_at"] == payload["executed_at"]
    assert not tmp_path.joinpath(*view_root.split("/")).exists()

    replay = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)
    assert replay.status == "succeeded"
    assert replay.output == outcome.output


def test_execution_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        "skills/aa-execute/SKILL.md",
        "skills/aa-run/SKILL.md",
        "personas/executor.md",
        "result-contracts/execution.v1.schema.json",
    )
    missing = [item for item in required if not (_RESOURCES / item).is_file()]
    assert missing == []
    hits = [
        path.relative_to(_RESOURCES).as_posix()
        for path in _resource_files()
        if _TOKEN.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
    lowered = "\n".join(path.read_text(encoding="utf-8").lower() for path in _resource_files())
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contract_matches_capability_schema() -> None:
    assert resource_bytes("result-contracts/execution.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ExecutionAgentResultV1.model_json_schema())
    )
    assert VALID_LEAFS


def test_execution_skills_keep_tool_environments_outside_candidate_and_use_family_runners() -> None:
    execute = (_RESOURCES / "skills/aa-execute/SKILL.md").read_text(encoding="utf-8")
    run = (_RESOURCES / "skills/aa-run/SKILL.md").read_text(encoding="utf-8")

    for skill in (execute, run):
        normalized = " ".join(skill.split())
        assert "Do not create or update `.venv`" in normalized
        assert "uv run --isolated pytest" in normalized
        assert "PYTHONDONTWRITEBYTECODE=1" in normalized
        assert "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id>" in normalized
        assert "-p no:cacheprovider" in normalized
        assert (
            "uv run --isolated pytest -p no:cacheprovider --tb=line --rootdir <execution_view_root>"
        ) in normalized
        assert "tokenized argv array" in normalized
        assert "Never return the whole Bash tool input as one array element" in normalized
        assert "--output=/tmp/aa-playwright-<batch_id>" in normalized
        assert (
            "uv run --isolated locust --locustfile <execution_view_root>/<mapped-locustfile> --headless"
        ) in normalized
        assert "Never" in normalized and "Locust file" in normalized and "pytest" in normalized
        assert (
            "For every family, the command receipt's passed, failed, and skipped counts must each be "
            "at least the corresponding counts in that family's result rows"
        ) in normalized
        assert ("passed + failed + skipped must equal collected") in normalized
        assert (
            "For Performance, Locust has no pytest-style collection summary: count the normalized "
            "mapped result rows"
        ) in normalized
        assert (
            "For a non-zero command exit with no native test report, emit one failed result row for "
            "every selected mapping in that family"
        ) in normalized


def test_cache_safe_pytest_recipe_does_not_dirty_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    external_hypothesis = tmp_path / "external-hypothesis"
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "HYPOTHESIS_STORAGE_DIRECTORY": str(external_hypothesis),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=candidate,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (candidate / ".pytest_cache").exists()
    assert not list(candidate.rglob("__pycache__"))
    assert not (candidate / ".hypothesis").exists()


@pytest.mark.asyncio
async def test_verified_prepare_rejects_self_consistent_unreviewed_runtime_before_action(
    tmp_path: Path,
) -> None:
    payload = _verified_prepare_input(
        tmp_path, tmp_path / ".managed/sut/db.sqlite3", runtime_source=b"def create_user(): return True\n"
    )
    outcome = await _execute_verified(tmp_path, payload, attempt_key="7" * 64)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "reviewed source" in outcome.failure.message
    assert not list(tmp_path.glob("qa/changes/*/.staging/**/verification-manifest.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["app", ".aa/user-oracle/sut-source/app", ".managed/sut/app"])
@pytest.mark.parametrize("change", ["bytes", "symlink"])
async def test_verified_prepare_rejects_reviewed_file_copy_drift(
    tmp_path: Path, location: str, change: str
) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    source = tmp_path / location / "user.py"
    if change == "bytes":
        source.write_bytes(b"def create_user(): return False\n")
    else:
        saved = source.parent.with_name(source.parent.name + "-saved")
        source.parent.rename(saved)
        source.parent.symlink_to(saved, target_is_directory=True)
    outcome = await _execute_verified(tmp_path, payload, attempt_key="8" * 64)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "reviewed source" in outcome.failure.message or "symbolic link" in outcome.failure.message


@pytest.mark.asyncio
async def test_verified_prepare_rejects_changed_frozen_artifact_lock(tmp_path: Path) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    lock = tmp_path / ".aa/user-oracle/runtime-lock.json"
    lock.write_bytes(lock.read_bytes() + b"\n")
    outcome = await _execute_verified(tmp_path, payload, attempt_key="9" * 64)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "frozen artifact" in outcome.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [".aa/user-oracle/bootstrap.py", ".managed/sut/undeclared.py"])
@pytest.mark.parametrize("recover", [False, True])
async def test_verified_admission_rechecks_entire_artifact_closure(
    tmp_path: Path, member: str, recover: bool
) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    if recover:
        first = await _execute_verified(tmp_path, payload, attempt_key="d" * 64)
        assert first.status == "succeeded"
    changed = tmp_path / member
    changed.write_bytes(b"# unreviewed artifact change after lifecycle validation\n")
    outcome = await _execute_verified(tmp_path, payload, attempt_key="d" * 64)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "NOT_READY" in outcome.failure.message


def test_artifact_admission_checks_frozen_fault_selection(tmp_path: Path) -> None:
    from assurance_execution.operations.managed_sut import authenticate_reviewed_sut_source

    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    profile = payload["verification"]
    plans = CaseExecutionPlanSetV1.model_validate_json(
        (tmp_path / profile["case_execution_plan_ref"]["path"]).read_bytes()
    )
    prepared = json.loads((tmp_path / profile["managed_sut_prepare_receipt_ref"]["path"]).read_bytes())
    prepared["fault"] = "wrong-value"
    with pytest.raises(ValueError, match="NOT_READY.*fault"):
        authenticate_reviewed_sut_source(tmp_path, plans.cases[0], prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("root", [".aa/user-oracle", ".managed/sut"])
@pytest.mark.parametrize(
    "drift", ["symlink", "hardlink", "fifo", "empty-directory", "bytecode", "db-lookalike"]
)
async def test_artifact_admission_rejects_unsafe_or_undeclared_members(
    tmp_path: Path, root: str, drift: str
) -> None:
    payload = _verified_prepare_input(tmp_path, tmp_path / ".managed/sut/db.sqlite3")
    directory = tmp_path / root
    if drift in {"symlink", "hardlink", "fifo"}:
        member = directory / ("bootstrap.py" if root.startswith(".aa/") else "app/user.py")
        saved = tmp_path / "saved-member"
        member.rename(saved)
        if drift == "symlink":
            member.symlink_to(saved)
        elif drift == "hardlink":
            os.link(saved, member)
        else:
            os.mkfifo(member)
    elif drift == "empty-directory":
        (directory / "extra").mkdir()
    elif drift == "bytecode":
        (directory / "__pycache__").mkdir()
        (directory / "__pycache__/injected.pyc").write_bytes(b"undeclared code")
    else:
        (directory / "db.sqlite3.py").write_bytes(b"# not an allowed SQLite sidecar")
    outcome = await _execute_verified(tmp_path, payload, attempt_key="e" * 64)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "NOT_READY" in outcome.failure.message
