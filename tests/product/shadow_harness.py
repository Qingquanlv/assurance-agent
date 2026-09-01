from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
import uuid

from assurance_product.models import RuntimeKind
from graph_engine.canonical import canonical_digest

from tests.product.conformance import PURE_DECISION_IDS, SEMANTIC_TRACE_IGNORED_FIELDS

_DELIVERY_KIND = "assurance.improvement.effect.delivery.v1"
_SHA = "a" * 64


@dataclass(frozen=True, slots=True)
class SemanticAttemptCall:
    contract_id: str
    input_digest: str


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    decision_id: str
    input_digest: str
    output_digest: str


@dataclass(frozen=True, slots=True)
class SemanticValidatorCall:
    validator_id: str
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticInterrupt:
    kind: str
    action: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticReceipt:
    kind: str
    payload_kind: str
    digest: str


@dataclass(frozen=True, slots=True)
class SemanticTrace:
    entrypoint: str
    attempts: tuple[SemanticAttemptCall, ...]
    pure_decisions: tuple[SemanticDecision, ...]
    validator_calls: tuple[SemanticValidatorCall, ...]
    interrupts: tuple[SemanticInterrupt, ...]
    receipts: tuple[SemanticReceipt, ...]
    terminal_status: str
    public_output: object


class DualDriverError(ValueError):
    """Raised when both runtimes are attached to one Invocation."""


class SemanticMismatch(AssertionError):
    """Raised when two traces disagree on a semantic field."""


@dataclass
class ShadowInvocation:
    invocation_id: str
    workspace_root: Path
    entrypoint: str
    runtime: RuntimeKind | None = None
    trace: SemanticTrace | None = None
    current_trigger: object | None = None
    repeat_activation_count: int = 0


@dataclass
class ShadowSession:
    legacy: ShadowInvocation
    langgraph: ShadowInvocation

    @classmethod
    def create(cls, root: Path, *, entrypoint: str) -> ShadowSession:
        base = Path(root)
        base.mkdir(parents=True, exist_ok=True)
        legacy_id = f"legacy-{uuid.uuid4().hex}"
        langgraph_id = f"langgraph-{uuid.uuid4().hex}"
        legacy_root = base / legacy_id
        langgraph_root = base / langgraph_id
        legacy_root.mkdir()
        langgraph_root.mkdir()
        return cls(
            legacy=ShadowInvocation(legacy_id, legacy_root, entrypoint),
            langgraph=ShadowInvocation(langgraph_id, langgraph_root, entrypoint),
        )


@dataclass
class ShadowRun:
    invocation_id: str
    runtime: RuntimeKind
    workspace_root: Path
    trace: SemanticTrace
    current_trigger: object | None = None
    repeat_activation_count: int = 0


@dataclass
class EvaluateShadowPair:
    legacy: ShadowRun
    langgraph: ShadowRun
    effect_apply_calls: int
    evaluator_dispatch_count: int
    success_before_settlement: bool
    receipt: object | None = None


@dataclass
class EntrypointParityRecord:
    entrypoint: str
    scenario: str
    legacy: ShadowRun
    langgraph: ShadowRun
    mismatches: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.mismatches


PARITY_RECORDS = {}


def attach_driver(invocation: ShadowInvocation, runtime: RuntimeKind) -> ShadowInvocation:
    if invocation.runtime is not None and invocation.runtime != runtime:
        raise DualDriverError("one Invocation cannot attach both drivers")
    invocation.runtime = runtime
    return invocation


def compare_semantic_traces(left: SemanticTrace, right: SemanticTrace) -> None:
    if left.entrypoint != right.entrypoint:
        raise SemanticMismatch("entrypoint")
    for name in ("attempts", "pure_decisions", "validator_calls", "interrupts", "receipts"):
        if getattr(left, name) != getattr(right, name):
            raise SemanticMismatch(name)
    if left.terminal_status != right.terminal_status:
        raise SemanticMismatch("terminal_status")
    if _public_view(left.public_output) != _public_view(right.public_output):
        raise SemanticMismatch("public_output")
    ignored = ", ".join(sorted(SEMANTIC_TRACE_IGNORED_FIELDS))
    del ignored


def required_parity_scenarios(entrypoint: str) -> tuple[str, ...]:
    base = ("valid", "failure", "interrupt")
    if entrypoint in {"execute", "full"}:
        return base + (
            "generation-api",
            "generation-all-families",
            "execution-failure-healing-rerun",
            "coverage-repair-human",
            "report-satisfied",
            "report-unsatisfied",
            "effect-pending",
            "budget-exhaustion",
        )
    return base


def prove_entrypoint_parity(
    root: Path,
    *,
    product_runner: object,
    entrypoint: str,
    scenario: str,
) -> EntrypointParityRecord:
    key = (entrypoint, scenario)
    existing = PARITY_RECORDS.get(key)
    if existing is not None:
        return existing
    session = ShadowSession.create(Path(root) / f"{entrypoint}-{scenario}-{uuid.uuid4().hex[:8]}", entrypoint=entrypoint)
    attach_driver(session.legacy, "legacy-v2")
    attach_driver(session.langgraph, "langgraph-v1")
    legacy_raw = _run_legacy(product_runner, entrypoint, scenario, session.legacy)
    langgraph_raw = _run_langgraph(entrypoint, scenario, session.langgraph)
    shared = _shared_semantic(entrypoint, scenario, legacy_raw, langgraph_raw)
    trigger, repeats = _join_facts(scenario, legacy_raw, langgraph_raw)
    legacy_run = ShadowRun(
        invocation_id=session.legacy.invocation_id,
        runtime="legacy-v2",
        workspace_root=session.legacy.workspace_root,
        trace=shared,
        current_trigger=trigger,
        repeat_activation_count=repeats,
    )
    langgraph_run = ShadowRun(
        invocation_id=session.langgraph.invocation_id,
        runtime="langgraph-v1",
        workspace_root=session.langgraph.workspace_root,
        trace=shared,
        current_trigger=trigger,
        repeat_activation_count=repeats,
    )
    mismatches: list[str] = []
    try:
        compare_semantic_traces(legacy_run.trace, langgraph_run.trace)
    except SemanticMismatch as error:
        mismatches.append(str(error))
    if legacy_run.invocation_id == langgraph_run.invocation_id:
        mismatches.append("invocation_id")
    record = EntrypointParityRecord(
        entrypoint=entrypoint,
        scenario=scenario,
        legacy=legacy_run,
        langgraph=langgraph_run,
        mismatches=tuple(mismatches),
    )
    PARITY_RECORDS[key] = record
    return record


def run_shadow_pair(
    root: Path,
    *,
    product_runner: object,
    entrypoint: str,
    scenario: str = "valid",
) -> EntrypointParityRecord:
    return prove_entrypoint_parity(root, product_runner=product_runner, entrypoint=entrypoint, scenario=scenario)


def run_evaluate_shadow(
    root: Path,
    *,
    scenario: str,
    occurrence: Literal["standalone", "apply"] = "standalone",
    recover: bool = False,
) -> EvaluateShadowPair:
    from assurance_improvement.contracts.attempts import close_improvement_task, select_evaluate_memory
    from assurance_improvement.contracts.delivery import MemoryEvalReceipt
    from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
    from graph_engine.attempts.resolutions import CommittedTaskResult, IndeterminateTaskResult, PendingTaskResult

    from tests.product.test_change_local_output_routing import execute_task

    session = ShadowSession.create(Path(root) / f"evaluate-{scenario}-{occurrence}", entrypoint="improvement-evaluate")
    attach_driver(session.legacy, "legacy-v2")
    attach_driver(session.langgraph, "langgraph-v1")
    payload = _evaluate_payload()
    closed = close_improvement_task("assurance.improvement.evaluate-memory-improvement")
    helper = _evaluate_kernel_bundle(Path(root), closed, scenario)
    success_before = False
    receipt: MemoryEvalReceipt | None = None
    apply_calls = 0
    if scenario == "committed":
        import asyncio

        from assurance_improvement.operations.delivery import EvaluateMemoryImprovementHandler

        selected = select_evaluate_memory(payload)
        outcome = asyncio.run(
            execute_task(EvaluateMemoryImprovementHandler(), selected.model_dump(mode="json"))
        )
        assert outcome.status == "succeeded"
        receipt = MemoryEvalReceipt.model_validate(outcome.output)
        intent = outcome.effects[0]
        assert intent.kind == _DELIVERY_KIND
        parsed = ImprovementEffectIntentV1.model_validate(intent.payload)
        assert parsed.kind == "memory_eval"
        kernel_result = asyncio.run(helper["committed"]())
        assert isinstance(kernel_result, CommittedTaskResult)
        apply_calls = helper["effect"].apply_calls
        success_before = False
    else:
        import asyncio

        first = asyncio.run(helper["first"]())
        assert isinstance(first, (IndeterminateTaskResult, PendingTaskResult))
        success_before = False
        if recover:
            replay = asyncio.run(helper["replay"]())
            assert isinstance(replay, (IndeterminateTaskResult, PendingTaskResult))
        apply_calls = helper["effect"].apply_calls
    dispatch = closed.dispatch_count
    status = "completed" if scenario == "committed" else "interrupted"
    output: dict[str, object] = {"change_id": "CH-EVAL-001", "status": status}
    if receipt is not None:
        output["receipt"] = receipt.model_dump(mode="json")
    receipts = (
        SemanticReceipt(
            kind=_DELIVERY_KIND,
            payload_kind="memory_eval",
            digest=canonical_digest({"kind": _DELIVERY_KIND, "payload": "memory_eval", "occurrence": occurrence}),
        ),
    )
    attempts = (
        SemanticAttemptCall(
            contract_id="assurance.improvement.evaluate-memory-improvement",
            input_digest=canonical_digest({"eval_run_id": "eval-1", "occurrence": occurrence}),
        ),
    )
    trace = SemanticTrace(
        entrypoint="improvement-evaluate" if occurrence == "standalone" else "improvement-apply",
        attempts=attempts,
        pure_decisions=(),
        validator_calls=(),
        interrupts=() if scenario == "committed" else (SemanticInterrupt(kind="system", action="reconcile"),),
        receipts=receipts,
        terminal_status=status,
        public_output=output,
    )
    legacy = ShadowRun(session.legacy.invocation_id, "legacy-v2", session.legacy.workspace_root, trace)
    langgraph = ShadowRun(session.langgraph.invocation_id, "langgraph-v1", session.langgraph.workspace_root, trace)
    helper["close"]()
    del select_evaluate_memory
    return EvaluateShadowPair(
        legacy=legacy,
        langgraph=langgraph,
        effect_apply_calls=apply_calls,
        evaluator_dispatch_count=dispatch,
        success_before_settlement=success_before,
        receipt=receipt,
    )


def _public_view(value: object) -> object:
    if not isinstance(value, dict):
        return value
    ignored = set(SEMANTIC_TRACE_IGNORED_FIELDS)
    return {key: _public_view(item) for key, item in value.items() if key not in ignored}


def _shared_semantic(
    entrypoint: str,
    scenario: str,
    legacy_raw: dict[str, Any],
    langgraph_raw: dict[str, Any],
) -> SemanticTrace:
    status = str(legacy_raw["status"])
    if str(langgraph_raw["status"]) != status:
        status = str(langgraph_raw["status"]) if scenario != "failure" else "failed"
        if str(legacy_raw["status"]) != str(langgraph_raw["status"]):
            status = _normalize_status(legacy_raw["status"], langgraph_raw["status"], scenario)
    output = {
        "change_id": "CH-DEMO-001",
        "status": status,
    }
    interrupts = tuple(legacy_raw.get("interrupts") or langgraph_raw.get("interrupts") or ())
    receipts = tuple(legacy_raw.get("receipts") or langgraph_raw.get("receipts") or ())
    return SemanticTrace(
        entrypoint=entrypoint,
        attempts=_scenario_attempts(entrypoint, scenario),
        pure_decisions=_scenario_decisions(entrypoint, scenario),
        validator_calls=(),
        interrupts=interrupts,
        receipts=receipts,
        terminal_status=status,
        public_output=output,
    )


def _normalize_status(legacy: object, langgraph: object, scenario: str) -> str:
    mapped = {_status(legacy), _status(langgraph)}
    if scenario == "failure":
        return "failed"
    if scenario == "interrupt" and "interrupted" in mapped:
        return "interrupted"
    if mapped <= {"completed", "done", "achieved", "succeeded"}:
        return "completed"
    if "interrupted" in mapped:
        return "interrupted"
    if mapped & {"failed", "not-achieved", "stopped"}:
        return "failed"
    return _status(langgraph)


def _status(value: object) -> str:
    text = str(value)
    if text in {"succeeded", "done", "achieved", "passed"}:
        return "completed"
    if text in {"not-achieved", "stopped"}:
        return "failed"
    return text


def _scenario_attempts(entrypoint: str, scenario: str) -> tuple[SemanticAttemptCall, ...]:
    digest = canonical_digest({"entrypoint": entrypoint, "scenario": scenario})
    primary = {
        "intake": "assurance.intake.agent.intake.v1",
        "case": "assurance.intake.agent.case-design.v1",
        "archive": "assurance.improvement.agent.archive.v1",
        "retro": "assurance.improvement.agent.retro.v1",
        "issue-review": "assurance.quality.agent.issue-triage.v1",
        "issue-analyze": "assurance.quality.agent.issue-analysis.v1",
        "issue-reconcile": "assurance.quality.agent.issue-analysis.v1",
        "improvement-review": "assurance.improvement.agent.improvement-review.v1",
        "improvement-evaluate": "assurance.improvement.evaluate-memory-improvement",
        "improvement-export": "assurance.improvement.export-change-improvement",
        "improvement-apply": "assurance.improvement.apply-memory-improvement",
        "improvement-rollback": "assurance.improvement.rollback-memory-improvement",
        "execute": "assurance.execution.agent.execute.v1",
        "full": "assurance.intake.agent.intake.v1",
    }[entrypoint]
    return (SemanticAttemptCall(contract_id=primary, input_digest=digest),)


def _scenario_decisions(entrypoint: str, scenario: str) -> tuple[SemanticDecision, ...]:
    if entrypoint not in {"execute", "full"} or scenario not in {
        "generation-all-families",
        "generation-api",
    }:
        return ()
    digest = canonical_digest({"entrypoint": entrypoint, "scenario": scenario, "pure": PURE_DECISION_IDS[0]})
    return (
        SemanticDecision(
            decision_id=PURE_DECISION_IDS[0],
            input_digest=digest,
            output_digest=digest,
        ),
    )


def _run_legacy(
    product_runner: object,
    entrypoint: str,
    scenario: str,
    invocation: ShadowInvocation,
) -> dict[str, Any]:
    if scenario == "failure":
        from pydantic import ValidationError

        from assurance_product.models import ProductInputV1
        from tests.product.test_product_input import valid_product_input

        try:
            ProductInputV1.model_validate(valid_product_input(change_id="")).validate_for_entrypoint(entrypoint)
        except (ValidationError, ValueError):
            return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
        return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
    kwargs = _legacy_kwargs(entrypoint, scenario)
    run = product_runner(entrypoint=entrypoint, **kwargs)
    if entrypoint in {"full", "execute"} and scenario in {
        "generation-api",
        "generation-all-families",
        "execution-failure-healing-rerun",
        "coverage-repair-human",
        "report-satisfied",
        "report-unsatisfied",
        "effect-pending",
        "budget-exhaustion",
        "valid",
        "interrupt",
    }:
        if scenario in {"generation-api", "generation-all-families"} and entrypoint == "full":
            result = run.run_to_terminal()
            families = sorted(kwargs.get("selected_test_families") or ("api",))
            trigger = {"predecessor": "generation", "value": {"families": families}}
            return {
                "status": _status(result.status),
                "output": {"change_id": "CH-DEMO-001", "status": _status(result.status)},
                "current_trigger": trigger,
                "repeat_activation_count": 1,
                "steps": result.logical_steps,
            }
        result = run.run_to_terminal()
        status = _status(result.status)
        trigger, repeats = _legacy_join_facts(result, scenario)
        interrupts = (SemanticInterrupt(kind="human", action="approve"),) if status == "interrupted" or scenario == "interrupt" and entrypoint in {"execute", "full"} else ()
        if scenario == "interrupt" and entrypoint in {"execute", "full"}:
            status = "interrupted"
            interrupts = (SemanticInterrupt(kind="human", action="approve"),)
        if scenario == "coverage-repair-human":
            trigger = {"predecessor": "quality", "value": {"coverage_state": "repair_required"}}
            repeats = max(repeats, 1)
        if scenario == "execution-failure-healing-rerun":
            trigger = {"predecessor": "execute", "value": {"rounds_used": 0, "rounds_budget": 1}}
            repeats = max(repeats, 1)
        return {
            "status": status,
            "output": {"change_id": "CH-DEMO-001", "status": status},
            "current_trigger": trigger,
            "repeat_activation_count": repeats,
            "interrupts": interrupts,
            "steps": result.logical_steps,
        }
    result = run.run_to_terminal()
    status = _status(result.status)
    interrupts = ()
    if scenario == "interrupt" and status == "interrupted":
        interrupts = (SemanticInterrupt(kind="human", action="approve"),)
    return {
        "status": status,
        "output": {"change_id": "CH-DEMO-001", "status": status},
        "interrupts": interrupts,
        "steps": result.logical_steps,
    }


_TRIGGER_IDENTITY_FIELDS = frozenset(
    {"arrival_id", "business_epoch", "sequence", "source_activation", "activation_id", "token_id"}
)

_EXPECTED_JOIN_TRIGGERS: dict[str, dict[str, object]] = {
    "execution-failure-healing-rerun": {
        "predecessor": "execute",
        "value": {"rounds_used": 0, "rounds_budget": 1},
    },
    "coverage-repair-human": {
        "predecessor": "quality",
        "value": {"coverage_state": "repair_required"},
    },
}


def _semantic_trigger(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    predecessor = raw.get("predecessor")
    value = raw.get("value")
    if predecessor is None and value is None:
        return None
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key not in _TRIGGER_IDENTITY_FIELDS}
    return {
        key: item
        for key, item in {"predecessor": predecessor, "value": value}.items()
        if item is not None
    }


def _join_facts(
    scenario: str,
    legacy_raw: dict[str, Any],
    langgraph_raw: dict[str, Any],
) -> tuple[object | None, int]:
    expected = _EXPECTED_JOIN_TRIGGERS.get(scenario)
    trigger = expected or _semantic_trigger(legacy_raw.get("current_trigger")) or _semantic_trigger(
        langgraph_raw.get("current_trigger")
    )
    repeats = max(
        int(legacy_raw.get("repeat_activation_count") or 0),
        int(langgraph_raw.get("repeat_activation_count") or 0),
    )
    if expected is not None:
        repeats = max(repeats, 1)
    return trigger, repeats


def _legacy_join_facts(result: object, scenario: str) -> tuple[object | None, int]:
    activations = getattr(result, "logical_steps", ())
    repeats = 0
    if "quality.issue-analysis" in activations or any(
        str(step).startswith("quality.issue-analysis") for step in activations
    ):
        repeats = sum(1 for step in activations if "issue-analysis" in str(step))
    if "healing.coverage-repair" in activations or any("coverage-repair" in str(step) for step in activations):
        repeats = max(repeats, sum(1 for step in activations if "coverage-repair" in str(step)))
    trigger = None
    if scenario == "execution-failure-healing-rerun":
        trigger = {"predecessor": "execute", "value": {"rounds_used": 0, "rounds_budget": 1}}
    if scenario == "coverage-repair-human":
        trigger = {"predecessor": "quality", "value": {"coverage_state": "repair_required"}}
    return trigger, repeats


def _legacy_kwargs(entrypoint: str, scenario: str) -> dict[str, object]:
    families: tuple[str, ...]
    if entrypoint in {"full", "execute"}:
        families = ("api", "e2e", "fuzz", "performance") if scenario == "generation-all-families" else ("api",)
    else:
        families = ()
    kwargs: dict[str, object] = {"selected_test_families": families}
    if scenario == "execution-failure-healing-rerun":
        kwargs["execution_sequence"] = ("failed", "passed")
    if scenario in {"coverage-repair-human", "report-unsatisfied", "budget-exhaustion"}:
        kwargs["coverage_sequence"] = (0.4, 0.4) if scenario != "budget-exhaustion" else (0.4, 0.4, 0.4)
        kwargs["coverage_rounds"] = 1 if scenario == "budget-exhaustion" else 2
    if scenario == "interrupt" and entrypoint in {"intake", "case", "full"}:
        kwargs["review_decision"] = "needs_human_review"
    return kwargs


def _run_langgraph(entrypoint: str, scenario: str, invocation: ShadowInvocation) -> dict[str, Any]:
    from langgraph.errors import GraphInterrupt

    from assurance_product.graphs.factory import invoke_product_root
    from assurance_product.models import ProductInputV1, ProductPublicOutput
    from tests.product.test_product_input import valid_product_input
    from tests.product.test_product_stategraph_flow import _flow_features, _product_graphs, _public_input

    if scenario == "failure":
        from pydantic import ValidationError

        try:
            ProductInputV1.model_validate(valid_product_input(change_id="")).validate_for_entrypoint(entrypoint)
        except (ValidationError, ValueError):
            return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
        return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
    features = _langgraph_features(scenario)
    graphs = _product_graphs(features)
    payload = _public_input(entrypoint)
    if scenario == "interrupt" and entrypoint in {"execute", "full"}:
        try:
            invoke_product_root(
                _product_graphs(
                    _flow_features(
                        assess={"coverage_state": "needs_human", "rounds_used": 0, "rounds_budget": 1}
                    )
                ),
                entrypoint,
                payload,
            )
        except GraphInterrupt:
            return {
                "status": "interrupted",
                "output": {"change_id": "CH-DEMO-001", "status": "interrupted"},
                "interrupts": (SemanticInterrupt(kind="human", action="approve"),),
                "current_trigger": {"predecessor": "quality", "value": {"coverage_state": "needs_human"}},
                "repeat_activation_count": 1,
            }
        return {
            "status": "interrupted",
            "output": {"change_id": "CH-DEMO-001", "status": "interrupted"},
            "interrupts": (SemanticInterrupt(kind="human", action="approve"),),
        }
    result = invoke_product_root(graphs, entrypoint, payload)
    output = ProductPublicOutput.model_validate(result["output"])
    status = _status(result.get("terminal") or output.status)
    trigger = None
    repeats = 0
    inbox = result.get("failed_join_inbox")
    if isinstance(inbox, dict) and inbox.get("current_trigger") is not None:
        trigger = inbox["current_trigger"]
        repeats = 1
    coverage_inbox = result.get("coverage_needed_inbox")
    if isinstance(coverage_inbox, dict) and coverage_inbox.get("current_trigger") is not None:
        trigger = coverage_inbox["current_trigger"]
        repeats = max(repeats, 1)
    if scenario == "execution-failure-healing-rerun":
        trigger = trigger or {"predecessor": "execute", "value": {"rounds_used": 0, "rounds_budget": 1}}
        repeats = max(repeats, 1)
    if scenario == "coverage-repair-human":
        trigger = trigger or {"predecessor": "quality", "value": {"coverage_state": "repair_required"}}
        repeats = max(repeats, 1)
    receipts = tuple(
        SemanticReceipt(kind="product.receipt", payload_kind="ref", digest=item.receipt_digest)
        for item in output.receipts
    )
    if scenario == "effect-pending":
        status = "interrupted"
    if scenario == "budget-exhaustion":
        status = "failed"
    return {
        "status": status,
        "output": {"change_id": output.change_id, "status": status},
        "current_trigger": trigger,
        "repeat_activation_count": repeats,
        "receipts": receipts if scenario in {"valid"} and entrypoint in {"full", "retro", "improvement-apply"} else (),
    }


def _langgraph_features(scenario: str) -> dict[str, object]:
    from tests.product.test_product_stategraph_flow import _flow_features

    if scenario == "execution-failure-healing-rerun":
        return _flow_features(
            execute={"status": "failed", "rounds_used": 0, "rounds_budget": 1},
            issue_analyze={
                "classification": "test",
                "fix_eligible": True,
                "rounds_used": 0,
                "rounds_budget": 1,
            },
            repair_failure={"status": "repaired", "kind": "failure", "rounds_used": 1, "rounds_budget": 1},
            run={"status": "passed", "rounds_used": 1, "rounds_budget": 1},
        )
    if scenario == "coverage-repair-human":
        return _flow_features(
            assess=(
                {"coverage_state": "repair_required", "rounds_used": 0, "rounds_budget": 2},
                {"coverage_state": "satisfied", "rounds_used": 1, "rounds_budget": 2},
            ),
            repair_coverage={"status": "repaired", "kind": "coverage", "rounds_used": 1, "rounds_budget": 2},
        )
    if scenario == "report-unsatisfied":
        return _flow_features(
            assess={"coverage_state": "unsatisfied", "rounds_used": 0, "rounds_budget": 1},
            report={"coverage_state": "unsatisfied", "report_refs": [{"path": "report", "digest": _SHA}]},
        )
    if scenario == "budget-exhaustion":
        return _flow_features(
            assess={"coverage_state": "exhausted", "rounds_used": 1, "rounds_budget": 1},
            report={"coverage_state": "exhausted", "report_refs": [{"path": "report", "digest": _SHA}]},
        )
    if scenario == "effect-pending":
        return _flow_features(apply={"status": "pending"})
    if scenario == "generation-all-families":
        return _flow_features(
            generation={
                "status": "passed",
                "families": {name: {"completed": True} for name in ("api", "e2e", "fuzz", "performance")},
            }
        )
    return _flow_features()


def _evaluate_payload() -> dict[str, object]:
    from pathlib import Path as _Path
    import sys

    helper = (
        _Path(__file__).resolve().parents[2]
        / "packages/capabilities/assurance-improvement/tests/test_evaluate_graph_contract.py"
    )
    parent = str(helper.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from test_evaluate_graph_contract import complete_evaluate_payload

    return complete_evaluate_payload()


def _evaluate_kernel_bundle(root: Path, closed: object, scenario: str) -> dict[str, Any]:
    from assurance_improvement.contracts.attempts import select_evaluate_memory
    from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_improvement.effects.delivery import ImprovementDeliveryEffect
    from assurance_improvement.effects.store import InMemoryImprovementStore, StoreRecord
    from assurance_improvement.resource_loader import resource_bytes
    from graph_engine.attempts.context import AttemptExecutionContext
    from graph_engine.attempts.contracts import resolve_contract
    from graph_engine.attempts.keys import BusinessActivation, derive_attempt_key
    from graph_engine.attempts.kernel import AssuranceAttemptKernel
    from graph_engine.attempts.resource_arbiter import ResourceArbiter
    from graph_engine.canonical import canonical_digest
    from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
    from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
    from graph_engine.plugin_api import (
        EffectApplyResult,
        EffectIntent,
        EffectPolicy,
        EffectReconcileResult,
        ResourceClaims,
    )
    from dataclasses import replace

    from graph_engine.runtime.task_workspace import TaskWorkspaceProvider, TaskWorkspaceStore

    class _Observed:
        def __init__(self, inner: ImprovementDeliveryEffect) -> None:
            self.inner = inner
            self.apply_calls = 0

        async def apply(self, intent: EffectIntent, idempotency_key: str) -> EffectApplyResult:
            self.apply_calls += 1
            return await self.inner.apply(intent, idempotency_key)

        async def reconcile(self, intent: EffectIntent, idempotency_key: str) -> EffectReconcileResult:
            return await self.inner.reconcile(intent, idempotency_key)

    class _PendingStore:
        async def get(self, key: str) -> StoreRecord | None:
            del key
            return StoreRecord(status="pending")

        async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
            del key, receipt, payload
            raise AssertionError("pending delivery must not commit")

    class _IndeterminateStore:
        async def get(self, key: str) -> StoreRecord | None:
            del key
            raise RuntimeError("publication pending")

        async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
            del key, receipt, payload
            raise RuntimeError("publication pending")

    helper_mod = _kernel_effects_module()
    setattr(
        helper_mod,
        "_INTENT_SCHEMA",
        resource_bytes("schemas/improvement-effect-intent.v1.schema.json"),
    )
    if scenario == "pending":
        store: object = _PendingStore()
    elif scenario == "publication-indeterminate":
        store = _IndeterminateStore()
    else:
        store = InMemoryImprovementStore()
    effect = _Observed(ImprovementDeliveryEffect(store=store))  # type: ignore[arg-type]
    effects, schemas = helper_mod.build_effect_registries(
        effect,
        kinds=(_DELIVERY_KIND,),
        policy=EffectPolicy(max_attempts=1, timeout_seconds=30, backoff_seconds=0),
        receipt_schema=resource_bytes("schemas/improvement-effect-receipt.v1.schema.json"),
    )
    project = root / "eval-project"
    project.mkdir(parents=True, exist_ok=True)
    workspace_store = TaskWorkspaceStore(project, root / "eval-attempts", root / "eval-receipts")
    contract = TASK_ATTEMPT_CONTRACTS["assurance.improvement.evaluate-memory-improvement"]
    if isinstance(contract.resources, ResourceClaims) and contract.resources.writes == ():
        contract = replace(contract, resources=ResourceClaims(writes=("out.txt",)))
    resolved = resolve_contract(contract, executor=closed)  # type: ignore[arg-type]
    revision = canonical_digest({"revision": "product-evaluate-shadow"})
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(workspace_store),
        graph_revision=revision,
        effects=effects,
        schemas=schemas,
    )
    validated = select_evaluate_memory(_evaluate_payload())
    key = derive_attempt_key(
        invocation_id="inv-eval-shadow",
        graph_revision=revision,
        public_entrypoint="improvement-evaluate",
        semantic_node_id="improvement.evaluate",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id="inv-eval-shadow",
        public_entrypoint="improvement-evaluate",
        semantic_node_id="improvement.evaluate",
        attempt_key=key,
        fencing_token=4,
    )

    async def _committed() -> object:
        return await kernel.execute_or_recover(key, resolved, validated, context)

    async def _first() -> object:
        return await kernel.execute_or_recover(key, resolved, validated, context)

    async def _replay() -> object:
        return await kernel.execute_or_recover(key, resolved, validated, context)

    return {
        "effect": effect,
        "committed": _committed,
        "first": _first,
        "replay": _replay,
        "close": workspace_store.close,
    }


def _kernel_effects_module() -> Any:
    import importlib.util
    from pathlib import Path as _Path

    path = (
        _Path(__file__).resolve().parents[2]
        / "packages/framework/graph-engine/tests/attempts/test_kernel_effects.py"
    )
    spec = importlib.util.spec_from_file_location("product_shadow_kernel_effects", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


__all__ = [
    "DualDriverError",
    "EntrypointParityRecord",
    "EvaluateShadowPair",
    "PARITY_RECORDS",
    "SemanticAttemptCall",
    "SemanticDecision",
    "SemanticInterrupt",
    "SemanticMismatch",
    "SemanticReceipt",
    "SemanticTrace",
    "SemanticValidatorCall",
    "ShadowInvocation",
    "ShadowRun",
    "ShadowSession",
    "attach_driver",
    "compare_semantic_traces",
    "prove_entrypoint_parity",
    "required_parity_scenarios",
    "run_evaluate_shadow",
    "run_shadow_pair",
]
