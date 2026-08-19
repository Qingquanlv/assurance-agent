"""Candidate validation receipts and closed precommit validators."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import field_validator, model_validator

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.artifacts.models.common import StrictWireModel
from assurance_kernel.artifacts.models.healing_codegen import (
    ApiCodegenFixApplyIntentV1,
    E2eCodegenFixApplyIntentV1,
    FixerAuthorityV1,
    FixerProposalApprovalReceiptV1,
)
from assurance_kernel.artifacts.models.plan_checks import PlanCheckDocument
from assurance_kernel.artifacts.paths import codegen_mapping_rel
from assurance_kernel.artifacts.registry import (
    Wire,
    load_registered_artifact,
    match_artifact,
    parse_wire,
    resolve_artifact,
)
from assurance_kernel.exceptions import AaError
from assurance_kernel.knowledge.capabilities import compute_missing_capabilities
from assurance_kernel.verification.generated_entries import (
    MappingExtractionError,
    extract_layer_mapping,
    mapped_case_ids_for_path,
    selected_private_root_targets,
)
from assurance_kernel.verification.generated_files import (
    get_generated_files_contract,
    get_generated_files_model,
)
from assurance_kernel.verification.plan_checks import run_layer_plan_checks
from assurance_kernel.verification.profiles import get_layer_assurance_profile
from assurance_kernel.workflow.graph.diff_safety import evaluate_diff_safety
from assurance_kernel.workflow.graph.evidence_paths import (
    EvidencePathError,
    pinned_write_set_roots,
    resolve_evidence_path,
)
from assurance_kernel.workflow.graph.task_inputs import TaskInputSnapshotV1
from assurance_kernel.workflow.graph.product_hooks import current_product_hooks
from assurance_kernel.workflow.graph.workspace import TreeStore, WriteSet, WorkspaceError

GENERATED_FILES_CANDIDATE_V1 = "generated_files_candidate/v1"
CODEGEN_FIX_CANDIDATE_V1 = "codegen_fix_candidate/v1"
PLAN_MECHANICAL_CANDIDATE_V1 = "plan_mechanical_candidate/v1"
ARCHIVE_INTEGRITY_V1 = "archive_integrity/v1"
PROBLEM_APPLY_CANDIDATE_V1 = "problem_apply_candidate/v1"
CROSS_ARTIFACT_INVARIANTS_V1 = "cross_artifact_invariants/v1"

KNOWN_PRECOMMIT_VALIDATORS: frozenset[str] = frozenset(
    {
        GENERATED_FILES_CANDIDATE_V1,
        CODEGEN_FIX_CANDIDATE_V1,
        PLAN_MECHANICAL_CANDIDATE_V1,
        ARCHIVE_INTEGRITY_V1,
        PROBLEM_APPLY_CANDIDATE_V1,
        CROSS_ARTIFACT_INVARIANTS_V1,
    }
)
IMPLEMENTED_PRECOMMIT_VALIDATORS: frozenset[str] = frozenset(KNOWN_PRECOMMIT_VALIDATORS)

# Closed commit-safety dependency inventory for runtime_commit_safety/v1 (Task 10).
# Keep beside the validator registry; do not accept caller-supplied lists.
COMMIT_SAFETY_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "assurance_agent.workflow.graph.precommit.GENERATED_FILES_CANDIDATE_V1",
        "validator",
        GENERATED_FILES_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit.CODEGEN_FIX_CANDIDATE_V1",
        "validator",
        CODEGEN_FIX_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit.PLAN_MECHANICAL_CANDIDATE_V1",
        "validator",
        PLAN_MECHANICAL_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit.ARCHIVE_INTEGRITY_V1",
        "validator",
        ARCHIVE_INTEGRITY_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit.PROBLEM_APPLY_CANDIDATE_V1",
        "validator",
        PROBLEM_APPLY_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit.CROSS_ARTIFACT_INVARIANTS_V1",
        "validator",
        CROSS_ARTIFACT_INVARIANTS_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit.KNOWN_PRECOMMIT_VALIDATORS",
        "helper",
        "precommit_validator_registry",
    ),
    (
        "assurance_agent.workflow.graph.precommit.PrecommitValidationContext",
        "model",
        "precommit_validation_context",
    ),
    (
        "assurance_agent.workflow.graph.precommit.CandidateValidationReceiptV1",
        "model",
        "candidate_validation_receipt",
    ),
    (
        "assurance_agent.workflow.graph.precommit.validate_candidate",
        "helper",
        "validate_candidate",
    ),
    (
        "assurance_agent.workflow.graph.precommit.verify_candidate_receipt",
        "helper",
        "verify_candidate_receipt",
    ),
    (
        "assurance_agent.workflow.graph.precommit.validator_semantics_digest",
        "helper",
        "validator_semantics_digest",
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_generated_files_candidate",
        "helper",
        GENERATED_FILES_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_generated_python_local_imports",
        "helper",
        GENERATED_FILES_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._assigned_local_module_names",
        "helper",
        GENERATED_FILES_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._top_level_import_collisions",
        "helper",
        GENERATED_FILES_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._is_positional_hypothesis_given",
        "helper",
        GENERATED_FILES_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_codegen_fix_candidate",
        "helper",
        CODEGEN_FIX_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_plan_mechanical_candidate",
        "helper",
        PLAN_MECHANICAL_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_archive_integrity",
        "helper",
        ARCHIVE_INTEGRITY_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_problem_apply_candidate",
        "helper",
        PROBLEM_APPLY_CANDIDATE_V1,
    ),
    (
        "assurance_agent.workflow.graph.precommit._validate_cross_artifact_invariants",
        "helper",
        CROSS_ARTIFACT_INVARIANTS_V1,
    ),
    (
        "assurance_agent.workflow.graph.diff_safety.evaluate_diff_safety",
        "helper",
        "diff_safety",
    ),
    (
        "assurance_agent.workflow.graph.evidence_paths.resolve_evidence_path",
        "helper",
        "evidence_path_resolver",
    ),
    (
        "assurance_agent.workflow.graph.evidence_paths.pinned_write_set_roots",
        "helper",
        "evidence_path_resolver",
    ),
    (
        "assurance_agent.verification.generated_entries.extract_layer_mapping",
        "helper",
        "generated_entries_mapping",
    ),
    (
        "assurance_agent.verification.generated_entries.mapped_case_ids_for_path",
        "helper",
        "generated_entries_mapping",
    ),
    (
        "assurance_agent.verification.generated_entries.selected_private_root_targets",
        "helper",
        "generated_entries_mapping",
    ),
    (
        "assurance_agent.verification.generated_files.get_generated_files_contract",
        "helper",
        "generated_files_registry",
    ),
    (
        "assurance_agent.verification.generated_files.get_generated_files_model",
        "helper",
        "generated_files_registry",
    ),
    (
        "assurance_agent.workflow.healing.safety.load_product_code_roots",
        "helper",
        "product_code_roots",
    ),
)

_TESTDATA_ROOT = "tests/testdata"


class CandidateValidationError(AaError):
    """Candidate failed precommit validation or receipt verification."""


class PrecommitValidationContext(StrictWireModel):
    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    target: str
    base_tree_id: str
    current_tree_id: str
    input_snapshot_id: str
    contract_digest: str
    policy_object_id: str
    policy_digest: str
    gate_attempt_id: str | None
    interrupt_id: str | None
    output_digests: dict[str, str]
    write_set_id: str
    definition_semantics: dict[str, str]

    @field_validator(
        "root_invocation_id",
        "invocation_id",
        "task_id",
        "attempt_id",
        "target",
        "base_tree_id",
        "current_tree_id",
        "input_snapshot_id",
        "contract_digest",
        "policy_object_id",
        "policy_digest",
        "write_set_id",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_canonical_maps(self) -> PrecommitValidationContext:
        _require_canonical_digest_map(self.output_digests, label="output_digests")
        _require_canonical_string_map(self.definition_semantics, label="definition_semantics")
        return self


class CandidateValidationReceiptV1(StrictWireModel):
    schema_version: Literal["1"]
    validator_id: str
    validator_semantics_digest: str
    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    input_snapshot_id: str
    output_digests: dict[str, str]
    write_set_id: str
    decision_payload_sha256: str

    @field_validator(
        "validator_id",
        "validator_semantics_digest",
        "root_invocation_id",
        "invocation_id",
        "task_id",
        "attempt_id",
        "input_snapshot_id",
        "write_set_id",
        "decision_payload_sha256",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_canonical(self) -> CandidateValidationReceiptV1:
        _require_canonical_digest_map(self.output_digests, label="output_digests")
        if self.schema_version != "1":
            raise ValueError("schema_version must be 1")
        return self


def validator_semantics_digest(validator_id: str) -> str:
    """Return the pinned semantics digest for one closed validator ID."""
    if validator_id not in KNOWN_PRECOMMIT_VALIDATORS:
        raise CandidateValidationError(f"unknown precommit validator: {validator_id}")
    payload = {
        "rules": _SEMANTICS_RULES[validator_id],
        "validator_id": validator_id,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def validate_precommit_validator_id(validator_id: str | None) -> None:
    """Reject unknown validator IDs at contract load."""
    if validator_id is None:
        return
    from assurance_kernel.workflow.graph.capability_state import current_validator_ids

    if validator_id not in current_validator_ids():
        raise CandidateValidationError(f"unknown precommit validator: {validator_id}")
    if validator_id not in IMPLEMENTED_PRECOMMIT_VALIDATORS:
        raise CandidateValidationError(f"precommit validator not implemented: {validator_id}")


def resolve_precommit_validator(
    *,
    node_validate: str | None,
    contract_validator: str | None,
) -> str | None:
    """Node `validate` is authoritative; contract is the historical/mirror fallback."""
    from assurance_kernel.workflow.graph.schema_v2 import VALIDATE_NONE

    if node_validate == VALIDATE_NONE:
        return None
    if node_validate is not None:
        return node_validate
    return contract_validator


def validate_candidate(
    validator_id: str,
    context: PrecommitValidationContext,
    *,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
    change_id: str,
    layer: str,
    current_change_repo_path: str,
    project_root: Path | None = None,
    host_change_dir: Path | None = None,
) -> tuple[str, CandidateValidationReceiptV1]:
    """Dispatch one registered validator; return CAS receipt id and receipt."""
    from assurance_kernel.workflow.graph.capability_state import current_validator_ids

    if validator_id not in current_validator_ids():
        raise CandidateValidationError(f"unknown precommit validator: {validator_id}")
    if validator_id not in IMPLEMENTED_PRECOMMIT_VALIDATORS:
        raise CandidateValidationError(f"precommit validator not implemented: {validator_id}")
    _assert_context_bindings(
        context,
        write_set=write_set,
        input_snapshot=input_snapshot,
    )
    if validator_id == CODEGEN_FIX_CANDIDATE_V1:
        decision = _validate_codegen_fix_candidate(
            context=context,
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            layer=layer,
            current_change_repo_path=current_change_repo_path,
            project_root=project_root,
        )
    elif validator_id == PLAN_MECHANICAL_CANDIDATE_V1:
        decision = _validate_plan_mechanical_candidate(
            context=context,
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            cases=cases,
            change_id=change_id,
            layer=layer,
            project_root=project_root,
        )
    elif validator_id == GENERATED_FILES_CANDIDATE_V1:
        decision = _validate_generated_files_candidate(
            context=context,
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            plan_text=plan_text,
            cases=cases,
            change_id=change_id,
            layer=layer,
            current_change_repo_path=current_change_repo_path,
        )
    elif validator_id == ARCHIVE_INTEGRITY_V1:
        decision = _validate_archive_integrity(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            change_id=change_id,
        )
    elif validator_id == PROBLEM_APPLY_CANDIDATE_V1:
        decision = _validate_problem_apply_candidate(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
        )
    elif validator_id == CROSS_ARTIFACT_INVARIANTS_V1:
        decision = _validate_cross_artifact_invariants(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            project_root=project_root,
            host_change_dir=host_change_dir,
        )
    else:
        raise CandidateValidationError(f"unknown precommit validator: {validator_id}")
    decision_bytes = canonical_json_bytes(decision)
    decision_digest = sha256_bytes(decision_bytes)
    decision_id = hashlib.sha256(decision_bytes).hexdigest()
    store._write_object(decision_id, decision_bytes)
    receipt = CandidateValidationReceiptV1(
        schema_version="1",
        validator_id=validator_id,
        validator_semantics_digest=validator_semantics_digest(validator_id),
        root_invocation_id=context.root_invocation_id,
        invocation_id=context.invocation_id,
        task_id=context.task_id,
        attempt_id=context.attempt_id,
        input_snapshot_id=context.input_snapshot_id,
        output_digests=dict(sorted(context.output_digests.items())),
        write_set_id=context.write_set_id,
        decision_payload_sha256=decision_digest,
    )
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_id = hashlib.sha256(receipt_bytes).hexdigest()
    if hashlib.sha256(decision_bytes).hexdigest() != decision_digest.removeprefix("sha256:"):
        raise CandidateValidationError("decision payload digest mismatch")
    # decision_payload_sha256 is prefixed; CAS object id is bare hex of those bytes.
    if decision_id != decision_digest.removeprefix("sha256:"):
        raise CandidateValidationError("decision CAS identity mismatch")
    store._write_object(receipt_id, receipt_bytes)
    verify_candidate_receipt(
        receipt,
        context,
        store=store,
        write_set=write_set,
        input_snapshot=input_snapshot,
        plan_text=plan_text,
        cases=cases,
        change_id=change_id,
        layer=layer,
        current_change_repo_path=current_change_repo_path,
        project_root=project_root,
        host_change_dir=host_change_dir,
    )
    return receipt_id, receipt


def verify_candidate_receipt(
    receipt: CandidateValidationReceiptV1,
    context: PrecommitValidationContext,
    *,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
    change_id: str,
    layer: str,
    current_change_repo_path: str,
    project_root: Path | None = None,
    host_change_dir: Path | None = None,
) -> None:
    """Recompute every binding or raise CandidateValidationError."""
    if receipt.validator_id not in IMPLEMENTED_PRECOMMIT_VALIDATORS:
        raise CandidateValidationError(f"unsupported receipt validator: {receipt.validator_id}")
    expected_semantics = validator_semantics_digest(receipt.validator_id)
    if receipt.validator_semantics_digest != expected_semantics:
        raise CandidateValidationError("validator_semantics_digest mismatch")
    if receipt.root_invocation_id != context.root_invocation_id:
        raise CandidateValidationError("root_invocation_id mismatch")
    if receipt.invocation_id != context.invocation_id:
        raise CandidateValidationError("invocation_id mismatch")
    if receipt.task_id != context.task_id:
        raise CandidateValidationError("task_id mismatch")
    if receipt.attempt_id != context.attempt_id:
        raise CandidateValidationError("attempt_id mismatch")
    if receipt.input_snapshot_id != context.input_snapshot_id:
        raise CandidateValidationError("input_snapshot_id mismatch")
    if receipt.write_set_id != context.write_set_id:
        raise CandidateValidationError("write_set_id mismatch")
    if dict(sorted(receipt.output_digests.items())) != dict(sorted(context.output_digests.items())):
        raise CandidateValidationError("output_digests mismatch")
    _assert_context_bindings(context, write_set=write_set, input_snapshot=input_snapshot)
    if receipt.validator_id == CODEGEN_FIX_CANDIDATE_V1:
        decision = _validate_codegen_fix_candidate(
            context=context,
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            layer=layer,
            current_change_repo_path=current_change_repo_path,
            project_root=project_root,
        )
    elif receipt.validator_id == PLAN_MECHANICAL_CANDIDATE_V1:
        decision = _validate_plan_mechanical_candidate(
            context=context,
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            cases=cases,
            change_id=change_id,
            layer=layer,
            project_root=project_root,
        )
    elif receipt.validator_id == GENERATED_FILES_CANDIDATE_V1:
        decision = _validate_generated_files_candidate(
            context=context,
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            plan_text=plan_text,
            cases=cases,
            change_id=change_id,
            layer=layer,
            current_change_repo_path=current_change_repo_path,
        )
    elif receipt.validator_id == ARCHIVE_INTEGRITY_V1:
        decision = _validate_archive_integrity(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            change_id=change_id,
        )
    elif receipt.validator_id == PROBLEM_APPLY_CANDIDATE_V1:
        decision = _validate_problem_apply_candidate(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
        )
    elif receipt.validator_id == CROSS_ARTIFACT_INVARIANTS_V1:
        decision = _validate_cross_artifact_invariants(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            project_root=project_root,
            host_change_dir=host_change_dir,
        )
    else:
        raise CandidateValidationError(f"unsupported receipt validator: {receipt.validator_id}")
    decision_bytes = canonical_json_bytes(decision)
    if sha256_bytes(decision_bytes) != receipt.decision_payload_sha256:
        raise CandidateValidationError("decision_payload_sha256 mismatch")
    decision_id = receipt.decision_payload_sha256.removeprefix("sha256:")
    try:
        stored = store.read_object(decision_id)
    except WorkspaceError as exc:
        raise CandidateValidationError(f"missing decision CAS object: {decision_id}") from exc
    if stored != decision_bytes:
        raise CandidateValidationError("decision CAS bytes mismatch")
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_id = hashlib.sha256(receipt_bytes).hexdigest()
    try:
        stored_receipt = store.read_object(receipt_id)
    except WorkspaceError as exc:
        raise CandidateValidationError(f"missing receipt CAS object: {receipt_id}") from exc
    if stored_receipt != receipt_bytes:
        raise CandidateValidationError("receipt CAS bytes mismatch")


def load_candidate_receipt(store: TreeStore, receipt_id: str) -> CandidateValidationReceiptV1:
    raw = store.read_object(receipt_id)
    if hashlib.sha256(raw).hexdigest() != receipt_id:
        raise CandidateValidationError(f"receipt CAS identity mismatch: {receipt_id}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError("receipt is not canonical JSON") from exc
    try:
        receipt = CandidateValidationReceiptV1.model_validate(payload)
    except Exception as exc:
        raise CandidateValidationError(f"invalid candidate receipt: {exc}") from exc
    if canonical_json_bytes(receipt) != raw:
        raise CandidateValidationError("receipt bytes are not canonical")
    return receipt


def infer_assurance_layer(target: str, task_input: Mapping[str, object] | None = None) -> str:
    """Resolve the assurance layer for a codegen-style target."""
    if task_input is not None:
        with_block = task_input.get("with")
        if isinstance(with_block, Mapping):
            layer = with_block.get("assurance_layer")
            if isinstance(layer, str) and layer.strip():
                return layer.strip()
    mapping = {
        "skill:aa-api-codegen": "api",
        "skill:aa-api-codegen-fixer": "api",
        "skill:aa-api-plan-reviewer": "api",
        "skill:aa-e2e-codegen": "e2e",
        "skill:aa-e2e-codegen-fixer": "e2e",
        "skill:aa-e2e-plan-reviewer": "e2e",
        "skill:aa-fuzz-codegen": "fuzz",
        "skill:aa-fuzz-plan-reviewer": "fuzz",
        "skill:aa-performance-codegen": "performance",
        "skill:aa-performance-plan-reviewer": "performance",
    }
    if target in mapping:
        return mapping[target]
    raise CandidateValidationError(f"cannot infer assurance layer for target: {target}")


def codegen_plan_logical_path(layer: str) -> str:
    return f"change:plans/{layer}-codegen-plan.md"


def load_plan_text_from_snapshot(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
    *,
    layer: str,
) -> str:
    """Load the layer codegen plan from the frozen input snapshot only."""
    logical = codegen_plan_logical_path(layer)
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        aliases = set(entry.logical_aliases)
        if logical in aliases or any(
            alias.startswith("change:plans/") and alias.endswith(f"{layer}-codegen-plan.md")
            for alias in aliases
        ):
            digest = entry.sha256.removeprefix("sha256:")
            try:
                return store.read_object(digest).decode("utf-8")
            except (WorkspaceError, UnicodeDecodeError) as exc:
                raise CandidateValidationError(
                    f"codegen plan blob unreadable for layer {layer}: {logical}"
                ) from exc
    raise CandidateValidationError(f"missing codegen plan in input snapshot for layer {layer}: {logical}")


def codegen_mapping_logical_path(layer: str) -> str:
    return f"change:{codegen_mapping_rel(layer)}"


def _is_codegen_mapping_alias(alias: str, *, layer: str) -> bool:
    return alias.startswith("change:plans/") and (
        alias.endswith(f"{layer}-codegen-mapping.json") or alias.endswith(f"{layer}-codegen-mapping.yaml")
    )


def load_codegen_mapping_from_snapshot(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
    *,
    layer: str,
) -> dict[str, object] | None:
    """Load the structured codegen mapping when the snapshot carries one."""
    logical = codegen_mapping_logical_path(layer)
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        aliases = set(entry.logical_aliases)
        matched = next(
            (alias for alias in aliases if alias == logical or _is_codegen_mapping_alias(alias, layer=layer)),
            None,
        )
        if matched is None:
            continue
        digest = entry.sha256.removeprefix("sha256:")
        rel = matched.removeprefix("change:")
        if resolve_artifact(rel) is None:
            raise CandidateValidationError(f"codegen mapping is not a registered artifact: {matched}")
        try:
            raw = store.read_object(digest).decode("utf-8")
            data = load_registered_artifact(rel, raw)
        except (WorkspaceError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise CandidateValidationError(
                f"codegen mapping blob unreadable for layer {layer}: {matched}"
            ) from exc
        if not isinstance(data, dict):
            raise CandidateValidationError(f"codegen mapping must be a mapping: {matched}")
        return data
    return None


def load_case_documents_from_snapshot(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
) -> list[dict[str, object]]:
    """Load case documents from the frozen input snapshot only."""
    documents: list[dict[str, object]] = []
    seen_aliases: set[str] = set()
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        case_aliases = sorted(alias for alias in entry.logical_aliases if alias.startswith("change:cases/"))
        if not case_aliases:
            continue
        for alias in case_aliases:
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
        digest = entry.sha256.removeprefix("sha256:")
        rel = case_aliases[0].removeprefix("change:")
        spec = match_artifact(rel)
        if spec is not None:
            wire: Wire = spec.wire
        elif rel.endswith(".json"):
            wire = "json"
        elif rel.endswith((".yaml", ".yml")):
            wire = "yaml"
        else:
            continue
        try:
            raw = store.read_object(digest)
        except WorkspaceError as exc:
            raise CandidateValidationError(f"case document blob missing from CAS: {case_aliases[0]}") from exc
        try:
            text = raw.decode("utf-8")
            data = parse_wire(wire, text)
        except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise CandidateValidationError(
                f"malformed case document in input snapshot: {case_aliases[0]}"
            ) from exc
        if not isinstance(data, dict):
            raise CandidateValidationError(f"case document must be a mapping: {case_aliases[0]}")
        documents.append(data)
    if not documents:
        raise CandidateValidationError("missing case documents in input snapshot under change:cases/**")
    return documents


def bind_receipt_to_success_event(
    receipt: CandidateValidationReceiptV1,
    *,
    validator_id: str,
    invocation_id: str,
    task_id: str,
    attempt_id: str,
    input_snapshot_id: str | None,
    write_set_id: str | None,
) -> None:
    """Fail closed when a folded receipt does not bind the success attempt."""
    if receipt.validator_id != validator_id:
        raise CandidateValidationError("receipt validator_id mismatch")
    if receipt.invocation_id != invocation_id:
        raise CandidateValidationError("receipt invocation_id mismatch")
    if receipt.task_id != task_id:
        raise CandidateValidationError("receipt task_id mismatch")
    if receipt.attempt_id != attempt_id:
        raise CandidateValidationError("receipt attempt_id mismatch")
    if input_snapshot_id is not None and receipt.input_snapshot_id != input_snapshot_id:
        raise CandidateValidationError("receipt input_snapshot_id mismatch")
    if write_set_id is not None and receipt.write_set_id != write_set_id:
        raise CandidateValidationError("receipt write_set_id mismatch")


# ---------------------------------------------------------------------------
# internals


_SEMANTICS_RULES: dict[str, list[str]] = {
    GENERATED_FILES_CANDIDATE_V1: [
        "derive_mapping_from_plan_and_cases",
        "require_summary_and_manifest_outputs",
        "reconcile_manifest_to_write_set_and_snapshot",
        "reject_support_shared_builder_selected_credit",
        "reject_positional_hypothesis_given",
        "reject_conflicting_top_level_import_bindings",
        "reject_unresolved_generated_python_local_imports",
        "reject_unresolved_pytest_fixture_parameters",
        "resolve_evidence_path_for_every_write",
    ],
    CODEGEN_FIX_CANDIDATE_V1: [
        "require_target_specific_intent",
        "proposal_and_authority_path_subsets",
        "exact_claimed_test_write_equality",
        "regular_file_add_or_content_modify_only",
        "baseline_before_digest_for_reused_authority",
        "high_risk_requires_approval_receipt",
        "code_owned_diff_safety_predicates",
    ],
    PLAN_MECHANICAL_CANDIDATE_V1: [
        "recompute_plan_checks_from_snapshot_and_write_set_review",
        "compare_canonical_plan_check_document_to_write_set",
    ],
    ARCHIVE_INTEGRITY_V1: [
        "require_archived_files_in_write_set",
        "compare_archived_bytes_to_snapshot_source_change",
        "reject_when_no_snapshot_source_compared",
    ],
    PROBLEM_APPLY_CANDIDATE_V1: [
        "require_apply_receipt_in_write_set",
        "reconcile_receipt_problem_id_to_snapshot_context",
    ],
    CROSS_ARTIFACT_INVARIANTS_V1: [
        "issue_candidate_digest",
        "case_review_mrc",
        "qa_yaml_case_automation",
        "case_design_source_verification",
    ],
}


def _require_canonical_digest_map(values: Mapping[str, str], *, label: str) -> None:
    if list(values.keys()) != sorted(values.keys()):
        raise ValueError(f"{label} keys must be canonically sorted")
    if len(set(values.keys())) != len(values):
        raise ValueError(f"{label} keys must be unique")
    for key, digest in values.items():
        if not key:
            raise ValueError(f"{label} must not contain empty keys")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"{label} values must be bare lowercase sha256 digests")


def _require_canonical_string_map(values: Mapping[str, str], *, label: str) -> None:
    if list(values.keys()) != sorted(values.keys()):
        raise ValueError(f"{label} keys must be canonically sorted")
    if len(set(values.keys())) != len(values):
        raise ValueError(f"{label} keys must be unique")
    for key, value in values.items():
        if not key or not value:
            raise ValueError(f"{label} must not contain empty keys or values")


def _assert_context_bindings(
    context: PrecommitValidationContext,
    *,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
) -> None:
    if write_set.write_set_id != context.write_set_id:
        raise CandidateValidationError("write_set_id does not match loaded write set")
    if write_set.base_tree_id != context.base_tree_id:
        raise CandidateValidationError("base_tree_id does not match write set")
    if input_snapshot.attempt_id != context.attempt_id:
        raise CandidateValidationError("attempt_id does not match input snapshot")
    if input_snapshot.task_id != context.task_id:
        raise CandidateValidationError("task_id does not match input snapshot")
    if input_snapshot.invocation_id != context.invocation_id:
        raise CandidateValidationError("invocation_id does not match input snapshot")
    outputs = dict(sorted(write_set.outputs_sha256.items()))
    if outputs != dict(sorted(context.output_digests.items())):
        raise CandidateValidationError("output_digests do not match write set outputs")


def _validate_codegen_fix_candidate(
    *,
    context: PrecommitValidationContext,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    layer: str,
    current_change_repo_path: str,
    project_root: Path | None,
) -> dict[str, object]:
    if layer not in {"api", "e2e"}:
        raise CandidateValidationError(f"codegen_fix_candidate only supports api/e2e, got {layer}")
    intent_logical = f"change:healing/{layer}-apply-intent.json"
    intent_digest = context.output_digests.get(intent_logical)
    if intent_digest is None:
        raise CandidateValidationError(f"missing required intent output: {intent_logical}")
    try:
        intent_bytes = store.read_object(intent_digest)
        intent_payload = json.loads(intent_bytes.decode("utf-8"))
        intent_model = (
            ApiCodegenFixApplyIntentV1 if layer == "api" else E2eCodegenFixApplyIntentV1
        ).model_validate(intent_payload)
    except Exception as exc:
        raise CandidateValidationError(f"invalid codegen fix intent: {exc}") from exc
    if intent_model.target != layer:
        raise CandidateValidationError("intent target mismatch")

    proposal = _load_snapshot_json(store, input_snapshot, logical="change:healing/fix-proposal.json")
    authority_raw = _load_snapshot_json(store, input_snapshot, logical="change:healing/fixer-authority.json")
    try:
        authority = FixerAuthorityV1.model_validate(authority_raw)
    except Exception as exc:
        raise CandidateValidationError(f"invalid fixer authority: {exc}") from exc
    target_authority = next((item for item in authority.targets if item.target == layer), None)
    if target_authority is None or target_authority.status != "ready":
        raise CandidateValidationError("fixer authority target missing or not ready")
    authority_paths = {path.repo_path: path for path in target_authority.paths}
    proposal_paths = _proposal_paths_for_target(proposal, layer)
    if not set(proposal_paths).issubset(set(authority_paths)):
        raise CandidateValidationError("proposal paths are not a subset of fixer-authority paths")

    try:
        tree_roots = pinned_write_set_roots(write_set)
    except EvidencePathError as exc:
        raise CandidateValidationError(str(exc)) from exc
    write_by_repo = _repository_test_writes(
        write_set,
        store=store,
        tree_roots=tree_roots,
        current_change_repo_path=current_change_repo_path,
    )
    # Exclude the intent artifact itself from claimed/write equality.
    claimed = list(intent_model.claimed_modified_paths)
    write_paths = sorted(write_by_repo)
    if intent_model.outcome == "applied":
        if not intent_model.proposal_ids or not claimed:
            raise CandidateValidationError("applied intent requires proposals and claimed paths")
        if write_paths != sorted(claimed):
            raise CandidateValidationError("claimed_modified_paths must equal test/testdata writes")
        if not set(claimed).issubset(set(proposal_paths)):
            raise CandidateValidationError("claimed paths exceed proposal authorization")
        if not set(claimed).issubset(set(authority_paths)):
            raise CandidateValidationError("claimed paths exceed fixer-authority authorization")
        for path in claimed:
            binding = write_by_repo[path]
            if binding["operation"] not in {"add", "modify"}:
                raise CandidateValidationError(f"invalid write operation for {path}")
            auth_path = authority_paths[path]
            if auth_path.disposition == "reused":
                before = binding.get("before_sha256")
                expected_before = auth_path.content_sha256.removeprefix("sha256:")
                if before != expected_before:
                    raise CandidateValidationError(f"reused authority before-digest mismatch for {path}")
            elif auth_path.disposition in {"generated", "updated"}:
                # Authority binds the codegen after digest. Fixer content-modify
                # must present that digest as before (not only add-with-before).
                if binding["operation"] == "modify":
                    before = binding.get("before_sha256")
                    expected_before = auth_path.content_sha256.removeprefix("sha256:")
                    if before != expected_before:
                        raise CandidateValidationError(f"authority before-digest mismatch for {path}")
    else:
        if write_paths:
            raise CandidateValidationError(f"{intent_model.outcome} intent must have zero test writes")
        if claimed:
            raise CandidateValidationError(f"{intent_model.outcome} intent must not claim paths")

    high_risk = _proposal_is_high_risk(proposal, intent_model.proposal_ids, layer)
    approval_digest = None
    if high_risk:
        approval_raw = _load_snapshot_json(
            store,
            input_snapshot,
            logical="change:healing/fixer-proposal-approval.json",
            required=True,
        )
        try:
            approval = FixerProposalApprovalReceiptV1.model_validate(approval_raw)
        except Exception as exc:
            raise CandidateValidationError(f"invalid approval receipt: {exc}") from exc
        approval_digest = sha256_bytes(canonical_json_bytes(approval))
        if approval.source_tree_id not in {context.current_tree_id, context.base_tree_id}:
            raise CandidateValidationError("approval receipt tree drift")
        _bind_approval_to_snapshot_artifacts(
            approval,
            store=store,
            input_snapshot=input_snapshot,
            proposal=proposal,
            authority=authority,
            layer=layer,
            policy_digest=context.policy_digest,
        )

    blobs: dict[str, bytes] = {}
    for binding in write_by_repo.values():
        for key in ("before_sha256", "after_sha256"):
            digest = binding.get(key)
            if isinstance(digest, str) and digest and digest not in blobs:
                try:
                    blobs[digest] = store.read_object(digest)
                except WorkspaceError:
                    continue
    roots = (
        current_product_hooks().load_product_code_roots(project_root)
        if project_root is not None
        else ["app", "web/src", "src"]
    )
    private_root = get_generated_files_contract(layer).private_test_root
    safety = evaluate_diff_safety(
        write_bindings=list(write_by_repo.values()),
        blobs=blobs,
        authorized_paths=proposal_paths,
        private_root=private_root,
        product_roots=roots,
    )
    if any(safety.values()):
        raise CandidateValidationError(f"diff-safety rejected candidate: {safety}")

    return {
        "approval_sha256": approval_digest,
        "authority_paths": sorted(authority_paths),
        "claimed_modified_paths": claimed,
        "high_risk": high_risk,
        "intent": intent_model.model_dump(mode="json"),
        "layer": layer,
        "proposal_paths": sorted(proposal_paths),
        "safety": safety,
        "validator_id": CODEGEN_FIX_CANDIDATE_V1,
        "write_paths": write_paths,
        "write_set_id": write_set.write_set_id,
    }


def _load_snapshot_json(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
    *,
    logical: str,
    required: bool = True,
) -> dict[str, object]:
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        if logical not in set(entry.logical_aliases):
            continue
        digest = entry.sha256.removeprefix("sha256:")
        try:
            raw = store.read_object(digest)
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise CandidateValidationError(f"unreadable snapshot artifact {logical}: {exc}") from exc
        if not isinstance(data, dict):
            raise CandidateValidationError(f"snapshot artifact must be object: {logical}")
        return data
    if required:
        raise CandidateValidationError(f"missing snapshot artifact: {logical}")
    return {}


def _snapshot_entry_prefixed_digest(
    snapshot: TaskInputSnapshotV1,
    *,
    logical: str,
) -> str:
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        if logical not in set(entry.logical_aliases):
            continue
        digest = entry.sha256
        return digest if digest.startswith("sha256:") else f"sha256:{digest}"
    raise CandidateValidationError(f"missing snapshot artifact digest: {logical}")


def _normalize_prefixed_digest(value: str) -> str:
    if value.startswith("sha256:"):
        return value
    return f"sha256:{value}"


def _bind_approval_to_snapshot_artifacts(
    approval: FixerProposalApprovalReceiptV1,
    *,
    store: TreeStore,
    input_snapshot: TaskInputSnapshotV1,
    proposal: Mapping[str, object],
    authority: FixerAuthorityV1,
    layer: str,
    policy_digest: str,
) -> None:
    """Reject forged approval digests/targets/paths that do not match snapshot artifacts."""
    del store  # bytes already resolved via snapshot digests; keep signature parallel to loaders
    expected = {
        "proposal_sha256": _snapshot_entry_prefixed_digest(
            input_snapshot, logical="change:healing/fix-proposal.json"
        ),
        "fixer_authority_sha256": _snapshot_entry_prefixed_digest(
            input_snapshot, logical="change:healing/fixer-authority.json"
        ),
        "entry_baseline_sha256": _snapshot_entry_prefixed_digest(
            input_snapshot, logical="change:healing/entry-baseline.json"
        ),
        "policy_sha256": _normalize_prefixed_digest(policy_digest),
    }
    actual = {
        "proposal_sha256": approval.proposal_sha256,
        "fixer_authority_sha256": approval.fixer_authority_sha256,
        "entry_baseline_sha256": approval.entry_baseline_sha256,
        "policy_sha256": approval.policy_sha256,
    }
    for field_name, expected_digest in expected.items():
        if actual[field_name] != expected_digest:
            raise CandidateValidationError(f"approval {field_name} does not match snapshot-bound artifact")
    if layer not in approval.targets:
        raise CandidateValidationError("approval targets omit current fixer layer")
    expected_paths: list[str] = []
    for target in approval.targets:
        expected_paths.extend(_proposal_paths_for_target(proposal, target))
        auth_target = next((item for item in authority.targets if item.target == target), None)
        if auth_target is None:
            raise CandidateValidationError(f"approval target missing from fixer-authority: {target}")
        auth_paths = {path.repo_path for path in auth_target.paths}
        proposal_paths = set(_proposal_paths_for_target(proposal, target))
        if not proposal_paths.issubset(auth_paths):
            raise CandidateValidationError(f"approval target {target} proposal paths exceed fixer-authority")
    if list(approval.paths) != sorted(set(expected_paths)):
        raise CandidateValidationError("approval paths do not match snapshot-bound proposal authorization")


def _proposal_paths_for_target(proposal: Mapping[str, object], layer: str) -> list[str]:
    paths: list[str] = []
    items = proposal.get("proposals")
    if not isinstance(items, list):
        return paths
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("target") != layer or not item.get("eligible", True):
            continue
        files = item.get("files_to_modify") or item.get("paths") or []
        if isinstance(files, list):
            paths.extend(str(path) for path in files)
    return sorted(set(paths))


def _proposal_is_high_risk(
    proposal: Mapping[str, object],
    proposal_ids: Sequence[str],
    layer: str,
) -> bool:
    items = proposal.get("proposals")
    if not isinstance(items, list):
        return False
    wanted = set(proposal_ids)
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("target") != layer:
            continue
        pid = str(item.get("proposal_id") or "")
        if wanted and pid not in wanted:
            continue
        risk = str(item.get("risk_level") or "").lower()
        if risk in {"high", "critical"} or item.get("needs_review") is True:
            return True
    return False


def _validate_generated_files_candidate(
    *,
    context: PrecommitValidationContext,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    plan_text: str,
    cases: Sequence[Mapping[str, object]],
    change_id: str,
    layer: str,
    current_change_repo_path: str,
) -> dict[str, object]:
    contract = get_generated_files_contract(layer)
    summary_digest = context.output_digests.get(contract.summary_path)
    manifest_digest = context.output_digests.get(contract.manifest_path)
    if summary_digest is None:
        raise CandidateValidationError(f"missing required summary output: {contract.summary_path}")
    if manifest_digest is None:
        raise CandidateValidationError(f"missing required manifest output: {contract.manifest_path}")

    try:
        manifest_bytes = store.read_object(manifest_digest)
    except WorkspaceError as exc:
        raise CandidateValidationError("manifest blob missing from CAS") from exc
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"))
        manifest = get_generated_files_model(layer).model_validate(payload)
    except Exception as exc:
        raise CandidateValidationError(f"invalid generated-files manifest: {exc}") from exc
    if manifest.change_id != change_id:
        raise CandidateValidationError("manifest change_id mismatch")
    layer_value = getattr(manifest, "layer", None)
    if layer_value != contract.layer:
        raise CandidateValidationError("manifest layer mismatch")

    try:
        mapping = load_codegen_mapping_from_snapshot(store, input_snapshot, layer=layer)
        relation = extract_layer_mapping(
            layer=layer,
            plan_text=plan_text,
            cases=cases,
            mapping=mapping,
        )
    except MappingExtractionError as exc:
        raise CandidateValidationError(str(exc)) from exc

    try:
        tree_roots = pinned_write_set_roots(write_set)
    except EvidencePathError as exc:
        raise CandidateValidationError(str(exc)) from exc

    write_by_repo = _repository_test_writes(
        write_set,
        store=store,
        tree_roots=tree_roots,
        current_change_repo_path=current_change_repo_path,
    )
    # Exact equality: every repository test/testdata write must appear as a
    # generated/updated manifest entry; reused entries write nothing.
    expected_write_paths = {
        entry.repo_path for entry in manifest.files if entry.disposition in {"generated", "updated"}
    }
    if set(write_by_repo) != expected_write_paths:
        raise CandidateValidationError(
            "manifest/write-set path set mismatch: "
            f"writes={sorted(write_by_repo)} manifest_generated_updated={sorted(expected_write_paths)}"
        )

    private_targets = selected_private_root_targets(relation, contract.private_test_root)
    # Fail closed: selected cases / private-root mappings require a non-empty
    # manifest. Summary+manifest with files:[] (and an empty write-set path set)
    # must not validate successfully.
    if (relation.selected_case_ids or private_targets) and not manifest.files:
        raise CandidateValidationError(
            "empty generated-files manifest cannot cover selected cases or "
            f"private-root targets: selected_case_ids={list(relation.selected_case_ids)} "
            f"private_targets={sorted(private_targets)}"
        )
    write_bindings: list[dict[str, object]] = []
    for entry in manifest.files:
        expected_cases = mapped_case_ids_for_path(relation, entry.repo_path)
        if entry.role == "test_entry":
            if tuple(entry.case_ids) != expected_cases:
                raise CandidateValidationError(
                    f"case_ids mismatch for {entry.repo_path}: "
                    f"manifest={list(entry.case_ids)} expected={list(expected_cases)}"
                )
        elif entry.case_ids:
            # support / shared_builder cannot claim selected-test credit.
            raise CandidateValidationError(f"{entry.role} entry cannot claim case_ids: {entry.repo_path}")

        if entry.disposition in {"generated", "updated"}:
            binding = write_by_repo.get(entry.repo_path)
            if binding is None:
                raise CandidateValidationError(f"missing write-set entry for {entry.repo_path}")
            if binding["operation"] not in {"add", "modify"}:
                raise CandidateValidationError(
                    f"generated/updated entry requires add/content-modify: {entry.repo_path}"
                )
            after = binding["after_sha256"]
            before = binding["before_sha256"]
            if not isinstance(after, str) or not after:
                raise CandidateValidationError(f"missing after digest for {entry.repo_path}")
            if entry.disposition == "generated" and binding["operation"] != "add":
                raise CandidateValidationError(f"generated disposition requires add: {entry.repo_path}")
            if entry.disposition == "updated" and binding["operation"] != "modify":
                raise CandidateValidationError(f"updated disposition requires modify: {entry.repo_path}")
            if binding["operation"] == "modify" and before == after:
                raise CandidateValidationError(f"no-op modify rejected: {entry.repo_path}")
            if entry.content_sha256 != f"sha256:{after}":
                raise CandidateValidationError(f"content_sha256 mismatch for {entry.repo_path}")
            write_bindings.append(binding)
        elif entry.disposition == "reused":
            if entry.repo_path not in private_targets:
                raise CandidateValidationError(
                    f"reused path is not a selected private-root mapping target: {entry.repo_path}"
                )
            if entry.role != "test_entry":
                raise CandidateValidationError(
                    f"reused support/shared_builder is not authorized: {entry.repo_path}"
                )
            snapshot_digest = _snapshot_digest_for_repo_path(input_snapshot, entry.repo_path)
            if snapshot_digest is None:
                raise CandidateValidationError(f"reused path missing from input snapshot: {entry.repo_path}")
            if entry.content_sha256 != snapshot_digest:
                raise CandidateValidationError(
                    f"reused content_sha256 does not match input snapshot: {entry.repo_path}"
                )
        else:
            raise CandidateValidationError(f"unknown disposition: {entry.disposition}")

    _validate_generated_python_local_imports(
        store=store,
        base_tree_id=write_set.base_tree_id,
        write_by_repo=write_by_repo,
        input_snapshot=input_snapshot,
    )
    if layer in {"api", "e2e", "fuzz"}:
        _validate_generated_pytest_fixture_closure(
            store=store,
            base_tree_id=write_set.base_tree_id,
            write_by_repo=write_by_repo,
            input_snapshot=input_snapshot,
            mapped_entries=relation.entries,
        )

    return {
        "change_id": change_id,
        "files": [entry.model_dump(mode="json") for entry in manifest.files],
        "layer": layer,
        "manifest_sha256": f"sha256:{manifest_digest}",
        "mapping": [
            {
                "case_id": item.case_id,
                "symbol": item.symbol,
                "target_file": item.target_file,
            }
            for item in relation.entries
        ],
        "selected_case_ids": list(relation.selected_case_ids),
        "summary_sha256": f"sha256:{summary_digest}",
        "validator_id": GENERATED_FILES_CANDIDATE_V1,
        "write_bindings": write_bindings,
        "write_set_id": write_set.write_set_id,
    }


def _validate_generated_python_local_imports(
    *,
    store: TreeStore,
    base_tree_id: str,
    write_by_repo: Mapping[str, Mapping[str, object]],
    input_snapshot: TaskInputSnapshotV1,
) -> None:
    """Reject generated Python that imports absent ``tests.*`` modules.

    Codegen runs in an isolated candidate tree. A local import that is absent
    from its base tree, frozen inputs, and candidate writes cannot become
    resolvable during execution, so fail before committing the candidate and
    let the bounded codegen retry repair all missing support modules together.
    """
    available_paths = _candidate_available_paths(
        store=store,
        base_tree_id=base_tree_id,
        write_by_repo=write_by_repo,
        input_snapshot=input_snapshot,
    )

    unresolved: list[str] = []
    for repo_path, binding in sorted(write_by_repo.items()):
        if not repo_path.endswith(".py"):
            continue
        after_sha256 = binding.get("after_sha256")
        if not isinstance(after_sha256, str) or not after_sha256:
            continue
        try:
            source = store.read_object(after_sha256).decode("utf-8")
            tree = ast.parse(source, filename=repo_path)
        except (WorkspaceError, UnicodeDecodeError, SyntaxError) as exc:
            raise CandidateValidationError(f"generated Python is not parseable: {repo_path}: {exc}") from exc

        import_collisions = _top_level_import_collisions(tree)
        if import_collisions:
            raise CandidateValidationError(
                "generated Python has conflicting imported bindings: "
                + ", ".join(
                    f"{repo_path}:{binding} ({', '.join(origins)})" for binding, origins in import_collisions
                )
            )

        positional_given = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_is_positional_hypothesis_given(item) for item in node.decorator_list)
        ]
        if positional_given:
            raise CandidateValidationError(
                "Hypothesis @given must use keyword strategies: "
                + ", ".join(f"{repo_path}:{name}" for name in sorted(positional_given))
            )

        for node in tree.body:
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.append(node.module)
            modules.extend(_assigned_local_module_names(node))
            for module in modules:
                if module != "tests" and not module.startswith("tests."):
                    continue
                if _local_module_is_available(module, available_paths):
                    continue
                if isinstance(node, ast.ImportFrom) and all(
                    alias.name != "*"
                    and _local_module_is_available(f"{module}.{alias.name}", available_paths)
                    for alias in node.names
                ):
                    continue
                unresolved.append(f"{repo_path} -> {module}")

    if unresolved:
        raise CandidateValidationError("unresolved local imports: " + ", ".join(sorted(set(unresolved))))


_PYTEST_RUNTIME_FIXTURES = frozenset(
    {
        "anyio_backend",
        "anyio_backend_name",
        "anyio_backend_options",
        "base_url",
        "browser",
        "browser_name",
        "browser_type",
        "cache",
        "capfd",
        "capfdbinary",
        "capsys",
        "capsysbinary",
        "context",
        "doctest_namespace",
        "event_loop",
        "monkeypatch",
        "new_context",
        "page",
        "pytestconfig",
        "record_property",
        "record_testsuite_property",
        "record_xml_attribute",
        "recwarn",
        "request",
        "tmp_path",
        "tmp_path_factory",
    }
)


def _validate_generated_pytest_fixture_closure(
    *,
    store: TreeStore,
    base_tree_id: str,
    write_by_repo: Mapping[str, Mapping[str, object]],
    input_snapshot: TaskInputSnapshotV1,
    mapped_entries: Sequence[object],
) -> None:
    """Reject mapped pytest functions whose fixture parameters have no provider.

    The check is intentionally static and candidate-tree based: it sees committed
    conftests, frozen inputs, and candidate writes without executing SUT fixtures.
    Framework fixtures are a closed allowlist; project fixtures must be defined or
    imported by the test module or an ancestor ``conftest.py``.
    """
    sources = _candidate_python_sources(
        store=store,
        base_tree_id=base_tree_id,
        write_by_repo=write_by_repo,
        input_snapshot=input_snapshot,
    )
    available_paths = _candidate_available_paths(
        store=store,
        base_tree_id=base_tree_id,
        write_by_repo=write_by_repo,
        input_snapshot=input_snapshot,
    )
    unresolved: list[str] = []
    unresolved_imports: list[str] = []
    for entry in mapped_entries:
        repo_path = getattr(entry, "target_file", None)
        symbol = getattr(entry, "symbol", None)
        if not isinstance(repo_path, str) or not isinstance(symbol, str):
            continue
        source = sources.get(repo_path)
        if source is None or not repo_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(source, filename=repo_path)
        except SyntaxError as exc:
            raise CandidateValidationError(f"generated Python is not parseable: {repo_path}: {exc}") from exc
        function_name = symbol.rsplit(".", 1)[-1]
        function = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
            ),
            None,
        )
        if function is None:
            continue
        required = _pytest_fixture_parameters(function)
        available = set(_PYTEST_RUNTIME_FIXTURES)
        available.update(_module_fixture_bindings(tree))
        _collect_unresolved_local_imports(
            repo_path=repo_path,
            tree=tree,
            available_paths=available_paths,
            unresolved=unresolved_imports,
        )
        for conftest_path in _ancestor_conftest_paths(repo_path):
            conftest_source = sources.get(conftest_path)
            if conftest_source is None:
                continue
            try:
                conftest_tree = ast.parse(conftest_source, filename=conftest_path)
                available.update(_module_fixture_bindings(conftest_tree))
                _collect_unresolved_local_imports(
                    repo_path=conftest_path,
                    tree=conftest_tree,
                    available_paths=available_paths,
                    unresolved=unresolved_imports,
                )
            except SyntaxError as exc:
                raise CandidateValidationError(
                    f"generated Python is not parseable: {conftest_path}: {exc}"
                ) from exc
        missing = sorted(required - available)
        if missing:
            unresolved.append(f"{repo_path}::{symbol} -> {', '.join(missing)}")
    if unresolved:
        raise CandidateValidationError("unresolved pytest fixtures: " + "; ".join(sorted(set(unresolved))))
    if unresolved_imports:
        raise CandidateValidationError(
            "unresolved local imports: " + ", ".join(sorted(set(unresolved_imports)))
        )


def _candidate_available_paths(
    *,
    store: TreeStore,
    base_tree_id: str,
    write_by_repo: Mapping[str, Mapping[str, object]],
    input_snapshot: TaskInputSnapshotV1,
) -> set[str]:
    try:
        paths = set(store._load_tree(base_tree_id).entries)
    except WorkspaceError as exc:
        raise CandidateValidationError(f"candidate base tree is unreadable: {base_tree_id}") from exc
    paths.update(
        entry.repo_relpath
        for entry in input_snapshot.entries
        if entry.kind == "file" and entry.repo_relpath is not None
    )
    paths.update(write_by_repo)
    return paths


def _collect_unresolved_local_imports(
    *,
    repo_path: str,
    tree: ast.Module,
    available_paths: set[str],
    unresolved: list[str],
) -> None:
    """Collect missing absolute imports whose root belongs to this repository."""
    local_roots = {PurePosixPath(path).parts[0] for path in available_paths if PurePosixPath(path).parts}
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
        for module in modules:
            if module.split(".", 1)[0] not in local_roots:
                continue
            if _local_module_is_available(module, available_paths):
                continue
            unresolved.append(f"{repo_path} -> {module}")


def _candidate_python_sources(
    *,
    store: TreeStore,
    base_tree_id: str,
    write_by_repo: Mapping[str, Mapping[str, object]],
    input_snapshot: TaskInputSnapshotV1,
) -> dict[str, str]:
    try:
        tree = store._load_tree(base_tree_id)
    except WorkspaceError as exc:
        raise CandidateValidationError(f"candidate base tree is unreadable: {base_tree_id}") from exc
    digests: dict[str, str] = {
        path: entry.sha256
        for path, entry in tree.entries.items()
        if entry.kind == "file" and path.startswith("tests/") and path.endswith(".py")
    }
    for entry in input_snapshot.entries:
        if (
            entry.kind == "file"
            and entry.sha256 is not None
            and entry.repo_relpath is not None
            and entry.repo_relpath.startswith("tests/")
            and entry.repo_relpath.endswith(".py")
        ):
            digests[entry.repo_relpath] = entry.sha256.removeprefix("sha256:")
    for repo_path, binding in write_by_repo.items():
        digest = binding.get("after_sha256")
        if repo_path.startswith("tests/") and repo_path.endswith(".py") and isinstance(digest, str):
            digests[repo_path] = digest

    sources: dict[str, str] = {}
    for path, digest in digests.items():
        try:
            sources[path] = store.read_object(digest).decode("utf-8")
        except (WorkspaceError, UnicodeDecodeError) as exc:
            raise CandidateValidationError(f"Python source is unreadable: {path}") from exc
    return sources


def _ancestor_conftest_paths(repo_path: str) -> tuple[str, ...]:
    path = PurePosixPath(repo_path)
    parents: list[str] = []
    current = path.parent
    while current.parts and current.parts[0] == "tests":
        parents.append((current / "conftest.py").as_posix())
        if current == PurePosixPath("tests"):
            break
        current = current.parent
    return tuple(reversed(parents))


def _module_fixture_bindings(tree: ast.Module) -> set[str]:
    pytest_aliases = {"pytest"}
    fixture_aliases = {"fixture"}
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    pytest_aliases.add(alias.asname or "pytest")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                binding = alias.asname or alias.name
                imported.add(binding)
                if node.module == "pytest" and alias.name == "fixture":
                    fixture_aliases.add(binding)

    fixtures = set(imported)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            _is_fixture_decorator(decorator, pytest_aliases, fixture_aliases)
            for decorator in node.decorator_list
        ):
            fixtures.add(node.name)
    return fixtures


def _is_fixture_decorator(
    decorator: ast.expr,
    pytest_aliases: set[str],
    fixture_aliases: set[str],
) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id in fixture_aliases
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "fixture"
        and isinstance(target.value, ast.Name)
        and target.value.id in pytest_aliases
    )


def _pytest_fixture_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    parameters = {
        arg.arg
        for arg in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    parameters.difference_update({"self", "cls"})
    parameters.difference_update(_direct_parametrize_names(function.decorator_list))
    parameters.difference_update(_hypothesis_given_names(function.decorator_list))
    return parameters


def _direct_parametrize_names(decorators: Sequence[ast.expr]) -> set[str]:
    names: set[str] = set()
    for decorator in decorators:
        if not isinstance(decorator, ast.Call) or not _decorator_name(decorator.func).endswith("parametrize"):
            continue
        indirect = next((item.value for item in decorator.keywords if item.arg == "indirect"), None)
        if isinstance(indirect, ast.Constant) and indirect.value is True:
            continue
        if not decorator.args:
            continue
        direct = _literal_parameter_names(decorator.args[0])
        if isinstance(indirect, (ast.List, ast.Tuple)):
            indirect_names = _literal_parameter_names(indirect)
            direct.difference_update(indirect_names)
        names.update(direct)
    return names


def _hypothesis_given_names(decorators: Sequence[ast.expr]) -> set[str]:
    return {
        keyword.arg
        for decorator in decorators
        if isinstance(decorator, ast.Call) and _decorator_name(decorator.func).endswith("given")
        for keyword in decorator.keywords
        if keyword.arg is not None
    }


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_parameter_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {name.strip() for name in node.value.split(",") if name.strip()}
    if isinstance(node, (ast.List, ast.Tuple)):
        return {
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value
        }
    return set()


def _assigned_local_module_names(node: ast.stmt) -> list[str]:
    """Return dynamic ``tests.*`` module names assigned to ``*_MODULE`` constants."""
    targets: list[ast.expr] = []
    value: ast.expr | None = None
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
        value = node.value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return []
    if not any(isinstance(target, ast.Name) and target.id.endswith("_MODULE") for target in targets):
        return []
    module = value.value
    if module != "tests" and not module.startswith("tests."):
        return []
    return [module]


def _top_level_import_collisions(tree: ast.Module) -> list[tuple[str, tuple[str, ...]]]:
    """Return imported names rebound to different origins at module scope."""
    origins_by_binding: dict[str, set[str]] = {}
    for node in tree.body:
        bindings: list[tuple[str, str]] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings.append((alias.asname, alias.name))
                else:
                    package = alias.name.split(".", 1)[0]
                    bindings.append((package, package))
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            bindings.extend(
                (alias.asname or alias.name, f"{module}.{alias.name}")
                for alias in node.names
                if alias.name != "*"
            )
        for binding, origin in bindings:
            origins_by_binding.setdefault(binding, set()).add(origin)
    return [
        (binding, tuple(sorted(origins)))
        for binding, origins in sorted(origins_by_binding.items())
        if len(origins) > 1
    ]


def _is_positional_hypothesis_given(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == "given"
    return isinstance(node.func, ast.Attribute) and node.func.attr == "given"


def _local_module_is_available(module: str, available_paths: set[str]) -> bool:
    module_path = module.replace(".", "/")
    return f"{module_path}.py" in available_paths or f"{module_path}/__init__.py" in available_paths


def _is_test_or_testdata(repo_path: str) -> bool:
    return (
        repo_path.startswith("tests/")
        or repo_path == _TESTDATA_ROOT
        or repo_path.startswith(_TESTDATA_ROOT + "/")
    )


def _repository_test_writes(
    write_set: WriteSet,
    *,
    store: TreeStore,
    tree_roots: Mapping[str, str],
    current_change_repo_path: str,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for entry in write_set.entries:
        try:
            resolved = resolve_evidence_path(
                logical_path=entry.logical_path,
                tree_roots=tree_roots,
                current_change_repo_path=current_change_repo_path,
            )
        except EvidencePathError as exc:
            raise CandidateValidationError(str(exc)) from exc
        repo_path = resolved.repo_relpath
        if repo_path is None or not _is_test_or_testdata(repo_path):
            continue
        if entry.operation == "delete":
            raise CandidateValidationError(f"delete writes are invalid for codegen candidate: {repo_path}")
        if entry.after_sha256 is None or entry.blob_sha256 is None:
            raise CandidateValidationError(f"write missing after/blob digest: {repo_path}")
        if entry.operation == "modify" and entry.before_sha256 == entry.after_sha256:
            raise CandidateValidationError(f"no-op modify rejected: {repo_path}")
        # Regular-file only: freeze already rejects symlink changes; defend here.
        try:
            store.read_object(entry.after_sha256)
        except WorkspaceError as exc:
            raise CandidateValidationError(f"write blob missing: {repo_path}") from exc
        if repo_path in result:
            raise CandidateValidationError(f"duplicate repository write path: {repo_path}")
        result[repo_path] = {
            "after_sha256": entry.after_sha256,
            "before_sha256": entry.before_sha256,
            "logical_path": entry.logical_path,
            "operation": entry.operation,
            "physical_relpath": resolved.physical_relpath,
            "repo_relpath": repo_path,
        }
    return result


def _validate_plan_mechanical_candidate(
    *,
    context: PrecommitValidationContext,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    cases: Sequence[Mapping[str, object]],
    change_id: str,
    layer: str,
    project_root: Path | None,
) -> dict[str, object]:
    del context
    del project_root
    try:
        profile = get_layer_assurance_profile(layer)
    except ValueError as exc:
        raise CandidateValidationError(str(exc)) from exc

    plan_texts = _load_plan_texts_from_snapshot(store, input_snapshot, layer=layer)
    review_logical = f"change:{profile.review_artifact}"
    review_payload = _load_write_set_json(store, write_set, logical=review_logical)
    data_knowledge = _load_l1_from_snapshot(store, input_snapshot)
    try:
        expected = run_layer_plan_checks(
            layer=layer,
            cases=cases,
            plan_texts=plan_texts,
            review_payload=review_payload,
            data_knowledge=data_knowledge,
            change_id=change_id,
            require_review=True,
        )
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(f"cannot recompute plan-checks for layer {layer}: {exc}") from exc

    missing_capabilities = compute_missing_capabilities(review_payload, data_knowledge)
    if (
        review_payload.get("decision") in {"pass", "approved"}
        and review_payload.get("codegen_readiness") in {"ready", "ready_with_warnings"}
        and missing_capabilities
    ):
        raise CandidateValidationError(
            "ready plan review references missing L1 capabilities: " + ", ".join(missing_capabilities)
        )

    checks_logical = f"change:{profile.checks_artifact}"
    written_bytes = _load_write_set_bytes(store, write_set, logical=checks_logical)
    try:
        written = PlanCheckDocument.model_validate(json.loads(written_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CandidateValidationError(
            f"write-set plan-checks document is not a valid PlanCheckDocument for layer {layer}: {exc}"
        ) from exc
    expected_bytes = canonical_json_bytes(expected)
    written_canonical = canonical_json_bytes(written)
    if written_canonical != expected_bytes:
        raise CandidateValidationError(
            f"write-set plan-checks document does not match snapshot recomputation for layer {layer}"
        )

    return {
        "checks_logical": checks_logical,
        "document_sha256": sha256_bytes(expected_bytes),
        "layer": layer,
    }


_ARCHIVE_GENERATED_NAMES = frozenset({"archive-summary.md"})


def _validate_archive_integrity(
    *,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    change_id: str,
) -> dict[str, object]:
    prefix = f"project:qa/archive/{change_id}/"
    archived = {
        path: digest
        for path, digest in write_set.outputs_sha256.items()
        if path.startswith(prefix) and not path.endswith("/")
    }
    if not archived:
        archived = {
            entry.logical_path: entry.after_sha256
            for entry in write_set.entries
            if entry.after_sha256 is not None
            and entry.logical_path.startswith(prefix)
            and not entry.logical_path.endswith("/")
        }
    if not archived:
        raise CandidateValidationError(f"archive integrity: write-set missing files under {prefix}")
    compared = 0
    for logical, digest in sorted(archived.items()):
        rel = logical.removeprefix(prefix)
        source_bytes: bytes | None = None
        for source_logical in (f"project:qa/changes/{change_id}/{rel}", f"change:{rel}"):
            try:
                source_bytes = _load_snapshot_bytes(store, input_snapshot, logical=source_logical)
                break
            except CandidateValidationError:
                continue
        if source_bytes is None:
            if Path(rel).name in _ARCHIVE_GENERATED_NAMES and "/" not in rel:
                continue
            raise CandidateValidationError(f"archive integrity: missing snapshot source for {rel}")
        expected = hashlib.sha256(source_bytes).hexdigest()
        actual = digest.removeprefix("sha256:")
        if actual != expected:
            raise CandidateValidationError(
                f"archive integrity: {rel} digest {actual} does not match snapshot source {expected}"
            )
        try:
            archived_bytes = store.read_object(actual)
        except WorkspaceError as exc:
            raise CandidateValidationError(
                f"archive integrity: write-set blob missing for {logical}"
            ) from exc
        if hashlib.sha256(archived_bytes).hexdigest() != actual:
            raise CandidateValidationError(f"archive integrity: write-set blob mismatch for {logical}")
        compared += 1
    if compared == 0:
        raise CandidateValidationError("archive integrity: no snapshot sources compared for archived files")
    return {"archived_files": len(archived), "compared_to_source": compared, "change_id": change_id}


def _validate_problem_apply_candidate(
    *,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
) -> dict[str, object]:
    receipts = sorted(
        path
        for path in write_set.outputs_sha256
        if path.startswith("change:issue-review/") and path.endswith("/apply-receipt.json")
    )
    if not receipts:
        raise CandidateValidationError(
            "problem apply: write-set missing change:issue-review/**/apply-receipt.json"
        )
    logical = receipts[0]
    payload = _load_write_set_json(store, write_set, logical=logical)
    if not isinstance(payload, dict):
        raise CandidateValidationError(f"problem apply: {logical} must be a mapping")
    problem_id = payload.get("problem_id")
    review_id = payload.get("review_id")
    action = payload.get("action")
    if not isinstance(problem_id, str) or not problem_id.strip():
        raise CandidateValidationError(f"problem apply: {logical} missing problem_id")
    if not isinstance(review_id, str) or not review_id.strip():
        raise CandidateValidationError(f"problem apply: {logical} missing review_id")
    if not isinstance(action, str) or not action.strip():
        raise CandidateValidationError(f"problem apply: {logical} missing action")
    context_logical = f"change:issue-review/{review_id}/context.json"
    try:
        context_text = _load_snapshot_text(store, input_snapshot, logical=context_logical)
        saved = json.loads(context_text)
    except (CandidateValidationError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(
            f"problem apply: snapshot context {context_logical} unreadable: {exc}"
        ) from exc
    if not isinstance(saved, dict) or saved.get("problem_id") != problem_id:
        raise CandidateValidationError(
            f"problem apply: receipt problem_id {problem_id!r} does not match snapshot context"
        )
    return {
        "receipt_logical": logical,
        "problem_id": problem_id,
        "review_id": review_id,
        "action": action,
        "expected_problem_version": saved.get("expected_problem_version"),
    }


def _validate_cross_artifact_invariants(
    *,
    store: TreeStore,
    write_set: WriteSet,
    input_snapshot: TaskInputSnapshotV1,
    project_root: Path | None,
    host_change_dir: Path | None = None,
) -> dict[str, object]:
    from assurance_kernel.workflow.graph.invariants import run_cross_artifact_invariants

    try:
        ran = run_cross_artifact_invariants(
            store=store,
            write_set=write_set,
            input_snapshot=input_snapshot,
            project_root=project_root,
            host_change_dir=host_change_dir,
        )
    except ValueError as exc:
        raise CandidateValidationError(str(exc)) from exc
    return {"invariant_ids": ran}


def _load_plan_texts_from_snapshot(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
    *,
    layer: str,
) -> dict[str, str]:
    profile = get_layer_assurance_profile(layer)
    texts: dict[str, str] = {}
    for rel in profile.plan_artifacts:
        logical = f"change:{rel}"
        texts[rel] = _load_snapshot_text(store, snapshot, logical=logical)
    return texts


def _load_snapshot_bytes(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
    *,
    logical: str,
) -> bytes:
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        if logical not in set(entry.logical_aliases):
            continue
        digest = entry.sha256.removeprefix("sha256:")
        try:
            return store.read_object(digest)
        except WorkspaceError as exc:
            raise CandidateValidationError(f"unreadable snapshot artifact {logical}: {exc}") from exc
    raise CandidateValidationError(f"missing snapshot artifact: {logical}")


def _load_snapshot_text(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
    *,
    logical: str,
) -> str:
    try:
        return _load_snapshot_bytes(store, snapshot, logical=logical).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateValidationError(f"unreadable snapshot artifact {logical}: {exc}") from exc


def _load_l1_from_snapshot(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
) -> dict[str, object]:
    digest = _snapshot_digest_for_repo_path(snapshot, ".aa/data-knowledge.yaml")
    if digest is None:
        raise CandidateValidationError("missing repo L1 artifact in input snapshot: .aa/data-knowledge.yaml")
    bare = digest.removeprefix("sha256:")
    try:
        raw = store.read_object(bare)
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise CandidateValidationError(f"unreadable snapshot L1 artifact: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateValidationError("snapshot L1 artifact must be a YAML mapping")
    return data


def _load_write_set_bytes(store: TreeStore, write_set: WriteSet, *, logical: str) -> bytes:
    digest = write_set.outputs_sha256.get(logical)
    if digest is None:
        raise CandidateValidationError(f"missing write-set output: {logical}")
    try:
        return store.read_object(digest)
    except WorkspaceError as exc:
        raise CandidateValidationError(f"write-set blob missing for {logical}") from exc


def _load_write_set_json(store: TreeStore, write_set: WriteSet, *, logical: str) -> dict[str, object]:
    raw = _load_write_set_bytes(store, write_set, logical=logical)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(f"write-set artifact is not JSON: {logical}: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateValidationError(f"write-set artifact must be a JSON object: {logical}")
    return data


def _snapshot_digest_for_repo_path(snapshot: TaskInputSnapshotV1, repo_path: str) -> str | None:
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        if entry.repo_relpath == repo_path:
            return entry.sha256
        aliases = set(entry.logical_aliases)
        if f"repo:{repo_path}" in aliases or f"project:{repo_path}" in aliases:
            return entry.sha256
    return None


__all__ = [
    "CODEGEN_FIX_CANDIDATE_V1",
    "COMMIT_SAFETY_INVENTORY",
    "CandidateValidationError",
    "CandidateValidationReceiptV1",
    "GENERATED_FILES_CANDIDATE_V1",
    "IMPLEMENTED_PRECOMMIT_VALIDATORS",
    "KNOWN_PRECOMMIT_VALIDATORS",
    "PLAN_MECHANICAL_CANDIDATE_V1",
    "ARCHIVE_INTEGRITY_V1",
    "PROBLEM_APPLY_CANDIDATE_V1",
    "CROSS_ARTIFACT_INVARIANTS_V1",
    "PrecommitValidationContext",
    "bind_receipt_to_success_event",
    "codegen_plan_logical_path",
    "infer_assurance_layer",
    "load_candidate_receipt",
    "load_case_documents_from_snapshot",
    "load_codegen_mapping_from_snapshot",
    "load_plan_text_from_snapshot",
    "resolve_precommit_validator",
    "validate_candidate",
    "validate_precommit_validator_id",
    "validator_semantics_digest",
    "verify_candidate_receipt",
]
