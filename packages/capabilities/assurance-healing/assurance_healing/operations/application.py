"""Prepare and finalize an approved, existing-test implementation repair."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, cast

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentRunResult,
    InstructionPart,
    prompt_model_json,
    with_validation_retry,
)
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest
from pydantic import ValidationError

from assurance_execution.contracts import ExecutionEvidenceV1
from assurance_generation.contracts.codegen import durable_test_path
from assurance_generation.contracts.mapping import ClosedMappingV1, selected_test_file
from assurance_healing.contracts.agent import AgentBindingDataV1
from assurance_healing.contracts.application import (
    ApplyTestRepairInputV1,
    TestRepairResultV1,
    VerifiedTestRepairV1,
)
from assurance_healing.contracts.effects import ProposalApprovedIntentV1
from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.operations.agent import agent_workspace, result_contract
from assurance_healing.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_input,
)
from assurance_healing.operations.keys import derive_approval_id
from assurance_healing.resource_loader import resource_text
from assurance_intake.contracts import LoopRoundHistoryV1
from assurance_intake.operations.loop_history import build_loop_round_history
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

APPLICATION_SKILL = "skills/aa-apply-test-repair/SKILL.md"
APPLICATION_RESULT_ID = "assurance.healing.result.applied-test-repair.v1"
APPLICATION_RESULT_FILE = "result-contracts/applied-test-repair.v1.schema.json"


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


def authenticate_repair_history(
    context: TaskContext,
    *,
    relative: str,
    expected: LoopRoundHistoryV1,
) -> EvidenceArtifactRefV1:
    path = context.write_root.joinpath(*relative.split("/"))
    try:
        authored = LoopRoundHistoryV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        raise OutputError(f"invalid repair history: {error}") from error
    if authored != expected:
        raise OutputError("repair.json does not match the locked repair inputs")
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(path.read_bytes()).hexdigest())


def _canonical_file(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in posix.parts):
        raise OutputError(f"repair path must be canonical and relative: {relative}")
    path = root.joinpath(*posix.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise OutputError(f"repair file is missing: {relative}") from error
    if resolved != path or path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise OutputError(f"repair file must be a regular single-link file: {relative}")
    return path


def _authenticate_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    data = _canonical_file(root, ref.path).read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise OutputError(f"evidence digest changed: {ref.path}")
    return data


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


def _approved_sources(business: ApplyTestRepairInputV1, root: Path) -> dict[str, set[str]]:
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


def _verify_application(
    business: ApplyTestRepairInputV1,
    result: TestRepairResultV1,
    context: TaskContext,
) -> VerifiedTestRepairV1:
    if result.change_id != business.change_id:
        raise OutputError("repair result change_id does not match the locked change")
    approved_sources = _approved_sources(business, context.project_root)
    outputs = set(result.output_files)
    if outputs != set(approved_sources):
        raise OutputError("repair output set must exactly equal the approved candidate write set")
    staged = {
        path.relative_to(context.write_root).as_posix()
        for path in context.write_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "/healing/epochs/" not in f"/{path.relative_to(context.write_root).as_posix()}"
    }
    if staged != outputs:
        raise OutputError("repair result does not match the actual candidate write set")
    source_by_path = {ref.path: ref for ref in business.source_refs}
    changed: list[EvidenceArtifactRefV1] = []
    for path in result.output_files:
        before = _authenticate_ref(context.project_root, source_by_path[path])
        after = _canonical_file(context.write_root, path).read_bytes()
        symbols = approved_sources[path]
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


class ApplyTestRepairPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = ApplyTestRepairInputV1.model_validate(request.input)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            approved_paths = tuple(_approved_sources(business, context.project_root))
            payload = prompt_model_json(business)
            payload["allowed_test_paths"] = list(approved_paths)
            request_payload = AgentRunRequest(
                instructions=with_validation_retry(
                    (
                        InstructionPart.text("text/plain", resource_text(APPLICATION_SKILL)),
                        InstructionPart.from_json(payload),
                    ),
                    business.validation_error,
                ),
                result_contract=result_contract(APPLICATION_RESULT_ID, APPLICATION_RESULT_FILE),
                execution=binding.execution,
                workspace=agent_workspace(
                    context,
                    allowed_outputs=approved_paths,
                    agent_profile=binding.agent_profile,
                    scope_id=business.change_id,
                ),
                request_policy_digest=binding.request_policy_digest,
                request_config_digest=binding.request_config_digest,
            )
            return TaskOutcome.succeeded(request_payload.model_dump(mode="json"))
        except (ValidationError, InputError, OutputError) as error:
            return failed_input(InputError(str(error)))


class ApplyTestRepairFinalizeInputV1(ApplyTestRepairInputV1):
    agent_result: AgentRunResult


class ApplyTestRepairFinalizeHandler:
    input_model = ApplyTestRepairFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = validate_input(ApplyTestRepairFinalizeInputV1, request.input)
            envelope = business.agent_result
            try:
                result = TestRepairResultV1.model_validate(thaw_json(envelope.result_payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            verified = _verify_application(business, result, context)
            expected_history = expected_repair_history(business, verified)
            history_relative = repair_history_path(
                coverage_epoch=business.coverage_epoch,
                repair_round=business.repair_round,
            )
            history_path = context.write_root / history_relative
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_bytes(canonical_json_bytes(expected_history.model_dump(mode="json")) + b"\n")
            return TaskOutcome.succeeded(cast(JSONValue, verified.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


__all__ = [
    "ApplyTestRepairFinalizeHandler",
    "ApplyTestRepairPrepareHandler",
    "authenticate_repair_history",
    "expected_repair_history",
    "repair_history_path",
]
