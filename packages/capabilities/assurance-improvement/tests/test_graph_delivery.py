from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from assurance_improvement.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS,
    close_improvement_task,
    select_evaluate_memory,
)
from assurance_improvement.contracts.delivery import MemoryEvalReceipt, artifact_digest
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1, ImprovementEffectReceiptV1
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.effects.delivery import ImprovementDeliveryEffect
from graph_engine.effects.state import EffectCallContext, MemoryEffectState
from assurance_improvement.graphs.factory import build_improvement_graphs as _build_improvement_graphs
from assurance_improvement.contracts.delivery import evaluate_apply_route
from assurance_improvement.contracts.review import (
    AUTO_REVIEW_FAILED_STATES,
    HUMAN_REVIEW_FAILED_STATES,
    auto_review_route,
    human_review_route,
)
from assurance_improvement.operations.keys import delivery_effect_key
from assurance_improvement.resource_loader import resource_bytes
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import resolve_contract
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.attempts.resolutions import ReceiptRef
from pydantic import BaseModel, ValidationError
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.effects.contracts import GRAPH_NAMES_NOT_EFFECT_KINDS
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import EffectPolicy, ResourceClaims
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.flow import BoundFlow
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import ScriptedAttempt, _prepare_anchored_backend
from graph_engine.testing.recording_build_context import RecordingCapabilityBuildContext

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    IMPROVEMENT_ID,
    improvement_projection,
)
from test_improvement_graph_factory import (  # type: ignore[import-not-found]
    EFFECT_IDS,
    TASK_APPLY_ID,
    TASK_AUTO_REVIEW_ID,
    TASK_EVALUATE_ID,
    TASK_EXPORT_ID,
    TASK_ROLLBACK_ID,
    _REVIEW_ID,
    _receipt,
    improvement_contracts,
    skill_graph_fields,
)

from graph_engine.testing.feature_bundle import compile_bundle


def build_improvement_graphs(*args, **kwargs):
    return compile_bundle(_build_improvement_graphs(*args, **kwargs))


def test_publish_receipt_steps_are_the_receipt_refs_publishers() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = _build_improvement_graphs(context)

    def steps(child: object) -> tuple[str, ...]:
        assert isinstance(child, BoundFlow)
        return tuple(child.flow.public_receipt_steps)

    assert {
        "archive": steps(bundle.archive),
        "evaluate": steps(bundle.evaluate),
        "export": steps(bundle.export),
        "rollback": steps(bundle.rollback),
        "apply": steps(bundle.apply),
        "review": steps(bundle.review),
    } == {
        "archive": ("archive",),
        "evaluate": ("evaluate",),
        "export": ("export",),
        "rollback": ("rollback",),
        "apply": ("apply",),
        "review": (),
    }


class _ApplyChannels(CheckpointBridgeState, total=False):
    change_id: str
    retro_id: str
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    artifact_paths: list[str]
    owned_evidence_ids: list[str]
    source_manifest: dict[str, object]
    projection: dict[str, object]
    assessment: dict[str, object]
    current: dict[str, object]
    review_id: str
    eval_run_id: str
    outcome: str
    report_sha256: str
    staged_sha256: str
    baseline_sha256: str
    target_digest: str
    approved_state_digest: str
    approved_version: int
    before_sha256: str
    after_sha256: str
    receipt_sha256: str
    lifecycle_state: str
    human_action: str
    status: str
    memory_eval: dict[str, object]
    target: dict[str, object]
    receipt_refs: list[dict[str, str]]
    effect_refs: list[dict[str, str]]
    attempt_failure: dict[str, object]


_EVALUATE_HANDLER = "assurance.improvement.evaluate-memory-improvement"

_DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"


def _assessment(*, decision: str = "pass") -> dict[str, object]:
    return {
        "schema_version": "1",
        "review_type": "improvement",
        "decision": decision,
        "findings": [],
        "evidence_traceability": "complete",
        "scope_readiness": "ready",
        "verification_readiness": "ready",
        "delivery_safety": "ready",
        "human_review_required": decision == "needs_human_review",
        "review_id": "REV-1",
        "improvement_id": IMPROVEMENT_ID,
        "expected_improvement_version": 1,
        "subject_sha256": f"sha256:{HEX_A}",
    }


def complete_evaluate_payload() -> dict[str, object]:
    return {
        "projection": improvement_projection(),
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "target_digest": HEX_A,
    }


def evaluate_receipt(**overrides: object) -> dict[str, object]:
    projection = ImprovementProjection.model_validate(improvement_projection())
    payload: dict[str, object] = {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "approved_state_digest": artifact_digest(projection),
        "approved_version": projection.version,
    }
    payload.update(overrides)
    payload["route"] = evaluate_apply_route(payload.get("outcome"))
    payload["memory_eval"] = {
        key: payload[key]
        for key in (
            "eval_run_id",
            "outcome",
            "report_sha256",
            "staged_sha256",
            "baseline_sha256",
            "approved_state_digest",
            "approved_version",
        )
        if key in payload
    }
    payload.setdefault("effect_refs", [])
    return payload


def apply_graph_input(**overrides: object) -> dict[str, object]:
    projection = improvement_projection(state="proposed")
    approved = ImprovementProjection.model_validate(improvement_projection())
    payload: dict[str, object] = {
        **skill_graph_fields(),
        **complete_evaluate_payload(),
        "projection": projection,
        "assessment": _assessment(),
        "current": projection,
        "review_id": "REV-1",
        "approved_state_digest": artifact_digest(approved),
        "approved_version": 1,
        "before_sha256": "b",
        "after_sha256": "a",
        "receipt_sha256": "r",
        "lifecycle_state": "proposed",
    }
    payload.update(overrides)
    return payload


def auto_review_output(*, lifecycle_state: str) -> dict[str, object]:
    return {
        "status": {
            "schema_version": "1",
            "review_id": "REV-1",
            "improvement_id": IMPROVEMENT_ID,
            "result": "approved" if lifecycle_state == "approved" else "escalated",
        },
        "projection": improvement_projection(state=lifecycle_state),
        "route": auto_review_route(lifecycle_state),
    }


def human_review_output(*, lifecycle_state: str) -> dict[str, object]:
    return {
        "projection": improvement_projection(state=lifecycle_state, version=2),
        "route": human_review_route(lifecycle_state),
    }


def apply_receipt() -> dict[str, object]:
    return {
        "target": ".aa/memory/aa-api-plan.md",
        "before_sha256": "b",
        "after_sha256": "a",
        "receipt_sha256": "r",
    }


def export_receipt() -> dict[str, object]:
    return {
        "sha256": "e",
        "created": True,
        "artifact_path": "qa/results/export/change.json",
        "effect_refs": [],
    }


def rollback_receipt() -> dict[str, object]:
    return {
        "target": ".aa/memory/aa-api-plan.md",
        "restored_sha256": "x",
        "reason": "regressed",
        "lifecycle_state": "rolled_back",
        "effect_refs": [],
    }


def review_agent_output(*, decision: str = "pass") -> dict[str, object]:
    return {
        "schema_version": "1",
        "review_type": "improvement",
        "decision": decision,
        "findings": [],
        "evidence_traceability": "complete",
        "scope_readiness": "ready",
        "verification_readiness": "ready",
        "delivery_safety": "ready",
        "human_review_required": decision == "needs_human_review",
        "lifecycle_state": None,
        "evidence_refs": [],
    }


def export_graph_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **skill_graph_fields(),
        "projection": improvement_projection(),
        "artifact_path": "qa/results/export/change.json",
        "sha256": "e",
        "created": True,
        "target_digest": HEX_A,
    }
    payload.update(overrides)
    return payload


def rollback_graph_input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **skill_graph_fields(),
        "projection": improvement_projection(state="applied"),
        "reason": "regressed",
        "restored_sha256": "x",
        "target_digest": HEX_A,
    }
    payload.update(overrides)
    return payload


def test_graph_names_are_not_effect_kinds() -> None:
    assert GRAPH_NAMES_NOT_EFFECT_KINDS.isdisjoint(EFFECT_IDS)
    for name in ("archive", "retro", "review", "evaluate", "export", "apply", "rollback"):
        assert name not in EFFECT_IDS
        assert f"improvement-{name}" not in EFFECT_IDS
    assert "memory_eval" not in EFFECT_IDS


def test_registered_receipt_traces_use_only_the_three_improvement_effects() -> None:
    from graph_engine import RegistryPorts
    from graph_engine import ENGINE_API_VERSION

    from assurance_improvement.plugin import ImprovementPlugin

    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    kinds = tuple(sorted(item.kind for item in contribution.effects))
    assert kinds == tuple(sorted(EFFECT_IDS))


def test_auto_review_and_evaluate_routes_are_exclusive() -> None:
    assert auto_review_route("approved") == "apply-evaluate"
    assert auto_review_route("needs_rework") == "rework"
    assert auto_review_route("rejected") == "rejected"
    assert auto_review_route("proposed") == "human-review"
    assert auto_review_route("unknown") == "failed"
    assert {auto_review_route(state) for state in AUTO_REVIEW_FAILED_STATES} == {"failed"}
    assert human_review_route("approved") == "apply-evaluate"
    assert human_review_route("rejected") == "rejected"
    assert human_review_route("needs_rework") == "rework"
    assert human_review_route("superseded") == "superseded"
    assert human_review_route("proposed") == "failed"
    assert {human_review_route(state) for state in HUMAN_REVIEW_FAILED_STATES} == {"failed"}
    assert evaluate_apply_route("passed") == "apply"
    assert evaluate_apply_route("regressed") == "failed"


@pytest.mark.parametrize(
    ("lifecycle_state", "terminal"),
    [
        ("needs_rework", "rework"),
        ("rejected", "rejected"),
    ],
)
async def test_apply_auto_review_routes_typed_lifecycle(lifecycle_state: str, terminal: str) -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    result = await harness.run(
        bundle.apply,
        input=apply_graph_input(),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state=lifecycle_state), _receipt())
            ]
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == ["improvement.apply-auto-review"]
    assert [call.contract_id for call in result.semantic_calls] == [TASK_AUTO_REVIEW_ID]
    terminal_state = result.terminal
    assert isinstance(terminal_state, dict)
    assert terminal_state.get("status", terminal) == terminal


@pytest.mark.parametrize(
    "payload",
    [
        {"result": "approved"},
        auto_review_output(lifecycle_state="unknown"),
        {"projection": improvement_projection(state="approved")},
    ],
)
def test_auto_review_output_model_rejects_malformed_output(payload: dict[str, object]) -> None:
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS

    contract = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-improvement-auto-review"]
    with pytest.raises(ValidationError):
        contract.output_model.model_validate(payload)


async def test_apply_covers_auto_review_evaluate_and_apply() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    applied = apply_receipt()
    result = await harness.run(
        bundle.apply,
        input=apply_graph_input(),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state="approved"), receipt)
            ],
            "improvement.apply-evaluate": [committed(evaluate_receipt(), receipt)],
            "improvement.apply": [committed(applied, receipt)],
        },
    )
    assert [call.semantic_node_id for call in result.semantic_calls] == [
        "improvement.apply-auto-review",
        "improvement.apply-evaluate",
        "improvement.apply",
    ]
    assert [call.contract_id for call in result.semantic_calls] == [
        TASK_AUTO_REVIEW_ID,
        TASK_EVALUATE_ID,
        TASK_APPLY_ID,
    ]
    evaluate_selected = result.select_values[1]
    assert isinstance(evaluate_selected, dict)
    assert evaluate_selected["eval_run_id"] == "eval-1"
    assert applied["target"] == ".aa/memory/aa-api-plan.md"
    assert isinstance(result.terminal, dict)
    assert result.terminal["status"] == "done"


def _apply_resume_config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "improvement-apply",
        }
    }


def _interrupt_value(result: object) -> object | None:
    if isinstance(result, GraphInterrupt):
        interrupts = getattr(result, "args", ())
        if interrupts:
            first = interrupts[0]
            if isinstance(first, tuple) and first:
                return getattr(first[0], "value", first[0])
            return getattr(first, "value", first)
        return None
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)


async def _resume_apply_human_review(action: str) -> tuple[dict[str, object], tuple[str, ...]]:
    lifecycle = {
        "approve": "approved",
        "reject": "rejected",
        "request_rework": "needs_rework",
        "supersede": "superseded",
    }[action]
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_improvement_graphs(
        harness.recording_context(owner_id="assurance.improvement", contracts=improvement_contracts())
    )
    harness._kernel.load_script(
        {
            "improvement.apply-auto-review": [
                committed(
                    auto_review_output(lifecycle_state="proposed"),
                    ReceiptRef(receipt_id="receipt-1", receipt_digest="a" * 64),
                )
            ],
            "improvement.apply-human-review": [
                committed(
                    human_review_output(lifecycle_state=lifecycle),
                    ReceiptRef(receipt_id="receipt-2", receipt_digest="a" * 64),
                )
            ],
            "improvement.apply-evaluate": [
                committed(evaluate_receipt(), ReceiptRef(receipt_id="receipt-3", receipt_digest="a" * 64))
            ],
            "improvement.apply": [
                committed(apply_receipt(), ReceiptRef(receipt_id="receipt-4", receipt_digest="a" * 64))
            ],
        }
    )

    wrapper: StateGraph[_ApplyChannels] = StateGraph(_ApplyChannels)
    wrapper.add_node("apply", cast(Any, bundle.apply))
    wrapper.add_edge(START, "apply")
    wrapper.add_edge("apply", END)
    graph = wrapper.compile(checkpointer=backend)
    config = _apply_resume_config()
    interrupted: object | None
    try:
        interrupted = await graph.ainvoke(cast(Any, apply_graph_input()), config=config)
    except GraphInterrupt as error:
        interrupted = error
    assert _interrupt_value(interrupted) is not None
    resumed = await graph.ainvoke(Command(resume={"action": action}), config=config)
    assert isinstance(resumed, dict)
    assert _interrupt_value(resumed) is None
    return resumed, tuple(call.semantic_node_id for call in harness._kernel.semantic_calls)


@pytest.mark.parametrize(
    ("action", "terminal", "lifecycle_state"),
    [
        ("reject", "rejected", "rejected"),
        ("request_rework", "rework", "needs_rework"),
        ("supersede", "superseded", "superseded"),
    ],
)
async def test_apply_human_review_resume_rejects_rework_and_supersede(
    action: str, terminal: str, lifecycle_state: str
) -> None:
    resumed, calls = await _resume_apply_human_review(action)
    assert resumed["status"] == terminal
    assert calls == ("improvement.apply-auto-review", "improvement.apply-human-review")
    assert "improvement.apply-evaluate" not in calls
    assert "improvement.apply" not in calls


async def test_apply_human_review_approve_continues_to_evaluate_and_apply() -> None:
    resumed, calls = await _resume_apply_human_review("approve")
    assert calls == (
        "improvement.apply-auto-review",
        "improvement.apply-human-review",
        "improvement.apply-evaluate",
        "improvement.apply",
    )
    assert resumed["status"] == "done"


async def test_standalone_evaluate_and_apply_evaluate_are_effectful_memory_attempts() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    selected = select_evaluate_memory(complete_evaluate_payload())
    assert selected.eval_run_id == "eval-1"
    evaluated = evaluate_receipt()
    standalone = await harness.run(
        bundle.evaluate,
        input={**skill_graph_fields(), **complete_evaluate_payload()},
        script={"improvement.evaluate": [committed(evaluated, receipt)]},
    )
    assert [call.semantic_node_id for call in standalone.semantic_calls] == ["improvement.evaluate"]
    assert [call.contract_id for call in standalone.semantic_calls] == [TASK_EVALUATE_ID]
    standalone_selected = standalone.select_values[0]
    assert isinstance(standalone_selected, dict)
    assert standalone_selected["eval_run_id"] == "eval-1"
    assert standalone_selected["outcome"] == "passed"
    published = standalone.published_update
    assert published is not None
    receipt_model = MemoryEvalReceipt.model_validate(evaluated["memory_eval"])
    assert receipt_model.outcome == "passed"
    refs = evaluated.get("effect_refs") or []
    if isinstance(refs, list):
        assert not any(
            isinstance(item, dict) and item.get("kind") in GRAPH_NAMES_NOT_EFFECT_KINDS for item in refs
        )
        assert not any(
            isinstance(item, dict)
            and str(item.get("kind", "")).startswith("assurance.improvement.")
            and item.get("kind") not in EFFECT_IDS
            for item in refs
        )

    apply_result = await harness.run(
        bundle.apply,
        input=apply_graph_input(),
        script={
            "improvement.apply-auto-review": [
                committed(auto_review_output(lifecycle_state="approved"), receipt)
            ],
            "improvement.apply-evaluate": [committed(evaluate_receipt(), receipt)],
            "improvement.apply": [committed(apply_receipt(), receipt)],
        },
    )
    evaluate_calls = [call for call in apply_result.semantic_calls if call.contract_id == TASK_EVALUATE_ID]
    assert len(evaluate_calls) == 1
    assert evaluate_calls[0].semantic_node_id == "improvement.apply-evaluate"
    apply_eval_selected = apply_result.select_values[1]
    assert isinstance(apply_eval_selected, dict)
    assert apply_eval_selected["eval_run_id"] == "eval-1"
    assert apply_eval_selected.get("outcome") == "passed"


async def test_review_export_and_rollback_route_on_typed_results() -> None:
    harness = GraphHarness()
    context = harness.recording_context(
        owner_id="assurance.improvement",
        contracts=improvement_contracts(),
    )
    bundle = build_improvement_graphs(context)
    receipt = _receipt()
    reviewed = review_agent_output()
    review = await harness.run(
        bundle.review,
        input=skill_graph_fields(),
        script={"improvement.review": [committed(reviewed, receipt)]},
    )
    assert [call.semantic_node_id for call in review.semantic_calls] == ["improvement.review"]
    assert [call.contract_id for call in review.semantic_calls] == [_REVIEW_ID]
    assert reviewed["decision"] == "pass"
    assert isinstance(review.terminal, dict)
    assert review.terminal["status"] == "done"

    exported_output = export_receipt()
    exported = await harness.run(
        bundle.export,
        input=export_graph_input(),
        script={"improvement.export": [committed(exported_output, receipt)]},
    )
    assert [call.semantic_node_id for call in exported.semantic_calls] == ["improvement.export"]
    assert [call.contract_id for call in exported.semantic_calls] == [TASK_EXPORT_ID]
    assert isinstance(exported_output["effect_refs"], list)
    assert cast(dict[str, object], exported.terminal)["status"] == "done"

    rolled_output = rollback_receipt()
    rolled = await harness.run(
        bundle.rollback,
        input=rollback_graph_input(),
        script={"improvement.rollback": [committed(rolled_output, receipt)]},
    )
    assert [call.semantic_node_id for call in rolled.semantic_calls] == ["improvement.rollback"]
    assert [call.contract_id for call in rolled.semantic_calls] == [TASK_ROLLBACK_ID]
    assert rolled_output["lifecycle_state"] == "rolled_back"
    assert cast(dict[str, object], rolled.terminal)["status"] == "done"


class _RecordingWorkspace:
    def __init__(self, inner: TaskWorkspaceProvider) -> None:
        self.inner = inner
        self.promotions = 0

    async def open_or_create(
        self, attempt_key: object, claims: ResourceClaims, *, seed_from: object | None = None
    ) -> object:
        return await self.inner.open_or_create(attempt_key, claims, seed_from=seed_from)  # type: ignore[arg-type]

    async def seal(self, binding: object) -> object:
        return await self.inner.seal(binding)  # type: ignore[arg-type]

    async def prepare(self, binding: object, sealed: object) -> object:
        return await self.inner.prepare(binding, sealed)  # type: ignore[arg-type]

    async def promote(self, prepared: object) -> object:
        self.promotions += 1
        return await self.inner.promote(prepared)  # type: ignore[arg-type]

    async def recover_promotion(self, prepared: object) -> object:
        self.promotions += 1
        return await self.inner.recover_promotion(prepared)  # type: ignore[arg-type]


class _ObservedDeliveryEffect:
    def __init__(self, inner: ImprovementDeliveryEffect) -> None:
        self.inner = inner
        self.apply_calls = 0

    async def apply(self, intent: object, context: EffectCallContext) -> object:
        self.apply_calls += 1
        return await self.inner.apply(intent, context)  # type: ignore[arg-type]

    async def reconcile(self, intent: object, context: EffectCallContext) -> object:
        return await self.inner.reconcile(intent, context)  # type: ignore[arg-type]


class CountingEffectState(MemoryEffectState):
    def __init__(self) -> None:
        super().__init__()
        self.delivery_count = 0

    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ):
        result = await super().commit(
            effect_kind=effect_kind,
            settlement_key=settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            payload=payload,
            receipt=receipt,
            fencing_token=fencing_token,
        )
        self.delivery_count += 1
        return result


class _HybridAttemptKernel:
    def __init__(self, real: AssuranceAttemptKernel, scripted: ScriptedAttempt) -> None:
        self._real = real
        self._scripted = scripted
        self.traces: dict[str, list[str]] = {}
        self.keys: dict[str, AttemptKey] = {}

    async def execute_or_recover(
        self,
        attempt_key: AttemptKey,
        contract: Any,
        validated: BaseModel,
        context: AttemptExecutionContext,
    ) -> object:
        node = str(context.semantic_node_id)
        if node in {"improvement.evaluate", "improvement.apply-evaluate"}:
            trace: list[str] = []
            result = await self._real.execute_or_recover(
                attempt_key, contract, validated, context, trace=trace
            )
            self.traces[node] = list(trace)
            self.keys[node] = attempt_key
            return result
        return await self._scripted.execute_or_recover(attempt_key, contract, validated, context)


def _kernel_effects_module() -> Any:
    import importlib.util

    path = (
        Path(__file__).resolve().parents[3]
        / "framework"
        / "graph-engine"
        / "tests"
        / "attempts"
        / "test_kernel_effects.py"
    )
    spec = importlib.util.spec_from_file_location("improvement_graph_kernel_effects_helper", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_INTENT_SCHEMA", resource_bytes("schemas/improvement-effect-intent.v1.schema.json"))
    return module


def _live_evaluate_revision() -> str:
    return canonical_digest({"revision": "improvement-graph-evaluate"})


def _live_evaluate_config(*, entrypoint: str) -> RunnableConfig:
    revision = _live_evaluate_revision()
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": revision,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 4,
            "assurance_entrypoint": entrypoint,
        }
    }


def _build_live_evaluate_bundle(
    tmp_path: Path,
) -> tuple[
    Any,
    _HybridAttemptKernel,
    Any,
    CountingEffectState,
    RecordingCapabilityBuildContext,
    Any,
    MemoryAttemptJournal,
]:
    closed = close_improvement_task(_EVALUATE_HANDLER)
    delivery_store = CountingEffectState()
    effect = _ObservedDeliveryEffect(ImprovementDeliveryEffect())
    helper = _kernel_effects_module()
    effects, schemas = helper.build_effect_registries(
        effect,
        kinds=(_DELIVERY_KIND,),
        policy=EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0),
        receipt_schema=resource_bytes("schemas/improvement-effect-receipt.v1.schema.json"),
    )
    project = tmp_path / "project"
    project.mkdir()
    workspace_store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    workspace = _RecordingWorkspace(TaskWorkspaceProvider(workspace_store))
    journal = MemoryAttemptJournal()
    real_kernel = AssuranceAttemptKernel(
        journal=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,  # type: ignore[arg-type]
        graph_revision=_live_evaluate_revision(),
        effects=effects,
        schemas=schemas,
        effect_state=delivery_store,
    )
    scripted = ScriptedAttempt()
    hybrid = _HybridAttemptKernel(real_kernel, scripted)
    factory = AttemptNodeFactory(journal=journal, kernel=hybrid)
    evaluate_contract = TASK_ATTEMPT_CONTRACTS[_EVALUATE_HANDLER]
    if isinstance(evaluate_contract.resources, ResourceClaims) and evaluate_contract.resources.writes == ():
        evaluate_contract = replace(evaluate_contract, resources=ResourceClaims(writes=("out.txt",)))
    resolved = resolve_contract(evaluate_contract, executor=closed)
    contracts = improvement_contracts()
    contracts[TASK_EVALUATE_ID] = resolved
    context = RecordingCapabilityBuildContext(
        owner_id="assurance.improvement",
        contracts=contracts,
        attempt_factory=factory,
    )
    return (
        build_improvement_graphs(context),
        hybrid,
        closed,
        delivery_store,
        context,
        workspace_store,
        journal,
    )


async def test_standalone_evaluate_kernel_settles_delivery_v1_memory_eval(tmp_path: Path) -> None:
    bundle, hybrid, closed, delivery_store, _context, store, journal = _build_live_evaluate_bundle(tmp_path)
    try:
        result = await bundle.evaluate.ainvoke(
            {**skill_graph_fields(), **complete_evaluate_payload()},
            config=_live_evaluate_config(entrypoint="improvement-evaluate"),
        )
        assert isinstance(result, dict)
        snapshot = await journal.load(hybrid.keys["improvement.evaluate"])
        assert snapshot is not None
        assert snapshot.terminal is not None
        assert snapshot.terminal.resolution_kind == "committed"
        assert snapshot.effects
        assert snapshot.effects[0].kind == _DELIVERY_KIND
        assert snapshot.effects[0].receipt_digest
        evaluate_trace = hybrid.traces["improvement.evaluate"]
        assert "settle_effects" in evaluate_trace
        assert "publish_receipt" not in evaluate_trace
        assert evaluate_trace.index("settle_effects") < evaluate_trace.index("record_terminal")
        intents = snapshot.effects
        assert len(intents) == 1
        assert intents[0].kind == _DELIVERY_KIND
        intent = ImprovementEffectIntentV1.model_validate(intents[0].payload)
        assert intent.kind == "memory_eval"
        assert intent.memory_eval is not None
        assert intent.memory_eval.outcome == "passed"
        assert delivery_effect_key(intent) == f"{IMPROVEMENT_ID}:1:memory_eval:{HEX_A}"
        assert delivery_store.delivery_count == 1
        stored = ImprovementEffectReceiptV1.model_validate(
            next(iter(delivery_store._by_settlement.values())).receipt
        )
        assert stored.kind == "memory_eval"
        assert closed.dispatch_count == 1
    finally:
        store.close()


async def test_apply_evaluate_kernel_settles_delivery_v1_memory_eval(tmp_path: Path) -> None:
    bundle, hybrid, closed, delivery_store, _context, store, journal = _build_live_evaluate_bundle(tmp_path)
    try:
        from graph_engine.artifacts import stage_json_artifact

        from assurance_improvement.contracts.handoff import PROJECTION

        approved = ImprovementProjection.model_validate(improvement_projection(state="approved"))
        staged = stage_json_artifact(tmp_path / "project", PROJECTION, approved)
        hybrid._scripted.load_script(
            {
                "improvement.apply-auto-review": [
                    committed(
                        auto_review_output(lifecycle_state="approved"),
                        _receipt(),
                        artifacts=[{"path": staged.path, "digest": staged.digest}],
                    )
                ],
                "improvement.apply": [committed(apply_receipt(), _receipt())],
            }
        )
        result = await bundle.apply.ainvoke(
            apply_graph_input(),
            config=_live_evaluate_config(entrypoint="improvement-apply"),
        )
        assert isinstance(result, dict)
        snapshot = await journal.load(hybrid.keys["improvement.apply-evaluate"])
        assert snapshot is not None
        assert snapshot.terminal is not None
        assert snapshot.terminal.resolution_kind == "committed"
        assert snapshot.effects
        assert snapshot.effects[0].kind == _DELIVERY_KIND
        assert snapshot.effects[0].receipt_digest
        trace = hybrid.traces["improvement.apply-evaluate"]
        assert "settle_effects" in trace
        assert "publish_receipt" not in trace
        assert trace.index("settle_effects") < trace.index("record_terminal")
        intents = snapshot.effects
        assert len(intents) == 1
        assert intents[0].kind == _DELIVERY_KIND
        intent = ImprovementEffectIntentV1.model_validate(intents[0].payload)
        assert intent.kind == "memory_eval"
        assert intent.memory_eval is not None
        assert intent.memory_eval.outcome == "passed"
        assert delivery_effect_key(intent) == f"{IMPROVEMENT_ID}:1:memory_eval:{HEX_A}"
        assert delivery_store.delivery_count == 1
        stored = ImprovementEffectReceiptV1.model_validate(
            next(iter(delivery_store._by_settlement.values())).receipt
        )
        assert stored.kind == "memory_eval"
        assert closed.dispatch_count == 1
    finally:
        store.close()
