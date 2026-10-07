"""Bind the eligible test files, then verify the staged repair and write its history."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_execution.contracts.workflow import APPLIED_REPAIR_PATH, AppliedRepairHandoffV1
from assurance_healing.contracts.application import (
    VERIFIED_REPAIR_PATH,
    ApplyTestRepairInputV1,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.repair_input import ApplyBoundInputV1
from assurance_healing.operations.repair_input import opened_repair_input
from assurance_healing.operations.application import (
    repair_sources,
    expected_repair_history,
    repair_history_path,
    verify_application,
)


def _apply(
    ctx: PrepareContext | FinalizeContext, business: ApplyBoundInputV1, error: type[Exception]
) -> ApplyTestRepairInputV1:
    filled = opened_repair_input(ctx.project_root, business, error)
    fields = set(ApplyTestRepairInputV1.model_fields)
    return ApplyTestRepairInputV1.model_validate(
        {key: value for key, value in filled.items() if key in fields}
    )


def before(ctx: PrepareContext, business: ApplyBoundInputV1) -> ApplyTestRepairInputV1:
    skill = _apply(ctx, business, InputError)
    paths = tuple(repair_sources(skill, ctx.project_root))
    ctx.bind("qa/tests", paths)
    ctx.extra("allowed_test_paths", list(paths))
    return skill


def after(
    ctx: FinalizeContext, business: ApplyBoundInputV1, result: TestRepairResultV1
) -> VerifiedTestRepairV1:
    skill = _apply(ctx, business, OutputError)
    verified = verify_application(skill, result, ctx)
    ctx.stage(VERIFIED_REPAIR_PATH, verified)
    ctx.stage(
        APPLIED_REPAIR_PATH,
        AppliedRepairHandoffV1.model_validate(verified.model_dump(mode="json")),
    )
    history = expected_repair_history(skill, verified)
    relative = repair_history_path(
        coverage_epoch=skill.coverage_epoch,
        repair_round=skill.repair_round,
    )
    ctx.write(relative, canonical_json_bytes(cast(JSONValue, history.model_dump(mode="json"))) + b"\n")
    return verified
