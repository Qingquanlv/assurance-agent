"""Finalize the apply-test-repair Agent result."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import run_finalize, validate_output
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.application import TestRepairResultV1
from assurance_healing.operations.application import (
    ApplyTestRepairFinalizeInputV1,
    expected_repair_history,
    repair_history_path,
    verify_application,
)

input_model = ApplyTestRepairFinalizeInputV1


def _commit(business: ApplyTestRepairFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    envelope = business.agent_result
    result = validate_output(TestRepairResultV1, thaw_json(envelope.result_payload))
    verified = verify_application(business, result, context)
    expected_history = expected_repair_history(business, verified)
    history_relative = repair_history_path(
        coverage_epoch=business.coverage_epoch,
        repair_round=business.repair_round,
    )
    history_path = context.write_root / history_relative
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_bytes(canonical_json_bytes(expected_history.model_dump(mode="json")) + b"\n")
    return TaskOutcome.succeeded(cast(JSONValue, verified.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=ApplyTestRepairFinalizeInputV1, commit=_commit)
