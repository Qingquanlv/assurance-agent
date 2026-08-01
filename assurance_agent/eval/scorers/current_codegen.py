"""Dark-ship current-chain / selected-test codegen scoring (activated in Task 22)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import LayerName
from assurance_agent.artifacts.models.generated_files import GeneratedFilesV1
from assurance_agent.eval import write_scan
from assurance_agent.eval.evidence_export import ExecutionEvidenceV1
from assurance_agent.eval.scorers import shared
from assurance_agent.verification.generated_entries import (
    GeneratedEntryDecision,
    LayerMappingRelation,
    MappedTestEntry,
    classify_generated_entry,
    extract_layer_mapping,
)
from assurance_agent.verification.generated_files import get_generated_files_contract, get_generated_files_model
from assurance_agent.workflow.graph.precommit import (
    GENERATED_FILES_CANDIDATE_V1,
    CandidateValidationReceiptV1,
)
from assurance_agent.workflow.graph.task_inputs import TaskInputSnapshotV1
from assurance_agent.workflow.graph.workspace import WriteEntry, WriteSet

HARD_METRIC_NAMES = (
    "current_assurance_chain_rate",
    "current_codegen_attempt_rate",
    "selected_test_write_rate",
)

ChainRole = Literal[
    "applicability",
    "preflight",
    "reviewer",
    "mechanical",
    "plan_gate",
    "precheck",
    "codegen",
]


@dataclass(frozen=True, slots=True)
class AttemptBinding:
    """One epoch-closed attempt identity for a chain role."""

    role: ChainRole
    attempt_id: str
    task_id: str
    generation_id: str
    source_tree_id: str
    target_tree_id: str
    output_digests: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WriteAttribution:
    """Evidence needed to credit one selected private-root test write."""

    write_set: WriteSet
    receipt: CandidateValidationReceiptV1
    input_snapshot: TaskInputSnapshotV1
    input_snapshot_id: str
    runtime_context_sha256: str | None
    manifest: GeneratedFilesV1
    summary_digest: str
    manifest_digest: str
    content_changed_paths: frozenset[str]
    source_by_path: Mapping[str, str]
    mapping: LayerMappingRelation
    private_root: str
    selected_sibling_roots: frozenset[str] = frozenset()
    forbidden_write_count: int = 0


@dataclass(frozen=True, slots=True)
class CurrentLayerBinding:
    """Typed current-chain package for one selected layer (test seam + binder input)."""

    layer: LayerName
    applicable: bool
    root_invocation_id: str
    assurance_invocation_id: str
    branch_invocation_id: str
    cycle_generation_id: str
    attempts: tuple[AttemptBinding, ...]
    na_document_ok: bool = False
    plan_attempt_present: bool = False
    write_attribution: WriteAttribution | None = None
    policy_integrity_ok: bool = True


@dataclass(frozen=True, slots=True)
class CurrentLayerCodegenEvidence:
    layer: LayerName
    applicable: bool
    chain_ok: bool
    codegen_attempt_ok: bool
    selected_test_write_ok: bool
    policy_integrity_ok: bool
    reason_code: str = "ok"
    entry_decisions: tuple[GeneratedEntryDecision, ...] = ()


def calculate_current_assurance_chain_rate(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> float:
    if not evidences:
        return 0.0
    return sum(1.0 for item in evidences if item.chain_ok) / len(evidences)


def calculate_current_codegen_attempt_rate(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> float:
    applicable = [item for item in evidences if item.applicable]
    if not applicable:
        return 0.0
    return sum(1.0 for item in applicable if item.codegen_attempt_ok) / len(applicable)


def calculate_selected_test_write_rate(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> float:
    applicable = [item for item in evidences if item.applicable]
    if not applicable:
        return 0.0
    return sum(1.0 for item in applicable if item.selected_test_write_ok) / len(applicable)


def evaluate_layer_binding(binding: CurrentLayerBinding) -> CurrentLayerCodegenEvidence:
    """Evaluate one selected-layer current-chain package into typed evidence."""
    if not binding.policy_integrity_ok:
        return CurrentLayerCodegenEvidence(
            layer=binding.layer,
            applicable=binding.applicable,
            chain_ok=False,
            codegen_attempt_ok=False,
            selected_test_write_ok=False,
            policy_integrity_ok=False,
            reason_code="policy_integrity_failed",
        )

    chain_ok, chain_reason = _evaluate_chain(binding)
    if not chain_ok:
        return CurrentLayerCodegenEvidence(
            layer=binding.layer,
            applicable=binding.applicable,
            chain_ok=False,
            codegen_attempt_ok=False,
            selected_test_write_ok=False,
            policy_integrity_ok=True,
            reason_code=chain_reason,
        )

    if not binding.applicable:
        return CurrentLayerCodegenEvidence(
            layer=binding.layer,
            applicable=False,
            chain_ok=True,
            codegen_attempt_ok=False,
            selected_test_write_ok=False,
            policy_integrity_ok=True,
            reason_code="inapplicable_ok",
        )

    codegen_ok = _codegen_attempt_ok(binding)
    write_ok, decisions, write_reason = _selected_test_write_ok(binding)
    return CurrentLayerCodegenEvidence(
        layer=binding.layer,
        applicable=True,
        chain_ok=True,
        codegen_attempt_ok=codegen_ok,
        selected_test_write_ok=write_ok and codegen_ok,
        policy_integrity_ok=True,
        reason_code="ok" if write_ok and codegen_ok else write_reason if codegen_ok else "codegen_attempt_missing",
        entry_decisions=decisions,
    )


def score_current_codegen_metrics(
    evidences: Sequence[CurrentLayerCodegenEvidence],
) -> dict[str, float]:
    """Callable hard-metric calculations (not registered on the live scorer)."""
    return {
        "current_assurance_chain_rate": calculate_current_assurance_chain_rate(evidences),
        "current_codegen_attempt_rate": calculate_current_codegen_attempt_rate(evidences),
        "selected_test_write_rate": calculate_selected_test_write_rate(evidences),
    }


def replay_policy_integrity(attempt_dir: Path) -> bool:
    """Require byte-identical reconstructed WritePolicyV1 before write credit."""
    return shared.replay_policy_integrity(attempt_dir)


def bind_and_score_attempt(attempt_dir: Path) -> dict[str, float]:
    """Load execution evidence and return dark hard metrics (zero on integrity failure)."""
    if not shared.replay_policy_integrity(attempt_dir):
        return {name: 0.0 for name in HARD_METRIC_NAMES}
    # Full event-slice chain binding lands with Task 20/22 runtime matrix.
    # Until then, absence of an explicit binder package scores zero rather than
    # inventing credit from mutable raw-output trees.
    return {name: 0.0 for name in HARD_METRIC_NAMES}


def load_plan_case_bytes_from_snapshot(
    *,
    input_snapshot: TaskInputSnapshotV1,
    input_snapshot_id: str,
    blobs: Mapping[str, bytes],
    plan_logical: str,
    case_logicals: Sequence[str],
) -> tuple[str, list[dict[str, object]]]:
    """Load plan/case bytes only from attempt-bound snapshot/exported blobs.

    Callers must pass the snapshot already bound by receipt ``input_snapshot_id``.
    ``runtime_context_sha256`` is retained on the snapshot for D13 consumers.
    """
    if not input_snapshot_id:
        raise ValueError("input_snapshot_id is required")
    _ = input_snapshot.runtime_context_sha256
    _ = input_snapshot.input_sha256
    plan_text = _blob_text_for_logical(input_snapshot, blobs, plan_logical)
    cases: list[dict[str, object]] = []
    for logical in case_logicals:
        raw = _blob_bytes_for_logical(input_snapshot, blobs, logical)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"case blob for {logical} must be an object")
        cases.append(payload)
    return plan_text, cases


def classify_write_set_entries(
    *,
    layer: str,
    attribution: WriteAttribution,
) -> tuple[bool, tuple[GeneratedEntryDecision, ...]]:
    """Credit only current mapped behavioral selected private-root test writes."""
    private = attribution.private_root.rstrip("/")
    prefix = private + "/"
    decisions: list[GeneratedEntryDecision] = []
    credited = False

    # Zero sibling / forbidden policy writes.
    for entry in attribution.write_set.entries:
        path = _logical_to_repo(entry.logical_path)
        if any(path == root or path.startswith(root.rstrip("/") + "/") for root in attribution.selected_sibling_roots):
            return False, ()
    if attribution.forbidden_write_count:
        return False, ()

    # Receipt must be generated_files_candidate/v1 and bind digests.
    if attribution.receipt.validator_id != GENERATED_FILES_CANDIDATE_V1:
        return False, ()
    if attribution.receipt.input_snapshot_id != attribution.input_snapshot_id:
        return False, ()
    if attribution.receipt.write_set_id != attribution.write_set.write_set_id:
        return False, ()
    if not attribution.write_set.entries:
        return False, ()

    contract = get_generated_files_contract(layer)
    summary_key = contract.summary_path.removeprefix("change:")
    manifest_key = contract.manifest_path.removeprefix("change:")
    outputs = attribution.receipt.output_digests
    if outputs.get(summary_key) != attribution.summary_digest:
        return False, ()
    if outputs.get(manifest_key) != attribution.manifest_digest:
        return False, ()

    mapped_targets = {entry.target_file: entry for entry in attribution.mapping.entries}
    selected = set(attribution.mapping.selected_case_ids)
    role_by_path = {item.repo_path: item for item in attribution.manifest.files}

    for write in attribution.write_set.entries:
        path = _logical_to_repo(write.logical_path)
        if not (path == private or path.startswith(prefix)):
            continue
        if write.operation not in {"add", "modify"}:
            continue
        if write.before_sha256 == write.after_sha256:
            continue
        if write.after_sha256 is None or write.blob_sha256 is None:
            continue
        if write.after_sha256 != write.blob_sha256:
            continue
        manifest_entry = role_by_path.get(path)
        if manifest_entry is None or manifest_entry.role != "test_entry":
            continue
        if manifest_entry.content_sha256 != write.after_sha256:
            continue
        if path not in attribution.content_changed_paths:
            continue
        mapped = mapped_targets.get(path)
        if mapped is None or mapped.case_id not in selected:
            continue
        source = attribution.source_by_path.get(path)
        if source is None:
            continue
        decision = classify_generated_entry(layer=layer, source=source, entry=mapped)
        decisions.append(decision)
        if decision.accepted:
            credited = True

    return credited, tuple(decisions)


def _evaluate_chain(binding: CurrentLayerBinding) -> tuple[bool, str]:
    by_role = {item.role: item for item in binding.attempts}
    if binding.applicable:
        required: tuple[ChainRole, ...] = (
            "applicability",
            "reviewer",
            "mechanical",
            "plan_gate",
            "precheck",
            "codegen",
        )
        if binding.layer in {"fuzz", "performance"}:
            required = ("applicability", "preflight", *required[1:])
        for role in required:
            if role not in by_role:
                return False, f"missing_{role}"
        if binding.plan_attempt_present:
            return False, "plan_attempt_in_codegen_only"
        generation = binding.cycle_generation_id
        trees: set[tuple[str, str]] = set()
        for role in required:
            attempt = by_role[role]
            if attempt.generation_id != generation:
                return False, "epoch_generation_mismatch"
            trees.add((attempt.source_tree_id, attempt.target_tree_id))
        # All roles must share one contiguous producer tree chain identity.
        if len({by_role[role].generation_id for role in required}) != 1:
            return False, "epoch_generation_mismatch"
        return True, "ok"

    # Inapplicable: current N/A + required absence of reviewer/codegen.
    if not binding.na_document_ok:
        return False, "missing_na_document"
    if "applicability" not in by_role:
        return False, "missing_applicability"
    if binding.layer in {"fuzz", "performance"} and "preflight" not in by_role:
        return False, "missing_preflight"
    if "reviewer" in by_role or "codegen" in by_role:
        return False, "unexpected_reviewer_or_codegen"
    return True, "ok"


def _codegen_attempt_ok(binding: CurrentLayerBinding) -> bool:
    codegen = next((item for item in binding.attempts if item.role == "codegen"), None)
    if codegen is None:
        return False
    contract = get_generated_files_contract(binding.layer)
    summary_key = contract.summary_path.removeprefix("change:")
    manifest_key = contract.manifest_path.removeprefix("change:")
    digests = dict(codegen.output_digests)
    summary = digests.get(summary_key)
    manifest = digests.get(manifest_key)
    if binding.write_attribution is not None:
        return (
            summary == binding.write_attribution.summary_digest
            and manifest == binding.write_attribution.manifest_digest
        )
    return bool(summary and manifest)


def _selected_test_write_ok(
    binding: CurrentLayerBinding,
) -> tuple[bool, tuple[GeneratedEntryDecision, ...], str]:
    attribution = binding.write_attribution
    if attribution is None:
        return False, (), "missing_write_attribution"
    ok, decisions = classify_write_set_entries(layer=binding.layer, attribution=attribution)
    if not ok:
        return False, decisions, "selected_test_write_failed"
    return True, decisions, "ok"


def _logical_to_repo(logical_path: str) -> str:
    if logical_path.startswith("repo:"):
        return logical_path.removeprefix("repo:")
    if logical_path.startswith("change:"):
        return logical_path.removeprefix("change:")
    return logical_path


def _blob_bytes_for_logical(
    snapshot: TaskInputSnapshotV1,
    blobs: Mapping[str, bytes],
    logical: str,
) -> bytes:
    for entry in snapshot.entries:
        if logical in entry.logical_aliases or entry.repo_relpath == logical.removeprefix("change:"):
            if entry.sha256 is None:
                raise ValueError(f"snapshot entry for {logical} has no sha256")
            digest = entry.sha256.removeprefix("sha256:")
            raw = blobs.get(entry.sha256) or blobs.get(digest)
            if raw is None:
                raise ValueError(f"missing blob for {logical}")
            return raw
    raise ValueError(f"logical {logical} not present in input snapshot")


def _blob_text_for_logical(
    snapshot: TaskInputSnapshotV1,
    blobs: Mapping[str, bytes],
    logical: str,
) -> str:
    return _blob_bytes_for_logical(snapshot, blobs, logical).decode("utf-8")


def make_zero_evidence(layer: LayerName, *, applicable: bool) -> CurrentLayerCodegenEvidence:
    return CurrentLayerCodegenEvidence(
        layer=layer,
        applicable=applicable,
        chain_ok=False,
        codegen_attempt_ok=False,
        selected_test_write_ok=False,
        policy_integrity_ok=False,
        reason_code="zero",
    )


def with_policy_failure(evidence: CurrentLayerCodegenEvidence) -> CurrentLayerCodegenEvidence:
    return replace(
        evidence,
        chain_ok=False,
        codegen_attempt_ok=False,
        selected_test_write_ok=False,
        policy_integrity_ok=False,
        reason_code="policy_integrity_failed",
    )


def mapping_from_snapshot_bytes(
    *,
    layer: str,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
) -> LayerMappingRelation:
    return extract_layer_mapping(layer=layer, plan_text=plan_text, cases=cases)


def digest_bytes(data: bytes) -> str:
    return sha256_bytes(data)


def digest_model(model: object) -> str:
    return sha256_bytes(canonical_json_bytes(model))


def load_execution_envelope(attempt_dir: Path) -> ExecutionEvidenceV1 | None:
    path = attempt_dir / "execution.json"
    if not path.is_file():
        return None
    try:
        return ExecutionEvidenceV1.model_validate_json(path.read_bytes())
    except Exception:
        return None


__all__ = [
    "HARD_METRIC_NAMES",
    "AttemptBinding",
    "CurrentLayerBinding",
    "CurrentLayerCodegenEvidence",
    "WriteAttribution",
    "WriteEntry",
    "WriteSet",
    "bind_and_score_attempt",
    "calculate_current_assurance_chain_rate",
    "calculate_current_codegen_attempt_rate",
    "calculate_selected_test_write_rate",
    "classify_write_set_entries",
    "digest_bytes",
    "digest_model",
    "evaluate_layer_binding",
    "get_generated_files_model",
    "load_execution_envelope",
    "load_plan_case_bytes_from_snapshot",
    "make_zero_evidence",
    "mapping_from_snapshot_bytes",
    "MappedTestEntry",
    "replay_policy_integrity",
    "score_current_codegen_metrics",
    "with_policy_failure",
    "write_scan",
]
