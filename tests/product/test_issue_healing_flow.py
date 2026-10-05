"""Applied repair is the only input that reaches rerun."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from graph_engine.testing.graph_harness import committed

from assurance_execution.contracts.agent import RerunPrepareInputV1
from tests.product.test_execute_tail_flow import (
    _RECEIPT,
    _SHA,
    _budgets,
    _committed_inspect,
    _inspect_output,
    _invoke,
    _materialize,
    _report_output,
    _through_execute,
)


def test_applied_test_repair_is_the_only_path_to_rerun() -> None:
    asyncio.run(_applied_repair_reruns())


async def _applied_repair_reruns() -> None:
    from assurance_generation.contracts.workflow import GenerationCycleResultV1
    from assurance_healing.contracts.application import AppliedTestRepairV1
    from tests.product.test_execute_tail_flow import _execution_output
    from tests.product.test_product_stategraph_flow import _PLAN_DIGEST, _generation, _plan_ref, _ref

    generation = GenerationCycleResultV1.model_validate(_generation()["generation_result"])
    changed = _ref(generation.source_refs[0].path, "d" * 64)
    mapping = _ref(generation.mapping_ref.path, "e" * 64)
    verified = {
        "change_id": "CH-DEMO-001",
        "plan_digest": _PLAN_DIGEST,
        "plan_ref": _plan_ref().model_dump(mode="json"),
        "coverage_epoch": 0,
        "repair_round": 1,
        "changed_test_refs": [changed.model_dump(mode="json")],
        "mapping_ref": mapping.model_dump(mode="json"),
    }
    AppliedTestRepairV1.model_validate(
        {**verified, "status": "applied", "receipt": _RECEIPT.model_dump(mode="json")}
    )
    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize(), _materialize()]
    script["quality.inspect"] = [
        _committed_inspect("repairable_execution_failure"),
        _committed_inspect("satisfied", repair_round=1),
    ]
    script["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        )
    ]
    script["healing.apply-test-repair"] = [
        committed(
            verified,
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/applied-repair.json", "digest": _SHA}],
        )
    ]
    script["execution.run"] = [
        committed(
            _execution_output(repair_round=1),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        )
    ]
    script["quality.report"] = [committed(_report_output("reported"), _RECEIPT)]
    paused = await _invoke(script, budgets=_budgets(1))
    finished = await paused.resume(
        {
            "action": "approve",
            "approval_ref": {"path": "qa/results/healing/approval.json", "digest": _SHA},
        }
    )
    assert finished.outcome == "reported"
    apply = next(item for name, item in finished.captured if name == "healing.apply-test-repair")
    assert getattr(apply, "repair_round") == 1
    rerun = next(item for name, item in finished.captured if name == "execution.run")
    assert isinstance(rerun, RerunPrepareInputV1)
    assert rerun.apply_receipt == _RECEIPT
    assert rerun.applied_repair_ref == _ref("qa/results/healing/applied-repair.json", _SHA)


def test_fix_proposal_output_cannot_parse_as_applied_repair() -> None:
    asyncio.run(_proposal_does_not_rerun())


async def _proposal_does_not_rerun() -> None:
    script = _through_execute()
    script["quality.materialize-assessment-inputs"] = [_materialize()]
    script["quality.inspect"] = [committed(_inspect_output("repairable_execution_failure"), _RECEIPT)]
    script["healing.fix-proposal"] = [
        committed(
            {"schema_version": "1"},
            _RECEIPT,
            artifacts=[{"path": "qa/results/healing/fix-proposal.json", "digest": _SHA}],
        )
    ]
    paused = await _invoke(script, budgets=_budgets(1))
    assert isinstance(paused.result, Mapping)
    assert paused.result["__interrupt__"]
    names = [name for name, _item in paused.captured]
    assert "healing.fix-proposal" in names
    assert "healing.apply-test-repair" not in names
    assert "execution.run" not in names
    rejected = await paused.resume({"action": "reject"})
    assert rejected.outcome == "needs_human"
    names = [name for name, _item in rejected.captured]
    assert "healing.apply-test-repair" not in names
    assert "execution.run" not in names
