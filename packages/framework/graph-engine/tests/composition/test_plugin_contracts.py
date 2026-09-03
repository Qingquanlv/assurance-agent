from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

import graph_engine
import graph_engine.plugin_api as plugin_api
from graph_engine.canonical import canonical_digest
from graph_engine.composition.contributions import (
    ContributionProjection,
    ContributionSourceKeyProjection,
)
from graph_engine.composition.models import (
    AttemptContractRef,
    CapabilityRegistry,
    ContributionAuthority,
    EffectRegistry,
    RegistrySet,
    ResourceRegistry,
    SchemaRegistry,
    SourceKey,
    SourceRegistry,
    SourceRole,
)
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
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
    ProviderSource,
    RegistryPorts,
    ResourceClaims,
    ResourceContribution,
    SchemaContribution,
    TaskFailure,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
    TaskWorkspaceIdentity,
    InvocationMetadata,
    ValidationContext,
    ValidationResult,
    validate_contribution,
)


_TEST_INVOCATION = InvocationMetadata(
    invocation_id="inv-1",
    lock_digest="a" * 64,
    composition_digest="b" * 64,
    entrypoint="main",
)


def _workspace_identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


class _Handler:
    async def execute(self, *_args: object) -> TaskOutcome:
        return TaskOutcome.succeeded()


_handler = _Handler()


def test_plugin_descriptor_authenticates_its_static_source_expectation() -> None:
    source = ProviderSource(
        distribution="Toy_Runtime",
        version="1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="toy.runtime",
        entrypoint_value="toy_runtime.plugin:provider",
        declaration_path="toy_runtime/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=source,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=">=0.2,<0.3",
        dependencies=(),
        task_handlers=(),
        commit_validators=(),
    )

    assert descriptor.source.distribution == "toy-runtime"
    assert descriptor.source.version == "1.0"
    assert descriptor.source.entrypoint_value == "toy_runtime.plugin:provider"
    with pytest.raises(ValidationError, match="source version must equal plugin version"):
        PluginDescriptor.model_validate(
            {
                **descriptor.model_dump(mode="json"),
                "source": {**source.model_dump(mode="json"), "version": "2.0.0"},
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProviderSource.model_validate({**source.model_dump(mode="json"), "inferred": True})


def test_provider_source_authenticates_canonical_import_roots() -> None:
    source = ProviderSource(
        distribution="toy-runtime",
        version="1.0.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="toy.runtime",
        entrypoint_value="toy_runtime.plugin:provider",
        declaration_path="toy_runtime/plugin-declaration.json",
        import_roots=("", "src"),
    )

    assert source.import_roots == ("", "src")
    for invalid in (("src", ""), ("src", "src"), ("/src",), ("./src",), ("src\\pkg",)):
        with pytest.raises(ValidationError, match="import root"):
            source.model_copy(update={"import_roots": invalid}).__class__.model_validate(
                {**source.model_dump(mode="json"), "import_roots": invalid}
            )


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
            schema_version="1",
            source=None,
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


def test_plugin_binding_contribution_deep_freezes_data() -> None:
    contribution = PluginContribution(
        bindings=(
            CapabilityBindingContribution(
                capability_id="toy.flow.run",
                target_capability_id="toy.runtime.execute",
                data={"steps": ["one", "two"]},
            ),
        )
    )

    binding = contribution.bindings[0]
    assert binding.data == {"steps": ("one", "two")}
    steps = binding.data["steps"]  # type: ignore[index]
    with pytest.raises(TypeError):
        dict.__setitem__(binding.data, "other", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        list.append(steps, "three")  # type: ignore[arg-type]
    assert binding.model_dump(mode="json")["data"] == {"steps": ["one", "two"]}


def test_plugin_binding_contribution_validates_secret_handles() -> None:
    with pytest.raises(ValidationError, match="unique"):
        CapabilityBindingContribution(
            capability_id="toy.flow.run",
            target_capability_id="toy.runtime.execute",
            secret_handles=("dup.handle", "dup.handle"),
        )
    binding = CapabilityBindingContribution(
        capability_id="toy.flow.run",
        target_capability_id="toy.runtime.execute",
        secret_handles=("beta.token", "alpha.token"),
    )
    assert binding.secret_handles == ("alpha.token", "beta.token")


def test_binding_contribution_accepts_qualified_contract_id() -> None:
    binding = CapabilityBindingContribution(
        capability_id="toy.product.agent.worker.execute",
        target_capability_id="toy.runtime.execute",
        contract_id="toy.feature.agent.worker.v1",
    )
    assert binding.contract_id == "toy.feature.agent.worker.v1"


def test_binding_contribution_rejects_invalid_contract_id() -> None:
    with pytest.raises(ValidationError, match="contract id"):
        CapabilityBindingContribution(
            capability_id="toy.product.agent.worker.execute",
            target_capability_id="toy.runtime.execute",
            contract_id="NotAQualifiedId",
        )


def test_binding_contribution_defaults_contract_id_to_none() -> None:
    binding = CapabilityBindingContribution(
        capability_id="toy.flow.run",
        target_capability_id="toy.runtime.execute",
    )
    assert binding.contract_id is None


def test_plugin_provider_exposes_only_the_phase_two_execution_methods() -> None:
    assert "contribute" in PluginProvider.__dict__
    assert "bind" not in PluginProvider.__dict__
    assert "execute" in TaskHandler.__dict__
    assert "__call__" not in TaskHandler.__dict__


def test_phase_one_registry_spi_has_no_public_aliases() -> None:
    retired = (
        "CapabilityRegistry",
        "CapabilityRegistryError",
        "EnginePorts",
        "PluginRuntime",
        "assemble_registry",
    )
    assert all(not hasattr(plugin_api, name) for name in retired)
    assert all(not hasattr(graph_engine, name) for name in retired)


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
        lambda: PluginDescriptor(
            schema_version="1",
            source=None,
            plugin_id="toy.runtime",
            plugin_version="not-a-version",
            engine_api="1.0",
            task_handlers=(),
            commit_validators=(),
        ),
        lambda: RegistryPorts("not-a-version-or-specifier"),
        lambda: PluginDependency("toy.runtime", "not-a-specifier"),
    ],
)
def test_plugin_contracts_reject_invalid_versions_and_specifiers(factory: object) -> None:
    with pytest.raises((PluginContractError, ValidationError)):
        factory()  # type: ignore[operator]


def test_validate_contribution_rejects_duplicate_declared_ids() -> None:
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=">=0.2,<0.3",
        task_handlers=(),
        commit_validators=(),
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
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=">=0.2,<0.3",
        task_handlers=("toy.runtime.shared",),
        commit_validators=(),
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
        schema_version="1",
        source=None,
        plugin_id="toy.runtime",
        plugin_version="1.0.0",
        engine_api=">=0.2,<0.3",
        task_handlers=("toy.runtime.run",),
        commit_validators=("toy.runtime.validate",),
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


def test_task_request_is_frozen_forbids_extra_and_enforces_attempts() -> None:
    request = TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        capability_id="toy.runtime.run",
        invocation=_TEST_INVOCATION,
        attempt=1,
        input={"items": [1, None]},
    )
    with pytest.raises(ValidationError, match="frozen"):
        request.attempt = 2
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        TaskRequest.model_validate({**request.model_dump(), "attempt": 0})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskFailure.model_validate({"kind": "internal", "message": "bad", "code": 500})


def test_task_context_and_candidate_contracts_remain_frozen() -> None:
    context = TaskContext(
        project_root=Path("/project"),
        write_root=Path("/attempts/task-1/attempt-1"),
        workspace_identity=_workspace_identity(),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=_TEST_INVOCATION,
    )
    with pytest.raises(AttributeError):
        context.write_root = Path("/elsewhere")

    candidate = CandidateWriteSet(
        baseline_tree_id="base",
        candidate_tree_id="candidate",
        files=(CandidateFile(path="src/a.py", before_sha256=None, after_sha256="abc"),),
    )
    validation = ValidationContext(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="node-1",
        resources=ResourceClaims(reads=("src",)),
    )
    assert candidate.model_dump(mode="json")["files"] == [
        {"path": "src/a.py", "before_sha256": None, "after_sha256": "abc"}
    ]
    assert validation.resources.reads == ("src",)


@pytest.mark.parametrize("prefix", ("", "/root", "../secret", "src/../secret", "C:\\secret"))
def test_resource_claims_reject_unsafe_relative_prefixes(prefix: str) -> None:
    with pytest.raises(ValidationError, match="relative resource prefix"):
        ResourceClaims(writes=(prefix,))


@pytest.mark.parametrize(
    "fields",
    (
        {"accepted": False},
        {"accepted": False, "reason": ""},
        {"accepted": True, "reason": "not actually accepted"},
    ),
)
def test_validation_result_requires_reason_exactly_for_rejection(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="reason"):
        ValidationResult.model_validate(fields)


def _empty_descriptor(plugin_id: str = "toy.runtime") -> PluginDescriptor:
    return PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=plugin_id,
        plugin_version="1.0.0",
        engine_api=">=0.2,<0.3",
        task_handlers=(),
        commit_validators=(),
    )


def test_descriptor_and_realized_attempt_contracts_must_match_ids_and_digests() -> None:
    contract = AttemptContractRef(contract_id="toy.runtime.attempt.v1", digest="a" * 64)
    authority = ContributionAuthority(
        provider_binding=object(),
        descriptor=_empty_descriptor(),
        owner_id="toy.runtime",
        source_key=SourceKey(SourceRole.PLUGIN, "toy.runtime"),
        source_digest="b" * 64,
        contribution=PluginContribution.empty(),
        authorities=(),
        attempt_contracts=(contract,),
    )
    descriptor_projection = ContributionProjection.from_authority(authority)
    realized_projection = ContributionProjection.from_registry_owner(
        RegistrySet(
            sources=SourceRegistry(entries={}),
            capabilities=CapabilityRegistry.empty(),
            schemas=SchemaRegistry(entries={}),
            resources=ResourceRegistry(entries={}),
            effects=EffectRegistry(entries={}),
        ),
        authority,
    )
    assert descriptor_projection.attempt_contracts == (contract,)
    assert realized_projection.attempt_contracts == descriptor_projection.attempt_contracts
    mismatched = AttemptContractRef(contract_id="toy.runtime.attempt.v1", digest="c" * 64)
    assert descriptor_projection.attempt_contracts != (mismatched,)


def test_configuration_tree_contributions_cannot_declare_attempt_contracts() -> None:
    contract = AttemptContractRef(contract_id="toy.config.attempt.v1", digest="a" * 64)
    with pytest.raises(ValueError, match="attempt contract"):
        ContributionAuthority(
            provider_binding=None,
            descriptor=_empty_descriptor("toy.config"),
            owner_id="toy.config",
            source_key=SourceKey(SourceRole.CONFIG, "toy.config"),
            source_digest="b" * 64,
            contribution=PluginContribution.empty(),
            authorities=(),
            attempt_contracts=(contract,),
        )
    with pytest.raises(ValidationError, match="attempt contract"):
        ContributionProjection(
            owner_id="toy.config",
            source_key=ContributionSourceKeyProjection(role=SourceRole.CONFIG, owner_id="toy.config"),
            source_digest="b" * 64,
            task_handlers=(),
            commit_validators=(),
            schemas=(),
            resources=(),
            effects=(),
            bindings=(),
            attempt_contracts=(contract,),
        )
