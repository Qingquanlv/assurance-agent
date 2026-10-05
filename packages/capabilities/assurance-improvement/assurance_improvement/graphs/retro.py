"""Retro root: slice files, three parallel analyses, then synthesize."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Self

from pydantic import Field, model_validator

from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.ledger import InputBinding

from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.contracts.retro import RetroWindow
from assurance_improvement.ops.retro import op as retro
from assurance_improvement.contracts.retro_identity import (
    collect_retro_source_refs,
    prepare_retro_identity,
)
from assurance_improvement.ops.retro_eval_analysis import op as retro_eval_analysis
from assurance_improvement.ops.retro_issue_analysis import op as retro_issue_analysis
from assurance_improvement.ops.retro_workflow_analysis import op as retro_workflow_analysis
from assurance_intake.contracts import EvidenceArtifactRefV1

_COLLECT = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-collect-v3"]
_BUILD_SLICES = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-build-slices"]


def _extra(producer: object, name: str, field: str) -> InputBinding:
    handle = producer.artifact(name)  # type: ignore[attr-defined]
    return InputBinding(ledger_key=handle.ledger_key, field=field, many=False)


def _with(contract_id: str, extra: tuple[InputBinding, ...]) -> TaskAttemptContract[Any, Any]:
    current = TASK_ATTEMPT_CONTRACTS[contract_id]
    return replace(current, bindings=(*current.bindings, *extra))


_SYNTHESIZE = _with(
    "assurance.improvement.retro-synthesize",
    (
        _extra(retro_issue_analysis, "issue-analysis", "issue_analysis_ref"),
        _extra(retro_workflow_analysis, "workflow-analysis", "workflow_analysis_ref"),
        _extra(retro_eval_analysis, "eval-analysis", "eval_analysis_ref"),
    ),
)
_RECONCILE = _with(
    "assurance.improvement.reconcile-improvements",
    (_extra(retro, "candidates", "candidates_ref"),),
)


class RetroFlowInput(FrozenModel):
    change_id: str = Field(min_length=1)
    retro_id: str | None = None
    window: RetroWindow | None = None
    source_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    runtime_ref: EvidenceArtifactRefV1 | None = None
    report_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    history_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    issue_snapshot_ref: EvidenceArtifactRefV1 | None = None
    inspection_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    preparation_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    reviewed_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    inspection_outcome: dict[str, Any] | None = None
    carried_evidence_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    allowed_artifact_paths: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    dry_run: bool = False
    report_receipt: ReceiptRef | None = None

    @model_validator(mode="after")
    def _prepare_identity(self) -> Self:
        inspection_refs = self.inspection_refs
        if "inspection_refs" not in self.model_fields_set:
            inspection_refs = (*inspection_refs, *_inspection_candidates(self.inspection_outcome))
        collected = collect_retro_source_refs(
            source_refs=(*self.source_refs, *self.carried_evidence_refs),
            runtime_ref=self.runtime_ref,
            report_refs=self.report_refs,
            history_refs=self.history_refs,
            issue_snapshot_ref=self.issue_snapshot_ref,
            inspection_refs=inspection_refs,
            preparation_refs=self.preparation_refs,
            reviewed_refs=self.reviewed_refs,
        )
        receipt_digest = None if self.report_receipt is None else self.report_receipt.receipt_digest
        filled = prepare_retro_identity(
            change_id=self.change_id,
            window=self.window,
            source_refs=collected,
            report_receipt_digest=receipt_digest,
            retro_id=self.retro_id,
        )
        return self.model_copy(
            update={
                "retro_id": filled.retro_id,
                "window": filled.window,
                "source_refs": filled.source_refs,
            }
        )


def _inspection_candidates(raw: object) -> tuple[object, ...]:
    if not isinstance(raw, Mapping):
        return ()
    found: list[object] = list(raw.get("assessment_refs") or ())
    for name in ("mapping_ref", "plan_ref"):
        if raw.get(name) is not None:
            found.append(raw[name])
    return tuple(found)


def build_retro_graph(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("retro", input=RetroFlowInput, outcomes=("done", "failed"))
    flow.step("retro-build-slices", _BUILD_SLICES, then="retro-collect", on_failure="failed")
    flow.step("retro-collect", _COLLECT, then="analyses", on_failure="failed")
    flow.parallel(
        "analyses",
        branches={
            "retro-eval-analysis": retro_eval_analysis,
            "retro-issue-analysis": retro_issue_analysis,
            "retro-workflow-analysis": retro_workflow_analysis,
        },
        select=None,
        require="succeeded",
        then="retro-synthesize",
        on_failure="failed",
    )
    flow.step(
        "retro-synthesize",
        _SYNTHESIZE,
        route_on="route",
        routes={"synthesize": "retro", "empty": "retro-reconcile"},
        on_failure="failed",
    )
    flow.step(
        "retro",
        retro,
        route_on="analysis_status",
        routes={"ok": "retro-reconcile", "failed": "failed"},
        on_failure="failed",
    )
    flow.step("retro-reconcile", _RECONCILE, then="done", on_failure="failed")
    return flow.bind(context)


__all__ = ["RetroFlowInput", "build_retro_graph"]
