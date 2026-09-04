from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.composition import ProductFileSource, load_product_file
from graph_engine.frozen_json import thaw_json
from graph_engine.effects.state import EffectCallContext
from graph_engine.plugin_api import (
    CandidateFile,
    EffectApplyResult,
    PathWriteSet,
    EffectIntent,
    EffectReconcileResult,
    PluginContribution,
    PluginDescriptor,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    ValidationContext,
    ValidationResult,
)
from graph_engine_toy_a.plugin import ToyAPlugin

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentWorkspaceV1,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from tests.phase4.agent_harness import AgentSkillHarness, FakeAgentAdapter
from tests.phase4.conformance import (
    PluginExpectation,
    assert_effect_idempotent,
    assert_plugin_conforms,
    assert_validator_rejects,
    execute_task,
)
from tests.phase4.wheel_isolation import ALLOWED_PACKAGES, isolate_package

MINIMAL_PRODUCT = Path(__file__).resolve().parent / "fixtures" / "minimal-product.yaml"
_SHA = "a" * 64


def _agent_workspace(
    *,
    write_root: str = "qa/changes/CH-1/.staging/attempt-1",
    allowed_outputs: tuple[str, ...] = ("qa/changes/CH-1/proposal.md",),
    agent_profile: str = "assurance-v1-doc-author",
) -> AgentWorkspaceV1:
    payload = {
        "schema_version": "1",
        "agent_profile": agent_profile,
        "scope_id": "CH-1",
        "write_root": write_root,
        "allowed_outputs": list(allowed_outputs),
    }
    return AgentWorkspaceV1.model_validate({**payload, "identity_digest": canonical_digest(payload)})


class _ProviderWithUndeclaredResource:
    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=None,
            plugin_id="test.bad",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
        )

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        assert ports.engine_api == ENGINE_API_VERSION
        return PluginContribution(
            resources=(
                ResourceContribution(
                    resource_id="test.bad.undeclared",
                    media_type="text/plain",
                    content=b"secret",
                ),
            )
        )


class _NonCanonicalSchemaProvider:
    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=None,
            plugin_id="test.bad",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            schemas=("test.bad.schema.v1",),
        )

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        assert ports.engine_api == ENGINE_API_VERSION
        return PluginContribution(
            schemas=(
                SchemaContribution(
                    schema_id="test.bad.schema.v1",
                    media_type="application/schema+json",
                    content=b'{\n  "type": "object"\n}',
                ),
            )
        )


class _MixedKindSchemaAndPersonaProvider:
    """Prefix-correct Phase 4 schema + persona IDs; each kind is already sorted."""

    _SCHEMA_ID = "assurance.intake.schema.case.v1"
    _PERSONA_ID = "assurance.intake.persona.intake-host.v1"

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=None,
            plugin_id="assurance.intake",
            plugin_version="0.1.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=(),
            commit_validators=(),
            schemas=(self._SCHEMA_ID,),
            resources=(self._PERSONA_ID,),
        )

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        assert ports.engine_api == ENGINE_API_VERSION
        return PluginContribution(
            schemas=(
                SchemaContribution(
                    schema_id=self._SCHEMA_ID,
                    media_type="application/schema+json",
                    content=canonical_json_bytes({"type": "object"}),
                ),
            ),
            resources=(
                ResourceContribution(
                    resource_id=self._PERSONA_ID,
                    media_type="text/plain",
                    content=b"intake host",
                ),
            ),
        )


class _EchoHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.write_root / "echo.txt").write_bytes(b"echo\n")
        return TaskOutcome.succeeded(request.input)


class _PrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        schema: dict[str, JSONValue] = {"additionalProperties": False, "type": "object"}
        binding = cast(Mapping[str, JSONValue], thaw_json(request.binding_data))
        agent_request = AgentRunRequest(
            instructions=(InstructionPart.text("text/plain", "prepare the task"),),
            result_contract=ResultContract(
                schema_id="test.phase4.result.v1",
                schema_digest=canonical_digest(schema),
                delivery_mode="assistant_json_local_v1",
            ),
            execution=FrozenExecutionSelection.model_validate(binding["execution"]),
            workspace=_agent_workspace(),
            request_policy_digest=str(binding["request_policy_digest"]),
            request_config_digest=str(binding["request_config_digest"]),
        )
        return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


class _FinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        payload = cast(Mapping[str, JSONValue], thaw_json(request.input))
        result = cast(Mapping[str, JSONValue], payload["agent_result"])
        return TaskOutcome.succeeded(result["result_payload"])


class _RejectingValidator:
    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del staged, context
        return ValidationResult(accepted=False, reason="test.bad.reason")


class _AcceptingValidator:
    def validate(self, staged: PathWriteSet, context: ValidationContext) -> ValidationResult:
        del staged, context
        return ValidationResult(accepted=True)


class _IdempotentEffect:
    def __init__(self) -> None:
        self._receipts: dict[str, JSONValue] = {}

    async def apply(self, intent: EffectIntent, context: EffectCallContext) -> EffectApplyResult:
        key = context.settlement_key
        receipt = self._receipts.setdefault(key, {"kind": intent.kind, "key": key})
        return EffectApplyResult.applied(receipt)

    async def reconcile(self, intent: EffectIntent, context: EffectCallContext) -> EffectReconcileResult:
        del intent
        return EffectReconcileResult.applied(self._receipts[context.settlement_key])


def test_plugin_conformance_accepts_toy_a() -> None:
    assert_plugin_conforms(
        ToyAPlugin(),
        PluginExpectation(plugin_id="toy.a", dependencies=(), id_prefix="toy.a."),
    )


def test_plugin_conformance_rejects_undeclared_resource() -> None:
    with pytest.raises(AssertionError, match="descriptor/contribution mismatch"):
        assert_plugin_conforms(
            _ProviderWithUndeclaredResource(),
            PluginExpectation(plugin_id="test.bad", dependencies=(), id_prefix="test.bad."),
        )


def test_plugin_conformance_rejects_non_canonical_schema_bytes() -> None:
    with pytest.raises(AssertionError, match="canonical"):
        assert_plugin_conforms(
            _NonCanonicalSchemaProvider(),
            PluginExpectation(plugin_id="test.bad", dependencies=(), id_prefix="test.bad."),
        )


def test_plugin_conformance_accepts_mixed_kind_schema_and_persona() -> None:
    assert_plugin_conforms(
        _MixedKindSchemaAndPersonaProvider(),
        PluginExpectation(
            plugin_id="assurance.intake",
            dependencies=(),
            id_prefix="assurance.intake.",
        ),
    )


@pytest.mark.asyncio
async def test_execute_task_returns_outcome_and_workspace_bytes(tmp_path: Path) -> None:
    executed = await execute_task(_EchoHandler(), {"value": 1}, tmp_path)
    assert executed.status == "succeeded"
    assert executed.output == {"value": 1}
    assert executed.workspace_bytes["echo.txt"] == b"echo\n"
    assert (tmp_path / "echo.txt").read_bytes() == b"echo\n"


@pytest.mark.asyncio
async def test_agent_skill_harness_finalizes_fake_adapter_result() -> None:
    binding = {
        "agent_profile": "aa-doc-author",
        "execution": {
            "provider_model": "test-model",
            "worker_profile": "worker",
            "permission_profile_digest": _SHA,
            "limits": {"max_seconds": 5},
        },
        "request_policy_digest": _SHA,
        "request_config_digest": _SHA,
    }
    structured = {"ok": True, "leaf": "entities.item"}
    outcome = await AgentSkillHarness(_PrepareHandler(), _FinalizeHandler()).run(
        {"case": "CH-1"},
        binding,
        structured,
    )
    assert outcome.status == "succeeded"
    assert outcome.output == structured


def test_fake_adapter_records_canonical_request_bytes_and_digest() -> None:
    schema: dict[str, JSONValue] = {"type": "object"}
    request = AgentRunRequest(
        instructions=(InstructionPart.text("text/plain", "run the skill"),),
        result_contract=ResultContract(
            schema_id="test.phase4.result.v1",
            schema_digest=canonical_digest(schema),
            delivery_mode="assistant_json_local_v1",
        ),
        execution=FrozenExecutionSelection.model_validate(
            {
                "provider_model": "test-model",
                "worker_profile": "worker",
                "permission_profile_digest": _SHA,
                "limits": {"max_seconds": 5},
            }
        ),
        workspace=_agent_workspace(),
        request_policy_digest=_SHA,
        request_config_digest=_SHA,
    )
    structured: dict[str, JSONValue] = {"ok": True}
    adapter = FakeAgentAdapter(structured, adapter_id="test.fake.opencode")
    result = adapter.execute_request(request)
    assert adapter.recorded_request_bytes == request.canonical_bytes()
    assert result.result_digest == canonical_digest(structured)
    assert result.adapter_id == "test.fake.opencode"
    assert result.evidence_digest == FakeAgentAdapter.EVIDENCE_DIGEST


def test_assert_validator_rejects_requires_exact_reason() -> None:
    assert_validator_rejects(
        _RejectingValidator(),
        reason="test.bad.reason",
        files=(CandidateFile(path="qa/cases/x.yaml", before_sha256=None, after_sha256=_SHA),),
    )
    with pytest.raises(AssertionError):
        assert_validator_rejects(_RejectingValidator(), reason="other.reason")
    with pytest.raises(AssertionError):
        assert_validator_rejects(_AcceptingValidator(), reason="test.bad.reason")


@pytest.mark.asyncio
async def test_assert_effect_idempotent_requires_equal_receipts() -> None:
    await assert_effect_idempotent(
        _IdempotentEffect(),
        EffectIntent(kind="test.phase4.effect.v1", payload={"n": 1}),
        "key-1",
    )


def test_minimal_product_fixture_is_closed_declarative_product() -> None:
    product = load_product_file(ProductFileSource(path=MINIMAL_PRODUCT))
    assert product.manifest.product_id == "toy.a"
    assert product.manifest.engine_api == ENGINE_API_VERSION
    assert tuple(item.plugin_id for item in product.manifest.plugins) == ("toy.a",)


def test_wheel_isolation_rejects_unknown_package() -> None:
    assert "graph-engine-toy-a" in ALLOWED_PACKAGES
    with pytest.raises(ValueError, match="closed"):
        isolate_package("evil-package", "toy-a")
