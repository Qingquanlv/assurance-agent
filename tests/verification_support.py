from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1


FIXTURES = Path(__file__).parent / "fixtures" / "verification"


def read_fixture(name: str) -> dict[str, object]:
    if name not in {"user-case.json", "user-sources.json", "user-plan.json"}:
        raise ValueError("unknown verification fixture")
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def weaken_user_machine_plan(
    plan_set: CaseExecutionPlanSetV1,
    mutation: str,
) -> CaseExecutionPlanSetV1:
    case = plan_set.cases[0]
    if mutation == "delete_db_oracle":
        obligations = tuple(
            obligation for obligation in case.oracle.assertion_obligations if obligation != "user.email"
        )
        assert obligations != case.oracle.assertion_obligations
        weakened = case.model_copy(
            update={"oracle": case.oracle.model_copy(update={"assertion_obligations": obligations})}
        )
    elif mutation == "required_to_optional":
        required = tuple(obligation for obligation in case.required if obligation != "user.email")
        assert required != case.required
        weakened = case.model_copy(update={"required": required})
    else:  # pragma: no cover - closed test table
        raise AssertionError(mutation)
    return plan_set.model_copy(update={"cases": (weakened, *plan_set.cases[1:])})


def with_weakened_user_machine_plan(
    admit: Callable[..., Any],
    mutation: str,
) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        admission = admit(*args, **kwargs)
        return replace(
            admission,
            machine_plans=weaken_user_machine_plan(admission.machine_plans, mutation),
        )

    return wrapped


__all__ = ["read_fixture", "weaken_user_machine_plan", "with_weakened_user_machine_plan"]
