"""Input of the coverage-rework task. The document it writes is ``CaseReworkContextV1``."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

# One logical name. A later coverage round overwrites this file; the ledger keeps the latest ref.
# The epoch lives in the document. ``Out`` paths are static, so the epoch is not a directory segment.
REWORK_CONTEXT_PATH = "qa/results/cases/case-rework-context.json"
COVERAGE_REWORK_HANDOFF_PATH = "qa/results/inspect/coverage-rework-handoff.json"


class CoverageReworkHandoffV1(FrozenModel):
    """What inspect writes for the next coverage round. The inspect receipt stays on the ledger."""

    schema_version: Literal["1"] = "1"
    source_epoch: int = Field(ge=0)
    assessment_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)


class CoverageReworkInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    # ``coverage.next`` has already advanced the loop. This is the new round.
    coverage_epoch: int = Field(ge=0)
    handoff_ref: EvidenceArtifactRefV1
    reviewed_case_ref: EvidenceArtifactRefV1
    inspect_receipt: ReceiptRef


class CoverageReworkOutputV1(FrozenModel):
    rework_ref: EvidenceArtifactRefV1


__all__ = [
    "COVERAGE_REWORK_HANDOFF_PATH",
    "REWORK_CONTEXT_PATH",
    "CoverageReworkHandoffV1",
    "CoverageReworkInputV1",
    "CoverageReworkOutputV1",
]
