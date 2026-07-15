"""Public surface of the artifact contract models (spec 4a).

Every module that consumes structured artifacts imports from here.
"""
from assurance_agent.artifacts.models.cases import CaseEntry, CaseRemoval, CaseYaml, QaYaml
from assurance_agent.artifacts.models.common import (
    CoverageDimension,
    CoverageThreshold,
    FunctionalCounts,
    FunctionalDimension,
    GateStatus,
    ReportRiskLevel,
)

__all__ = [
    "CaseEntry",
    "CaseRemoval",
    "CaseYaml",
    "CoverageDimension",
    "CoverageThreshold",
    "FunctionalCounts",
    "FunctionalDimension",
    "GateStatus",
    "QaYaml",
    "ReportRiskLevel",
]
