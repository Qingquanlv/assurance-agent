"""Host-owned plan-review routing policy: wheel runner + sticky repair scope."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_RUNNER_CATEGORIES = frozenset(
    {
        "helper_invocation",
        "invocation",
        "runner-contract",
        "runner_contract",
        "runtime_helper_invocation",
        "sync_async",
        "sync_async_invocation",
        "sync-async-invocation",
        "test-runner",
        "test_runner",
    }
)
_RUNNER_TEXT_MARKERS = (
    "asyncio_mode",
    "markers: none",
    "pytest.mark.asyncio",
    "pytest_asyncio",
)
_SCOPE_RELATIVE = "qa/results/plan/{family}/reviews/epochs/{epoch}/finding-scope.json"


def finding_scope_path(family: str, coverage_epoch: int) -> str:
    return _SCOPE_RELATIVE.format(family=family, epoch=coverage_epoch)


def is_runner_contract_finding(finding: Mapping[str, Any]) -> bool:
    category = str(finding.get("category") or "").strip().casefold().replace(" ", "_")
    if category in _RUNNER_CATEGORIES:
        return True
    locator = finding.get("locator")
    key = ""
    if isinstance(locator, Mapping):
        key = str(locator.get("key") or "").casefold()
    if "run guidance" in key or key.endswith("markers") or "markers" in key:
        return True
    message = str(finding.get("message") or "").casefold()
    return any(marker in message for marker in _RUNNER_TEXT_MARKERS)


def apply_plan_review_policy(
    payload: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    updated = dict(payload)
    updated.pop("public_outcome", None)
    decision = str(updated.get("decision") or "")
    if decision in {"reject", "needs_human_review"} or updated.get("human_review_required") is True:
        return updated
    findings = [dict(item) for item in updated.get("findings") or [] if isinstance(item, Mapping)]
    findings = [item for item in findings if not is_runner_contract_finding(item)]
    if previous is not None:
        if previous.get("decision") == "pass":
            findings = []
        else:
            allowed = {str(item) for item in previous.get("finding_ids") or ()}
            if allowed:
                findings = [item for item in findings if str(item.get("id")) in allowed]
    updated["findings"] = findings
    ids = [str(item["id"]) for item in findings]
    if ids:
        updated["decision"] = "needs_fix"
        updated["auto_fix_allowed"] = True
        updated["human_review_required"] = False
        updated["auto_fix_plan"] = ids
        updated["codegen_readiness"] = "not_ready"
        updated["next_action"] = updated.get("next_action") or "run api planner"
        return updated
    updated["decision"] = "pass"
    updated["auto_fix_allowed"] = False
    updated["human_review_required"] = False
    updated["auto_fix_plan"] = []
    updated["codegen_readiness"] = "ready_with_warnings"
    updated["next_action"] = "proceed to codegen"
    return updated


def load_finding_scope(root: Path, *, family: str, coverage_epoch: int) -> dict[str, Any] | None:
    path = root.joinpath(*finding_scope_path(family, coverage_epoch).split("/"))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    decision = payload.get("decision")
    raw_ids = payload.get("finding_ids")
    if not isinstance(decision, str) or not isinstance(raw_ids, list):
        return None
    ids = [item for item in raw_ids if isinstance(item, str) and item]
    return {"decision": decision, "finding_ids": ids}


def write_finding_scope(
    root: Path,
    *,
    family: str,
    coverage_epoch: int,
    change_id: str,
    decision: str,
    finding_ids: Sequence[str],
) -> str:
    relative = finding_scope_path(family, coverage_epoch)
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "coverage_epoch": coverage_epoch,
                "family": family,
                "decision": decision,
                "finding_ids": list(finding_ids),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return relative


__all__ = [
    "apply_plan_review_policy",
    "finding_scope_path",
    "is_runner_contract_finding",
    "load_finding_scope",
    "write_finding_scope",
]
