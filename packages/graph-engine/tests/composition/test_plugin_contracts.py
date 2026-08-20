from dataclasses import dataclass

import pytest

from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    EffectApplyResult,
    EffectIntent,
    EffectPolicy,
    EffectReconcileResult,
    EffectRegistration,
    PluginDependency,
    PluginContribution,
    PluginContractError,
    PluginDescriptor,
    PluginProvider,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
    TaskFailure,
    TaskOutcome,
    validate_contribution,
)


async def _handler(*_args: object) -> TaskOutcome:
    return TaskOutcome.succeeded()


class _EffectHandler:
    async def apply(self, _intent: EffectIntent, _idempotency_key: str) -> EffectApplyResult:
        return EffectApplyResult.applied({"receipt": "ok"})

    async def reconcile(self, _intent: EffectIntent, _idempotency_key: str) -> EffectReconcileResult:
        return EffectReconcileResult.applied({"receipt": "ok"})


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


@pytest.mark.parametrize(
    ("result_type", "status"),
    [
        (EffectApplyResult, "permanent"),
        (EffectReconcileResult, "permanently_failed"),
    ],
)
def test_permanent_effect_results_require_non_retryable_failures(
    result_type: type[EffectApplyResult] | type[EffectReconcileResult], status: str
) -> None:
    retryable_failure = TaskFailure(kind="external_effect", message="denied")
    with pytest.raises(ValueError, match="permanent effect failure must not be retryable"):
        result_type(status=status, failure=retryable_failure)  # type: ignore[arg-type]

    result = result_type(
        status=status,
        failure=TaskFailure(kind="external_effect", message="denied", retryable=False),
    )  # type: ignore[arg-type]
    assert result.failure is not None
    assert result.failure.retryable is False


@pytest.mark.parametrize(
    ("result_type", "fields"),
    [
        (EffectApplyResult, {"status": "applied", "receipt": {"receipt": "ok"}}),
        (
            EffectApplyResult,
            {
                "status": "transient",
                "failure": TaskFailure(kind="transient", message="retry"),
            },
        ),
        (
            EffectApplyResult,
            {
                "status": "permanent",
                "failure": TaskFailure(kind="external_effect", message="denied", retryable=False),
            },
        ),
        (EffectReconcileResult, {"status": "not_applied"}),
        (EffectReconcileResult, {"status": "pending"}),
        (EffectReconcileResult, {"status": "applied", "receipt": {"receipt": "ok"}}),
        (
            EffectReconcileResult,
            {
                "status": "permanently_failed",
                "failure": TaskFailure(kind="external_effect", message="denied", retryable=False),
            },
        ),
    ],
)
def test_effect_result_statuses_accept_only_their_valid_fields(
    result_type: type[EffectApplyResult] | type[EffectReconcileResult], fields: dict[str, object]
) -> None:
    assert result_type(**fields).status == fields["status"]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("result_type", "fields"),
    [
        (
            EffectApplyResult,
            {
                "status": "applied",
                "failure": TaskFailure(kind="internal", message="unexpected"),
            },
        ),
        (EffectApplyResult, {"status": "transient"}),
        (
            EffectApplyResult,
            {
                "status": "permanent",
                "receipt": {"receipt": "unexpected"},
                "failure": TaskFailure(kind="external_effect", message="denied", retryable=False),
            },
        ),
        (
            EffectReconcileResult,
            {
                "status": "not_applied",
                "receipt": {"receipt": "unexpected"},
            },
        ),
        (
            EffectReconcileResult,
            {
                "status": "pending",
                "failure": TaskFailure(kind="transient", message="unexpected"),
            },
        ),
        (EffectReconcileResult, {"status": "permanently_failed"}),
    ],
)
def test_effect_result_statuses_reject_conflicting_fields(
    result_type: type[EffectApplyResult] | type[EffectReconcileResult], fields: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        result_type(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields",
    [
        {"max_attempts": 0, "timeout_seconds": 1, "backoff_seconds": 0},
        {"max_attempts": 1, "timeout_seconds": 0, "backoff_seconds": 0},
        {"max_attempts": 1, "timeout_seconds": 1, "backoff_seconds": -1},
    ],
)
def test_effect_policy_rejects_invalid_bounds(fields: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        EffectPolicy(**fields)


def test_plugin_contribution_snapshots_implementation_mappings() -> None:
    handlers = {"toy.runtime.run": _handler}
    contribution = PluginContribution(task_handlers=handlers)
    handlers.clear()
    assert tuple(contribution.task_handlers) == ("toy.runtime.run",)
    with pytest.raises(TypeError):
        contribution.task_handlers["toy.runtime.other"] = _handler


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PluginDescriptor("toy.runtime", "not-a-version", "1.0", (), ()),
        lambda: RegistryPorts("not-a-version-or-specifier"),
        lambda: PluginDependency("toy.runtime", "not-a-specifier"),
    ],
)
def test_plugin_contracts_reject_invalid_versions_and_specifiers(factory: object) -> None:
    with pytest.raises(PluginContractError):
        factory()  # type: ignore[operator]


def test_validate_contribution_rejects_duplicate_declared_ids() -> None:
    descriptor = PluginDescriptor(
        "toy.runtime",
        "1.0.0",
        ">=0.2,<0.3",
        (),
        (),
        schemas=("toy.runtime.schema", "toy.runtime.schema"),
    )
    contribution = PluginContribution(
        schemas=(
            SchemaContribution("toy.runtime.schema", "application/json", b"{}"),
            SchemaContribution("toy.runtime.schema", "application/json", b"{}"),
        )
    )
    with pytest.raises(PluginContractError, match="duplicate schema id"):
        validate_contribution(descriptor, contribution)


def test_validate_contribution_rejects_cross_kind_declared_ids() -> None:
    descriptor = PluginDescriptor(
        "toy.runtime",
        "1.0.0",
        ">=0.2,<0.3",
        ("toy.runtime.shared",),
        (),
        schemas=("toy.runtime.shared",),
    )
    contribution = PluginContribution(
        task_handlers={"toy.runtime.shared": _handler},
        schemas=(SchemaContribution("toy.runtime.shared", "application/json", b"{}"),),
    )
    with pytest.raises(PluginContractError, match="cross-kind contribution id"):
        validate_contribution(descriptor, contribution)


def test_validate_contribution_matches_all_declared_contribution_kinds() -> None:
    descriptor = PluginDescriptor(
        "toy.runtime",
        "1.0.0",
        ">=0.2,<0.3",
        ("toy.runtime.run",),
        ("toy.runtime.validate",),
        schemas=("toy.runtime.schema",),
        resources=("toy.runtime.resource",),
        effects=("toy.runtime.effect",),
        bindings=("toy.runtime.alias",),
    )
    contribution = PluginContribution(
        task_handlers={"toy.runtime.run": _handler},
        commit_validators={"toy.runtime.validate": object()},
        schemas=(SchemaContribution("toy.runtime.schema", "application/json", b"{}"),),
        resources=(ResourceContribution("toy.runtime.resource", "text/plain", b"prompt"),),
        effects=(
            EffectRegistration(
                "toy.runtime.effect",
                "toy.runtime.schema",
                "toy.runtime.schema",
                _EffectHandler(),
                EffectPolicy(max_attempts=1, timeout_seconds=1, backoff_seconds=0),
            ),
        ),
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.runtime.alias",
                target_capability_id="toy.runtime.run",
            ),
        ),
    )
    validate_contribution(descriptor, contribution)
