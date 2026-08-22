from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult, TaskFailure

from assurance_healing.contracts import (
    HealApplyIntentV2,
    HealingAllocationIntentV2,
    HealingAllocationReceiptV2,
    ProposalApprovedIntentV1,
)

_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64
_HEX_D = "d" * 64
_HEX_E = "e" * 64
_WHEEL_ROOT = Path(__file__).resolve().parent.parent

ALLOCATION_KIND = "assurance.healing.effect.allocation.v2"
APPROVAL_KIND = "assurance.healing.effect.proposal-approved.v1"
HEAL_APPLY_KIND = "assurance.healing.effect.heal-apply.v2"
ALLOCATION_KEY = "allocation-key"
APPROVAL_KEY = "approval-key"
HEAL_APPLY_KEY = "heal-apply-key"


class CrashCut(RuntimeError):
    """Simulated crash inside an injected store seam."""


class AllocationRecord:
    def __init__(
        self,
        *,
        status: str,
        receipt: dict[str, object] | None = None,
        failure: TaskFailure | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.status = status
        self.receipt = receipt
        self.failure = failure
        self.payload = payload


class FakeAllocationStore:
    def __init__(self) -> None:
        self.records: dict[str, AllocationRecord] = {}
        self.external_mutation_count = 0

    async def get(self, key: str) -> AllocationRecord | None:
        return self.records.get(key)

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
        self.external_mutation_count += 1
        self.records[key] = AllocationRecord(
            status="applied",
            receipt=receipt,
            payload=payload,
        )


class FakeApprovalStore(FakeAllocationStore):
    pass


class FakeHealStore(FakeAllocationStore):
    pass


class FaultingHealStore:
    def __init__(self, cut: str) -> None:
        self.cut = cut
        self.records: dict[str, AllocationRecord] = {}
        self.external_mutation_count = 0
        self._faulted = False

    async def get(self, key: str) -> AllocationRecord | None:
        return self.records.get(key)

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
        if self.cut == "before_mutation" and not self._faulted:
            self._faulted = True
            raise CrashCut("before_mutation")
        if self.cut != "before_mutation" and not self._faulted:
            self.external_mutation_count += 1
            self.records[key] = AllocationRecord(
                status="applied",
                receipt=receipt,
                payload=payload,
            )
            self._faulted = True
            if self.cut in {"after_mutation", "before_receipt"}:
                raise CrashCut(self.cut)
            return
        self.external_mutation_count += 1
        self.records[key] = AllocationRecord(
            status="applied",
            receipt=receipt,
            payload=payload,
        )


def allocation_payload(*, operation_id: str = ALLOCATION_KEY) -> dict[str, object]:
    return {
        "schema_version": "2",
        "episode_id": "ep-1",
        "attempt_id": "ha-ep1-1",
        "attempt_number": 1,
        "operation_id": operation_id,
        "change_id": "CH-DEMO-001",
        "owner_id": "assurance.healing",
        "source_batch_id": "batch-src",
        "entry_batch_id": "batch-entry",
        "candidate_digest": _HEX_A,
        "baseline_digest": _HEX_B,
        "policy_digest": _HEX_C,
        "execution_evidence_digest": _HEX_D,
        "baseline_embedded": True,
    }


def allocation_intent(*, operation_id: str = ALLOCATION_KEY) -> EffectIntent:
    HealingAllocationIntentV2.model_validate(allocation_payload(operation_id=operation_id))
    return EffectIntent(
        kind=ALLOCATION_KIND, payload=cast(JSONValue, allocation_payload(operation_id=operation_id))
    )


def approval_payload(*, approval_id: str = APPROVAL_KEY) -> dict[str, object]:
    return {
        "schema_version": "1",
        "approval_id": approval_id,
        "change_id": "CH-DEMO-001",
        "owner_id": "assurance.healing",
        "root_invocation_id": "inv-1",
        "interrupt_task_id": "task-1",
        "source_gate_attempt_id": "gate-1",
        "source_tree_id": "tree-src",
        "target_tree_id": "tree-dst",
        "proposal_digest": _HEX_A,
        "fixer_authority_digest": _HEX_B,
        "candidate_digest": _HEX_C,
        "baseline_digest": _HEX_D,
        "policy_digest": _HEX_E,
        "targets": ["api"],
        "paths": ["tests/api/test_users.py"],
        "action": "approve_and_apply",
    }


def approval_intent(*, approval_id: str = APPROVAL_KEY) -> EffectIntent:
    ProposalApprovedIntentV1.model_validate(approval_payload(approval_id=approval_id))
    return EffectIntent(
        kind=APPROVAL_KIND, payload=cast(JSONValue, approval_payload(approval_id=approval_id))
    )


def heal_payload(*, record_key: str = HEAL_APPLY_KEY) -> dict[str, object]:
    return {
        "schema_version": "2",
        "record_key": record_key,
        "change_id": "CH-DEMO-001",
        "owner_id": "assurance.healing",
        "target": "api",
        "entry_batch_id": "20260822T000000Z",
        "outcome": "applied",
        "candidate_digest": _HEX_A,
        "baseline_digest": _HEX_B,
        "policy_digest": _HEX_C,
        "write_set_id": "ws-1",
        "proposal_ids": ["P1"],
        "claimed_modified_paths": ["tests/api/test_users.py"],
        "safety_payload_digest": _HEX_E,
    }


def heal_intent(*, record_key: str = HEAL_APPLY_KEY) -> EffectIntent:
    HealApplyIntentV2.model_validate(heal_payload(record_key=record_key))
    return EffectIntent(kind=HEAL_APPLY_KIND, payload=cast(JSONValue, heal_payload(record_key=record_key)))


def allocation_receipt(payload: dict[str, object]) -> dict[str, object]:
    receipt = {**payload, "idempotency_key": payload["operation_id"]}
    HealingAllocationReceiptV2.model_validate(receipt)
    return receipt


def imported_symbols(package: str) -> set[str]:
    root = _WHEEL_ROOT / package
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[-1] for alias in node.names)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
                if node.module:
                    names.add(node.module)
                    names.update(node.module.split("."))
    return names


async def _drive_apply_then_reconcile(handler: object, intent: EffectIntent) -> EffectReconcileResult:
    payload = intent.payload
    if not isinstance(payload, dict):
        raise TypeError("effect payload must be a mapping")
    if intent.kind == APPROVAL_KIND:
        key = str(payload["approval_id"])
    elif intent.kind == HEAL_APPLY_KIND:
        key = str(payload["record_key"])
    else:
        key = str(payload["operation_id"])
    apply = getattr(handler, "apply")
    reconcile = getattr(handler, "reconcile")
    try:
        applied = await apply(intent, key)
        if isinstance(applied, EffectApplyResult) and applied.status == "applied":
            result = await reconcile(intent, key)
            if isinstance(result, EffectReconcileResult):
                return result
    except CrashCut:
        pass
    reconciled = await reconcile(intent, key)
    if not isinstance(reconciled, EffectReconcileResult):
        raise TypeError("reconcile must return EffectReconcileResult")
    if reconciled.status == "not_applied":
        try:
            applied = await apply(intent, key)
        except CrashCut:
            applied = None
        if not isinstance(applied, EffectApplyResult) or applied.status != "applied":
            return EffectReconcileResult(status="pending")
        retry = await reconcile(intent, key)
        if not isinstance(retry, EffectReconcileResult):
            raise TypeError("reconcile must return EffectReconcileResult")
        return retry
    return reconciled


def as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected mapping, got {type(value)!r}")
