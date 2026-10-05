from __future__ import annotations

import asyncio
import hashlib
import importlib
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
from graph_engine.stategraph.ledger import InputBinding

from agent_runtime_contracts.ops import Agent, ArtifactListResultV1, Dir, OpRouter, Out, WriteScopeError
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
    transport_business=True,
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

from agent_runtime_contracts.ops import OutputError
from PKG.ops import router


class CountInput(FrozenModel):
    value: int


class CountOutput(FrozenModel):
    value: int


def run(ctx, business):
    if business.value == -2:
        raise OutputError("bad result")
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

_TYPED_INIT = """
from graph_engine.plugin_api import FrozenModel
from agent_runtime_contracts.ops import Agent, Finalize, Out, Prepare
from PKG.ops import router

calls = []

class Input(FrozenModel):
    change_id: str

class Document(FrozenModel):
    change_id: str

class Output(FrozenModel):
    digest: str

def after(ctx, business, result):
    calls.append("after")
    assert ctx.file("qa/document.yaml") == Document(change_id=business.change_id)
    return Output(digest=ctx.ref("qa/document.yaml").digest)

op = router.agent(
    "typed",
    input=Input,
    prepare=Prepare(),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-typed",
        strict_files=True,
        writes=(Out("document", "qa/document.yaml", model=Document, format="yaml"),),
    ),
    finalize=Finalize(hook=after),
    output=Output,
)
"""

_TASK_DEP_INIT = """
from graph_engine.plugin_api import FrozenModel
from agent_runtime_contracts.ops import ArtifactHandle
from PKG.ops import router

class Input(FrozenModel):
    source_ref: dict[str, str]

class Document(FrozenModel):
    value: int

class Output(FrozenModel):
    value: int

SOURCE = ArtifactHandle("fixture.cap.source", slot="source_ref", model=Document)

def run(ctx, business, deps):
    return Output(value=deps[SOURCE.ledger_key].value)

op = router.task(
    "task-dep",
    input=Input,
    output=Output,
    run=run,
    depends=(SOURCE,),
    reads=("qa",),
    writes=(),
)
"""

_TYPED_INPUT_INIT = """
from graph_engine.plugin_api import FrozenModel
from agent_runtime_contracts.ops import Agent, ArtifactHandle, Finalize, Out, Prepare
from PKG.ops import router

calls = []

class Input(FrozenModel):
    source_ref: dict[str, str]

class Document(FrozenModel):
    value: int

class Result(FrozenModel):
    done: bool

class Output(FrozenModel):
    value: int

SOURCE = ArtifactHandle("fixture.cap.typed-input", slot="source_ref", model=Document)

def after(ctx, business, result):
    calls.append("after")
    return Output(value=ctx.project_file("qa/source.json").value)

op = router.agent(
    "typed-input",
    input=Input,
    prepare=Prepare(
        depends=(SOURCE,), eager_artifacts=True,
        reads=(Out("source", "qa/source.json", model=Document, format="json"),),
    ),
    agent=Agent(profile="assurance-v1-reviewer", skill="aa-typed-input", result=Result, writes=()),
    finalize=Finalize(hook=after),
    output=Output,
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


def test_agent_op_may_override_the_router_retry_policy(tmp_path: Path) -> None:
    override = _ECHO_INIT.replace(
        'op = router.agent(\n    "echo",',
        'op = router.agent(\n    "echo",\n    retry=router.agent_retry.model_copy(update={"carry_invalid_output": True}),',
    )
    assert override != _ECHO_INIT
    default_router = _router(_capability(tmp_path / "default"))
    override_router = _router(_capability(tmp_path / "override", extra={"ops/echo/__init__.py": override}))

    assert default_router.agent_contracts()["echo"].retry.carry_invalid_output is False
    retry = override_router.agent_contracts()["echo"].retry
    assert (retry.max_attempts, retry.carry_invalid_output) == (2, True)


def test_resource_manifest_comes_from_op_directories(tmp_path: Path) -> None:
    router = _router(_capability(tmp_path))

    assert router.resource_files() == {
        "fixture.cap.skill.aa-echo.notes.v1": "ops/echo/notes.md",
        "fixture.cap.skill.aa-echo.v1": "ops/echo/SKILL.md",
    }
    assert router.resource_text("ops/echo/notes.md") == "echo notes\n"


def test_result_contract_is_the_agent_result_model_schema(tmp_path: Path) -> None:
    op = _router(_capability(tmp_path)).agent_ops()["echo"]
    contract = op.result_contract()

    assert op.agent.result.__name__ == "EchoResult"
    assert contract.schema_id == "fixture.cap.result.echo.v1"
    assert contract.schema_digest == canonical_digest(op.agent.result.model_json_schema())


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
    assert isinstance(plain.output, dict)
    assert plain.output["prepared_business"] == {"change_id": "c1", "notes": []}
    request = AgentRunRequest.model_validate(plain.output["run_request"])
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

    request = AgentRunRequest.model_validate(outcome.output["run_request"])
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


def test_agent_result_defaults_to_the_file_receipt() -> None:
    agent = Agent(profile="assurance-v1-reviewer", skill="aa-echo", writes=("qa/notes.md",))

    assert agent.result is ArtifactListResultV1
    receipt = ArtifactListResultV1.model_validate({"output_files": ["qa/b.md", " qa/a.md", "qa/b.md"]})
    assert receipt.output_files == ("qa/a.md", "qa/b.md")
    with pytest.raises(ValueError, match="canonical and relative"):
        ArtifactListResultV1.model_validate({"output_files": ["../escape.md"]})


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


def test_declared_file_receipt_and_model_are_checked_before_after(tmp_path: Path) -> None:
    module = _capability(
        tmp_path,
        extra={"ops/typed/__init__.py": _TYPED_INIT, "ops/typed/SKILL.md": "typed skill\n"},
    )
    typed = importlib.import_module(f"{module.__name__}.typed")
    context = _context(tmp_path)
    request = lambda files: _Request(  # noqa: E731
        "fixture.cap.typed.finalize",
        {"prepare": {"change_id": "c1"}, "agent_result": _agent_result({"output_files": files})},
    )

    missing = _run(module, request([]), context)
    assert missing.failure is not None and missing.failure.kind == "invalid_output"
    assert typed.calls == []

    _write(context.write_root, "qa/document.yaml", "wrong: c1\n")
    invalid = _run(module, request(["qa/document.yaml"]), context)
    assert invalid.failure is not None and invalid.failure.kind == "invalid_output"
    assert typed.calls == []

    _write(context.write_root, "qa/document.yaml", "change_id: c1\n")
    valid = _run(module, request(["qa/document.yaml"]), context)
    assert valid.status == "succeeded"
    assert typed.calls == ["after"]

    extra = _run(module, request(["qa/document.yaml", "qa/extra.yaml"]), context)
    assert extra.failure is not None and extra.failure.kind == "invalid_output"
    assert typed.calls == ["after"]


def test_task_declared_artifact_is_authenticated_and_typed_before_run(tmp_path: Path) -> None:
    module = _capability(tmp_path, extra={"ops/task_dep/__init__.py": _TASK_DEP_INIT})
    context = _context(tmp_path)
    path = context.project_root / "qa/source.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"value": 7}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    request = lambda value: _Request("fixture.cap.task-dep", {"source_ref": value})  # noqa: E731

    valid = _run(module, request({"path": "qa/source.json", "digest": digest}), context)
    assert valid.status == "succeeded" and valid.output == {"value": 7}

    wrong_digest = _run(module, request({"path": "qa/source.json", "digest": "0" * 64}), context)
    assert wrong_digest.failure is not None and wrong_digest.failure.kind == "invalid_input"

    path.write_text('{"wrong": 7}')
    wrong_schema = _run(
        module,
        request({"path": "qa/source.json", "digest": hashlib.sha256(path.read_bytes()).hexdigest()}),
        context,
    )
    assert wrong_schema.failure is not None and wrong_schema.failure.kind == "invalid_input"


def test_declared_typed_input_rejects_change_after_eager_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _capability(
        tmp_path,
        extra={
            "ops/typed_input/__init__.py": _TYPED_INPUT_INIT,
            "ops/typed_input/SKILL.md": "typed input skill\n",
        },
    )
    typed = importlib.import_module(f"{module.__package__}.typed_input")
    context = _context(tmp_path)
    source = context.project_root / "qa/source.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"value": 7}', encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    router_module = importlib.import_module("agent_runtime_contracts.ops.router")
    original_read = router_module.read_workspace_file

    def mutate_on_typed_capture(workspace: Path, relative: str) -> bytes:
        if relative == "qa/source.json":
            source.write_text('{"value": 9}', encoding="utf-8")
        return original_read(workspace, relative)

    monkeypatch.setattr(router_module, "read_workspace_file", mutate_on_typed_capture)
    outcome = _run(
        module,
        _Request(
            "fixture.cap.typed-input.finalize",
            {
                "prepare": {"source_ref": {"path": "qa/source.json", "digest": digest}},
                "agent_result": _agent_result({"done": True}),
            },
        ),
        context,
    )
    assert outcome.failure is not None and outcome.failure.kind == "invalid_input"
    assert typed.calls == []


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
    rejected = _run(module, _Request("fixture.cap.count", {"value": -2}), context)

    assert ok.output == {"value": 2}
    assert negative.failure is not None and negative.failure.kind == "invalid_input"
    assert rejected.failure is not None and rejected.failure.kind == "invalid_output"
    assert rejected.failure.retryable is True


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


def test_finalize_accepts_a_flat_business_payload(tmp_path: Path) -> None:
    module = _capability(tmp_path)
    request = _Request(
        "fixture.cap.echo.finalize",
        {
            "change_id": "c1",
            "notes": (),
            "planning_facts": {"plan": "x"},
            "agent_result": _agent_result({"message": "hi"}),
        },
    )

    outcome = _run(module, request, _context(tmp_path))

    assert outcome.output == {"message": "hi", "plan": "c1", "recovered": False}


_DOTTED_INIT = """
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.ops import Agent, Dir, Prepare
from PKG.ops import router


class CodegenInput(FrozenModel):
    change_id: str
    files: tuple[str, ...] = ()


class CodegenResult(FrozenModel):
    message: str


def before(ctx, business):
    if business.files:
        ctx.bind("qa/tests", business.files)
    ctx.write("qa/tests/seed.txt", b"seed")
    return business


op = router.agent(
    "api.codegen",
    input=CodegenInput,
    prepare=Prepare(hook=before, writes=("qa/tests",)),
    agent=Agent(
        profile="assurance-v1-test-author",
        skill="aa-api-codegen",
        result=CodegenResult,
        writes=("qa/results/codegen/api-codegen-summary.md", Dir("qa/tests")),
    ),
    output=CodegenResult,
)
"""


def test_dotted_op_keeps_a_directory_shared_by_prepare_and_runtime(tmp_path: Path) -> None:
    module = _capability(
        tmp_path,
        extra={
            "ops/api_codegen/__init__.py": _DOTTED_INIT,
            "ops/api_codegen/SKILL.md": "codegen skill\n",
        },
    )
    router = _router(module)
    contract = router.agent_contracts()["api.codegen"]

    assert contract.contract_id == "fixture.cap.agent.api.codegen.v1"
    assert contract.prepare_handler_id == "fixture.cap.api.codegen.prepare"
    assert contract.phase_write_claims.as_projection() == {
        "prepare": ["qa/tests"],
        "runtime": ["qa/results/codegen/api-codegen-summary.md", "qa/tests"],
        "finalize": [],
    }
    assert router.output_routes()["api.codegen"] == ("qa/results/codegen/api-codegen-summary.md",)

    outcome = _run(
        module,
        _Request(
            "fixture.cap.api.codegen.prepare",
            {"change_id": "c1", "files": ["qa/tests/api/test_dept.py"]},
            _binding(),
        ),
        _context(tmp_path),
    )
    request = AgentRunRequest.model_validate(outcome.output)
    assert request.workspace.allowed_outputs == (
        "qa/results/codegen/api-codegen-summary.md",
        "qa/tests/api/test_dept.py",
    )


def test_directory_only_agent_routes_its_directory(tmp_path: Path) -> None:
    declaration = _DOTTED_INIT.replace(
        '("qa/results/codegen/api-codegen-summary.md", Dir("qa/tests"))', '(Dir("qa/tests"),)'
    )
    assert declaration != _DOTTED_INIT
    module = _capability(
        tmp_path,
        extra={
            "ops/api_codegen/__init__.py": declaration,
            "ops/api_codegen/SKILL.md": "codegen skill\n",
        },
    )

    assert _router(module).output_routes()["api.codegen"] == ("qa/tests",)


def test_unbound_directory_is_invalid_input(tmp_path: Path) -> None:
    module = _capability(
        tmp_path,
        extra={
            "ops/api_codegen/__init__.py": _DOTTED_INIT,
            "ops/api_codegen/SKILL.md": "codegen skill\n",
        },
    )

    outcome = _run(
        module,
        _Request("fixture.cap.api.codegen.prepare", {"change_id": "c1"}, _binding()),
        _context(tmp_path),
    )

    assert outcome.failure is not None and outcome.failure.kind == "invalid_input"


_SEAL_INIT = """
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.ops import Agent, Finalize, Out
from PKG.ops import router


class SealInput(FrozenModel):
    change_id: str


class SealResult(FrozenModel):
    message: str


op = router.agent(
    "seal",
    input=SealInput,
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-seal",
        result=SealResult,
        writes=(),
    ),
    finalize=Finalize(
        writes=(
            Out("reviewed_case", "qa/cases/reviewed-case.json"),
            "qa/cases/reviews",
        )
    ),
    output=SealResult,
)
"""


def test_named_finalize_write_is_a_ledger_key_and_not_a_digest_name(tmp_path: Path) -> None:
    module = _capability(
        tmp_path,
        extra={
            "ops/seal/__init__.py": _SEAL_INIT,
            "ops/seal/SKILL.md": "seal skill\n",
        },
    )
    op = _router(module).ops()["seal"]
    assert [(item.name, item.root, item.many) for item in op.ledger_writes()] == [
        ("reviewed_case", "qa/cases/reviewed-case.json", False)
    ]
    claims = op.contract().canonical_projection()["phase_write_claims"]
    assert claims == {
        "prepare": [],
        "runtime": [],
        "finalize": ["qa/cases/reviewed-case.json", "qa/cases/reviews"],
    }


_HISTORY_INIT = """
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.ops import Dir, Out
from PKG.ops import router


class Input(FrozenModel):
    change_id: str


class Output(FrozenModel):
    ok: bool


def run(ctx, business):
    return Output(ok=True)


op = router.task(
    "history",
    input=Input,
    output=Output,
    run=run,
    reads=("qa",),
    writes=(
        Dir("qa/history", name="history", accumulate=True),
        Dir("qa/notes", name="notes"),
        Out("summary", "qa/summary.md"),
    ),
)
"""


def test_task_op_input_bindings_and_models_match_agent_op(tmp_path: Path) -> None:
    module = _capability(tmp_path, extra={"ops/task_dep/__init__.py": _TASK_DEP_INIT})
    router = _router(module)
    task = router.task_ops()["task-dep"]
    echo = router.agent_ops()["echo"]

    assert task.input_bindings() == (InputBinding(ledger_key="fixture.cap.source", field="source_ref"),)
    assert task.input_model is task.input
    assert task.output_model is task.output
    assert echo.input_model is echo.input
    assert echo.output_model is echo.output


def test_dir_accumulate_is_stored_on_the_named_write_and_out_rejects_it(tmp_path: Path) -> None:
    module = _capability(tmp_path, extra={"ops/history/__init__.py": _HISTORY_INIT})
    writes = _router(module).task_ops()["history"].ledger_writes()

    assert [(item.name, item.accumulate) for item in writes] == [
        ("history", True),
        ("notes", False),
        ("summary", False),
    ]
    with pytest.raises(TypeError):
        Out("summary", "qa/summary.md", accumulate=True)


_MARK_INIT = """
from graph_engine.artifacts import ArtifactRef
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.ops import Agent, Finalize, InputError, Prepare
from PKG.ops import router

calls = []


class Input(FrozenModel):
    change_id: str
    plan_digest: str


class Result(FrozenModel):
    change_id: str
    plan_digest: str
    context_ref: str
    nested: dict[str, str] = {}


class Note(FrozenModel):
    text: str


class Output(FrozenModel):
    change_id: str
    artifacts: tuple[ArtifactRef, ...] = ()


def after(ctx, business, result):
    calls.append(business.change_id)
    if business.change_id == "raise-value":
        raise ValueError("hook rejected")
    if business.change_id == "raise-input":
        raise InputError("locked")
    ctx.stage("qa/b.json", Note(text="b"))
    ctx.stage("qa/a.json", Note(text="a"))
    ctx.stage("qa/a.json", Note(text="a"))
    return Output(change_id=result.change_id)


op = router.agent(
    "mark",
    input=Input,
    prepare=Prepare(),
    agent=Agent(profile="assurance-v1-reviewer", skill="aa-mark", result=Result, writes=()),
    finalize=Finalize(
        hook=after,
        same=("change_id", "plan_digest"),
        errors=(ValueError,),
        error_failure="output",
        artifacts="auto",
        writes=("qa/a.json", "qa/b.json"),
    ),
    output=Output,
)
"""

_REJECT_INIT = """
from graph_engine.plugin_api import FrozenModel

from agent_runtime_contracts.ops import Agent, Finalize, Prepare
from PKG.ops import router


class Input(FrozenModel):
    change_id: str


def reject(ctx, business, result):
    raise ValueError("mapped")


op = router.agent(
    "reject-input",
    input=Input,
    prepare=Prepare(),
    agent=Agent(profile="assurance-v1-reviewer", skill="aa-reject", result=Input, writes=()),
    finalize=Finalize(hook=reject, errors=(ValueError,)),
    output=Input,
)
"""


def _mark_module(tmp_path: Path) -> ModuleType:
    return _capability(
        tmp_path,
        extra={
            "ops/mark/__init__.py": _MARK_INIT,
            "ops/mark/SKILL.md": "mark\n",
            "ops/reject_input/__init__.py": _REJECT_INIT,
            "ops/reject_input/SKILL.md": "reject\n",
        },
    )


def _mark_finalize(
    module: ModuleType, tmp_path: Path, business: dict[str, Any], payload: dict[str, Any], *, name: str
) -> TaskOutcome:
    request = _Request(
        capability_id=f"fixture.cap.{name}.finalize",
        input={"prepare": business, "agent_result": _agent_result(payload)},
    )
    return _run(module, request, _context(tmp_path))


def test_finalize_same_accepts_matching_top_level_fields(tmp_path: Path) -> None:
    module = _mark_module(tmp_path)
    business = {"change_id": "c1", "plan_digest": "p1"}
    payload = {
        "change_id": "c1",
        "plan_digest": "p1",
        "context_ref": "explore/context.json",
        "nested": {"change_id": "other"},
    }

    outcome = _mark_finalize(module, tmp_path, business, payload, name="mark")

    assert outcome.status == "succeeded"
    artifacts = outcome.output["artifacts"]
    assert [item["path"] for item in artifacts] == ["qa/a.json", "qa/b.json"]
    assert len({item["digest"] for item in artifacts}) == 2
    mark = importlib.import_module(f"{module.__name__}.mark")
    assert mark.calls == ["c1"]


def test_finalize_same_mismatch_is_retryable_invalid_output(tmp_path: Path) -> None:
    module = _mark_module(tmp_path)
    outcome = _mark_finalize(
        module,
        tmp_path,
        {"change_id": "c1", "plan_digest": "p1"},
        {"change_id": "c1", "plan_digest": "other", "context_ref": "explore/context.json"},
        name="mark",
    )

    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True
    assert "plan_digest" in outcome.failure.message
    mark = importlib.import_module(f"{module.__name__}.mark")
    assert mark.calls == []


def test_finalize_errors_can_fail_as_output_or_input(tmp_path: Path) -> None:
    module = _mark_module(tmp_path)
    output_failure = _mark_finalize(
        module,
        tmp_path,
        {"change_id": "raise-value", "plan_digest": "p1"},
        {"change_id": "raise-value", "plan_digest": "p1", "context_ref": "explore/context.json"},
        name="mark",
    )
    input_guard = _mark_finalize(
        module,
        tmp_path,
        {"change_id": "raise-input", "plan_digest": "p1"},
        {"change_id": "raise-input", "plan_digest": "p1", "context_ref": "explore/context.json"},
        name="mark",
    )
    input_failure = _mark_finalize(
        module,
        tmp_path,
        {"change_id": "c1"},
        {"change_id": "c1"},
        name="reject-input",
    )

    assert output_failure.failure is not None
    assert (output_failure.failure.kind, output_failure.failure.retryable) == ("invalid_output", True)
    assert "hook rejected" in output_failure.failure.message
    assert input_guard.failure is not None
    assert (input_guard.failure.kind, input_guard.failure.retryable) == ("invalid_input", True)
    assert input_failure.failure is not None
    assert (input_failure.failure.kind, input_failure.failure.retryable) == ("invalid_input", True)
    assert "mapped" in input_failure.failure.message


def test_finalize_rejects_unknown_error_and_artifact_policies() -> None:
    from typing import Any, cast

    from agent_runtime_contracts.ops import Finalize

    with pytest.raises(ValueError, match="error_failure"):
        Finalize(error_failure=cast(Any, "nope"))
    with pytest.raises(ValueError, match="artifacts"):
        Finalize(artifacts=cast(Any, "manual"))


def test_artifact_handle_same_accepts_and_rejects_top_level_fields(tmp_path: Path) -> None:
    from graph_engine.plugin_api import FrozenModel

    from agent_runtime_contracts.ops import ArtifactHandle, InputError

    class Business(FrozenModel):
        change_id: str
        plan_digest: str
        source_ref: dict[str, str] | None = None
        refs: tuple[dict[str, str], ...] = ()

    class Document(FrozenModel):
        change_id: str
        plan_digest: str
        nested: dict[str, str] = {}

    matched = b'{"change_id":"c1","plan_digest":"p1","nested":{"change_id":"other"}}'
    drifted = b'{"change_id":"other","plan_digest":"p1","nested":{}}'
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / "doc.json").write_bytes(matched)
    (tmp_path / "qa" / "other.json").write_bytes(drifted)
    good = {"path": "qa/doc.json", "digest": hashlib.sha256(matched).hexdigest()}
    bad = {"path": "qa/other.json", "digest": hashlib.sha256(drifted).hexdigest()}
    handle = ArtifactHandle(
        "fixture.doc", slot="source_ref", model=Document, same=("change_id", "plan_digest")
    )
    business = Business(change_id="c1", plan_digest="p1", source_ref=good)

    loaded = handle.load(tmp_path, business)

    assert isinstance(loaded, Document)
    assert loaded.change_id == "c1"
    assert loaded.nested["change_id"] == "other"
    with pytest.raises(InputError, match="fixture.doc plan_digest"):
        handle.load(tmp_path, Business(change_id="c1", plan_digest="nope", source_ref=good))
    absent = ArtifactHandle("fixture.doc", slot="source_ref", model=Document, same=("missing",))
    with pytest.raises(InputError, match="missing"):
        absent.load(tmp_path, business)
    many = ArtifactHandle("fixture.docs", slot="refs", many=True, model=Document, same=("change_id",))
    assert len(many.load(tmp_path, Business(change_id="c1", plan_digest="p1", refs=(good,)))) == 1
    with pytest.raises(InputError, match="change_id"):
        many.load(tmp_path, Business(change_id="c1", plan_digest="p1", refs=(good, bad)))
    optional = ArtifactHandle(
        "fixture.doc", slot="source_ref", model=Document, optional=True, same=("change_id",)
    )
    assert optional.load(tmp_path, Business(change_id="c1", plan_digest="p1")) is None
