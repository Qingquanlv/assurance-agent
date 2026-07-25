from __future__ import annotations

from pydantic import BaseModel, Field

from assurance_agent.retro.types import EvidenceSource


class NightlyOptions(BaseModel):
    """Retro invocation options shared by CLI and graph callers.

    Window fields (``change_ids`` / ``since`` / ``until`` / ``last``) map onto
    ``RetroWindowSelection`` via ``selection_from_nightly_options``. Consumed-change
    cursor fields are intentionally absent — resolution never reads or writes
    ``_state.json`` consumed markers.

    Legacy nightly fields (``sut``, ``engine_root``, ``min_evidence``, …) remain
    until Task 12 removes the old runtime path.
    """

    sut: str
    engine_root: str | None = None
    retro_id: str | None = None
    dry_run: bool = False
    agent: str = "cursor-agent"
    min_evidence: int = 2
    rework_alert: int = 3
    skip_eval: bool = False
    last: int = Field(default=10, ge=1)
    change_ids: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None


class ChangeCandidate(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str
