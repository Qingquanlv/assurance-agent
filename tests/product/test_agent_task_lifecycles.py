from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.mark.parametrize(
    ("name", "contract_id"),
    (
        ("intake", "assurance.intake.agent.intake.v1"),
        ("explore", "assurance.intake.agent.explore.v1"),
        ("case-design", "assurance.intake.agent.case-design.v1"),
        ("case-review", "assurance.intake.agent.case-review.v1"),
    ),
)
def test_intake_task_phases_delegate_to_injected_phases(name: str, contract_id: str) -> None:
    from agent_runtime_contracts.lifecycle import validate_task_type

    from assurance_intake.operations.agent_tasks import CaseReviewTask, ExploreTask, IntakeTask
    from assurance_intake.operations.case_design import CaseDesignTask

    task_type = {
        "intake": IntakeTask,
        "explore": ExploreTask,
        "case-design": CaseDesignTask,
        "case-review": CaseReviewTask,
    }[name]
    calls: list[tuple[str, object, object]] = []

    class Phase:
        def __init__(self, name: str) -> None:
            self.name = name

        async def execute(self, value: object, scope: object) -> object:
            calls.append((self.name, value, scope))
            return self.name

    validate_task_type(task_type)
    assert task_type.contract.contract_id == contract_id
    task = task_type(
        prepare_phase=Phase("prepare"),
        opencode=Phase("runtime"),
        finalize_phase=Phase("finalize"),
    )
    scope = object()
    input_value = object()
    prepared = object()
    bundle = object()

    async def run_phases() -> tuple[object, object, object]:
        return (
            await task.prepare(input_value, scope),
            await task.run(prepared, scope),
            await task.finalize(bundle, scope),
        )

    assert asyncio.run(run_phases()) == ("prepare", "runtime", "finalize")
    assert calls == [
        ("prepare", input_value, scope),
        ("runtime", prepared, scope),
        ("finalize", bundle, scope),
    ]
