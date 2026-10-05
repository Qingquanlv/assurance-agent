"""One resolved plan is reused across coverage rounds."""

from __future__ import annotations

import asyncio

from graph_engine.testing.graph_harness import committed

from tests.product.test_execute_tail_flow import _SHA
from tests.product.test_full_flow import (
    _RECEIPT,
    _case,
    _front,
    _invoke,
    _reported,
    _tail_until_inspect,
)
from tests.product.test_product_stategraph_flow import _PLAN_DIGEST


def test_one_plan_survives_two_coverage_epochs(tmp_path) -> None:
    del tmp_path

    async def run() -> None:
        script: dict[str, list[object]] = {}
        _front(script)
        _case(script, "pass", "pass", epochs=(0, 1))
        _tail_until_inspect(script, ("coverage_insufficient", 0), ("satisfied", 1))
        script["intake.coverage-rework"] = [
            committed(
                {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
                _RECEIPT,
                artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
            )
        ]
        _reported(script)
        done = await _invoke(
            script,
            budgets={
                "review_rounds": 2,
                "coverage_rounds": 1,
                "healing_rounds": 1,
                "execution_retries": 1,
            },
        )
        names = [name for name, _item in done.captured]
        assert names.count("intake.resolve-plan") == 1
        assert names.count("intake.case-design") == 2
        assert "improvement.retro" not in names
        assert done.result["terminal"] == {"status": "completed", "reason": "achieved"}  # type: ignore[index]
        designs = [item for name, item in done.captured if name == "intake.case-design"]
        assert getattr(designs[0], "plan_digest") == _PLAN_DIGEST
        assert getattr(designs[1], "plan_digest") == getattr(designs[0], "plan_digest")
        assert getattr(designs[0], "coverage_epoch") == 0
        assert getattr(designs[1], "coverage_epoch") == 1

    asyncio.run(run())
