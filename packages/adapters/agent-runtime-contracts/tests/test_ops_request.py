from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    agent_workspace,
    failed_input,
    logical_write_root,
    result_contract_from,
    skill_request,
    validate_binding,
)
from graph_engine.plugin_api import FrozenModel

_SHA = "a" * 64


@dataclass(frozen=True)
class _Roots:
    project_root: Path
    write_root: Path


class _Business(FrozenModel):
    change_id: str
    validation_error: str | None = None


def _binding() -> AgentBindingDataV1:
    return validate_binding(
        {
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
    )


def test_logical_write_root_variants(tmp_path: Path) -> None:
    assert logical_write_root(_Roots(tmp_path, tmp_path / "qa" / "w")) == "qa/w"
    assert logical_write_root(_Roots(tmp_path, tmp_path)) == ".staging/write"
    assert logical_write_root(_Roots(tmp_path / "p", tmp_path / "elsewhere")) == "qa/.staging/write"


def test_agent_workspace_maps_profile_and_sorts_outputs(tmp_path: Path) -> None:
    workspace = agent_workspace(
        _Roots(tmp_path, tmp_path / "qa" / "w"),
        allowed_outputs=("b.json", "a.json", "a.json"),
        agent_profile="aa-reviewer",
        scope_id="chg-1",
    )
    assert workspace.agent_profile == "assurance-v1-reviewer"
    assert workspace.allowed_outputs == ("a.json", "b.json")
    assert workspace.write_root == "qa/w"


def test_validate_binding_maps_validation_error_to_input_error() -> None:
    with pytest.raises(InputError):
        validate_binding({"agent_profile": "Bad Profile"})
    assert failed_input(InputError("x")).failure is not None


def test_skill_request_orders_instructions(tmp_path: Path) -> None:
    request = skill_request(
        skill_text="SKILL",
        business=_Business(change_id="chg-1", validation_error="fix it"),
        business_extra={"planning_facts": {"k": 1}},
        binding=_binding(),
        result=result_contract_from("r.v1", {"type": "object"}),
        roots=_Roots(tmp_path, tmp_path / "qa" / "w"),
        allowed_outputs=("qa/out.json",),
        scope_id="chg-1",
    )
    kinds = [part.media_type for part in request.instructions]
    assert kinds == ["text/plain", "text/plain", "application/json"]
    assert request.instructions[1].text_content is not None
    assert request.instructions[1].text_content.endswith("fix it")
    assert request.workspace.scope_id == "chg-1"
    assert request.result_contract.schema_id == "r.v1"


def test_binding_rejects_unknown_fields() -> None:
    payload = _binding().model_dump(mode="json") | {"extra": 1}
    with pytest.raises(ValidationError):
        AgentBindingDataV1.model_validate(payload)
