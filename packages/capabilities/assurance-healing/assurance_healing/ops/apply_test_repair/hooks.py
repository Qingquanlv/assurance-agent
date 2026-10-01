"""Bind the approved test files, then verify the staged repair and write its history."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import FinalizeContext, PrepareContext
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_healing.contracts.application import (
    ApplyTestRepairInputV1,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.operations.application import (
    approved_sources,
    expected_repair_history,
    repair_history_path,
    verify_application,
)


def before(ctx: PrepareContext, business: ApplyTestRepairInputV1) -> ApplyTestRepairInputV1:
    paths = tuple(approved_sources(business, ctx.project_root))
    ctx.bind("qa/tests", paths)
    ctx.extra("allowed_test_paths", list(paths))
    return business


def after(
    ctx: FinalizeContext, business: ApplyTestRepairInputV1, result: TestRepairResultV1
) -> VerifiedTestRepairV1:
    verified = verify_application(business, result, ctx)
    history = expected_repair_history(business, verified)
    relative = repair_history_path(
        coverage_epoch=business.coverage_epoch,
        repair_round=business.repair_round,
    )
    ctx.write(relative, canonical_json_bytes(cast(JSONValue, history.model_dump(mode="json"))) + b"\n")
    return verified
