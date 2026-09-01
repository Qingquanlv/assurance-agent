from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_runtime_contracts.runtime_binding import (
    AgentRuntimeBinding,
    AgentRuntimeCapabilities,
    AgentRuntimePolicy,
)


def _valid_binding_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_id": "assurance.intake.agent.case-design.v1",
        "runtime_handler_id": "runtime.opencode.execute",
        "provider": "opencode",
        "model": "provider_default",
        "policy": AgentRuntimePolicy(
            request_policy_handle="assurance.policy.v1",
            request_config_handle="assurance.config.v1",
        ),
        "secret_handles": ("opencode.token",),
    }
    payload.update(overrides)
    return payload


def test_runtime_binding_contains_only_deployment_handles() -> None:
    binding = AgentRuntimeBinding.model_validate(_valid_binding_payload())
    assert binding.contract_id == "assurance.intake.agent.case-design.v1"
    assert binding.runtime_handler_id == "runtime.opencode.execute"
    assert binding.provider == "opencode"
    assert binding.model == "provider_default"
    assert binding.policy.request_policy_handle == "assurance.policy.v1"
    assert binding.policy.request_config_handle == "assurance.config.v1"
    assert binding.secret_handles == ("opencode.token",)
    dumped = binding.model_dump()
    assert set(dumped) == {
        "contract_id",
        "runtime_handler_id",
        "provider",
        "model",
        "policy",
        "secret_handles",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"resources": {"reads": ("qa",)}},
        {"validators": ()},
        {"prepare_handler_id": "assurance.intake.case-design.prepare"},
        {"finalize_handler_id": "assurance.intake.case-design.finalize"},
        {"input_model": "CaseDesignInput"},
        {"agent_result_model": "CaseDesignAgentResult"},
        {"output_model": "CaseDesignOutput"},
        {"skill_id": "aa-case-design"},
        {"agent_profile": "assurance-v1-doc-author"},
    ],
)
def test_runtime_binding_rejects_feature_authority(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="extra"):
        AgentRuntimeBinding.model_validate({**_valid_binding_payload(), **extra})


def test_runtime_capabilities_are_provider_schema_only() -> None:
    capabilities = AgentRuntimeCapabilities(provider_schema=False)
    assert capabilities.provider_schema is False
    with pytest.raises(ValidationError, match="extra"):
        AgentRuntimeCapabilities.model_validate({"provider_schema": False, "skill_id": "aa-case-design"})
