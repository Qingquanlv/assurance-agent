from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from graph_engine.plugin_api import (
    FrozenModel,
    InvocationMetadata,
    TaskContext,
    TaskOutcome,
    TaskWorkspaceIdentity,
)

from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    OutputError,
    run_finalize,
    run_prepare,
)
from agent_runtime_contracts.ops.request import result_contract_from, skill_request
from agent_runtime_contracts.wire.models import AgentRunRequest

_SHA = "a" * 64


@dataclass(frozen=True)
class _Request:
    input: object
    binding_data: object


@dataclass(frozen=True)
class _Roots:
    project_root: Path
    write_root: Path


class _Business(FrozenModel):
    change_id: str


def _binding_data() -> dict[str, object]:
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
            entrypoint="intake",
        ),
    )


def _run_request(tmp_path: Path, binding: AgentBindingDataV1) -> AgentRunRequest:
    return skill_request(
        skill_text="SKILL",
        business=_Business(change_id="chg-1"),
        binding=binding,
        result=result_contract_from("r.v1", {"type": "object"}),
        roots=_Roots(tmp_path, tmp_path / "qa" / "w"),
        allowed_outputs=("qa/out.json",),
        scope_id="chg-1",
    )


def test_run_prepare_returns_prepared_request(tmp_path: Path) -> None:
    context = _context(tmp_path)
    binding = AgentBindingDataV1.model_validate(_binding_data())
    expected = _run_request(tmp_path, binding)
    seen: list[tuple[_Business, AgentBindingDataV1, TaskContext]] = []

    def build(business: _Business, bound: AgentBindingDataV1, ctx: TaskContext) -> AgentRunRequest:
        seen.append((business, bound, ctx))
        return expected

    outcome = run_prepare(
        _Request(input={"change_id": "chg-1"}, binding_data=_binding_data()),
        context,
        input_model=_Business,
        build=build,
    )

    assert outcome.status == "succeeded"
    assert outcome.output == expected.model_dump(mode="json")
    assert seen == [(_Business(change_id="chg-1"), binding, context)]


def test_run_prepare_maps_invalid_input(tmp_path: Path) -> None:
    def build(business: _Business, bound: AgentBindingDataV1, ctx: TaskContext) -> AgentRunRequest:
        raise AssertionError("build must not run")

    outcome = run_prepare(
        _Request(input={"missing": "change_id"}, binding_data=_binding_data()),
        _context(tmp_path),
        input_model=_Business,
        build=build,
    )

    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is True


def test_run_prepare_maps_invalid_binding(tmp_path: Path) -> None:
    def build(business: _Business, bound: AgentBindingDataV1, ctx: TaskContext) -> AgentRunRequest:
        raise AssertionError("build must not run")

    outcome = run_prepare(
        _Request(input={"change_id": "chg-1"}, binding_data={"agent_profile": "Bad Profile"}),
        _context(tmp_path),
        input_model=_Business,
        build=build,
    )

    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


def test_run_prepare_maps_listed_output_error_to_invalid_input(tmp_path: Path) -> None:
    def build(business: _Business, bound: AgentBindingDataV1, ctx: TaskContext) -> AgentRunRequest:
        raise OutputError("bad shape")

    outcome = run_prepare(
        _Request(input={"change_id": "chg-1"}, binding_data=_binding_data()),
        _context(tmp_path),
        input_model=_Business,
        build=build,
        input_errors=(InputError, OutputError),
    )

    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.message == "bad shape"


def test_run_prepare_propagates_unlisted_output_error(tmp_path: Path) -> None:
    def build(business: _Business, bound: AgentBindingDataV1, ctx: TaskContext) -> AgentRunRequest:
        raise OutputError("bad shape")

    with pytest.raises(OutputError, match="bad shape"):
        run_prepare(
            _Request(input={"change_id": "chg-1"}, binding_data=_binding_data()),
            _context(tmp_path),
            input_model=_Business,
            build=build,
        )


def test_run_finalize_returns_commit_outcome(tmp_path: Path) -> None:
    context = _context(tmp_path)
    expected = TaskOutcome.succeeded({"status": "ok"})
    seen: list[tuple[_Business, TaskContext]] = []

    def commit(payload: _Business, ctx: TaskContext) -> TaskOutcome:
        seen.append((payload, ctx))
        return expected

    outcome = run_finalize(
        _Request(input={"change_id": "chg-1"}, binding_data=None),
        context,
        input_model=_Business,
        commit=commit,
    )

    assert outcome == expected
    assert seen == [(_Business(change_id="chg-1"), context)]


def test_run_finalize_maps_invalid_input(tmp_path: Path) -> None:
    def commit(payload: _Business, ctx: TaskContext) -> TaskOutcome:
        raise AssertionError("commit must not run")

    outcome = run_finalize(
        _Request(input={"missing": "change_id"}, binding_data=None),
        _context(tmp_path),
        input_model=_Business,
        commit=commit,
    )

    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is True


def test_run_finalize_maps_output_error_to_invalid_output(tmp_path: Path) -> None:
    def commit(payload: _Business, ctx: TaskContext) -> TaskOutcome:
        raise OutputError("cannot commit")

    outcome = run_finalize(
        _Request(input={"change_id": "chg-1"}, binding_data=None),
        _context(tmp_path),
        input_model=_Business,
        commit=commit,
    )

    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.message == "cannot commit"
    assert outcome.failure.retryable is True
