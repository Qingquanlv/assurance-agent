"""Independent store authentication for repairable pre-dispatch generation defects."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Literal, Self, cast

from assurance_execution.contracts.workflow import (
    ExecutionAttemptBindingV1,
    VerifiedGenerationDefectCycleV1,
)
from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_generation.contracts.workflow import VerifiedGenerationDefectV1
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.contracts import TerminalReceiptRef
from graph_engine.attempts.host_authority import HostAuthorityError, HostSealingAuthority
from graph_engine.attempts.host_receipts import TerminalReceiptError, TerminalReceiptStore
from graph_engine.attempts.production_host import invocation_activity_receipts_root
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.attempts.workspace import TaskWorkspaceViolation, authenticate_promotion_receipt
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel, TaskOutcome
from pydantic import Field, model_validator


_CURRENT_SELECTION_NAMESPACE = "assurance.execution.generation-defect-current.v1"


class GenerationDefectSelectionScopeV1(FrozenModel):
    """Trusted route scope for one repairable pre-dispatch execution attempt."""

    schema_version: Literal["1"] = "1"
    project_root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    public_entrypoint: str = Field(min_length=1)
    semantic_node_id: Literal["execution.execute", "execution.run"]
    repair_round: int = Field(ge=0)


class GenerationDefectSelectionV1(FrozenModel):
    """Exact attempt selected as current by the authenticated product route."""

    schema_version: Literal["1"] = "1"
    attempt_key: AttemptKey
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cycle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_id: None = None
    terminal_receipt: TerminalReceiptRef
    promotion_receipt: ReceiptRef


class CurrentGenerationDefectAuthorityV1(FrozenModel):
    """Product-recorded current execution, reauthenticated from engine-owned journals."""

    schema_version: Literal["1"] = "1"
    binding: ExecutionAttemptBindingV1
    cycle: VerifiedGenerationDefectCycleV1
    execution_id: None = None
    terminal_receipt: TerminalReceiptRef
    promotion_receipt: ReceiptRef

    @model_validator(mode="after")
    def _receipts_match(self) -> Self:
        if (
            self.binding.attempt_key != self.cycle.attempt.defect.attempt_key
            or self.terminal_receipt != self.cycle.attempt.authority_receipt
            or self.promotion_receipt != self.cycle.execution_provenance.promotion_receipt
        ):
            raise ValueError("current generation defect authority projection drifted")
        return self


def _expected_activation(binding: ExecutionAttemptBindingV1) -> object:
    from graph_engine.attempts import BusinessActivation

    if binding.semantic_node_id == "execution.execute":
        if binding.repair_round != 0:
            raise ValueError("initial execution binding must use repair round zero")
        return BusinessActivation.for_trigger(f"coverage.{binding.coverage_epoch}.execute")
    return BusinessActivation.for_trigger(
        f"coverage.{binding.coverage_epoch}.repair.{binding.repair_round}.rerun"
    )


def _expected_attempt_key(binding: ExecutionAttemptBindingV1) -> str:
    return canonical_digest(
        {
            "invocation_id": binding.invocation_id,
            "graph_revision": binding.graph_revision,
            "public_entrypoint": binding.public_entrypoint,
            "semantic_node_id": binding.semantic_node_id,
            "business_activation": binding.business_activation.model_dump(mode="json"),
            "contract_id": binding.contract_id,
            "task_input_digest": binding.input_digest,
        }
    )


def _authority_root(project_root: Path, change_id: str, invocation_id: str) -> Path:
    project = Path(project_root)
    if project.resolve(strict=True) != project or not project.is_dir():
        raise ValueError("current generation defect authority requires a canonical project root")
    if change_id in {"", ".", ".."} or "/" in change_id or "\\" in change_id:
        raise ValueError("current generation defect authority requires a safe change id")
    invocation_key = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()
    return project / "qa" / "changes" / change_id / ".runtime" / "current-generation-defects" / invocation_key


def _project_root_digest(project_root: Path) -> str:
    return canonical_digest(str(Path(project_root).resolve(strict=True)))


def _selection_scope(
    project_root: Path,
    binding: ExecutionAttemptBindingV1,
) -> GenerationDefectSelectionScopeV1:
    return GenerationDefectSelectionScopeV1(
        project_root_digest=_project_root_digest(project_root),
        change_id=binding.change_id,
        invocation_id=binding.invocation_id,
        public_entrypoint=binding.public_entrypoint,
        semantic_node_id=binding.semantic_node_id,
        repair_round=binding.repair_round,
    )


def _selection_payload(
    cycle: VerifiedGenerationDefectCycleV1,
    binding: ExecutionAttemptBindingV1,
) -> GenerationDefectSelectionV1:
    return GenerationDefectSelectionV1(
        attempt_key=binding.attempt_key,
        binding_digest=canonical_digest(cast(JSONValue, binding.model_dump(mode="json"))),
        cycle_digest=canonical_digest(cast(JSONValue, cycle.model_dump(mode="json"))),
        terminal_receipt=cycle.attempt.authority_receipt,
        promotion_receipt=cycle.execution_provenance.promotion_receipt,
    )


def authenticate_generation_defect_cycle(
    project_root: Path,
    cycle: VerifiedGenerationDefectCycleV1,
    expected: ExecutionAttemptBindingV1,
) -> VerifiedGenerationDefectV1:
    """Authenticate both host terminal and kernel promotion stores for this defect."""

    attempt = cycle.attempt
    identity = attempt.authority_identity
    provenance = cycle.execution_provenance
    defect = attempt.defect
    generation_payload: JSONValue = defect.generation.model_dump(mode="json")
    contract = TASK_ATTEMPT_CONTRACTS[expected.contract_id]
    if (
        expected.business_activation != _expected_activation(expected)
        or expected.contract_id
        != (
            "assurance.execution.task.execute.v1"
            if expected.semantic_node_id == "execution.execute"
            else "assurance.execution.task.run.v1"
        )
        or expected.contract_digest != canonical_digest(cast(JSONValue, contract.canonical_projection()))
        or expected.attempt_key.digest != _expected_attempt_key(expected)
        or provenance.invocation_id != expected.invocation_id
        or provenance.public_entrypoint != expected.public_entrypoint
        or provenance.semantic_node_id != expected.semantic_node_id
        or provenance.attempt_key != expected.attempt_key
        or provenance.graph_revision != expected.graph_revision
        or provenance.contract_digest != expected.contract_digest
        or provenance.input_digest != expected.input_digest
        or identity.invocation_id != expected.invocation_id
        or identity.activation_id != expected.semantic_node_id
        or defect.attempt_key != expected.attempt_key
        or defect.generation.change_id != expected.change_id
        or defect.generation.coverage_epoch != expected.coverage_epoch
        or defect.validation_profile != expected.validation_profile
        or canonical_digest(generation_payload) != expected.generation_digest
    ):
        raise ValueError("generation defect does not belong to the expected execution attempt")
    change_root = Path(project_root) / "qa" / "changes" / attempt.defect.generation.change_id
    store = TerminalReceiptStore(invocation_activity_receipts_root(change_root, identity.invocation_id))
    try:
        receipts = store.authenticate(identity)
    except (FileNotFoundError, OSError, TerminalReceiptError) as error:
        raise ValueError("generation defect host authority is not authentic") from error
    expected_outcome = TaskOutcome.succeeded(attempt.defect.model_dump(mode="json"))
    if (
        len(receipts) != 1
        or receipts[0].outcome != expected_outcome
        or canonical_digest(receipts[0].model_dump(mode="json")) != attempt.authority_receipt.receipt_digest
    ):
        raise ValueError("generation defect host authority is not authentic")
    try:
        promotion = authenticate_promotion_receipt(
            change_root / ".runtime" / "receipts",
            cycle.execution_provenance.promotion_receipt,
        )
    except (OSError, TaskWorkspaceViolation) as error:
        raise ValueError("generation defect promotion authority is not authentic") from error
    if promotion.identity_digest != identity.workspace_identity_digest:
        raise ValueError("generation defect promotion belongs to another execution workspace")
    payload: JSONValue = attempt.model_dump(mode="json")
    if canonical_digest(payload) != cycle.execution_provenance.output_digest:
        raise ValueError("generation defect payload differs from its Attempt authority")
    return defect


def record_current_generation_defect(
    project_root: Path,
    cycle: VerifiedGenerationDefectCycleV1,
    binding: ExecutionAttemptBindingV1,
    *,
    selection_authority: HostSealingAuthority,
) -> CurrentGenerationDefectAuthorityV1:
    """Install one immutable current-execution index after journal authentication."""

    authenticate_generation_defect_cycle(project_root, cycle, binding)
    record = CurrentGenerationDefectAuthorityV1(
        binding=binding,
        cycle=cycle,
        terminal_receipt=cycle.attempt.authority_receipt,
        promotion_receipt=cycle.execution_provenance.promotion_receipt,
    )
    change_root = Path(project_root) / "qa" / "changes" / binding.change_id
    store = TerminalReceiptStore(
        invocation_activity_receipts_root(change_root, binding.invocation_id),
        selection_authority=selection_authority,
    )
    scope = _selection_scope(project_root, binding)
    selection = _selection_payload(cycle, binding)
    try:
        store.publish_selection(
            namespace=_CURRENT_SELECTION_NAMESPACE,
            scope=scope.model_dump(mode="json"),
            selection=selection.model_dump(mode="json"),
        )
    except (FileNotFoundError, OSError, TerminalReceiptError) as error:
        raise ValueError("current generation defect selection publication failed") from error
    root = _authority_root(project_root, binding.change_id, binding.invocation_id)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("current generation defect authority root is invalid")
    path = root / f"{binding.attempt_key.digest}.json"
    payload = canonical_json_bytes(record.model_dump(mode="json"))
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or path.read_bytes() != payload
        ):
            raise ValueError("current generation defect authority record drifted") from None
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return record


def load_current_generation_defect(
    project_root: Path,
    *,
    change_id: str,
    invocation_id: str,
    public_entrypoint: str,
) -> CurrentGenerationDefectAuthorityV1:
    """Load the unique current execution selected independently of repair input."""

    change_root = Path(project_root) / "qa" / "changes" / change_id
    try:
        authority = HostSealingAuthority.open_for_project(Path(project_root))
    except (OSError, HostAuthorityError) as error:
        raise ValueError("current generation defect host authority is unavailable") from error
    store = TerminalReceiptStore(
        invocation_activity_receipts_root(change_root, invocation_id),
        selection_authority=authority,
    )
    try:
        selection_receipts = store.authenticate_selections(namespace=_CURRENT_SELECTION_NAMESPACE)
    except (FileNotFoundError, OSError, TerminalReceiptError) as error:
        raise ValueError("current generation defect selection is not authentic") from error
    if len(selection_receipts) != 1:
        raise ValueError("current generation defect selection is missing or ambiguous")
    selection_receipt = selection_receipts[0]
    try:
        scope = GenerationDefectSelectionScopeV1.model_validate(selection_receipt.scope)
        selection = GenerationDefectSelectionV1.model_validate(selection_receipt.selection)
    except ValueError as error:
        raise ValueError("current generation defect selection is invalid") from error
    if (
        scope.project_root_digest != _project_root_digest(project_root)
        or scope.change_id != change_id
        or scope.invocation_id != invocation_id
        or scope.public_entrypoint != public_entrypoint
    ):
        raise ValueError("current generation defect selection coordinates drifted")

    root = _authority_root(project_root, change_id, invocation_id)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("current generation defect execution is unavailable")
    records: list[CurrentGenerationDefectAuthorityV1] = []
    for path in sorted(root.iterdir()):
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise ValueError("current generation defect authority record is invalid")
        try:
            record = CurrentGenerationDefectAuthorityV1.model_validate_json(path.read_bytes())
        except ValueError as error:
            raise ValueError("current generation defect authority record is invalid") from error
        canonical = canonical_json_bytes(record.model_dump(mode="json"))
        if path.name != f"{record.binding.attempt_key.digest}.json" or path.read_bytes() != canonical:
            raise ValueError("current generation defect authority record is not canonical")
        if (
            record.binding.invocation_id != invocation_id
            or record.binding.public_entrypoint != public_entrypoint
            or record.binding.change_id != change_id
        ):
            raise ValueError("current generation defect authority coordinates drifted")
        authenticate_generation_defect_cycle(project_root, record.cycle, record.binding)
        records.append(record)
    if len(records) != 1:
        raise ValueError("current generation defect execution is missing or ambiguous")
    current = records[0]
    if (
        _selection_scope(project_root, current.binding) != scope
        or _selection_payload(current.cycle, current.binding) != selection
    ):
        raise ValueError("current generation defect index differs from its authenticated selection")
    return current


__all__ = [
    "CurrentGenerationDefectAuthorityV1",
    "GenerationDefectSelectionScopeV1",
    "GenerationDefectSelectionV1",
    "authenticate_generation_defect_cycle",
    "load_current_generation_defect",
    "record_current_generation_defect",
]
