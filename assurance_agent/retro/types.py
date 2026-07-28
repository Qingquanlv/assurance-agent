"""Schema-v2 Retro evidence types (current-run only; no legacy proposal tracks)."""

from __future__ import annotations

from itertools import chain
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs

# 共享 window/source 模型的唯一定义在 artifacts/models/retro_v3.py（分层：
# retro → artifacts 为既有方向）；此处 re-export 保持既有 import 路径。
from assurance_agent.artifacts.models.retro_v3 import (  # noqa: F401
    RetroIntegrity,
    RetroSelectionSnapshot,
    RetroSourceDescriptor,
    RetroWindow,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class RetroSourceManifest(BaseModel):
    model_config = _FROZEN

    issue_slice_sha256: str
    issue_sources: tuple[RetroSourceDescriptor, ...]
    workflow_sources: tuple[RetroSourceDescriptor, ...]
    eval_sources: tuple[RetroSourceDescriptor, ...]

    def resolvable_ids(self) -> frozenset[str]:
        sources = (*self.issue_sources, *self.workflow_sources, *self.eval_sources)
        return frozenset(chain.from_iterable(source.evidence_ids for source in sources))


class RetroSignal(BaseModel):
    model_config = _FROZEN

    signal_id: str
    source_refs: ImprovementSourceRefs
    metrics: dict[str, int | float | str]


class IssueRetroSignals(BaseModel):
    model_config = _FROZEN

    observation_distribution: tuple[RetroSignal, ...] = ()
    occurrence_trends: tuple[RetroSignal, ...] = ()
    assessment_corrections: tuple[RetroSignal, ...] = ()
    regressions: tuple[RetroSignal, ...] = ()
    review_decision_patterns: tuple[RetroSignal, ...] = ()
    resolution_outcomes: tuple[RetroSignal, ...] = ()
    repeated_not_an_issue: tuple[RetroSignal, ...] = ()


class WorkflowRetroSignals(BaseModel):
    model_config = _FROZEN

    gate_pushback: tuple[RetroSignal, ...] = ()
    healing_efficiency: tuple[RetroSignal, ...] = ()
    skill_execution_drift: tuple[RetroSignal, ...] = ()
    task_failures: tuple[RetroSignal, ...] = ()


class EvalRetroSignals(BaseModel):
    model_config = _FROZEN

    trends: tuple[RetroSignal, ...] = ()


class RetroSignalSet(BaseModel):
    model_config = _FROZEN

    issue: IssueRetroSignals
    workflow: WorkflowRetroSignals
    eval: EvalRetroSignals


class RetroContext(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2"] = "2"
    retro_id: str
    generated_at: str
    window: RetroWindow
    source_manifest: RetroSourceManifest
    integrity: RetroIntegrity
    signals: RetroSignalSet
    signal_count: int = Field(ge=0)

    @property
    def allows_domain_knowledge(self) -> bool:
        """True only when Issue source integrity is complete.

        Incomplete Workflow/Eval sources do not forbid domain_knowledge; only
        Issue ``analysis_failed`` / ``project_sync_pending`` reasons do.
        Other ImprovementKinds remain eligible when this is false.
        """
        issue_blockers = frozenset({"analysis_failed", "project_sync_pending"})
        return not any(
            reason in issue_blockers or reason.startswith("issue_pipeline_failed:")
            for reason in self.integrity.reasons
        )
