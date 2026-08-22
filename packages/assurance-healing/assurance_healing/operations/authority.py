"""Fixer-authority readiness gate."""

from __future__ import annotations

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.proposal import FixerAuthorityV1
from assurance_healing.operations.common import InputError, failed_input


class FixerAuthorityReadyHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            raw = request.input
            if not isinstance(raw, dict):
                raise InputError("fixer-authority-ready requires authority input")
            authority_raw = raw.get("authority", raw)
            authority = FixerAuthorityV1.model_validate(authority_raw)
            if not authority.targets:
                return TaskOutcome.stopped(
                    "no_active_targets", {"route": "stop", "reason": "no_active_targets"}
                )
            if any(target.status != "ready" for target in authority.targets):
                return TaskOutcome.stopped(
                    "unverified_fixer_authority",
                    {"route": "stop", "reason": "unverified_fixer_authority"},
                )
            proposal = raw.get("proposal")
            outside = _proposal_paths_outside_authority(authority, proposal)
            if outside:
                return TaskOutcome.stopped(
                    "proposal_paths_outside_fixer_authority",
                    {
                        "route": "stop",
                        "reason": "proposal_paths_outside_fixer_authority:" + ",".join(outside),
                    },
                )
            return TaskOutcome.succeeded({"route": "pass"})
        except Exception as error:
            return failed_input(InputError(str(error)))


def _proposal_paths_outside_authority(authority: FixerAuthorityV1, proposal: object) -> list[str]:
    if not isinstance(proposal, dict):
        return []
    items = proposal.get("proposals")
    if not isinstance(items, list):
        return []
    allowed = {
        target.target: {path.repo_path for path in target.paths}
        for target in authority.targets
        if target.status == "ready"
    }
    outside: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("eligible") is not True:
            continue
        target = item.get("target")
        files = item.get("files_to_modify")
        if target not in {"api", "e2e"} or not isinstance(files, list):
            continue
        target_allowed = allowed.get(str(target), set())
        outside.update(path for path in files if isinstance(path, str) and path not in target_allowed)
    return sorted(outside)
