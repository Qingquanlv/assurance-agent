from __future__ import annotations

from pydantic import BaseModel

from assurance_agent.retro.types import EvidenceSource


class NightlyOptions(BaseModel):
    sut: str
    retro_id: str | None = None
    dry_run: bool = False
    agent: str = "cursor-agent"
    history: int = 5
    min_evidence: int = 2
    rework_alert: int = 3
    skip_eval: bool = False
    last: int = 10


class ChangeCandidate(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str
