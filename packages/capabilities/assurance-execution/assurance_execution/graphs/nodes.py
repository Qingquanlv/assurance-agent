from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from assurance_execution.contracts.agent import ExecuteInputV1, RunSkillInputV1
from assurance_execution.graphs.state import ExecutionPublicOutput, ExecutionState
from graph_engine.attempts.keys import BusinessActivation

activation_execute = BusinessActivation.one_shot()

_SKILL_FIELDS = (
    "change_id",
    "batch_id",
    "capability_leafs",
    "case_ids",
    "artifact_paths",
    "mapping",
    "selected_targets",
    "baseline_tree_id",
    "runner_profile_digest",
)


def _skill_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {name: state[name] for name in _SKILL_FIELDS}


def select_execute(state: Mapping[str, object]) -> ExecuteInputV1:
    return ExecuteInputV1.model_validate(_skill_payload(state))


def select_rerun(state: Mapping[str, object]) -> RunSkillInputV1:
    return RunSkillInputV1.model_validate(_skill_payload(state))


def activation_rerun(state: Mapping[str, object]) -> BusinessActivation:
    raw = state.get("activation")
    if not isinstance(raw, Mapping):
        raise ValueError("rerun business activation must be parent-supplied")
    kind = raw.get("kind")
    value = raw.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError("rerun business activation value is missing")
    if kind == "round":
        return BusinessActivation.for_round(int(value))
    if kind == "trigger":
        return BusinessActivation.for_trigger(value)
    raise ValueError("rerun business activation is not canonical")


def publish_execution(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    payload = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    if not isinstance(payload, Mapping):
        raise TypeError("execution output must be a mapping")
    status = "failed" if payload.get("final_status") == "FAIL" else "passed"
    rounds_budget = state["rounds_budget"]
    rounds_used = state["rounds_used"]
    if not isinstance(rounds_budget, int) or isinstance(rounds_budget, bool):
        raise TypeError("rounds_budget must be an int")
    if not isinstance(rounds_used, int) or isinstance(rounds_used, bool):
        raise TypeError("rounds_used must be an int")
    return ExecutionPublicOutput(
        rounds_budget=rounds_budget,
        rounds_used=rounds_used,
        status=status,
    ).model_dump(mode="json")


def route_execution(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return "failed"
    return "committed"


def terminal_committed(state: ExecutionState) -> dict[str, object]:
    del state
    return {}


def terminal_failed(state: ExecutionState) -> dict[str, object]:
    del state
    return {}


__all__ = [
    "activation_execute",
    "activation_rerun",
    "publish_execution",
    "route_execution",
    "select_execute",
    "select_rerun",
    "terminal_committed",
    "terminal_failed",
]
