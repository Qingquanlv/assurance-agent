"""Shared models and fixed source mappings for Phase 6 admission evidence."""

from __future__ import annotations

from typing import Literal

from graph_engine.plugin_api import FrozenModel
from pydantic import Field


class ChangeLocalWaiverV1(FrozenModel):
    waiver_id: str
    disposition: Literal["carried_forward", "deferred_out_of_scope"]
    replacement_tasks: tuple[int, ...]
    approved_by: Literal["user"]
    evidence: tuple[str, ...]


class ChangeLocalAdmissionV1(FrozenModel):
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_verdict: Literal["accepted", "not_accepted"]
    admission_status: Literal["complete", "accepted_with_waivers"]
    waivers: tuple[ChangeLocalWaiverV1, ...]


class ResidualDispositionV1(FrozenModel):
    source_plan: str
    source_task: str
    disposition: Literal[
        "verified_complete",
        "carried_forward",
        "superseded",
        "deferred_out_of_scope",
    ]
    replacement_task: int | None
    evidence: tuple[str, ...]


REQUIRED_WAIVERS = {
    "change-local-openchamber-direct-discovery": ("deferred_out_of_scope", ()),
    "change-local-repository-type-package-gates": ("carried_forward", (4, 14)),
    "change-local-opencode-live": ("carried_forward", (3, 13)),
    "change-local-export-idempotency-live-proof": ("carried_forward", (12, 13)),
    "change-local-dirty-tree-closeout": ("carried_forward", (4, 14)),
    "change-local-stale-acceptance-refresh": ("carried_forward", (14,)),
    "change-local-graph-inventory": ("deferred_out_of_scope", ()),
    "change-local-unused-cli-flag": ("deferred_out_of_scope", ()),
    "change-local-skill-wording": ("deferred_out_of_scope", ()),
}

EXPECTED_RESIDUAL_MAPPINGS = {
    ("phase-1", "completed"): ("verified_complete", None),
    ("phase-2", "final 6 commits"): ("carried_forward", 2),
    ("phase-3", "main body"): ("verified_complete", None),
    ("phase-3", "OpenCode live"): ("carried_forward", 3),
    ("phase-3", "Cursor live"): ("deferred_out_of_scope", None),
    ("phase-4", "main body"): ("verified_complete", None),
    ("phase-5", "Tasks 1-23"): ("verified_complete", None),
    ("phase-5", "Task 24"): ("carried_forward", 3),
    ("phase-5", "Task 25"): ("deferred_out_of_scope", None),
    ("phase-5", "Task 26"): ("carried_forward", 4),
    ("phase-5", "Task 27"): ("carried_forward", 5),
    ("phase-6", "Task 1"): ("carried_forward", 1),
    ("phase-6", "Tasks 2-5"): ("superseded", 6),
    ("phase-6", "Task 6"): ("carried_forward", 7),
    ("phase-6", "Task 7"): ("carried_forward", 10),
    ("phase-6", "Task 8"): ("carried_forward", 8),
    ("phase-6", "Task 9"): ("carried_forward", 9),
    ("phase-6", "Task 10"): ("carried_forward", 10),
    ("phase-6", "Tasks 11-12"): ("carried_forward", 11),
    ("phase-6", "Tasks 13-14"): ("carried_forward", 12),
    ("phase-6", "Task 15"): ("carried_forward", 13),
    ("phase-6", "Task 16"): ("deferred_out_of_scope", None),
    ("phase-6", "Tasks 17-18"): ("carried_forward", 14),
}
