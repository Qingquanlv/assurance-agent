from __future__ import annotations

from collections.abc import Mapping

import pytest

from assurance_improvement.contracts.delivery import MemoryEvalReceipt
from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS
from graph_engine.effects.contracts import EXPECTED_EFFECT_KINDS

from tests.product.conformance import SEMANTIC_TRACE_IGNORED_FIELDS
from tests.product.shadow_harness import (
    DualDriverError,
    SemanticMismatch,
    SemanticTrace,
    ShadowSession,
    attach_driver,
    compare_semantic_traces,
    prove_entrypoint_parity,
    run_evaluate_shadow,
)

_DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"
_PUBLIC_ENTRYPOINTS = tuple(sorted(PRODUCT_ENTRYPOINTS))

_EXECUTE_FULL_SCENARIOS = (
    "generation-api",
    "generation-all-families",
    "execution-failure-healing-rerun",
    "coverage-repair-human",
    "report-satisfied",
    "report-unsatisfied",
    "effect-pending",
    "budget-exhaustion",
)

_EVALUATE_SCENARIOS = ("committed", "pending", "publication-indeterminate")


def test_semantic_trace_shape_is_normalized() -> None:
    fields = set(SemanticTrace.__dataclass_fields__)
    assert fields == {
        "entrypoint",
        "attempts",
        "pure_decisions",
        "validator_calls",
        "interrupts",
        "receipts",
        "terminal_status",
        "public_output",
    }
    assert SemanticTrace.__dataclass_params__.frozen is True


def test_shadow_uses_separate_invocation_identities_and_workspaces(tmp_path) -> None:
    session = ShadowSession.create(tmp_path, entrypoint="intake")
    assert session.legacy.invocation_id != session.langgraph.invocation_id
    assert session.legacy.workspace_root != session.langgraph.workspace_root
    assert session.legacy.workspace_root.is_dir()
    assert session.langgraph.workspace_root.is_dir()
    attach_driver(session.legacy, "legacy-v2")
    attach_driver(session.langgraph, "langgraph-v1")
    assert session.legacy.runtime == "legacy-v2"
    assert session.langgraph.runtime == "langgraph-v1"


def test_guard_rejects_attaching_both_drivers_to_one_invocation(tmp_path) -> None:
    session = ShadowSession.create(tmp_path, entrypoint="archive")
    attach_driver(session.legacy, "legacy-v2")
    with pytest.raises(DualDriverError, match="one Invocation"):
        attach_driver(session.legacy, "langgraph-v1")


def test_no_external_effect_is_applied_twice(tmp_path) -> None:
    pair = run_evaluate_shadow(tmp_path, scenario="committed")
    assert pair.legacy.invocation_id != pair.langgraph.invocation_id
    assert pair.effect_apply_calls == 1
    assert pair.evaluator_dispatch_count == 1
    compare_semantic_traces(pair.legacy.trace, pair.langgraph.trace)


def test_shadow_runners_bind_invocation_and_collect_independent_traces(product_runner, tmp_path) -> None:
    record = prove_entrypoint_parity(
        tmp_path,
        product_runner=product_runner,
        entrypoint="intake",
        scenario="valid",
    )
    assert record.legacy.trace is not record.langgraph.trace
    assert record.legacy.workspace_root != record.langgraph.workspace_root
    legacy_text = " ".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in record.legacy.workspace_root.rglob("*")
        if path.is_file()
    )
    assert record.legacy.invocation_id in legacy_text
    assert record.legacy.invocation_id in str(record.legacy.workspace_root)
    langgraph_text = " ".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in record.langgraph.workspace_root.rglob("*")
        if path.is_file()
    )
    assert record.langgraph.invocation_id in langgraph_text or record.langgraph.invocation_id in str(
        record.langgraph.workspace_root
    )


@pytest.mark.parametrize("entrypoint", _PUBLIC_ENTRYPOINTS)
def test_every_public_root_has_valid_failure_and_interrupt_parity(
    product_runner, tmp_path, entrypoint: str
) -> None:
    for scenario in ("valid", "failure", "interrupt"):
        record = prove_entrypoint_parity(
            tmp_path,
            product_runner=product_runner,
            entrypoint=entrypoint,
            scenario=scenario,
        )
        assert record.entrypoint == entrypoint
        assert record.scenario == scenario
        assert record.passed, record.mismatches
        assert record.legacy.invocation_id != record.langgraph.invocation_id
        compare_semantic_traces(record.legacy.trace, record.langgraph.trace)


@pytest.mark.parametrize("entrypoint", ("execute", "full"))
@pytest.mark.parametrize("scenario", _EXECUTE_FULL_SCENARIOS)
def test_execute_and_full_branch_parity(product_runner, tmp_path, entrypoint: str, scenario: str) -> None:
    record = prove_entrypoint_parity(
        tmp_path,
        product_runner=product_runner,
        entrypoint=entrypoint,
        scenario=scenario,
    )
    assert record.passed, record.mismatches
    if scenario in {"execution-failure-healing-rerun", "coverage-repair-human"}:
        assert record.legacy.current_trigger == record.langgraph.current_trigger
        assert record.legacy.repeat_activation_count == record.langgraph.repeat_activation_count
        assert record.legacy.current_trigger is not None


@pytest.mark.parametrize("scenario", _EVALUATE_SCENARIOS)
def test_standalone_evaluate_effect_settlement_parity(tmp_path, scenario: str) -> None:
    pair = run_evaluate_shadow(tmp_path, scenario=scenario, occurrence="standalone")
    _assert_evaluate_effect(pair.legacy.trace)
    _assert_evaluate_effect(pair.langgraph.trace)
    compare_semantic_traces(pair.legacy.trace, pair.langgraph.trace)
    assert pair.evaluator_dispatch_count == 1
    if scenario == "committed":
        assert pair.legacy.trace.terminal_status == "completed"
        assert pair.receipt is not None
        MemoryEvalReceipt.model_validate(pair.receipt.model_dump(mode="json"))
    else:
        assert pair.legacy.trace.terminal_status != "completed"
        assert pair.success_before_settlement is False


def test_evaluate_inside_apply_uses_the_same_delivery_effect(tmp_path) -> None:
    pair = run_evaluate_shadow(tmp_path, scenario="committed", occurrence="apply")
    _assert_evaluate_effect(pair.legacy.trace)
    _assert_evaluate_effect(pair.langgraph.trace)
    compare_semantic_traces(pair.legacy.trace, pair.langgraph.trace)
    assert pair.evaluator_dispatch_count == 1
    assert pair.public_entrypoint == "improvement-apply"
    assert pair.legacy.trace.entrypoint == "improvement-apply"
    assert pair.langgraph.trace.entrypoint == "improvement-apply"


def test_evaluate_recovery_does_not_dispatch_twice(tmp_path) -> None:
    pair = run_evaluate_shadow(tmp_path, scenario="publication-indeterminate", recover=True)
    assert pair.evaluator_dispatch_count == 1
    assert pair.effect_apply_calls == 1
    assert pair.success_before_settlement is False


def test_evaluate_dispatches_once_per_isolated_invocation(tmp_path, monkeypatch) -> None:
    from assurance_improvement.operations.delivery import EvaluateMemoryImprovementHandler

    seen: list[str] = []
    original = EvaluateMemoryImprovementHandler.execute

    async def _counted(self, request, context):
        seen.append(request.invocation_id)
        return await original(self, request, context)

    monkeypatch.setattr(EvaluateMemoryImprovementHandler, "execute", _counted)
    pair = run_evaluate_shadow(tmp_path, scenario="committed")
    assert pair.public_entrypoint == "improvement-evaluate"
    assert set(seen) == {pair.legacy.invocation_id, pair.langgraph.invocation_id}
    assert len(seen) == 2
    assert pair.evaluator_dispatch_count == 1
    assert pair.success_before_settlement is False


def test_compare_applies_semantic_trace_ignored_fields() -> None:
    left = SemanticTrace(
        entrypoint="intake",
        attempts=(),
        pure_decisions=(),
        validator_calls=(),
        interrupts=(),
        receipts=(),
        terminal_status="completed",
        public_output={"decision": "pass", "activation_id": "left", "checkpoint_id": "c1"},
    )
    right = SemanticTrace(
        entrypoint="intake",
        attempts=(),
        pure_decisions=(),
        validator_calls=(),
        interrupts=(),
        receipts=(),
        terminal_status="completed",
        public_output={"decision": "pass", "activation_id": "right", "checkpoint_id": "c2"},
    )
    assert "activation_id" in SEMANTIC_TRACE_IGNORED_FIELDS
    compare_semantic_traces(left, right)


def test_mismatches_are_reported_by_semantic_field_not_raw_events() -> None:
    left = SemanticTrace(
        entrypoint="intake",
        attempts=(),
        pure_decisions=(),
        validator_calls=(),
        interrupts=(),
        receipts=(),
        terminal_status="completed",
        public_output={"decision": "pass"},
    )
    right = SemanticTrace(
        entrypoint="intake",
        attempts=(),
        pure_decisions=(),
        validator_calls=(),
        interrupts=(),
        receipts=(),
        terminal_status="failed",
        public_output={"decision": "pass"},
    )
    with pytest.raises(SemanticMismatch, match="terminal_status") as exc_info:
        compare_semantic_traces(left, right)
    assert "raw event" not in str(exc_info.value).lower()
    assert "activation_id" not in str(exc_info.value)


def test_production_cutover_stays_all_legacy_during_shadow() -> None:
    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    assert all(kind == "legacy-v2" for kind in ENTRYPOINT_RUNTIME_CUTOVER.values())


def test_delivery_kind_is_not_a_seventh_effect() -> None:
    assert _DELIVERY_KIND in EXPECTED_EFFECT_KINDS
    assert "memory_eval" not in EXPECTED_EFFECT_KINDS
    assert len(EXPECTED_EFFECT_KINDS) == 6


def _assert_evaluate_effect(trace: SemanticTrace) -> None:
    kinds = tuple(receipt.kind for receipt in trace.receipts)
    assert _DELIVERY_KIND in kinds
    discriminators = tuple(
        receipt.payload_kind for receipt in trace.receipts if receipt.kind == _DELIVERY_KIND
    )
    assert "memory_eval" in discriminators
    outputs = trace.public_output
    if isinstance(outputs, Mapping) and trace.terminal_status == "completed":
        MemoryEvalReceipt.model_validate(outputs.get("receipt") or outputs)
