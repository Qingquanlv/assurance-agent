from __future__ import annotations

import pytest

from assurance_agent.config import ModelRoutingCfg
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.model_routing import (
    ModelRouteContext,
    ModelRouter,
    ModelRoutingError,
)
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2


def _policy() -> ModelRoutingCfg:
    return ModelRoutingCfg.model_validate(
        {
            "default": "anthropic/deepseek-v4-flash",
            "strict_routes": True,
            "routes": {
                "aa-case-design": "anthropic/glm-5.2",
                "aa-case-reviewer": "anthropic/deepseek-v4-flash",
                "aa-improvement-reviewer": "anthropic/glm-5.2",
            },
            "escalation": {
                "model": "anthropic/glm-5.2",
                "on_error_kinds": ["invalid_output", "forbidden_write"],
            },
        }
    )


@pytest.mark.parametrize(
    ("skill", "expected"),
    [
        ("aa-case-design", "anthropic/glm-5.2"),
        ("aa-case-reviewer", "anthropic/deepseek-v4-flash"),
        ("aa-improvement-reviewer", "anthropic/glm-5.2"),
    ],
)
def test_routes_exact_workflow_skill(skill: str, expected: str) -> None:
    resolution = ModelRouter(_policy()).resolve(ModelRouteContext(adapter="opencode", skill=skill))

    assert resolution.model == expected
    assert resolution.source == "skill_route"
    assert resolution.policy_sha256 is not None


def test_cli_override_has_priority_over_skill_and_escalation() -> None:
    resolution = ModelRouter(_policy()).resolve(
        ModelRouteContext(
            adapter="opencode",
            skill="aa-case-design",
            prior_error_kind="invalid_output",
            cli_override="anthropic/deepseek-v4-flash",
        )
    )

    assert resolution.model == "anthropic/deepseek-v4-flash"
    assert resolution.source == "cli_override"


@pytest.mark.parametrize("kind", ["invalid_output", "forbidden_write"])
def test_contract_failure_uses_escalation_model(kind: ErrorKind) -> None:
    resolution = ModelRouter(_policy()).resolve(
        ModelRouteContext(
            adapter="opencode",
            skill="aa-case-reviewer",
            prior_error_kind=kind,
        )
    )

    assert resolution.model == "anthropic/glm-5.2"
    assert resolution.source == "escalation"


def test_escalation_stays_sticky_after_later_infrastructure_failure() -> None:
    resolution = ModelRouter(_policy()).resolve(
        ModelRouteContext(
            adapter="opencode",
            skill="aa-case-reviewer",
            prior_error_kind="timeout",
            contract_failure_kinds_seen=("invalid_output",),
        )
    )

    assert resolution.model == "anthropic/glm-5.2"
    assert resolution.source == "escalation"


def test_sticky_failure_must_match_configured_escalation_kind() -> None:
    base_policy = _policy()
    assert base_policy.escalation is not None
    policy = base_policy.model_copy(
        update={
            "escalation": base_policy.escalation.model_copy(update={"on_error_kinds": ("invalid_output",)})
        }
    )

    resolution = ModelRouter(policy).resolve(
        ModelRouteContext(
            adapter="opencode",
            skill="aa-case-reviewer",
            prior_error_kind="timeout",
            contract_failure_kinds_seen=("forbidden_write",),
        )
    )

    assert resolution.model == "anthropic/deepseek-v4-flash"
    assert resolution.source == "skill_route"


def test_strict_policy_rejects_unrouted_skill() -> None:
    with pytest.raises(ModelRoutingError, match="no model route"):
        ModelRouter(_policy()).resolve(ModelRouteContext(adapter="opencode", skill="aa-new-phase"))


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "transport", "timeout"])
def test_infrastructure_failure_does_not_escalate(kind: ErrorKind) -> None:
    resolution = ModelRouter(_policy()).resolve(
        ModelRouteContext(
            adapter="opencode",
            skill="aa-case-reviewer",
            prior_error_kind=kind,
        )
    )

    assert resolution.model == "anthropic/deepseek-v4-flash"
    assert resolution.source == "skill_route"


def test_strict_policy_validates_compiled_skill_coverage() -> None:
    compiled = compile_workflow(
        parse_workflow_v2(
            """
schema_version: "2"
name: routing
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 5
    nodes:
      design: {uses: skill:aa-case-design}
      missing: {uses: skill:aa-new-phase}
    edges:
      - {from: START, to: design}
      - {from: design, to: missing}
      - {from: missing, to: END}
gates: {}
"""
        )
    )

    with pytest.raises(ModelRoutingError, match="aa-new-phase"):
        ModelRouter(_policy()).validate_compiled(
            compiled,
            adapter="opencode",
            cli_override=None,
        )

    ModelRouter(_policy()).validate_compiled(
        compiled,
        adapter="opencode",
        cli_override="anthropic/glm-5.2",
    )
