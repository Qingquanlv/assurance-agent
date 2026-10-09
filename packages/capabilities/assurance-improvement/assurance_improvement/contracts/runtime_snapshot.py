"""Input and output for the pre-retro runtime snapshot task."""

from __future__ import annotations

import hashlib

from pydantic import Field

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel

from assurance_improvement.contracts.retro import WorkflowRuntimeEvidenceV2
from assurance_intake.contracts import EvidenceArtifactRefV1

RUNTIME_EVIDENCE_ROOT = "qa/results/workflow"


class RetroRuntimeSnapshotInputV1(FrozenModel):
    change_id: str = Field(min_length=1)


class RetroRuntimeSnapshotOutputV1(FrozenModel):
    evidence_ref: EvidenceArtifactRefV1


def pre_retro_evidence_path(document: WorkflowRuntimeEvidenceV2) -> tuple[str, bytes]:
    """Same relative path and bytes as ``publish_runtime_evidence(stage='pre-retro')``."""
    encoded = canonical_json_bytes(document.model_dump(mode="json")) + b"\n"
    content_digest = hashlib.sha256(encoded).hexdigest()
    invocation = canonical_digest(document.invocation_id)
    relative = f"{RUNTIME_EVIDENCE_ROOT}/{invocation}/pre-retro/{content_digest}/workflow-evidence.json"
    return relative, encoded


__all__ = [
    "RUNTIME_EVIDENCE_ROOT",
    "RetroRuntimeSnapshotInputV1",
    "RetroRuntimeSnapshotOutputV1",
    "pre_retro_evidence_path",
]
