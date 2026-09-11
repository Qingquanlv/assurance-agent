from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    InvocationMetadata,
    TaskContext,
    TaskHandler,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.phase4.conformance import ExecutedTask

from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
from assurance_product.output_routes import OutputRouteCatalog

_SHA = "a" * 64
_FORBIDDEN_SEGMENTS = frozenset({".runtime", ".staging"})
_FORBIDDEN_PREFIXES = ("/".join(("qa", "archive")), "tests/")
EXECUTE_ALIASES = tuple(sorted(AGENT_EXECUTION_CONTRACTS))
BINDING: dict[str, JSONValue] = {
    "agent_profile": "aa-doc-author",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}


def dual_roots(project: Path, change_id: str = "CH-DEMO-001") -> tuple[Path, Path]:
    del change_id
    write_root = project / "qa" / ".staging" / "attempt-1"
    write_root.mkdir(parents=True, exist_ok=True)
    return project, write_root


def workspace_identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": _SHA,
        "write_root_digest": _SHA,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


def task_context(project: Path, write_root: Path) -> TaskContext:
    return TaskContext(
        project_root=project,
        write_root=write_root,
        workspace_identity=workspace_identity(),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest=_SHA,
            entrypoint="phase5",
        ),
    )


def task_request(
    payload: JSONValue,
    *,
    binding_data: JSONValue = None,
    capability_id: str = "test.phase5.capability",
) -> TaskRequest:
    invocation = InvocationMetadata(
        invocation_id="inv-1",
        lock_digest=_SHA,
        composition_digest=_SHA,
        entrypoint="phase5",
    )
    return TaskRequest(
        invocation_id=invocation.invocation_id,
        task_id="phase5-task",
        graph_instance_id="phase5-graph",
        node_id="phase5-node",
        capability_id=capability_id,
        binding_data=binding_data,
        invocation=invocation,
        attempt=1,
        input=payload,
    )


async def execute_task(
    handler: TaskHandler,
    payload: TaskRequest | JSONValue,
    workspace: Path | None = None,
    *,
    binding_data: JSONValue = None,
    write_root: Path | None = None,
    capability_id: str = "test.phase5.capability",
) -> ExecutedTask:
    if workspace is None:
        with TemporaryDirectory(prefix="phase5-dual-root-") as temporary:
            return await execute_task(
                handler,
                payload,
                Path(temporary),
                binding_data=binding_data,
                write_root=write_root,
                capability_id=capability_id,
            )
    project = workspace
    stage = write_root
    if stage is None:
        _, stage = dual_roots(project)
    request = (
        payload
        if isinstance(payload, TaskRequest)
        else task_request(
            payload,
            binding_data=binding_data,
            capability_id=capability_id,
        )
    )
    outcome = await handler.execute(request, task_context(project, stage))
    files: dict[str, bytes] = {}
    for root in (project, stage):
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return ExecutedTask(outcome=outcome, workspace_bytes=MappingProxyType(files))


def fake_agent_result(structured_result: JSONValue) -> AgentRunResult:
    return AgentRunResult(
        result_payload=structured_result,
        result_digest=canonical_digest(structured_result),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )


def test_every_agent_triplet_has_a_closed_output_route_that_stays_inside_the_change() -> None:
    catalog = OutputRouteCatalog()

    assert len(EXECUTE_ALIASES) == 32
    assert catalog.aliases() == EXECUTE_ALIASES

    for alias in EXECUTE_ALIASES:
        outputs = catalog.outputs(alias, "CH-1")
        assert outputs, f"{alias} must declare at least one exact output"
        assert outputs == tuple(sorted(set(outputs)))
        for relative in outputs:
            parts = relative.split("/")
            assert not any(part in _FORBIDDEN_SEGMENTS for part in parts), relative
            assert not any(
                relative == prefix or relative.startswith(f"{prefix}") for prefix in _FORBIDDEN_PREFIXES
            ), relative
            assert not relative.startswith("tests/"), relative


def test_output_routes_are_owned_by_the_installed_product_and_are_not_project_configurable(
    tmp_path: Path,
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace

    project = tmp_path / "project"
    (project / "qa").mkdir(parents=True)
    workspace = ChangeWorkspace.open(project, "CH-1")

    catalog = OutputRouteCatalog()
    assert workspace.output_route("assurance.intake.agent.intake.v1") == catalog.outputs(
        "assurance.intake.agent.intake.v1",
        "CH-1",
    )
    assert workspace.output_route("assurance.intake.agent.explore.v1") == (
        "qa/results/explore/exploration.json",
    )
    assert workspace.output_route("assurance.quality.agent.report.v1") == ("qa/results/report/report.md",)


def test_intake_prepare_injects_the_catalog_route_into_the_agent_request(tmp_path: Path) -> None:
    from assurance_intake.operations import IntakePrepareHandler

    project, write_root = dual_roots(tmp_path, "RET-dept-management")
    prepared = asyncio.run(
        execute_task(
            IntakePrepareHandler(),
            {
                "change_id": "RET-dept-management",
                "requirement": "Cover department CRUD.",
                "capability_leafs": [],
                "artifact_paths": ["qa/cases", "qa/fixtures", "qa/results", "qa/tests"],
            },
            project,
            binding_data=BINDING,
            write_root=write_root,
        )
    )
    request = AgentRunRequest.model_validate(prepared.output)
    assert request.workspace.allowed_outputs == (
        "qa/.qa.yaml",
        "qa/requirement.md",
    )
    assert request.workspace.write_root == "qa/.staging/attempt-1"
    assert request.workspace.agent_profile == "assurance-v1-doc-author"


def test_failed_explore_validation_does_not_mutate_promoted_output(tmp_path: Path) -> None:
    from assurance_intake.operations import ExploreFinalizeHandler

    project, write_root = dual_roots(tmp_path)
    canonical = project / "qa/results/explore/exploration.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    promoted = b"not-json"
    canonical.write_bytes(promoted)

    executed = asyncio.run(
        execute_task(
            ExploreFinalizeHandler(),
            {
                "agent_result": fake_agent_result(
                    {
                        "output_files": [
                            "qa/results/explore/exploration.json",
                        ]
                    }
                ).model_dump(mode="json"),
                "change_id": "CH-DEMO-001",
                "capability_leafs": ["entities.item.create"],
                "artifact_paths": ["qa/results/explore/exploration.json"],
            },
            project,
            write_root=write_root,
        )
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert canonical.read_bytes() == promoted
    assert ".staging" not in json.dumps(cast(Mapping[str, Any], executed.output or {}))
