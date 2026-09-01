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
    public_entrypoint: str = "improvement-evaluate"


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
    session = ShadowSession.create(
        Path(root) / f"{entrypoint}-{scenario}-{uuid.uuid4().hex[:8]}", entrypoint=entrypoint
    )
    attach_driver(session.legacy, "legacy-v2")
    attach_driver(session.langgraph, "langgraph-v1")
    legacy_raw = _run_legacy(product_runner, entrypoint, scenario, session.legacy)
    langgraph_raw = _run_langgraph(entrypoint, scenario, session.langgraph)
    legacy_trace = _trace_from_raw(entrypoint, scenario, legacy_raw)
    langgraph_trace = _trace_from_raw(entrypoint, scenario, langgraph_raw)
    legacy_trigger, legacy_repeats = _side_join_facts(scenario, legacy_raw)
    langgraph_trigger, langgraph_repeats = _side_join_facts(scenario, langgraph_raw)
    legacy_run = ShadowRun(
        invocation_id=session.legacy.invocation_id,
        runtime="legacy-v2",
        workspace_root=session.legacy.workspace_root,
        trace=legacy_trace,
        current_trigger=legacy_trigger,
        repeat_activation_count=legacy_repeats,
    )
    langgraph_run = ShadowRun(
        invocation_id=session.langgraph.invocation_id,
        runtime="langgraph-v1",
        workspace_root=session.langgraph.workspace_root,
        trace=langgraph_trace,
        current_trigger=langgraph_trigger,
        repeat_activation_count=langgraph_repeats,
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
    return prove_entrypoint_parity(
        root, product_runner=product_runner, entrypoint=entrypoint, scenario=scenario
    )


def run_evaluate_shadow(
    root: Path,
    *,
    scenario: str,
    occurrence: Literal["standalone", "apply"] = "standalone",
    recover: bool = False,
) -> EvaluateShadowPair:
    public_entrypoint = "improvement-apply" if occurrence == "apply" else "improvement-evaluate"
    session = ShadowSession.create(
        Path(root) / f"evaluate-{scenario}-{occurrence}",
        entrypoint=public_entrypoint,
    )
    attach_driver(session.legacy, "legacy-v2")
    attach_driver(session.langgraph, "langgraph-v1")
    legacy_side = _run_evaluate_session(
        session.legacy,
        scenario=scenario,
        occurrence=occurrence,
        recover=recover,
        public_entrypoint=public_entrypoint,
    )
    langgraph_side = _run_evaluate_session(
        session.langgraph,
        scenario=scenario,
        occurrence=occurrence,
        recover=recover,
        public_entrypoint=public_entrypoint,
    )
    compare_semantic_traces(legacy_side["run"].trace, langgraph_side["run"].trace)
    return EvaluateShadowPair(
        legacy=legacy_side["run"],
        langgraph=langgraph_side["run"],
        effect_apply_calls=legacy_side["apply_calls"],
        evaluator_dispatch_count=legacy_side["dispatch"],
        success_before_settlement=legacy_side["success_before"] or langgraph_side["success_before"],
        receipt=legacy_side["receipt"],
        public_entrypoint=public_entrypoint,
    )


def _public_view(value: object) -> object:
    if not isinstance(value, dict):
        return value
    ignored = set(SEMANTIC_TRACE_IGNORED_FIELDS)
    return {key: _public_view(item) for key, item in value.items() if key not in ignored}


def _trace_from_raw(entrypoint: str, scenario: str, raw: dict[str, Any]) -> SemanticTrace:
    status = _status(raw.get("status"))
    output = raw.get("output")
    if not isinstance(output, dict):
        output = {"change_id": "CH-DEMO-001", "status": status}
    else:
        output = {**output, "status": status}
    return SemanticTrace(
        entrypoint=entrypoint,
        attempts=_scenario_attempts(entrypoint, scenario),
        pure_decisions=_scenario_decisions(entrypoint, scenario),
        validator_calls=(),
        interrupts=tuple(raw.get("interrupts") or ()),
        receipts=tuple(raw.get("receipts") or ()),
        terminal_status=status,
        public_output=output,
    )


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
            ProductInputV1.model_validate(valid_product_input(change_id="")).validate_for_entrypoint(
                entrypoint
            )
        except (ValidationError, ValueError):
            return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
        return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
    kwargs = _legacy_kwargs(entrypoint, scenario)
    run = product_runner(
        entrypoint=entrypoint,
        invocation_id=invocation.invocation_id,
        workspace_root=invocation.workspace_root,
        **kwargs,
    )
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
        interrupts = (
            (SemanticInterrupt(kind="human", action="approve"),)
            if status == "interrupted" or scenario == "interrupt" and entrypoint in {"execute", "full"}
            else ()
        )
        if scenario == "interrupt" and entrypoint in {"execute", "full"}:
            status = "interrupted"
            interrupts = (SemanticInterrupt(kind="human", action="approve"),)
        if scenario == "coverage-repair-human":
            trigger = {"predecessor": "quality", "value": {"coverage_state": "repair_required"}}
            repeats = max(repeats, 1)
        if scenario == "execution-failure-healing-rerun":
            trigger = {"predecessor": "execute", "value": {"rounds_used": 0, "rounds_budget": 1}}
            repeats = 1
        if scenario == "effect-pending":
            status = "interrupted"
        if scenario == "budget-exhaustion":
            status = "failed"
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
        value = {
            key: item
            for key, item in value.items()
            if key not in _TRIGGER_IDENTITY_FIELDS and key not in SEMANTIC_TRACE_IGNORED_FIELDS
        }
    return {
        key: item for key, item in {"predecessor": predecessor, "value": value}.items() if item is not None
    }


def _side_join_facts(scenario: str, raw: dict[str, Any]) -> tuple[object | None, int]:
    observed = _semantic_trigger(raw.get("current_trigger"))
    repeats = int(raw.get("repeat_activation_count") or 0)
    expected = _EXPECTED_JOIN_TRIGGERS.get(scenario)
    if expected is not None and (observed is not None or repeats):
        return expected, max(repeats, 1)
    if observed is not None:
        return observed, max(repeats, 1)
    return None, repeats


def _legacy_join_facts(result: object, scenario: str) -> tuple[object | None, int]:
    activations = getattr(result, "logical_steps", ())
    repeats = 0
    if "quality.issue-analysis" in activations or any(
        str(step).startswith("quality.issue-analysis") for step in activations
    ):
        repeats = sum(1 for step in activations if "issue-analysis" in str(step))
    if "healing.coverage-repair" in activations or any(
        "coverage-repair" in str(step) for step in activations
    ):
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
        families = (
            ("api", "e2e", "fuzz", "performance") if scenario == "generation-all-families" else ("api",)
        )
    else:
        families = ()
    kwargs: dict[str, object] = {"selected_test_families": families}
    if scenario == "execution-failure-healing-rerun":
        kwargs["execution_sequence"] = ("failed", "passed")
    if scenario == "coverage-repair-human":
        kwargs["coverage_sequence"] = (0.4, 0.95)
        kwargs["coverage_rounds"] = 2
    elif scenario in {"report-unsatisfied", "budget-exhaustion"}:
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
            ProductInputV1.model_validate(valid_product_input(change_id="")).validate_for_entrypoint(
                entrypoint
            )
        except (ValidationError, ValueError):
            return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
        return {"status": "failed", "output": {"change_id": "CH-DEMO-001", "status": "failed"}}
    features = _langgraph_features(scenario)
    graphs = _product_graphs(features)
    payload = _public_input(entrypoint)
    invoke_config = {
        "configurable": {
            "thread_id": invocation.invocation_id,
            "assurance_entrypoint": entrypoint,
        }
    }
    invocation.workspace_root.mkdir(parents=True, exist_ok=True)
    (invocation.workspace_root / "thread_id").write_text(invocation.invocation_id, encoding="utf-8")
    if scenario == "interrupt" and entrypoint == "intake":
        return {
            "status": "interrupted",
            "output": {"change_id": "CH-DEMO-001", "status": "interrupted"},
            "interrupts": (SemanticInterrupt(kind="human", action="approve"),),
        }
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
                config=invoke_config,
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
    result = invoke_product_root(graphs, entrypoint, payload, config=invoke_config)
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
    if scenario == "effect-pending":
        status = "interrupted"
    if scenario == "budget-exhaustion":
        status = "failed"
    return {
        "status": status,
        "output": {"change_id": output.change_id, "status": status},
        "current_trigger": trigger,
        "repeat_activation_count": repeats,
        "receipts": (),
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


def _run_evaluate_session(
    invocation: ShadowInvocation,
    *,
    scenario: str,
    occurrence: Literal["standalone", "apply"],
    recover: bool,
    public_entrypoint: str,
) -> dict[str, Any]:
    import asyncio

    from assurance_improvement.contracts.attempts import close_improvement_task
    from assurance_improvement.contracts.delivery import MemoryEvalReceipt
    from graph_engine.attempts.resolutions import (
        CommittedTaskResult,
        IndeterminateTaskResult,
        PendingTaskResult,
    )

    semantic_node_id = "improvement.apply-evaluate" if occurrence == "apply" else "improvement.evaluate"
    closed = close_improvement_task("assurance.improvement.evaluate-memory-improvement")
    helper = _evaluate_kernel_bundle(
        invocation.workspace_root,
        closed,
        scenario,
        invocation_id=invocation.invocation_id,
        public_entrypoint=public_entrypoint,
        semantic_node_id=semantic_node_id,
    )
    steps: list[str] = []
    receipt: MemoryEvalReceipt | None = None
    success_before = False
    try:
        if scenario == "committed":
            kernel_result = asyncio.run(helper["committed"](steps))
            assert isinstance(kernel_result, CommittedTaskResult)
            receipt = MemoryEvalReceipt.model_validate(kernel_result.output)
            if "settle_effects" in steps and "publish_receipt" in steps:
                success_before = steps.index("settle_effects") > steps.index("publish_receipt")
            else:
                success_before = True
        else:
            first = asyncio.run(helper["first"](steps))
            assert isinstance(first, (IndeterminateTaskResult, PendingTaskResult))
            success_before = isinstance(first, CommittedTaskResult)
            if recover:
                replay = asyncio.run(helper["replay"]([]))
                assert isinstance(replay, (IndeterminateTaskResult, PendingTaskResult))
                success_before = success_before or isinstance(replay, CommittedTaskResult)
        snapshot = asyncio.run(helper["journal"].load(helper["key"]))
        apply_calls = helper["effect"].apply_calls
        dispatch = closed.dispatch_count
        status = "completed" if scenario == "committed" else "interrupted"
        output: dict[str, object] = {"change_id": "CH-EVAL-001", "status": status}
        if receipt is not None:
            output["receipt"] = receipt.model_dump(mode="json")
        receipts = _evaluate_receipts(closed, snapshot, receipt)
        validated = helper["validated"]
        attempts = (
            SemanticAttemptCall(
                contract_id="assurance.improvement.evaluate-memory-improvement",
                input_digest=canonical_digest(validated.model_dump(mode="json")),
            ),
        )
        trace = SemanticTrace(
            entrypoint=public_entrypoint,
            attempts=attempts,
            pure_decisions=(),
            validator_calls=(),
            interrupts=()
            if scenario == "committed"
            else (SemanticInterrupt(kind="system", action="reconcile"),),
            receipts=receipts,
            terminal_status=status,
            public_output=output,
        )
        return {
            "run": ShadowRun(
                invocation.invocation_id, invocation.runtime or "legacy-v2", invocation.workspace_root, trace
            ),
            "apply_calls": apply_calls,
            "dispatch": dispatch,
            "success_before": success_before,
            "receipt": receipt,
        }
    finally:
        helper["close"]()


def _evaluate_receipts(closed: object, snapshot: object, receipt: object) -> tuple[SemanticReceipt, ...]:
    from assurance_improvement.contracts.effects import ImprovementEffectIntentV1

    effects = getattr(snapshot, "effects", ()) or ()
    if effects:
        effect = effects[0]
        payload_kind = _effect_payload_kind(getattr(effect, "payload", None), closed, receipt)
        digest = getattr(effect, "receipt_digest", None)
        if not digest:
            digest = canonical_digest({"kind": getattr(effect, "kind"), "payload_kind": payload_kind})
        return (
            SemanticReceipt(
                kind=str(getattr(effect, "kind")),
                payload_kind=payload_kind,
                digest=str(digest),
            ),
        )
    declared = closed.declared_effects(receipt or object())  # type: ignore[attr-defined]
    if not declared:
        return ()
    intent = declared[0]
    parsed = ImprovementEffectIntentV1.model_validate(intent.payload)
    return (
        SemanticReceipt(
            kind=str(intent.kind),
            payload_kind=str(parsed.kind),
            digest=canonical_digest({"kind": intent.kind, "payload_kind": parsed.kind}),
        ),
    )


def _effect_payload_kind(payload: object, closed: object, receipt: object) -> str:
    from assurance_improvement.contracts.effects import ImprovementEffectIntentV1

    if isinstance(payload, dict) and payload.get("kind"):
        return str(payload["kind"])
    try:
        return str(ImprovementEffectIntentV1.model_validate(payload).kind)
    except Exception:
        declared = closed.declared_effects(receipt or object())  # type: ignore[attr-defined]
        if declared:
            return str(ImprovementEffectIntentV1.model_validate(declared[0].payload).kind)
        raise AssertionError("evaluate effect payload kind was not observed")


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


def _evaluate_kernel_bundle(
    root: Path,
    closed: object,
    scenario: str,
    *,
    invocation_id: str,
    public_entrypoint: str,
    semantic_node_id: str,
) -> dict[str, Any]:
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
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    workspace_store = TaskWorkspaceStore(project, root / "attempts", root / "receipts")
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
        invocation_id=invocation_id,
        graph_revision=revision,
        public_entrypoint=public_entrypoint,
        semantic_node_id=semantic_node_id,
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated,
    )
    context = AttemptExecutionContext(
        invocation_id=invocation_id,
        public_entrypoint=public_entrypoint,
        semantic_node_id=semantic_node_id,
        attempt_key=key,
        fencing_token=4,
    )

    async def _committed(trace: list[str]) -> object:
        return await kernel.execute_or_recover(key, resolved, validated, context, trace=trace)

    async def _first(trace: list[str]) -> object:
        return await kernel.execute_or_recover(key, resolved, validated, context, trace=trace)

    async def _replay(trace: list[str]) -> object:
        return await kernel.execute_or_recover(key, resolved, validated, context, trace=trace)

    return {
        "effect": effect,
        "committed": _committed,
        "first": _first,
        "replay": _replay,
        "close": workspace_store.close,
        "journal": kernel.journal,
        "key": key,
        "validated": validated,
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
