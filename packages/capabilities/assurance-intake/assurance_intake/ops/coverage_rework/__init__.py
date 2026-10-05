"""Coverage rework: seal the case-design context for the next coverage round."""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts.ops import Out

from assurance_intake.contracts.coverage_rework import (
    REWORK_CONTEXT_PATH,
    CoverageReworkInputV1,
    CoverageReworkOutputV1,
)
from assurance_intake.contracts.workflow import CaseReworkContextV1
from assurance_intake.ops import router
from assurance_intake.ops.coverage_rework import hooks
from assurance_intake.validators import SEALED_ARTIFACT_REFS_VALIDATOR_ID

op = router.task(
    "coverage-rework",
    input=CoverageReworkInputV1,
    output=CoverageReworkOutputV1,
    run=hooks.run,
    reads=("qa",),
    writes=(Out("rework", REWORK_CONTEXT_PATH, model=CaseReworkContextV1, format="json"),),
    errors=(ValueError, ValidationError, OSError),
    validators=(SEALED_ARTIFACT_REFS_VALIDATOR_ID,),
)

__all__ = ["CoverageReworkInputV1", "CoverageReworkOutputV1", "op"]
