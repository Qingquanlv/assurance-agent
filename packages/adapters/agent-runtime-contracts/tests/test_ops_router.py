from __future__ import annotations

import asyncio
import importlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import (
    InvocationMetadata,
    TaskContext,
    TaskOutcome,
    TaskWorkspaceIdentity,
)

from agent_runtime_contracts.ops import Agent, Dir, OpRouter, WriteScopeError
from agent_runtime_contracts.wire.models import AgentRunRequest, AgentRunResult

_SHA = "a" * 64

_OPS_INIT = """
from agent_runtime_contracts.ops import OpRouter
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy

router = OpRouter(
    "fixture.cap",
    package=__name__,
    reads=("qa",),
    agent_retry=AttemptRetryPolicy(max_attempts=2),
    task_retry=AttemptRetryPolicy(max_attempts=1),
    timeout=AttemptTimeoutPolicy(seconds=60),
    scope=lambda business: business.change_id,
)


async def execute(request, context):
    return await router.execute(request, context)
"""

_ECHO_INIT = """
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.ops import Agent, Dir, Finalize, OutputError, Prepare
from PKG.ops import router


class EchoInput(FrozenModel):
    change_id: str
    notes: tuple[str, ...] = ()


class EchoResult(FrozenModel):
    message: str


class EchoOutput(FrozenModel):
    message: str
    plan: str
    recovered: bool = False


def frozen_plan(ctx, business):
    return f"plan-for-{business.change_id}"


def before(ctx, business):
    ctx.write("qa/seed.txt", ctx.dep(frozen_plan).encode())
    ctx.extra("planning_facts", {"plan": ctx.dep(frozen_plan)})
    return business


def after(ctx, business, result):
    if result.message == "bad":
        raise OutputError("bad message")
    if result.message == "escape":
        ctx.write("qa/elsewhere.txt", b"x")
    ctx.write("qa/final.txt", result.message.encode())
    return EchoOutput(message=result.message, plan=business.change_id)


def recover(ctx, business, error):
    if business.change_id == "strict":
        raise error
    return EchoOutput(message=str(error), plan=business.change_id, recovered=True)


op = router.agent(
    "echo",
    input=EchoInput,
    prepare=Prepare(hook=before, depends=(frozen_plan,), writes=("qa/seed.txt",)),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-echo",
        result=EchoResult,
        writes=("qa/echo.json", Dir("qa/notes", files=lambda business: business.notes)),
    ),
    finalize=Finalize(hook=after, on_output_error=recover, writes=("qa/final.txt",)),
    output=EchoOutput,
)
"""

_COUNT_INIT = """
from graph_engine.plugin_api import FrozenModel

from PKG.ops import router


class CountInput(FrozenModel):
    value: int


class CountOutput(FrozenModel):
    value: int


def run(ctx, business):
    if business.value < 0:
        raise ValueError("negative")
    return CountOutput(value=business.value + 1)


op = router.task(
    "count",
    input=CountInput,
    output=CountOutput,
    run=run,
    reads=("qa",),
    writes=("qa/count.json",),
    errors=(ValueError,),
)
"""


@dataclass(frozen=True)
class _Request:
    capability_id: str
    input: object
    binding_data: object = None
    target_capability_id: str | None = None


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _capability(tmp_path: Path, *, extra: dict[str, str] | None = None) -> ModuleType:
    name = f"opfix_{uuid.uuid4().hex}"
    root = tmp_path / name
    files = {
        "__init__.py": "",
        "ops/__init__.py": _OPS_INIT,
        "ops/echo/__init__.py": _ECHO_INIT.replace("PKG", name),
        "ops/echo/SKILL.md": "echo skill\n",
        "ops/echo/notes.md": "echo notes\n",
        "ops/echo/result.schema.json": json.dumps({"type": "object"}),
        "ops/count/__init__.py": _COUNT_INIT.replace("PKG", name),
        **(extra or {}),
    }
    for relative, text in files.items():
        _write(root, relative, text.replace("PKG", name))
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module(f"{name}.ops")
    finally:
        sys.path.remove(str(tmp_path))


def _router(module: ModuleType) -> OpRouter:
    router = module.router
    assert isinstance(router, OpRouter)
    return router


def _context(tmp_path: Path) -> TaskContext:
    project = tmp_path / "project"
    write = tmp_path / "write"
    project.mkdir(exist_ok=True)
    write.mkdir(exist_ok=True)
    identity = TaskWorkspaceIdentity.model_construct(
        task_id="task",
        attempt=1,
        attempt_id="attempt-1",
        output_paths=(),
        baseline_files=(),
        project_digest=_SHA,
        write_root_digest=_SHA,
        identity_digest=_SHA,
        layout_schema_version="1",
    )
    return TaskContext(
        project_root=project,
        write_root=write,
        workspace_identity=identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest=_SHA,
            entrypoint="fixture",
        ),
    )


def _binding() -> dict[str, object]:
    return {
        "agent_profile": "aa-reviewer",
        "execution": {
            "provider_model": "provider_default",
            "worker_profile": "fixture-v1",
            "permission_profile_digest": _SHA,
            "limits": {"max_seconds": 120},
        },
        "request_policy_digest": _SHA,
        "request_config_digest": _SHA,
    }


def _run(module: ModuleType, request: _Request, context: TaskContext) -> TaskOutcome:
    return asyncio.run(module.execute(request, context))


def _agent_result(payload: dict[str, Any]) -> dict[str, Any]:
    return AgentRunResult.model_validate(
        {
            "result_payload": payload,
            "result_digest": canonical_digest(payload),
            "evidence_digest": _SHA,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    ).model_dump(mode="json")


def _finalize(
    module: ModuleType, tmp_path: Path, business: dict[str, Any], payload: dict[str, Any]
) -> TaskOutcome:
    request = _Request(
        capability_id="fixture.cap.echo.finalize",
        input={
            "prepare": {**business, "planning_facts": {"plan": "x"}},
            "agent_result": _agent_result(payload),
        },
    )
    return _run(module, request, _context(tmp_path))


def test_discovery_derives_handlers_contracts_and_routes(tmp_path: Path) -> None:
    router = _router(_capability(tmp_path))

    assert set(router.ops()) == {"count", "echo"}
    assert sorted(router.routes()) == [
        "fixture.cap.count",
        "fixture.cap.echo.finalize",
        "fixture.cap.echo.prepare",
    ]
    contract = router.agent_contracts()["echo"]
    assert contract.contract_id == "fixture.cap.agent.echo.v1"
    assert contract.skill_id == "aa-echo"
    assert contract.resources.writes == ("qa/echo.json", "qa/final.txt", "qa/notes", "qa/seed.txt")
    assert contract.phase_write_claims.as_projection() == {
        "prepare": ["qa/seed.txt"],
        "runtime": ["qa/echo.json", "qa/notes"],
        "finalize": ["qa/final.txt"],
    }
    task = router.task_contracts()["count"]
    assert (task.contract_id, task.handler_id) == ("fixture.cap.task.count", "fixture.cap.count")
    assert router.output_routes() == {"echo": ("qa/echo.json",)}
    assert [ref.contract_id for ref in router.attempt_contract_refs()] == [
        "fixture.cap.agent.echo.v1",
        "fixture.cap.task.count",
    ]


def test_resource_manifest_comes_from_op_directories(tmp_path: Path) -> None:
    router = _router(_capability(tmp_path))

    assert router.resource_files() == {
        "fixture.cap.result.echo.v1": "ops/echo/result.schema.json",
        "fixture.cap.skill.aa-echo.notes.v1": "ops/echo/notes.md",
        "fixture.cap.skill.aa-echo.v1": "ops/echo/SKILL.md",
    }
    assert router.resource_text("ops/echo/notes.md") == "echo notes\n"


def test_second_skill_file_in_an_op_is_rejected(tmp_path: Path) -> None:
    router = _router(_capability(tmp_path, extra={"ops/echo/repair.SKILL.md": "x"}))

    with pytest.raises(ValueError, match="declare another skill as its own op"):
        router.resource_files()


def test_prepare_runs_depends_and_before(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    context = _context(tmp_path)

    plain = _run(
        module,
        _Request("fixture.cap.echo.prepare", {"change_id": "c1"}, _binding()),
        context,
    )

    assert plain.status == "succeeded"
    request = AgentRunRequest.model_validate(plain.output)
    texts = [part.text_content for part in request.instructions if part.media_type == "text/plain"]
    assert texts == ["echo skill\n"]
    business = [part.json_content for part in request.instructions if part.media_type == "application/json"]
    assert business == [{"change_id": "c1", "notes": (), "planning_facts": {"plan": "plan-for-c1"}}]
    assert request.workspace.scope_id == "c1"
    assert request.workspace.allowed_outputs == ("qa/echo.json",)
    assert (context.write_root / "qa" / "seed.txt").read_text() == "plan-for-c1"


def test_dir_write_expands_to_the_exact_files_of_this_run(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    business = {"change_id": "c1", "notes": ["qa/notes/b.md", "qa/notes/deep/a.md"]}

    outcome = _run(module, _Request("fixture.cap.echo.prepare", business, _binding()), _context(tmp_path))

    request = AgentRunRequest.model_validate(outcome.output)
    assert request.workspace.allowed_outputs == ("qa/echo.json", "qa/notes/b.md", "qa/notes/deep/a.md")


@pytest.mark.parametrize("path", ["qa/other.md", "qa/notes", "qa/notes/../escape.md", "/qa/notes/a.md"])
def test_dir_file_outside_its_root_is_invalid_input(tmp_path: Path, path: str) -> None:
    module = _capability(tmp_path)
    business = {"change_id": "c1", "notes": [path]}

    outcome = _run(module, _Request("fixture.cap.echo.prepare", business, _binding()), _context(tmp_path))

    assert outcome.failure is not None and outcome.failure.kind == "invalid_input"


def test_agent_writes_must_be_unique_across_files_and_dirs() -> None:
    with pytest.raises(ValueError, match="agent writes must be unique"):
        Agent(
            profile="assurance-v1-reviewer",
            skill="aa-echo",
            result=AgentRunResult,
            writes=("qa/notes", Dir("qa/notes", files=lambda business: ())),
        )


def test_override_replaces_a_dependency_for_tests(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    router = _router(module)
    echo = importlib.import_module(f"{module.__name__}.echo")
    context = _context(tmp_path)

    with router.override(echo.frozen_plan, lambda ctx, business: "stub-plan"):
        _run(module, _Request("fixture.cap.echo.prepare", {"change_id": "c1"}, _binding()), context)
    assert (context.write_root / "qa" / "seed.txt").read_text() == "stub-plan"

    _run(module, _Request("fixture.cap.echo.prepare", {"change_id": "c1"}, _binding()), context)
    assert (context.write_root / "qa" / "seed.txt").read_text() == "plan-for-c1"


def test_prepare_maps_invalid_input(tmp_path: Path) -> None:
    module = _capability(tmp_path)

    outcome = _run(module, _Request("fixture.cap.echo.prepare", {"wrong": 1}, _binding()), _context(tmp_path))

    assert outcome.status == "failed"
    assert outcome.failure is not None and outcome.failure.kind == "invalid_input"


def test_finalize_filters_prompt_extras_and_runs_after(tmp_path: Path) -> None:
    module = _capability(tmp_path)

    outcome = _finalize(module, tmp_path, {"change_id": "c1"}, {"message": "hi"})

    assert outcome.status == "succeeded"
    assert outcome.output == {"message": "hi", "plan": "c1", "recovered": False}
    assert (tmp_path / "write" / "qa" / "final.txt").read_text() == "hi"


def test_finalize_output_error_handler_recovers_or_reraises(tmp_path: Path) -> None:
    module = _capability(tmp_path)

    recovered = _finalize(module, tmp_path, {"change_id": "c1"}, {"message": "bad"})
    invalid_result = _finalize(module, tmp_path, {"change_id": "c1"}, {"unexpected": 1})
    strict = _finalize(module, tmp_path, {"change_id": "strict"}, {"message": "bad"})

    assert recovered.output == {"message": "bad message", "plan": "c1", "recovered": True}
    assert invalid_result.status == "succeeded"
    assert isinstance(invalid_result.output, dict) and invalid_result.output["recovered"] is True
    assert strict.failure is not None and strict.failure.kind == "invalid_output"


def test_finalize_write_outside_claims_is_a_scope_error(tmp_path: Path) -> None:
    module = _capability(tmp_path)

    with pytest.raises(WriteScopeError, match="qa/elsewhere.txt"):
        _finalize(module, tmp_path, {"change_id": "c1"}, {"message": "escape"})


def test_prepare_write_refuses_symlinked_claim(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    context = _context(tmp_path)
    (context.write_root / "qa").mkdir()
    (context.write_root / "qa" / "seed.txt").symlink_to(tmp_path / "outside.txt")

    with pytest.raises(WriteScopeError, match="symlink"):
        _run(module, _Request("fixture.cap.echo.prepare", {"change_id": "c1"}, _binding()), context)
    assert not (tmp_path / "outside.txt").exists()


def test_task_op_runs_and_maps_declared_errors(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    context = _context(tmp_path)

    ok = _run(module, _Request("fixture.cap.count", {"value": 1}), context)
    negative = _run(module, _Request("fixture.cap.count", {"value": -1}), context)

    assert ok.output == {"value": 2}
    assert negative.failure is not None and negative.failure.kind == "invalid_input"


def test_dispatch_prefers_bound_target_and_fails_closed(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    context = _context(tmp_path)

    bound = _run(
        module,
        _Request("alias.binding", {"value": 1}, target_capability_id="fixture.cap.count"),
        context,
    )
    unknown = _run(module, _Request("fixture.cap.missing", {}), context)

    assert bound.output == {"value": 2}
    assert unknown.failure is not None and unknown.failure.kind == "configuration"
    assert unknown.failure.retryable is False


def test_duplicate_declaration_fails(tmp_path: Path) -> None:
    router = _router(_capability(tmp_path))
    echo = router.agent_ops()["echo"]

    with pytest.raises(ValueError, match="duplicate op declaration"):
        router.agent("echo", input=echo.input, agent=echo.agent, output=echo.output)


def test_op_package_without_declaration_fails_discovery(tmp_path: Path) -> None:
    module = _capability(tmp_path, extra={"ops/orphan/__init__.py": ""})

    with pytest.raises(RuntimeError, match="disagree with declarations"):
        _router(module).discover()
