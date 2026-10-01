"""Seal a coverage-repair summary against the locked brief."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError

from assurance_healing.contracts.agent import CoverageRepairInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairApplySummary
from assurance_healing.operations.agent import brief_locator_ids, under_root, workspace_file


def after(
    ctx: FinalizeContext, business: CoverageRepairInputV1, result: CoverageRepairApplySummary
) -> CoverageRepairApplySummary:
    locked = ctx.prepared.get("prepare")
    if not isinstance(locked, dict):
        raise InputError("finalize digests do not match the locked prepare payload")
    locked_business = CoverageRepairInputV1.model_validate(locked)
    if (
        business.baseline_digest != locked_business.baseline_digest
        or business.change_id != locked_business.change_id
        or business.brief != locked_business.brief
    ):
        raise InputError("finalize digests do not match the locked prepare payload")
    if result.change_id != business.change_id or result.change_id != business.brief.change_id:
        raise OutputError("coverage repair identity does not match the locked change")
    locators = brief_locator_ids(business.brief)
    unknown = [item for item in result.addressed_items if item not in locators]
    if unknown:
        raise OutputError(f"addressed item is not a brief locator: {unknown[0]}")
    allowed = set(business.brief.allowed_test_files)
    for path in result.files_modified:
        if path not in allowed or not under_root(path, business.allowed_roots):
            raise OutputError(f"undeclared target file: {path}")
        workspace_file(ctx.project_root, path)
    artifact_paths = ctx.prepared.get("artifact_paths") or ()
    if isinstance(artifact_paths, str) or not isinstance(artifact_paths, (list, tuple)):
        raise OutputError("artifact_paths must be a list of workspace paths")
    for path in artifact_paths:
        workspace_file(ctx.project_root, path)
    return result
