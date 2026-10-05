"""Full product flow, compiled and mounted as the production ``full`` root."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from langchain_core.runnables.config import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from assurance_intake.graphs.factory import build_intake_graphs
from assurance_product.graphs.full import build_full_flow
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.testing.graph_harness import GraphHarness, _prepare_anchored_backend, committed
from tests.product.test_execute_tail_flow import (
    _SHA,
    _features,
    _HANDOFF_ARTIFACT,
    _committed_issue,
    _materialize,
    _report_output,
)
from tests.product.test_product_stategraph_flow import (
    _PLAN_DIGEST,
    _execution,
    _generation,
    _inspection,
    _plan_ref,
    _public_input,
    _ref,
    _reviewed,
)
from tests.product.test_stategraph_entrypoints import _contracts_for

pytestmark = pytest.mark.usefixtures("installed_sources")

_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _failure() -> PermanentTaskFailure:
    return PermanentTaskFailure(kind="invalid_output", message="scripted failure")


def _bundles(harness: GraphHarness) -> object:
    features = _features(harness)
    features["assurance.intake"] = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts_for("assurance.intake"))
    )

    class Bundles:
        quality = features["assurance.quality"]
        generation = features["assurance.generation"]
        execution = features["assurance.execution"]
        healing = features["assurance.healing"]
        improvement = features["assurance.improvement"]
        intake = features["assurance.intake"]

    return Bundles()


def _policy() -> dict[str, object]:
    from assurance_intake.contracts.plan import TestFamilyPolicyV1

    return TestFamilyPolicyV1(required=("api",), allowed=("api",)).model_dump(mode="json")


def _input(**overrides: object) -> dict[str, object]:
    public = _public_input(
        "full",
        capability_leafs=("auth.session",),
        budgets={
            "review_rounds": 2,
            "coverage_rounds": 1,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    )
    public["family_policy"] = _policy()
    public.update(overrides)
    return public


def _surface(readiness: str = "ready") -> dict[str, object]:
    return {
        "readiness": readiness,
        "ui_exploration_ref": _ref("qa/results/facts/ui-exploration.json").model_dump(mode="json"),
        "api_discovery_ref": _ref("qa/results/facts/api-discovery.json").model_dump(mode="json"),
        "ui_source": "unused",
        "api_source": "live",
    }


def _surface_commit(readiness: str = "ready") -> object:
    payload = _surface(readiness)
    artifacts = [
        cast(dict[str, object], payload["ui_exploration_ref"]),
        cast(dict[str, object], payload["api_discovery_ref"]),
    ]
    return committed(payload, _RECEIPT, artifacts=artifacts)


def _plan() -> dict[str, object]:
    ref = _plan_ref().model_dump(mode="json")
    return {
        "plan_ref": ref,
        "plan": {"plan_digest": _PLAN_DIGEST, "selected_test_families": ["api"]},
        "preparation_refs": [ref],
    }


_INSPECT_ARTIFACTS = [
    {"path": "qa/results/inspect/inspection-outcome.json", "digest": _SHA},
    {"path": "qa/results/inspect/coverage-rework-handoff.json", "digest": _SHA},
]
_REVIEWED_CASE_ARTIFACT = {"path": "qa/cases/reviewed-case.json", "digest": _SHA}
_EXECUTION_CYCLE_ARTIFACT = {"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}


def _inspect_commit(payload: object) -> object:
    return committed(payload, _RECEIPT, artifacts=list(_INSPECT_ARTIFACTS))


def _plan_commit() -> object:
    return committed(_plan(), _RECEIPT, artifacts=[_plan_ref().model_dump(mode="json")])


def _review(outcome: str, *, epoch: int = 0) -> dict[str, object]:
    return {
        "public_outcome": outcome,
        "reviewed_case": _reviewed(epoch).model_dump(mode="json"),
    }


def _front(script: dict[str, list[object]]) -> None:
    script.setdefault("quality.surface-baseline", [_surface_commit()])
    script.setdefault("intake.intake", [committed({"artifacts": []}, _RECEIPT)])
    script.setdefault(
        "intake.explore",
        [
            committed(
                {"artifacts": []},
                _RECEIPT,
                artifacts=[
                    {"path": "qa/results/explore/exploration.json", "digest": _SHA},
                    {"path": "qa/results/explore/impact-inventory.json", "digest": _SHA},
                ],
            )
        ],
    )
    script.setdefault("intake.resolve-plan", [_plan_commit()])
    script.setdefault(
        "generation.init-test-runtime",
        [committed({"schema_version": "1", "change_id": "CH-DEMO-001"}, _RECEIPT)],
    )


def _case(script: dict[str, list[object]], *outcomes: str, epochs: tuple[int, ...] = ()) -> None:
    if not outcomes:
        outcomes = ("pass",)
    script.setdefault(
        "intake.case-design",
        [committed({"artifacts": []}, _RECEIPT) for _ in outcomes],
    )
    script.setdefault(
        "intake.case-review",
        [
            committed(
                _review(outcome, epoch=epochs[index] if index < len(epochs) else 0),
                _RECEIPT,
                artifacts=[{"path": "qa/cases/reviewed-case.json", "digest": _SHA}],
            )
            for index, outcome in enumerate(outcomes)
        ],
    )


def _round_generation(epoch: int) -> dict[str, list[object]]:
    published = {"generation_result": _generation(epoch)["generation_result"]}
    return {
        "generation.resolve-inputs": [committed(_reviewed(epoch).model_dump(mode="json"), _RECEIPT)],
        "generation.api.codegen": [committed({"schema_version": "1"}, _RECEIPT)],
        "generation.api.codegen-review": [committed({"route": "codegen"}, _RECEIPT)],
        "generation.publish-cycle": [
            committed(
                published,
                _RECEIPT,
                artifacts=[{"path": "qa/results/codegen/generation-cycle.json", "digest": _SHA}],
            )
        ],
    }


def _mapping(payload: Mapping[str, object], key: str) -> dict[str, object]:
    raw = payload[key]
    if not isinstance(raw, dict):
        raise TypeError(key)
    return dict(raw)


def _round_execution(epoch: int) -> dict[str, object]:
    execution = _mapping(_execution(epoch), "execution_result")
    return {
        "admission": "committed",
        "batch_id": execution["batch_id"],
        "execution_evidence": {"status": "PASS"},
        "execution_digest": _SHA,
        "execution_semantic_node_id": "execution.execute",
        "family_outcomes": [{"family": "api", "state": "executed"}],
        "execution_result": execution,
    }


def _round_inspect(disposition: str, epoch: int, *, repair_round: int = 0) -> dict[str, object]:
    inspection = _inspection(epoch, disposition, repair_round=repair_round)
    return {
        "disposition": disposition,
        "inspection_outcome": inspection["inspection_outcome"],
        "assessment": inspection["assessment_inputs"],
        "evidence_refs": [{"path": "qa/results/inspect/extra-evidence.json", "digest": "1" * 64}],
        "coverage_state": inspection["coverage_state"],
        "observations_ref": inspection["observations_ref"],
        "issue_evidence_manifest_ref": inspection["issue_evidence_manifest_ref"],
        "owned_evidence_ids": inspection["owned_evidence_ids"],
        "evidence_bundle_digest": inspection["evidence_bundle_digest"],
    }


def _tail_until_inspect(script: dict[str, list[object]], *rounds: tuple[str, int]) -> None:
    if not rounds:
        rounds = (("satisfied", 0),)
    fact = committed(
        {"fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}},
        _RECEIPT,
        artifacts=[{"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}],
    )
    base: dict[str, list[object]] = {
        "quality.fact-baseline": [fact for _ in rounds],
        "generation.resolve-inputs": [],
        "generation.api.codegen": [],
        "generation.api.codegen-review": [],
        "generation.publish-cycle": [],
    }
    for _disposition, epoch in rounds:
        for key, values in _round_generation(epoch).items():
            base[key].extend(values)
    base["execution.execute"] = [
        committed(
            _round_execution(epoch),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        )
        for _, epoch in rounds
    ]
    base["quality.materialize-assessment-inputs"] = [_materialize() for _ in rounds]
    base["quality.inspect"] = [
        committed(
            _round_inspect(disposition, epoch),
            _RECEIPT,
            artifacts=list(_INSPECT_ARTIFACTS),
        )
        for disposition, epoch in rounds
    ]
    for key, values in base.items():
        script.setdefault(key, values)


def _reported(script: dict[str, list[object]]) -> None:
    script.setdefault("quality.report", [committed(_report_output("reported"), _RECEIPT)])


class _Run:
    def __init__(
        self,
        result: object,
        captured: list[tuple[str, object]],
        *,
        state: Mapping[str, object] | None = None,
    ) -> None:
        self.result = result
        self.captured = captured
        self.state = state if state is not None else {}

    @property
    def outcome(self) -> object:
        if isinstance(self.result, Mapping):
            return self.result.get("flow_outcome")
        return None


async def _invoke(
    script: Mapping[str, Sequence[object]],
    *,
    resumes: tuple[Mapping[str, object], ...] = (),
    **overrides: object,
) -> _Run:
    harness = GraphHarness()
    flow = build_full_flow(_bundles(harness))
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    compiled = flow.compile(
        EngineGraphBuildContext(contracts={}, checkpointer=backend, approved_source_roots=())
    )
    harness._kernel.load_script(cast(Any, script))
    captured: list[tuple[str, object]] = []
    original = harness._kernel.execute_or_recover

    async def spy(attempt_key: Any, contract: Any, validated_input: Any, context: Any) -> Any:
        captured.append((str(getattr(context, "semantic_node_id")), validated_input))
        return await original(attempt_key, contract, validated_input, context)

    harness._kernel.execute_or_recover = spy  # type: ignore[method-assign]
    config: RunnableConfig = {
        "recursion_limit": 8192,
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "full",
        },
    }

    async def drive(value: object) -> object:
        try:
            return await compiled.ainvoke(value, config=config)
        except GraphInterrupt as error:
            return {"__interrupt__": error.args}

    result = await drive(_input(**overrides))
    for payload in resumes:
        if not isinstance(result, Mapping) or "__interrupt__" not in result:
            break
        result = await drive(Command(resume=payload))
    snapshot = await compiled.aget_state(config)
    values = getattr(snapshot, "values", {})
    state = dict(values) if isinstance(values, Mapping) else {}
    return _Run(result, captured, state=state)


def _public(result: object) -> Mapping[str, object]:
    assert isinstance(result, Mapping)
    return result


def test_full_flow_outcomes() -> None:
    asyncio.run(_outcomes())


async def _outcomes() -> None:
    achieved = {}
    _front(achieved)
    _case(achieved)
    _tail_until_inspect(achieved, ("satisfied", 0))
    _reported(achieved)
    done = await _invoke(achieved)
    assert done.outcome == "achieved"
    public = _public(done.result)
    assert public["status"] == "completed"
    assert public["terminal"] == {"status": "completed", "reason": "achieved"}
    assert public["output"] == {
        "change_id": "CH-DEMO-001",
        "status": "completed",
        "receipts": [],
    }
    assert public["receipts"] == []

    second = {}
    _front(second)
    _case(second, "pass", "pass", epochs=(0, 1))
    _tail_until_inspect(second, ("coverage_insufficient", 0), ("satisfied", 1))
    second["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    second["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    reworked = await _invoke(second)
    assert reworked.outcome == "achieved"
    design = [item for name, item in reworked.captured if name == "intake.case-design"]
    assert len(design) == 2
    first_design = cast(Any, design[0])
    assert first_design.plan_ref == _plan_ref()
    assert first_design.ui_exploration_ref == _ref("qa/results/facts/ui-exploration.json")
    assert first_design.api_discovery_ref == _ref("qa/results/facts/api-discovery.json")
    assert getattr(design[0], "rework_ref", None) is None
    assert getattr(design[1], "rework_ref").path == "qa/results/cases/case-rework-context.json"

    exhausted = {}
    _front(exhausted)
    _case(exhausted)
    _tail_until_inspect(exhausted, ("coverage_insufficient", 0))
    stopped = await _invoke(
        exhausted,
        budgets={
            "review_rounds": 2,
            "coverage_rounds": 0,
            "healing_rounds": 1,
            "execution_retries": 1,
        },
    )
    assert stopped.outcome == "not_achieved"
    assert [name for name, _item in stopped.captured].count("intake.case-design") == 1
    assert _public(stopped.result)["terminal"] == {"status": "failed", "reason": "not_achieved"}

    surface = {"quality.surface-baseline": [_surface_commit("not_ready")]}
    assert (await _invoke(surface)).outcome == "not_achieved"
    failed_surface = {"quality.surface-baseline": [_failure()]}
    assert (await _invoke(failed_surface)).outcome == "not_achieved"

    prepare = {}
    _front(prepare)
    prepare["intake.intake"] = [_failure()]
    assert (await _invoke(prepare)).outcome == "not_achieved"

    init = {}
    _front(init)
    init["generation.init-test-runtime"] = [_failure()]
    assert (await _invoke(init)).outcome == "not_achieved"

    rejected = {}
    _front(rejected)
    _case(rejected, "reject")
    assert (await _invoke(rejected)).outcome == "not_achieved"

    failed_case = {}
    _front(failed_case)
    failed_case["intake.case-design"] = [_failure()]
    assert (await _invoke(failed_case)).outcome == "not_achieved"

    review_failed = {}
    _front(review_failed)
    _case(review_failed)
    review_failed["intake.case-review"] = [_failure()]
    assert (await _invoke(review_failed)).outcome == "not_achieved"

    budget = {}
    _front(budget)
    budget["intake.case-design"] = [committed({"artifacts": []}, _RECEIPT)]
    budget["intake.case-review"] = [committed(_review("needs_fix"), _RECEIPT)]
    assert (
        await _invoke(
            budget,
            budgets={
                "review_rounds": 0,
                "coverage_rounds": 1,
                "healing_rounds": 1,
                "execution_retries": 1,
            },
        )
    ).outcome == "not_achieved"

    for disposition in ("needs_human", "blocked"):
        tail = {}
        _front(tail)
        _case(tail)
        _tail_until_inspect(tail, (disposition, 0))
        if disposition == "blocked":
            tail["quality.issue-analyze"] = [_failure()]
        assert (await _invoke(tail)).outcome == "not_achieved"

    diagnostic = {}
    _front(diagnostic)
    _case(diagnostic)
    _tail_until_inspect(diagnostic, ("analysis_required", 0))
    diagnostic["quality.issue-analyze"] = [_committed_issue("report_issue")]
    diagnostic["quality.report"] = [_failure()]
    assert (await _invoke(diagnostic)).outcome == "not_achieved"

    rework_failed = {}
    _front(rework_failed)
    _case(rework_failed)
    _tail_until_inspect(rework_failed, ("coverage_insufficient", 0))
    rework_failed["intake.coverage-rework"] = [_failure()]
    assert (await _invoke(rework_failed)).outcome == "not_achieved"


def test_a_new_coverage_round_resets_inner_loop_counters() -> None:
    asyncio.run(_counters())


async def _counters() -> None:
    script: dict[str, list[object]] = {}
    _front(script)
    script["intake.case-design"] = [committed({"artifacts": []}, _RECEIPT) for _ in range(3)]
    script["intake.case-repair"] = [committed({"artifacts": []}, _RECEIPT)]
    script["intake.case-review"] = [
        committed(_review("needs_fix", epoch=0), _RECEIPT, artifacts=[_REVIEWED_CASE_ARTIFACT]),
        committed(_review("pass", epoch=0), _RECEIPT, artifacts=[_REVIEWED_CASE_ARTIFACT]),
        committed(_review("pass", epoch=1), _RECEIPT, artifacts=[_REVIEWED_CASE_ARTIFACT]),
    ]
    _tail_until_inspect(script, ("coverage_insufficient", 0), ("satisfied", 1))
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    _reported(script)
    done = await _invoke(script)
    assert done.outcome == "achieved"
    rounds = [
        getattr(item, "review_round")
        for name, item in done.captured
        if name == "intake.case-review" and hasattr(item, "review_round")
    ]
    assert rounds == [0, 1, 0]


def test_repair_reads_issue_analysis_only_inside_its_coverage_epoch() -> None:
    asyncio.run(_repair_epoch_handoff())


async def _repair_epoch_handoff() -> None:
    """Epoch 0's analysis stays on the ledger; epoch 1's repair has not analyzed yet."""
    script: dict[str, list[object]] = {}
    _front(script)
    _case(script, "pass", "pass", epochs=(0, 1))
    _issue_then_coverage(script)
    _one_repair(script, 1, "satisfied")
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    approval = {
        "action": "approve",
        "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
    }
    done = await _invoke(script, resumes=(approval,))
    proposals = [item for name, item in done.captured if name == "healing.fix-proposal"]
    assert len(proposals) == 2
    first, second = proposals
    assert getattr(first, "coverage_epoch") == 0
    assert getattr(first, "issue_analysis_handoff_ref").model_dump(mode="json") == _HANDOFF_ARTIFACT
    assert getattr(second, "coverage_epoch") == 1
    assert getattr(second, "issue_analysis_handoff_ref").model_dump(mode="json") == _HANDOFF_ARTIFACT


def _issue_then_coverage(script: dict[str, list[object]]) -> None:
    for key, values in _round_generation(0).items():
        _extend(script, key, *values)
    _extend(
        script,
        "quality.fact-baseline",
        committed(
            {"fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}],
        ),
    )
    _extend(
        script,
        "execution.execute",
        committed(
            _round_execution(0),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
    )
    _extend(script, "quality.materialize-assessment-inputs", _materialize(), _materialize())
    _extend(
        script,
        "quality.inspect",
        _inspect_commit(_round_inspect("analysis_required", 0)),
        _inspect_commit(_round_inspect("coverage_insufficient", 0, repair_round=1)),
    )
    _extend(script, "quality.issue-analyze", _committed_issue("fix_eligible", handoff=True))
    _extend(
        script,
        "healing.fix-proposal",
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        ),
    )
    generated = _mapping(_generation(0), "generation_result")
    sources = generated["source_refs"]
    mapping = generated["mapping_ref"]
    if not isinstance(sources, list) or not sources or not isinstance(mapping, dict):
        raise TypeError("generation result")
    source = sources[0]
    if not isinstance(source, dict):
        raise TypeError("generation source")
    _extend(
        script,
        "healing.apply-test-repair",
        committed(
            {
                "change_id": "CH-DEMO-001",
                "plan_digest": _PLAN_DIGEST,
                "plan_ref": _plan_ref().model_dump(mode="json"),
                "coverage_epoch": 0,
                "repair_round": 1,
                "changed_test_refs": [{"path": source["path"], "digest": "d" * 64}],
                "mapping_ref": {"path": mapping["path"], "digest": "e" * 64},
            },
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        ),
    )
    rerun = _mapping(_execution(0, repair_round=1), "execution_result")
    _extend(
        script,
        "execution.run",
        committed(
            {
                "admission": "committed",
                "batch_id": rerun["batch_id"],
                "execution_evidence": {"status": "PASS"},
                "execution_digest": _SHA,
                "execution_semantic_node_id": "execution.run",
                "family_outcomes": [{"family": "api", "state": "executed"}],
                "execution_result": rerun,
            },
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
    )


def test_a_new_coverage_round_resets_the_healing_loop() -> None:
    asyncio.run(_healing_counters())


def _extend(script: dict[str, list[object]], key: str, *values: object) -> None:
    script.setdefault(key, []).extend(values)


def _one_repair(script: dict[str, list[object]], epoch: int, follow: str) -> None:
    for key, values in _round_generation(epoch).items():
        _extend(script, key, *values)
    _extend(
        script,
        "quality.fact-baseline",
        committed(
            {"fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}],
        ),
    )
    _extend(
        script,
        "execution.execute",
        committed(
            _round_execution(epoch),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
    )
    _extend(script, "quality.materialize-assessment-inputs", _materialize(), _materialize())
    _extend(
        script,
        "quality.inspect",
        _inspect_commit(_round_inspect("repairable_execution_failure", epoch)),
        _inspect_commit(_round_inspect(follow, epoch, repair_round=1)),
    )
    _extend(
        script,
        "healing.fix-proposal",
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        ),
    )
    generated = _mapping(_generation(epoch), "generation_result")
    sources = generated["source_refs"]
    mapping = generated["mapping_ref"]
    if not isinstance(sources, list) or not sources or not isinstance(mapping, dict):
        raise TypeError("generation result")
    source = sources[0]
    if not isinstance(source, dict):
        raise TypeError("generation source")
    _extend(
        script,
        "healing.apply-test-repair",
        committed(
            {
                "change_id": "CH-DEMO-001",
                "plan_digest": _PLAN_DIGEST,
                "plan_ref": _plan_ref().model_dump(mode="json"),
                "coverage_epoch": epoch,
                "repair_round": 1,
                "changed_test_refs": [{"path": source["path"], "digest": "d" * 64}],
                "mapping_ref": {"path": mapping["path"], "digest": "e" * 64},
            },
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        ),
    )
    rerun = _mapping(_execution(epoch, repair_round=1), "execution_result")
    _extend(
        script,
        "execution.run",
        committed(
            {
                "admission": "committed",
                "batch_id": rerun["batch_id"],
                "execution_evidence": {"status": "PASS"},
                "execution_digest": _SHA,
                "execution_semantic_node_id": "execution.run",
                "family_outcomes": [{"family": "api", "state": "executed"}],
                "execution_result": rerun,
            },
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
    )


async def _healing_counters() -> None:
    script: dict[str, list[object]] = {}
    _front(script)
    _case(script, "pass", "pass", epochs=(0, 1))
    _one_repair(script, 0, "coverage_insufficient")
    _one_repair(script, 1, "satisfied")
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    _reported(script)
    approval = {
        "action": "approve",
        "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
    }
    done = await _invoke(script, resumes=(approval, approval))
    assert done.outcome == "achieved"
    repair_rounds = [
        getattr(item, "repair_round")
        for name, item in done.captured
        if name == "healing.apply-test-repair" and hasattr(item, "repair_round")
    ]
    assert repair_rounds == [1, 1]


def _interrupt_id(result: object) -> str:
    assert isinstance(result, Mapping)
    interrupts = result.get("__interrupt__")
    assert interrupts
    first = interrupts[0]
    if isinstance(first, tuple):
        first = first[0]
    value = getattr(first, "value", first)
    assert isinstance(value, dict)
    return str(value["interrupt_id"])


def test_mounted_full_root_uses_full_interrupt_ids() -> None:
    asyncio.run(_mounted_interrupt_ids())


async def _mounted_interrupt_ids() -> None:
    case: dict[str, list[object]] = {}
    _front(case)
    case["intake.case-design"] = [committed({"artifacts": []}, _RECEIPT)]
    case["intake.case-review"] = [committed(_review("needs_human"), _RECEIPT)]
    assert _interrupt_id((await _invoke(case)).result) == "full.human-review"

    healing: dict[str, list[object]] = {}
    _front(healing)
    _case(healing)
    _tail_until_inspect(healing, ("repairable_execution_failure", 0))
    healing["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        )
    ]
    assert _interrupt_id((await _invoke(healing)).result) == "full.approval"

    lane: dict[str, list[object]] = {}
    _front(lane)
    _case(lane)
    lane["quality.fact-baseline"] = [
        committed(
            {"fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}],
        )
    ]
    lane["generation.resolve-inputs"] = [committed(_reviewed(0).model_dump(mode="json"), _RECEIPT)]
    lane["generation.api.codegen"] = [committed({"schema_version": "1"}, _RECEIPT)]
    lane["generation.api.codegen-review"] = [committed({"route": "human"}, _RECEIPT)]
    assert _interrupt_id((await _invoke(lane)).result) == "full.api.human-review"
