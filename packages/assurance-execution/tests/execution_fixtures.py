from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from tempfile import TemporaryDirectory
from types import MappingProxyType

from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    InvocationMetadata,
    ResourceClaims,
    TaskContext,
    TaskHandler,
    TaskRequest,
    TaskWorkspaceIdentity,
    ValidationContext,
)
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.phase4.conformance import ExecutedTask

from assurance_execution.operations.runner import ExecutionProcessHost, ProcessReceipt
from assurance_product.execution_view import ExecutionView, build_execution_view
from assurance_product.generated_merge import MergedGeneratedSet

VALID_LEAFS = ("auth.session.create", "entities.item.create")
VALID_CASES = ("TC_A", "TC_B")
_SHA = "a" * 64
BINDING: dict[str, Any] = {
    "agent_profile": "aa-executor",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}


def write_test(path: Path, *, body: str = "def test_ok():\n    assert True\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def mapping_entry(
    test: str,
    *,
    case_id: str = "TC_A",
    capability: str = "entities.item.create",
    layer: str = "api",
) -> dict[str, object]:
    return {"test": test, "case_id": case_id, "capability": capability, "layer": layer}


def closed_mapping(selected: list[str]) -> dict[str, object]:
    return {"selected": selected, "mappings": [mapping_entry(item) for item in selected]}


def run_request(
    selected: list[str],
    *,
    change_id: str = "CH-DEMO-001",
    batch_id: str = "20260822T000000Z",
) -> dict[str, Any]:
    return {
        "change_id": change_id,
        "batch_id": batch_id,
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": closed_mapping(selected),
        "capability_leafs": list(VALID_LEAFS),
        "case_ids": list(VALID_CASES),
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
    }


def executed_paths(outcome: Any) -> tuple[str, ...]:
    output = outcome.output
    if not isinstance(output, Mapping):
        return ()
    executed = output.get("executed")
    if isinstance(executed, list | tuple):
        return tuple(str(item) for item in executed)
    results = output.get("results")
    if isinstance(results, list | tuple):
        return tuple(str(item["test"]) for item in results if isinstance(item, Mapping))
    return ()


class FakePytestHost:
    def __init__(self, *, outcomes: Mapping[str, str] | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.cwds: list[Path] = []
        self._outcomes = dict(outcomes or {})

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt:
        self.commands.append(argv)
        self.cwds.append(cwd)
        selected = _selected_from_argv(argv)
        tests: list[dict[str, object]] = []
        failed = 0
        passed = 0
        skipped = 0
        for path in selected:
            status = self._outcomes.get(path, "passed")
            if status == "failed":
                failed += 1
            elif status == "skipped":
                skipped += 1
            else:
                passed += 1
            tests.append(
                {
                    "nodeid": f"{path}::test_tc_a_001__ok",
                    "outcome": status,
                    "call": {
                        "outcome": status,
                        "duration": 0.001,
                        "longrepr": "AssertionError: failed" if status == "failed" else "",
                    },
                }
            )
        exit_code = 1 if failed else 0
        return ProcessReceipt(
            command=argv,
            exit_code=exit_code,
            stdout="",
            stderr="",
            report={
                "tests": tests,
                "exitcode": exit_code,
                "summary": {
                    "collected": len(selected),
                    "passed": passed,
                    "failed": failed,
                    "skipped": skipped,
                },
            },
        )


def _selected_from_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    selected: list[str] = []
    for item in argv[1:]:
        if item.startswith("-"):
            continue
        if item.endswith(".py") or "::" in item:
            selected.append(item)
    return tuple(selected)


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


def materialize_execution_view(
    project: Path,
    selected: list[str],
    *,
    change_id: str = "CH-DEMO-001",
    batch_id: str = "20260822T000000Z",
) -> ExecutionView:
    (project / "qa" / "changes" / change_id).mkdir(parents=True, exist_ok=True)
    return build_execution_view(
        project,
        change_id=change_id,
        batch_id=batch_id,
        merged=MergedGeneratedSet(files=(), digest=canonical_digest([])),
        selected=tuple(selected),
    )


async def execute_task(
    handler: TaskHandler,
    payload: TaskRequest | Mapping[str, Any],
    workspace: Path | None = None,
    *,
    binding_data: JSONValue = None,
) -> ExecutedTask:
    if workspace is None:
        with TemporaryDirectory(prefix="execution-dual-root-") as temporary:
            return await execute_task(handler, payload, Path(temporary), binding_data=binding_data)
    write_root = workspace / "qa" / "changes" / "CH-DEMO-001" / ".staging" / "attempt-1"
    write_root.mkdir(parents=True, exist_ok=True)
    invocation = InvocationMetadata(
        invocation_id="inv-1",
        lock_digest=_SHA,
        composition_digest=_SHA,
        entrypoint="phase5",
    )
    request = (
        payload
        if isinstance(payload, TaskRequest)
        else TaskRequest(
            invocation_id=invocation.invocation_id,
            task_id="phase5-task",
            graph_instance_id="phase5-graph",
            node_id="phase5-node",
            capability_id="test.execution.capability",
            binding_data=binding_data,
            invocation=invocation,
            attempt=1,
            input=cast(JSONValue, dict(payload)),
        )
    )
    outcome = await handler.execute(
        request,
        TaskContext(
            project_root=workspace,
            write_root=write_root,
            workspace_identity=workspace_identity(),
            heartbeat=lambda: None,
            cancel_requested=lambda: False,
            invocation=invocation,
        ),
    )
    files: dict[str, bytes] = {}
    for root in (workspace, write_root):
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return ExecutedTask(outcome=outcome, workspace_bytes=MappingProxyType(files))


def fake_pytest_host(*, outcomes: Mapping[str, str] | None = None) -> ExecutionProcessHost:
    return FakePytestHost(outcomes=outcomes)


def candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def candidate_with_result(test: str) -> CandidateWriteSet:
    return candidate_with(test)


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def codegen_mapping(
    *,
    layer: str = "api",
    target_file: str = "tests/generated_test.py",
    case_id: str = "TC_A",
    symbol: str = "test_tc_a_001__ok",
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "layer": layer,
        "entries": [{"case_id": case_id, "symbol": symbol, "target_file": target_file}],
    }


def reviewed_cases(*, case_id: str = "TC_A", leaf: str = "entities.item.create") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "added": [
            {
                "case_id": case_id,
                "title": "generated happy path",
                "status": "active",
                "priority": "P1",
                "severity": "major",
                "type": "API",
                "module": "items",
                "requirement_id": "REQ-1",
                "feature_name": "item-management",
                "test_condition_id": "COND-1",
                "design_technique": "use_case",
                "objective": "verify create",
                "summary": "create an item",
                "preconditions": [],
                "test_data": [],
                "steps": ["create the item"],
                "assertions": ["the item exists"],
                "postconditions": [],
                "edge_cases": [],
                "related_cases": [],
                "risk": {
                    "level": "high",
                    "likelihood": 3,
                    "impact": 4,
                    "rationale": "important administration path",
                },
                "automation": {
                    "required": True,
                    "framework": "pytest",
                    "status": "planned",
                },
                "regression": {
                    "candidate": True,
                    "tier": "smoke",
                    "rationale": "protect the administration path",
                    "selection_reason": ["critical_user_journey"],
                    "maintenance_rule": "keep_until_feature_deprecated",
                },
                "trace": {leaf: {"covered": True}},
            }
        ],
        "modified": [],
        "removed": [],
    }


def select_request(
    *,
    target_file: str = "tests/generated_test.py",
    selected_targets: dict[str, bool] | None = None,
    extra_mappings: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    mappings = [codegen_mapping(target_file=target_file), *(extra_mappings or [])]
    return {
        "change_id": "CH-DEMO-001",
        "selected_targets": selected_targets
        or {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mappings": mappings,
        "reviewed_cases": reviewed_cases(),
        "capability_leafs": list(VALID_LEAFS),
        "case_ids": list(VALID_CASES),
    }


def as_object(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value


def fake_agent_result(structured_result: dict[str, Any]) -> dict[str, Any]:
    payload = cast(JSONValue, structured_result)
    result = AgentRunResult(
        structured_result=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    return {
        "agent_result": result.model_dump(mode="json"),
        "capability_leafs": list(VALID_LEAFS),
        "case_ids": list(VALID_CASES),
        "artifact_paths": [],
        "mapping": closed_mapping(["tests/generated_test.py"]),
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
    }
