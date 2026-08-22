"""Cross-wheel durable-effect crash cuts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import EffectApplyResult, EffectIntent, EffectReconcileResult

from assurance_healing.contracts import (
    HealApplyIntentV2,
    HealingAllocationIntentV2,
    HealingAllocationReceiptV2,
    ProposalApprovedIntentV1,
)
from assurance_healing.effects.allocation import HealingAllocationEffect
from assurance_healing.effects.apply import HealApplyEffect
from assurance_healing.effects.approval import ProposalApprovedEffect
from assurance_improvement.effects.store import ImprovementStore
from assurance_improvement.effects.store import StoreRecord as ImprovementStoreRecord
from assurance_healing.operations.keys import (
    derive_allocation_ids,
    derive_approval_id,
    derive_heal_record_key,
)
from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.effects.archive import ImprovementArchiveEffect
from assurance_improvement.effects.delivery import ImprovementDeliveryEffect
from assurance_improvement.effects.promotion import ImprovementPromotionEffect
from assurance_improvement.operations.keys import (
    archive_effect_key,
    delivery_effect_key,
    promotion_effect_key,
)

EFFECT_KINDS = (
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.proposal-approved.v1",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.improvement.effect.delivery.v1",
    "assurance.improvement.effect.promotion.v1",
    "assurance.improvement.effect.archive.v1",
)
FAULT_CUTS = (
    "before_mutation",
    "after_mutation",
    "before_receipt",
    "after_receipt",
    "reconcile_error",
)

_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64
_HEX_D = "d" * 64
_HEX_E = "e" * 64
_ALLOCATION_IDS = derive_allocation_ids(
    change_id="CH-DEMO-001",
    source_batch_id="batch-src",
    entry_batch_id="batch-entry",
    candidate_digest=_HEX_A,
    attempt_number=1,
)
ALLOCATION_KEY = str(_ALLOCATION_IDS["operation_id"])
APPROVAL_KEY = derive_approval_id(
    owner_id="assurance.healing",
    candidate_digest=_HEX_C,
    baseline_digest=_HEX_D,
    policy_digest=_HEX_E,
    proposal_digest=_HEX_A,
)
HEAL_APPLY_KEY = derive_heal_record_key(
    owner_id="assurance.healing",
    write_set_id="ws-1",
    candidate_digest=_HEX_A,
    safety_payload_digest=_HEX_E,
    target="api",
)
DELIVERY_KEY = "IMP-1:1:memory_apply:" + _HEX_A
PROMOTION_KEY = "IMP-1:1:" + _HEX_B
ARCHIVE_KEY = "inv-archive-1:" + _HEX_A


class CrashCut(RuntimeError):
    """Simulated crash at an effect store seam."""


@dataclass
class EffectCutResult:
    status: str
    idempotency_key: str
    expected_key: str
    receipt: object
    expected_receipt: object
    external_mutation_count: int


class FaultingEffectStore:
    def __init__(self, cut: str) -> None:
        self.cut = cut
        self.records: dict[str, ImprovementStoreRecord] = {}
        self.external_mutation_count = 0
        self._faulted = False
        self._reconcile_faulted = False

    async def get(self, key: str) -> ImprovementStoreRecord | None:
        if self.cut == "reconcile_error" and self._faulted and not self._reconcile_faulted:
            self._reconcile_faulted = True
            raise CrashCut("reconcile_error")
        return self.records.get(key)

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
        if self.cut == "before_mutation" and not self._faulted:
            self._faulted = True
            raise CrashCut("before_mutation")
        if not self._faulted:
            self.external_mutation_count += 1
            self.records[key] = ImprovementStoreRecord(status="applied", receipt=receipt, payload=payload)
            self._faulted = True
            if self.cut in {"after_mutation", "before_receipt"}:
                raise CrashCut(self.cut)
            return
        self.external_mutation_count += 1
        self.records[key] = ImprovementStoreRecord(status="applied", receipt=receipt, payload=payload)


def _allocation_intent() -> EffectIntent:
    payload = {
        "schema_version": "2",
        "episode_id": str(_ALLOCATION_IDS["episode_id"]),
        "attempt_id": str(_ALLOCATION_IDS["attempt_id"]),
        "attempt_number": 1,
        "operation_id": ALLOCATION_KEY,
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
    HealingAllocationIntentV2.model_validate(payload)
    return EffectIntent(kind="assurance.healing.effect.allocation.v2", payload=cast(JSONValue, payload))


def _approval_intent() -> EffectIntent:
    payload = {
        "schema_version": "1",
        "approval_id": APPROVAL_KEY,
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
    ProposalApprovedIntentV1.model_validate(payload)
    return EffectIntent(
        kind="assurance.healing.effect.proposal-approved.v1", payload=cast(JSONValue, payload)
    )


def _heal_intent() -> EffectIntent:
    payload = {
        "schema_version": "2",
        "record_key": HEAL_APPLY_KEY,
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
    HealApplyIntentV2.model_validate(payload)
    return EffectIntent(kind="assurance.healing.effect.heal-apply.v2", payload=cast(JSONValue, payload))


def _delivery_intent() -> EffectIntent:
    payload: dict[str, object] = {
        "schema_version": "1",
        "kind": "memory_apply",
        "improvement_id": "IMP-1",
        "version": 1,
        "target_kind": "memory_apply",
        "target_digest": _HEX_A,
        "target": ".aa/memory/aa-api-plan.md",
        "memory_apply": {
            "target": ".aa/memory/aa-api-plan.md",
            "before_sha256": "b",
            "after_sha256": "a",
            "receipt_sha256": "r",
        },
    }
    ImprovementEffectIntentV1.model_validate(payload)
    return EffectIntent(kind="assurance.improvement.effect.delivery.v1", payload=cast(JSONValue, payload))


def _promotion_intent() -> EffectIntent:
    payload: dict[str, object] = {
        "schema_version": "1",
        "kind": "test_promotion",
        "improvement_id": "IMP-1",
        "version": 1,
        "promotion_digest": _HEX_B,
        "candidate_id": "C-1",
        "promotion": {
            "schema_version": "1",
            "receipt_id": "promo-1",
            "improvement_id": "IMP-1",
            "candidate_id": "C-1",
            "applied_at": "2026-08-21T12:00:00Z",
            "write_set": [
                {
                    "path": "tests/api/test_example.py",
                    "before_sha256": None,
                    "after_sha256": _HEX_B,
                }
            ],
            "status": "applied",
            "source_digests": {"manifest": _HEX_B},
            "write_authorization": ["tests/api/test_example.py"],
        },
    }
    ImprovementEffectIntentV1.model_validate(payload)
    return EffectIntent(kind="assurance.improvement.effect.promotion.v1", payload=cast(JSONValue, payload))


def _archive_intent() -> EffectIntent:
    payload = {
        "schema_version": "1",
        "kind": "archive",
        "improvement_id": "IMP-1",
        "invocation_id": "inv-archive-1",
        "archive_digest": _HEX_A,
    }
    ImprovementEffectIntentV1.model_validate(payload)
    return EffectIntent(kind="assurance.improvement.effect.archive.v1", payload=cast(JSONValue, payload))


def _handler_for(kind: str, store: FaultingEffectStore) -> object:
    if kind == "assurance.healing.effect.allocation.v2":
        return HealingAllocationEffect(store=cast(Any, store))
    if kind == "assurance.healing.effect.proposal-approved.v1":
        return ProposalApprovedEffect(store=cast(Any, store))
    if kind == "assurance.healing.effect.heal-apply.v2":
        return HealApplyEffect(store=cast(Any, store))
    if kind == "assurance.improvement.effect.delivery.v1":
        return ImprovementDeliveryEffect(store=cast(ImprovementStore, store))
    if kind == "assurance.improvement.effect.promotion.v1":
        return ImprovementPromotionEffect(store=cast(ImprovementStore, store))
    if kind == "assurance.improvement.effect.archive.v1":
        return ImprovementArchiveEffect(store=cast(ImprovementStore, store))
    raise ValueError(kind)


def _intent_for(kind: str) -> tuple[EffectIntent, str]:
    mapping = {
        "assurance.healing.effect.allocation.v2": (_allocation_intent, ALLOCATION_KEY),
        "assurance.healing.effect.proposal-approved.v1": (_approval_intent, APPROVAL_KEY),
        "assurance.healing.effect.heal-apply.v2": (_heal_intent, HEAL_APPLY_KEY),
        "assurance.improvement.effect.delivery.v1": (_delivery_intent, DELIVERY_KEY),
        "assurance.improvement.effect.promotion.v1": (_promotion_intent, PROMOTION_KEY),
        "assurance.improvement.effect.archive.v1": (_archive_intent, ARCHIVE_KEY),
    }
    factory, key = mapping[kind]
    return factory(), key


def _expected_receipt(kind: str, intent: EffectIntent, key: str) -> object:
    if kind == "assurance.healing.effect.allocation.v2":
        payload = dict(cast(dict[str, object], intent.payload))
        return HealingAllocationReceiptV2.model_validate(
            {**payload, "idempotency_key": payload["operation_id"]}
        ).model_dump(mode="json")
    if kind.startswith("assurance.improvement."):
        model = ImprovementEffectIntentV1.model_validate(intent.payload)
        if kind.endswith("delivery.v1"):
            assert delivery_effect_key(model) == key
        elif kind.endswith("promotion.v1"):
            assert promotion_effect_key(model) == key
        else:
            assert archive_effect_key(model) == key
    return None


async def drive_effect_cut(kind: str, cut: str) -> EffectCutResult:
    intent, key = _intent_for(kind)
    store = FaultingEffectStore(cut=cut)
    handler = _handler_for(kind, store)
    apply = getattr(handler, "apply")
    reconcile = getattr(handler, "reconcile")
    expected_receipt = _expected_receipt(kind, intent, key)
    applied: EffectApplyResult | None = None
    try:
        applied = await apply(intent, key)
        if cut == "after_receipt" and isinstance(applied, EffectApplyResult) and applied.status == "applied":
            pass
    except CrashCut:
        applied = None
    try:
        reconciled = await reconcile(intent, key)
    except CrashCut:
        reconciled = await reconcile(intent, key)
    if not isinstance(reconciled, EffectReconcileResult):
        raise TypeError("reconcile must return EffectReconcileResult")
    if reconciled.status == "not_applied":
        try:
            applied = await apply(intent, key)
        except CrashCut:
            applied = None
        if not isinstance(applied, EffectApplyResult) or applied.status != "applied":
            return EffectCutResult(
                status="pending",
                idempotency_key=key,
                expected_key=key,
                receipt=None,
                expected_receipt=expected_receipt,
                external_mutation_count=store.external_mutation_count,
            )
        reconciled = await reconcile(intent, key)
    status = reconciled.status
    if status not in {"applied", "pending", "not_applied"}:
        status = "indeterminate"
    receipt = reconciled.receipt
    if expected_receipt is None:
        expected_receipt = receipt
    return EffectCutResult(
        status=status,
        idempotency_key=key,
        expected_key=key,
        receipt=receipt,
        expected_receipt=expected_receipt,
        external_mutation_count=store.external_mutation_count,
    )
