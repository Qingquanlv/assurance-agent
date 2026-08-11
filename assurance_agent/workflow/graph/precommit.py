"""D14 candidate validation receipts and closed precommit validator registry.

Both ``generated_files_candidate/v1`` and ``codegen_fix_candidate/v1`` are
implemented. Packaged contracts still select neither until Task 15.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import field_validator, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.artifacts.models.healing_codegen import (
    ApiCodegenFixApplyIntentV1,
    E2eCodegenFixApplyIntentV1,
    FixerAuthorityV1,
    FixerProposalApprovalReceiptV1,
)
from assurance_agent.exceptions import AaError
from assurance_agent.verification.generated_entries import (
    MappingExtractionError,
    extract_layer_mapping,
    mapped_case_ids_for_path,
    selected_private_root_targets,
)
from assurance_agent.verification.generated_files import (
    get_generated_files_contract,
    get_generated_files_model,
)
from assurance_agent.workflow.graph.diff_safety import evaluate_diff_safety
from assurance_agent.workflow.graph.evidence_paths import (
    EvidencePathError,
    pinned_write_set_roots,
    resolve_evidence_path,
)
from assurance_agent.workflow.graph.task_inputs import TaskInputSnapshotV1
from assurance_agent.workflow.graph.workspace import TreeStore, WriteSet, WorkspaceError
from assurance_agent.workflow.healing.safety import load_product_code_roots

GENERATED_FILES_CANDIDATE_V1 = "generated_files_candidate/v1"
CODEGEN_FIX_CANDIDATE_V1 = "codegen_fix_candidate/v1"

KNOWN_PRECOMMIT_VALIDATORS: frozenset[str] = frozenset(
    {GENERATED_FILES_CANDIDATE_V1, CODEGEN_FIX_CANDIDATE_V1}
)
IMPLEMENTED_PRECOMMIT_VALIDATORS: frozenset[str] = frozenset(
    {GENERATED_FILES_CANDIDATE_V1, CODEGEN_FIX_CANDIDATE_V1}
)

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
        "assurance_agent.workflow.graph.precommit._validate_codegen_fix_candidate",
        "helper",
        CODEGEN_FIX_CANDIDATE_V1,
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
    if validator_id not in KNOWN_PRECOMMIT_VALIDATORS:
        raise CandidateValidationError(f"unknown precommit validator: {validator_id}")
    if validator_id not in IMPLEMENTED_PRECOMMIT_VALIDATORS:
        raise CandidateValidationError(f"precommit validator not implemented: {validator_id}")


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
) -> tuple[str, CandidateValidationReceiptV1]:
    """Dispatch one registered validator; return CAS receipt id and receipt."""
    if validator_id not in KNOWN_PRECOMMIT_VALIDATORS:
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
    else:
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
        "skill:aa-e2e-codegen": "e2e",
        "skill:aa-fuzz-codegen": "fuzz",
        "skill:aa-performance-codegen": "performance",
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


def load_case_documents_from_snapshot(
    store: TreeStore,
    snapshot: TaskInputSnapshotV1,
) -> list[dict[str, object]]:
    """Load case YAML/JSON documents from the frozen input snapshot only."""
    documents: list[dict[str, object]] = []
    seen_aliases: set[str] = set()
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.sha256 is None:
            continue
        case_aliases = sorted(
            alias
            for alias in entry.logical_aliases
            if alias.startswith("change:cases/") and alias.endswith((".yaml", ".yml", ".json"))
        )
        if not case_aliases:
            continue
        for alias in case_aliases:
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
        digest = entry.sha256.removeprefix("sha256:")
        try:
            raw = store.read_object(digest)
        except WorkspaceError as exc:
            raise CandidateValidationError(f"case document blob missing from CAS: {case_aliases[0]}") from exc
        try:
            text = raw.decode("utf-8")
            if case_aliases[0].endswith(".json"):
                data = json.loads(text)
            else:
                data = yaml.safe_load(text)
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
    roots = load_product_code_roots(project_root) if project_root is not None else ["app", "web/src", "src"]
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
        relation = extract_layer_mapping(layer=layer, plan_text=plan_text, cases=cases)
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
    "PrecommitValidationContext",
    "bind_receipt_to_success_event",
    "codegen_plan_logical_path",
    "infer_assurance_layer",
    "load_candidate_receipt",
    "load_case_documents_from_snapshot",
    "load_plan_text_from_snapshot",
    "validate_candidate",
    "validate_precommit_validator_id",
    "validator_semantics_digest",
    "verify_candidate_receipt",
]
