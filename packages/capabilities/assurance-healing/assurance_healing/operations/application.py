"""Authenticate and verify an approved, existing-test implementation repair."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, Protocol, cast

from agent_runtime_contracts.ops import OutputError
from graph_engine.artifacts import ArtifactReadError, open_artifact, read_workspace_file
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest
from pydantic import ValidationError

from assurance_execution.contracts import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import APPLIED_REPAIR_PATH
from assurance_generation.contracts.codegen import durable_test_path
from assurance_generation.contracts.mapping import ClosedMappingV1, selected_test_file
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.application import (
    VERIFIED_REPAIR_PATH,
    ApplyTestRepairInputV1,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.effects import ProposalApprovedIntentV1
from assurance_healing.operations.keys import derive_approval_id
from assurance_intake.contracts import LoopRoundHistoryV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.loop_history import build_loop_round_history

_REPAIR_DERIVED = {VERIFIED_REPAIR_PATH, APPLIED_REPAIR_PATH}


class _Workspace(Protocol):
    project_root: Path
    write_root: Path


def repair_history_path(*, coverage_epoch: int, repair_round: int) -> str:
    return f"qa/results/healing/epochs/{coverage_epoch}/rounds/{repair_round}/repair.json"


def expected_repair_history(
    business: ApplyTestRepairInputV1,
    verified: VerifiedTestRepairV1,
) -> LoopRoundHistoryV1:
    input_refs = tuple(
        sorted(
            (
                *business.reviewed_case.preparation_refs,
                *business.reviewed_case.case_refs,
                business.reviewed_case.review_ref,
                business.proposal_ref,
                business.execution_ref,
                business.mapping_ref,
            ),
            key=lambda item: item.path,
        )
    )
    input_digest = engine_digest(cast(JSONValue, [item.model_dump(mode="json") for item in input_refs]))
    source_by_path = {item.path: item for item in (*input_refs, *verified.changed_test_refs)}
    return build_loop_round_history(
        change_id=business.change_id,
        coverage_epoch=business.coverage_epoch,
        loop_kind="implementation_repair",
        family=None,
        round_index=business.repair_round,
        outcome="applied",
        review_input_digest=input_digest,
        source_refs=tuple(source_by_path[path] for path in sorted(source_by_path)),
    )


def _read_repair_file(root: Path, relative: str) -> bytes:
    try:
        return read_workspace_file(root, relative)
    except ArtifactReadError as error:
        raise OutputError(str(error)) from error


def _authenticate_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    try:
        return open_artifact(root, ref)
    except ArtifactReadError as error:
        if error.reason == "digest":
            raise OutputError(f"evidence digest changed: {ref.path}") from error
        raise OutputError(str(error)) from error


def _load_ref(root: Path, ref: EvidenceArtifactRefV1, model: type[Any]) -> Any:
    try:
        return model.model_validate_json(_authenticate_ref(root, ref))
    except ValidationError as error:
        raise OutputError(str(error)) from error


def _mapped_sources(change_id: str, mapping: ClosedMappingV1) -> dict[str, set[str]]:
    del change_id
    sources: dict[str, set[str]] = {}
    for entry in mapping.mappings:
        source = durable_test_path(selected_test_file(entry.test))
        sources.setdefault(source, set()).add(entry.test.partition("::")[2])
    return sources


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _oracle_fingerprint(tree: ast.AST) -> tuple[str, ...]:
    protected: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            protected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "pytest.raises" or name.split(".")[-1].startswith("assert"):
                protected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                rendered = ast.dump(decorator, include_attributes=False)
                if "skip" in rendered.lower() or "xfail" in rendered.lower():
                    protected.append(rendered)
    return tuple(sorted(protected))


def _defined_test_symbols(tree: ast.AST) -> set[str]:
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
    return symbols


def _prove_implementation_only(before: bytes, after: bytes, symbols: set[str], path: str) -> None:
    if before == after:
        raise OutputError(f"repair did not change bytes: {path}")
    try:
        before_tree = ast.parse(before.decode("utf-8"))
        after_tree = ast.parse(after.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as error:
        raise OutputError(f"repair is not a provable Python test implementation change: {path}") from error
    if _oracle_fingerprint(before_tree) != _oracle_fingerprint(after_tree):
        raise OutputError(f"repair changes a reviewed oracle or expectation: {path}")
    before_symbols = _defined_test_symbols(before_tree)
    after_symbols = _defined_test_symbols(after_tree)
    if any(symbol.split("::")[-1] not in before_symbols for symbol in symbols):
        raise OutputError(f"mapping names a missing existing test identity: {path}")
    if any(symbol.split("::")[-1] not in after_symbols for symbol in symbols):
        raise OutputError(f"repair removes or renames a mapped test identity: {path}")


def approved_sources(business: ApplyTestRepairInputV1, root: Path) -> dict[str, set[str]]:
    for ref in (
        *business.reviewed_case.preparation_refs,
        *business.reviewed_case.case_refs,
        business.reviewed_case.review_ref,
        business.proposal_ref,
        business.execution_ref,
        business.mapping_ref,
        *business.source_refs,
    ):
        _authenticate_ref(root, ref)
    if business.approval_ref is None:
        raise OutputError("repair application requires an authenticated approval")

    proposal = _load_ref(root, business.proposal_ref, FixProposalResultV1)
    approval = _load_ref(root, business.approval_ref, ProposalApprovedIntentV1)
    execution = _load_ref(root, business.execution_ref, ExecutionEvidenceV1)
    mapping = _load_ref(root, business.mapping_ref, ClosedMappingV1)
    if proposal.change_id != business.change_id or approval.change_id != business.change_id:
        raise OutputError("proposal or approval belongs to another change")
    if approval.proposal_digest != engine_digest(cast(JSONValue, proposal.model_dump(mode="json"))):
        raise OutputError("approval does not authenticate the repair proposal")
    expected_approval_id = derive_approval_id(
        owner_id=approval.owner_id,
        candidate_digest=approval.candidate_digest,
        baseline_digest=approval.baseline_digest,
        policy_digest=approval.policy_digest,
        proposal_digest=approval.proposal_digest,
    )
    if approval.approval_id != expected_approval_id or approval.owner_id != "assurance.healing":
        raise OutputError("approval identity is not authentic")
    eligible = tuple(
        item
        for item in proposal.proposals
        if item.eligible and not item.needs_review and item.risk_level != "critical"
    )
    proposed_sources = {path for item in eligible for path in item.files_to_modify}
    proposed_layers = {item.target for item in eligible}
    if not proposed_sources:
        raise OutputError("proposal has no eligible existing-test repair")
    if not proposed_sources <= set(approval.paths) or not proposed_layers <= set(approval.targets):
        raise OutputError("approval scope does not cover the proposed repair")

    mapped_sources = _mapped_sources(business.change_id, mapping)
    if not proposed_sources <= set(mapped_sources):
        raise OutputError("proposal changes a file outside the reviewed mapping")
    if mapping != execution.mapping:
        raise OutputError("mapping membership or test identity changed")
    if execution.status != "failed" or not any(item.status == "failed" for item in execution.results):
        raise OutputError("repair application requires failed existing-test evidence")
    if not proposed_sources <= set(business.allowed_test_paths) or not proposed_sources <= {
        ref.path for ref in business.source_refs
    }:
        raise OutputError("proposal paths must be current generated test sources")
    return {path: mapped_sources[path] for path in sorted(proposed_sources)}


def verify_application(
    business: ApplyTestRepairInputV1,
    result: TestRepairResultV1,
    context: _Workspace,
) -> VerifiedTestRepairV1:
    sources = approved_sources(business, context.project_root)
    outputs = set(result.output_files)
    if outputs != set(sources):
        raise OutputError("repair output set must exactly equal the approved candidate write set")
    staged = {
        relative
        for path in context.write_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (relative := path.relative_to(context.write_root).as_posix()) not in _REPAIR_DERIVED
        and "/healing/epochs/" not in f"/{relative}"
    }
    if staged != outputs:
        raise OutputError("repair result does not match the actual candidate write set")
    source_by_path = {ref.path: ref for ref in business.source_refs}
    changed: list[EvidenceArtifactRefV1] = []
    for path in result.output_files:
        before = _authenticate_ref(context.project_root, source_by_path[path])
        after = _read_repair_file(context.write_root, path)
        symbols = sources[path]
        _prove_implementation_only(before, after, symbols, path)
        changed.append(EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(after).hexdigest()))
    return VerifiedTestRepairV1(
        change_id=business.change_id,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        coverage_epoch=business.coverage_epoch,
        repair_round=business.repair_round,
        changed_test_refs=tuple(sorted(changed, key=lambda item: (item.path, item.digest))),
        mapping_ref=business.mapping_ref,
    )


__all__ = [
    "approved_sources",
    "expected_repair_history",
    "repair_history_path",
    "verify_application",
]
