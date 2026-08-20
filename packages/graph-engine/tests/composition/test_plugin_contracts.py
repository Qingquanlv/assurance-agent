from dataclasses import dataclass

import pytest

from graph_engine.plugin_api import (
    EffectIntent,
    PluginContribution,
    PluginContractError,
    PluginDescriptor,
    PluginProvider,
    RegistryPorts,
    TaskFailure,
    TaskOutcome,
    validate_contribution,
)


@dataclass(frozen=True)
class _Provider:
    descriptor_value: PluginDescriptor
    contribution: PluginContribution

    def descriptor(self) -> PluginDescriptor:
        return self.descriptor_value

    def contribute(self, _ports: RegistryPorts) -> PluginContribution:
        return self.contribution


def test_task_failure_retryability_and_effect_outcome_invariants() -> None:
    failure = TaskFailure(kind="external_effect", message="denied", retryable=False)
    assert failure.retryable is False
    intent = EffectIntent(kind="toy.audit.append", payload={"line": "hello"})
    outcome = TaskOutcome.succeeded(output={"ok": True}, effects=(intent,))
    assert outcome.effects == (intent,)
    with pytest.raises(ValueError, match="effects are allowed only"):
        TaskOutcome(status="failed", failure=failure, effects=(intent,))


def test_plugin_contribution_must_match_descriptor_ids() -> None:
    provider = _Provider(
        descriptor_value=PluginDescriptor(
            plugin_id="toy.runtime",
            plugin_version="1.0.0",
            engine_api=">=0.2,<0.3",
            dependencies=(),
            task_handlers=("toy.runtime.run",),
            commit_validators=(),
            schemas=(),
            resources=(),
            effects=(),
            bindings=(),
        ),
        contribution=PluginContribution.empty(),
    )
    with pytest.raises(PluginContractError, match="task handler declarations disagree"):
        validate_contribution(provider.descriptor(), provider.contribute(RegistryPorts("0.2")))


def test_plugin_provider_additively_exposes_contribution_method() -> None:
    assert "contribute" in PluginProvider.__dict__
