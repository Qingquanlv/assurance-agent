"""Change-relative artifact path -> pydantic model registry (spec 4a).

Globs mirror ARTIFACT_SPECS in the TS source src/schema/index.ts, plus
healing/fixer-safety-check.json (read by fixer-safety-gate; the plan-series
contract lists SafetyCheck as a core model). Glob semantics: `*` matches
within one path segment, `**/` matches zero or more segments — same effective
behavior micromatch gave the TS globs. `free`-grade artifacts (markdown
reports, events extensions) are deliberately absent: they are not validated.
"""

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    Advisory,
    ApplySummary,
    CaseYaml,
    ExecutionManifest,
    FactBaseline,
    FailureAnalysis,
    FixProposal,
    QaYaml,
    QualityGateResult,
    QualityReport,
    Review,
    SafetyCheck,
    WorkflowState,
)

Compat = Literal["must_compat", "versioned", "free"]


class ArtifactSpec(BaseModel):
    artifact_type: str
    pattern: str
    model: type[BaseModel]
    compat: Compat


REGISTRY: list[ArtifactSpec] = [
    ArtifactSpec(
        artifact_type="case_yaml", pattern="cases/**/case.yaml", model=CaseYaml, compat="must_compat"
    ),
    ArtifactSpec(artifact_type="qa_yaml", pattern=".qa.yaml", model=QaYaml, compat="must_compat"),
    ArtifactSpec(
        artifact_type="execution_manifest",
        pattern="execution/execution-manifest.yaml",
        model=ExecutionManifest,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="failure_analysis",
        pattern="inspect/failure-analysis.json",
        model=FailureAnalysis,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="quality_gate_result",
        pattern="inspect/quality-gate-result.json",
        model=QualityGateResult,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="quality_report",
        pattern="report/quality-report.json",
        model=QualityReport,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="fix_proposal",
        pattern="healing/fix-proposal.json",
        model=FixProposal,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="apply_summary",
        pattern="healing/*-apply-summary.json",
        model=ApplySummary,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="safety_check",
        pattern="healing/fixer-safety-check.json",
        model=SafetyCheck,
        compat="must_compat",
    ),
    ArtifactSpec(artifact_type="review", pattern="review/*.json", model=Review, compat="must_compat"),
    ArtifactSpec(
        artifact_type="fact_baseline",
        pattern="facts/fact-baseline.json",
        model=FactBaseline,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="advisory", pattern="explore/advisory.json", model=Advisory, compat="must_compat"
    ),
    ArtifactSpec(
        artifact_type="workflow_state", pattern="workflow-state.yaml", model=WorkflowState, compat="versioned"
    ),
]


@lru_cache(maxsize=None)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_artifact(relpath: str) -> ArtifactSpec | None:
    """Return the first registry spec whose glob matches the change-relative path."""
    norm = relpath.replace("\\", "/")
    for spec in REGISTRY:
        if _pattern_regex(spec.pattern).match(norm):
            return spec
    return None
