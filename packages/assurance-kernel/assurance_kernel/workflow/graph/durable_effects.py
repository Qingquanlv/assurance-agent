"""D14 inline durable-effect protocol: intents, acknowledgements, and reconciliation.

Production registry gains the three healing kinds in Task 8. Packaged contracts
still select none until Task 15. Tests may inject a local registry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.artifacts.models.common import StrictWireModel
from assurance_kernel.exceptions import AaError
from assurance_kernel.workflow.core.events import read_events_strict
from assurance_kernel.workflow.core.graph_events import (
    DurableEffectAcknowledgedEvent,
    DurableEffectIntegrityFailedEvent,
    GraphTerminalEvent,
    TaskAttemptSucceededEvent,
)
from assurance_kernel.workflow.core.progression import ProgressionLockTimeout, transaction
from assurance_kernel.workflow.graph.effect_retry import (
    EffectRetryStore,
    RootEffectFenceStore,
)

DurableEffectKind = Annotated[str, Field(min_length=1)]

HEALING_ALLOCATION_V2 = "healing_allocation/v2"
FIXER_PROPOSAL_APPROVED_V1 = "fixer_proposal_approved/v1"
HEAL_RECORD_APPLY_V2 = "heal_record_apply/v2"

KNOWN_DURABLE_EFFECT_KINDS: frozenset[str] = frozenset(
    {
        HEALING_ALLOCATION_V2,
        FIXER_PROPOSAL_APPROVED_V1,
        HEAL_RECORD_APPLY_V2,
    }
)

# Closed commit-safety dependency inventory for runtime_commit_safety/v1 (Task 10).
COMMIT_SAFETY_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "assurance_agent.workflow.graph.durable_effects.HEALING_ALLOCATION_V2",
        "effect",
        HEALING_ALLOCATION_V2,
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.FIXER_PROPOSAL_APPROVED_V1",
        "effect",
        FIXER_PROPOSAL_APPROVED_V1,
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.HEAL_RECORD_APPLY_V2",
        "effect",
        HEAL_RECORD_APPLY_V2,
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.KNOWN_DURABLE_EFFECT_KINDS",
        "helper",
        "durable_effect_registry",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.DurableEffectIntentV1",
        "model",
        "durable_effect_intent",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.DurableEffectAcknowledgementV1",
        "model",
        "durable_effect_acknowledgement",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.DurableEffectContext",
        "model",
        "durable_effect_context",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.payload_sha256",
        "helper",
        "payload_sha256",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.reconciler_semantics_digest",
        "helper",
        "reconciler_semantics_digest",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.derive_effect_id",
        "helper",
        "derive_effect_id",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.build_intent",
        "helper",
        "build_intent",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.validate_acknowledgement",
        "helper",
        "validate_acknowledgement",
    ),
    (
        "assurance_agent.workflow.graph.durable_effects.reconcile_effect",
        "helper",
        "reconcile_effect",
    ),
)


class DurableEffectError(AaError):
    """Base error for durable-effect validation or reconciliation."""


class DurableEffectValidationError(DurableEffectError):
    """Malformed/opaque intent or contract cardinality mismatch (invalid_output)."""


class DurableEffectIntegrityError(DurableEffectError):
    """Permanent model/digest/domain conflict; stop the invocation."""


class DurableEffectRetryableError(DurableEffectError):
    """Retryable lock/I/O failure; leave the effect unacknowledged."""

    def __init__(self, message: str, *, error_code: str = "retryable_io") -> None:
        super().__init__(message)
        self.error_code = error_code


class DurableEffectIntentV1(StrictWireModel):
    schema_version: Literal["1"]
    effect_id: str
    kind: str
    reconciler_semantics_digest: str
    payload_sha256: str
    payload: dict[str, object]

    @field_validator("effect_id", "kind", "reconciler_semantics_digest", "payload_sha256")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_payload_digest(self) -> DurableEffectIntentV1:
        expected = payload_sha256(self.payload)
        if self.payload_sha256 != expected:
            raise ValueError("payload_sha256 must equal the canonical payload digest")
        return self


class DurableEffectAcknowledgementV1(StrictWireModel):
    schema_version: Literal["1"]
    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    effect_id: str
    kind: str
    reconciler_semantics_digest: str
    payload_sha256: str
    domain_source_sequence: Annotated[int, Field(strict=True, gt=0)]
    domain_event_digest: str

    @field_validator(
        "root_invocation_id",
        "invocation_id",
        "task_id",
        "attempt_id",
        "effect_id",
        "kind",
        "reconciler_semantics_digest",
        "payload_sha256",
        "domain_event_digest",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class DurableEffectContext(StrictWireModel):
    """Wire-stable producer/attempt bindings passed into reconcilers."""

    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    target: str
    output_digests: dict[str, str]
    write_set_id: str | None = None

    @field_validator(
        "root_invocation_id",
        "invocation_id",
        "task_id",
        "attempt_id",
        "target",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_output_digests(self) -> DurableEffectContext:
        keys = list(self.output_digests)
        if keys != sorted(keys):
            raise ValueError("output_digests keys must be sorted")
        for digest in self.output_digests.values():
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("output_digests values must be bare lowercase sha256 digests")
        return self


@dataclass(frozen=True)
class EffectRegistration:
    kind: str
    payload_model: type[StrictWireModel]
    reconciler_semantics_digest: str
    domain_idempotency_key: Callable[[Mapping[str, object]], str]
    reconcile: Callable[
        [DurableEffectIntentV1, DurableEffectContext, "DurableEffectRuntime"],
        DurableEffectAcknowledgementV1,
    ]


@dataclass(frozen=True)
class DurableEffectRuntime:
    """Non-wire host handles for reconciliation."""

    change_dir: Path
    project_root: Path
    fence_store: RootEffectFenceStore
    retry_store: EffectRetryStore


class EffectRegistry:
    """Kind → strict payload model + reconciler. Production starts empty."""

    def __init__(self) -> None:
        self._entries: dict[str, EffectRegistration] = {}

    def register(self, entry: EffectRegistration) -> None:
        if not entry.kind or not entry.kind.strip():
            raise DurableEffectValidationError("effect kind must be non-empty")
        if entry.kind in self._entries:
            raise DurableEffectValidationError(f"duplicate durable effect kind: {entry.kind}")
        self._entries[entry.kind] = entry

    def get(self, kind: str) -> EffectRegistration | None:
        return self._entries.get(kind)

    def kinds(self) -> frozenset[str]:
        return frozenset(self._entries)

    def __contains__(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in self._entries

    def __len__(self) -> int:
        return len(self._entries)


_PRODUCTION_REGISTRY = EffectRegistry()
_HEALING_EFFECTS_REGISTERED = False


def _ensure_healing_effects_registered() -> None:
    global _HEALING_EFFECTS_REGISTERED
    if _HEALING_EFFECTS_REGISTERED:
        return
    from assurance_kernel.workflow.graph.product_hooks import current_product_hooks

    current_product_hooks().register_healing_effects(_PRODUCTION_REGISTRY)
    _HEALING_EFFECTS_REGISTERED = True


def production_effect_registry() -> EffectRegistry:
    """Return the process-wide production registry (healing kinds from Task 8)."""
    _ensure_healing_effects_registered()
    return _PRODUCTION_REGISTRY


def payload_sha256(payload: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(payload)))


def reconciler_semantics_digest(*, kind: str, rules: Sequence[str]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "kind": kind,
                "rules": list(rules),
            }
        )
    )


def derive_effect_id(
    *,
    kind: str,
    invocation_id: str,
    task_id: str,
    attempt_id: str,
    domain_idempotency_key: str,
) -> str:
    """Deterministic effect ID; recovery of the same success cannot drift."""
    digest = sha256_bytes(
        canonical_json_bytes(
            {
                "attempt_id": attempt_id,
                "domain_idempotency_key": domain_idempotency_key,
                "invocation_id": invocation_id,
                "kind": kind,
                "task_id": task_id,
            }
        )
    )
    return hashlib.sha256(digest.encode("utf-8")).hexdigest()


def validate_durable_effect_kinds(
    kinds: Sequence[str],
    *,
    registry: EffectRegistry | None = None,
) -> None:
    """Reject unknown or unregistered kinds at contract load."""
    if not kinds:
        return
    active = registry if registry is not None else production_effect_registry()
    seen: set[str] = set()
    for kind in kinds:
        if not kind or not kind.strip():
            raise DurableEffectValidationError("durable effect kind must be non-empty")
        if kind in seen:
            raise DurableEffectValidationError(f"duplicate durable effect kind: {kind}")
        seen.add(kind)
        if kind not in active:
            raise DurableEffectValidationError(f"unregistered durable effect kind: {kind}")


def build_intent(
    *,
    kind: str,
    payload: Mapping[str, object] | StrictWireModel,
    invocation_id: str,
    task_id: str,
    attempt_id: str,
    registry: EffectRegistry | None = None,
) -> DurableEffectIntentV1:
    """Validate payload through the registry and return a canonical intent."""
    active = registry if registry is not None else production_effect_registry()
    entry = active.get(kind)
    if entry is None:
        raise DurableEffectValidationError(f"unregistered durable effect kind: {kind}")
    raw = payload.model_dump(mode="json") if isinstance(payload, StrictWireModel) else dict(payload)
    try:
        validated = entry.payload_model.model_validate(raw)
    except ValidationError as exc:
        raise DurableEffectValidationError(f"malformed durable effect payload for {kind}: {exc}") from exc
    wire = validated.model_dump(mode="json")
    if not isinstance(wire, dict):
        raise DurableEffectValidationError(f"durable effect payload for {kind} must be an object")
    key = entry.domain_idempotency_key(wire)
    effect_id = derive_effect_id(
        kind=kind,
        invocation_id=invocation_id,
        task_id=task_id,
        attempt_id=attempt_id,
        domain_idempotency_key=key,
    )
    return DurableEffectIntentV1(
        schema_version="1",
        effect_id=effect_id,
        kind=kind,
        reconciler_semantics_digest=entry.reconciler_semantics_digest,
        payload_sha256=payload_sha256(wire),
        payload=wire,
    )


def validate_result_intents(
    *,
    declared_kinds: Sequence[str],
    intents: Sequence[DurableEffectIntentV1 | Mapping[str, object]],
    invocation_id: str,
    task_id: str,
    attempt_id: str,
    target: str,
    registry: EffectRegistry | None = None,
) -> tuple[DurableEffectIntentV1, ...]:
    """Enforce contract cardinality and canonical identity before success."""
    active = registry if registry is not None else production_effect_registry()
    validate_durable_effect_kinds(declared_kinds, registry=active)
    parsed: list[DurableEffectIntentV1] = []
    for raw in intents:
        try:
            intent = (
                raw if isinstance(raw, DurableEffectIntentV1) else DurableEffectIntentV1.model_validate(raw)
            )
        except ValidationError as exc:
            raise DurableEffectValidationError(f"malformed durable effect intent: {exc}") from exc
        entry = active.get(intent.kind)
        if entry is None:
            raise DurableEffectValidationError(f"unregistered durable effect kind: {intent.kind}")
        if intent.reconciler_semantics_digest != entry.reconciler_semantics_digest:
            raise DurableEffectValidationError(f"reconciler_semantics_digest mismatch for {intent.kind}")
        try:
            entry.payload_model.model_validate(intent.payload)
        except ValidationError as exc:
            raise DurableEffectValidationError(
                f"malformed durable effect payload for {intent.kind}: {exc}"
            ) from exc
        key = entry.domain_idempotency_key(intent.payload)
        expected_id = derive_effect_id(
            kind=intent.kind,
            invocation_id=invocation_id,
            task_id=task_id,
            attempt_id=attempt_id,
            domain_idempotency_key=key,
        )
        if intent.effect_id != expected_id:
            raise DurableEffectValidationError(
                f"unstable or wrong-producer effect_id for {intent.kind} on {target}"
            )
        parsed.append(intent)

    declared = list(declared_kinds)
    if len(parsed) != len(declared):
        raise DurableEffectValidationError(
            f"durable effect cardinality mismatch for {target}: declared {len(declared)}, got {len(parsed)}"
        )
    by_kind: dict[str, list[DurableEffectIntentV1]] = {}
    for intent in parsed:
        by_kind.setdefault(intent.kind, []).append(intent)
    for kind in declared:
        matches = by_kind.get(kind, [])
        if len(matches) != 1:
            raise DurableEffectValidationError(
                f"durable effect kind cardinality mismatch for {target}: {kind}"
            )
    ids = [intent.effect_id for intent in parsed]
    if len(set(ids)) != len(ids):
        raise DurableEffectValidationError(f"duplicate durable effect_id for {target}")
    if "" in ids or any(not effect_id.strip() for effect_id in ids):
        raise DurableEffectValidationError(f"empty durable effect_id for {target}")
    return tuple(sorted(parsed, key=lambda item: item.effect_id))


def intents_from_success_event(event: TaskAttemptSucceededEvent) -> tuple[DurableEffectIntentV1, ...]:
    if not event.durable_effects:
        return ()
    try:
        return tuple(
            sorted(
                (DurableEffectIntentV1.model_validate(raw) for raw in event.durable_effects),
                key=lambda item: item.effect_id,
            )
        )
    except ValidationError as exc:
        raise DurableEffectIntegrityError(f"inline durable effect intents are corrupt: {exc}") from exc


def validate_acknowledgement(
    ack: DurableEffectAcknowledgementV1 | Mapping[str, object],
    *,
    intent: DurableEffectIntentV1,
    context: DurableEffectContext,
) -> DurableEffectAcknowledgementV1:
    """Accept ack only when it binds the exact inline intent and producer identity."""
    try:
        parsed = (
            ack
            if isinstance(ack, DurableEffectAcknowledgementV1)
            else DurableEffectAcknowledgementV1.model_validate(ack)
        )
    except ValidationError as exc:
        raise DurableEffectValidationError(f"malformed durable effect acknowledgement: {exc}") from exc
    if parsed.effect_id != intent.effect_id:
        raise DurableEffectIntegrityError("acknowledgement effect_id does not match inline intent")
    if parsed.kind != intent.kind:
        raise DurableEffectIntegrityError("acknowledgement kind does not match inline intent")
    if parsed.reconciler_semantics_digest != intent.reconciler_semantics_digest:
        raise DurableEffectIntegrityError("acknowledgement reconciler digest does not match intent")
    if parsed.payload_sha256 != intent.payload_sha256:
        raise DurableEffectIntegrityError("acknowledgement payload digest does not match intent")
    if parsed.invocation_id != context.invocation_id or parsed.task_id != context.task_id:
        raise DurableEffectIntegrityError("acknowledgement producer identity mismatch")
    if parsed.attempt_id != context.attempt_id:
        raise DurableEffectIntegrityError("acknowledgement attempt identity mismatch")
    if parsed.root_invocation_id != context.root_invocation_id:
        raise DurableEffectIntegrityError("acknowledgement root identity mismatch")
    return parsed


def reconcile_effect(
    intent: DurableEffectIntentV1,
    context: DurableEffectContext,
    runtime: DurableEffectRuntime,
    *,
    registry: EffectRegistry | None = None,
) -> DurableEffectAcknowledgementV1:
    """Append or reuse one verified domain event by registered kind, then ack."""
    active = registry if registry is not None else production_effect_registry()
    entry = active.get(intent.kind)
    if entry is None:
        raise DurableEffectIntegrityError(f"unregistered durable effect kind: {intent.kind}")
    if intent.reconciler_semantics_digest != entry.reconciler_semantics_digest:
        raise DurableEffectIntegrityError(f"reconciler_semantics_digest mismatch for {intent.kind}")
    root_id = context.root_invocation_id
    with runtime.fence_store.guard(root_id):
        runtime.fence_store.reject_if_terminal(root_id)
        existing = _find_acknowledgement(runtime.change_dir, intent.effect_id)
        if existing is not None:
            return validate_acknowledgement(existing, intent=intent, context=context)
        try:
            ack = entry.reconcile(intent, context, runtime)
        except DurableEffectRetryableError:
            raise
        except ProgressionLockTimeout as exc:
            raise DurableEffectRetryableError(str(exc), error_code="progression_lock_timeout") from exc
        except (OSError, TimeoutError) as exc:
            raise DurableEffectRetryableError(str(exc), error_code="retryable_io") from exc
        except DurableEffectIntegrityError:
            raise
        except DurableEffectError:
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            raise DurableEffectIntegrityError(str(exc)) from exc
        validated = validate_acknowledgement(ack, intent=intent, context=context)
        _append_acknowledgement(runtime.change_dir, validated)
        runtime.retry_store.clear_if_acknowledged(intent.effect_id)
        return validated


def scan_unacknowledged_intents(
    change_dir: Path,
    invocation_id: str,
) -> tuple[tuple[TaskAttemptSucceededEvent, DurableEffectIntentV1], ...]:
    """Return committed-success inline intents that still lack an acknowledgement."""
    events = read_events_strict(change_dir)
    committed_tasks: set[str] = set()
    successes: dict[str, TaskAttemptSucceededEvent] = {}
    acked: set[str] = set()
    for raw in events:
        if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
            continue
        event_type = raw.get("type")
        if event_type == "superstep_committed":
            raw_ids = raw.get("committed_task_ids")
            if isinstance(raw_ids, list):
                for task_id in raw_ids:
                    if isinstance(task_id, str):
                        committed_tasks.add(task_id)
        elif event_type == "task_attempt_succeeded":
            event = TaskAttemptSucceededEvent.model_validate(
                {k: v for k, v in raw.items() if k not in {"seq", "ts"}}
            )
            successes[event.task_id] = event
        elif event_type == "durable_effect_acknowledged":
            effect_id = raw.get("effect_id")
            if isinstance(effect_id, str):
                acked.add(effect_id)
    pending: list[tuple[TaskAttemptSucceededEvent, DurableEffectIntentV1]] = []
    for task_id, success in successes.items():
        if task_id not in committed_tasks:
            continue
        for intent in intents_from_success_event(success):
            if intent.effect_id not in acked:
                pending.append((success, intent))
    pending.sort(key=lambda item: item[1].effect_id)
    return tuple(pending)


def task_effects_fully_acknowledged(
    *,
    durable_effects: Sequence[Mapping[str, object] | DurableEffectIntentV1],
    acknowledged_effect_ids: Sequence[str],
) -> bool:
    if not durable_effects:
        return True
    acked = set(acknowledged_effect_ids)
    for raw in durable_effects:
        intent = raw if isinstance(raw, DurableEffectIntentV1) else DurableEffectIntentV1.model_validate(raw)
        if intent.effect_id not in acked:
            return False
    return True


def record_integrity_failure(
    change_dir: Path,
    *,
    invocation_id: str,
    checkpoint_ns: str,
    task_id: str,
    attempt_id: str,
    effect_id: str,
    reason: str,
) -> None:
    """Append integrity diagnostic + terminal stop for a permanent conflict."""
    with transaction(change_dir) as txn:
        txn.append_strict(
            DurableEffectIntegrityFailedEvent(
                type="durable_effect_integrity_failed",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                task_id=task_id,
                attempt_id=attempt_id,
                effect_id=effect_id,
                reason=reason,
            )
        )
        txn.append_strict(
            GraphTerminalEvent(
                type="graph_failed",
                invocation_id=invocation_id,
                checkpoint_ns=checkpoint_ns,
                reason="durable_effect_integrity_failed",
            )
        )


def _find_acknowledgement(
    change_dir: Path,
    effect_id: str,
) -> DurableEffectAcknowledgementV1 | None:
    found: DurableEffectAcknowledgementV1 | None = None
    for raw in read_events_strict(change_dir):
        if raw.get("source") != "graph" or raw.get("type") != "durable_effect_acknowledged":
            continue
        if raw.get("effect_id") != effect_id:
            continue
        ack = DurableEffectAcknowledgementV1.model_validate(
            {
                "schema_version": "1",
                "root_invocation_id": raw["root_invocation_id"],
                "invocation_id": raw["invocation_id"],
                "task_id": raw["task_id"],
                "attempt_id": raw["attempt_id"],
                "effect_id": raw["effect_id"],
                "kind": raw["kind"],
                "reconciler_semantics_digest": raw["reconciler_semantics_digest"],
                "payload_sha256": raw["payload_sha256"],
                "domain_source_sequence": raw["domain_source_sequence"],
                "domain_event_digest": raw["domain_event_digest"],
            }
        )
        if found is None:
            found = ack
            continue
        if found.model_dump(mode="json") != ack.model_dump(mode="json"):
            raise DurableEffectIntegrityError(
                f"conflicting durable_effect_acknowledged payloads for {effect_id}"
            )
    return found


def _append_acknowledgement(change_dir: Path, ack: DurableEffectAcknowledgementV1) -> None:
    existing = _find_acknowledgement(change_dir, ack.effect_id)
    if existing is not None:
        if existing.model_dump(mode="json") != ack.model_dump(mode="json"):
            raise DurableEffectIntegrityError(
                f"conflicting durable_effect_acknowledged payloads for {ack.effect_id}"
            )
        return
    event = DurableEffectAcknowledgedEvent(
        type="durable_effect_acknowledged",
        root_invocation_id=ack.root_invocation_id,
        invocation_id=ack.invocation_id,
        checkpoint_ns=ack.invocation_id,
        task_id=ack.task_id,
        attempt_id=ack.attempt_id,
        effect_id=ack.effect_id,
        kind=ack.kind,
        reconciler_semantics_digest=ack.reconciler_semantics_digest,
        payload_sha256=ack.payload_sha256,
        domain_source_sequence=ack.domain_source_sequence,
        domain_event_digest=ack.domain_event_digest,
    )
    # Prefer progression txn when caller holds no lock; append_event_strict under lock.
    with transaction(change_dir) as txn:
        txn.append_strict(event)


def intents_as_wire(
    intents: Sequence[DurableEffectIntentV1],
) -> list[dict[str, object]]:
    return [intent.model_dump(mode="json") for intent in intents]


__all__ = [
    "KNOWN_DURABLE_EFFECT_KINDS",
    "COMMIT_SAFETY_INVENTORY",
    "HEALING_ALLOCATION_V2",
    "FIXER_PROPOSAL_APPROVED_V1",
    "HEAL_RECORD_APPLY_V2",
    "DurableEffectKind",
    "DurableEffectError",
    "DurableEffectValidationError",
    "DurableEffectIntegrityError",
    "DurableEffectRetryableError",
    "DurableEffectIntentV1",
    "DurableEffectAcknowledgementV1",
    "DurableEffectContext",
    "DurableEffectRuntime",
    "EffectRegistration",
    "EffectRegistry",
    "production_effect_registry",
    "payload_sha256",
    "reconciler_semantics_digest",
    "derive_effect_id",
    "validate_durable_effect_kinds",
    "build_intent",
    "validate_result_intents",
    "intents_from_success_event",
    "validate_acknowledgement",
    "reconcile_effect",
    "scan_unacknowledged_intents",
    "task_effects_fully_acknowledged",
    "record_integrity_failure",
    "intents_as_wire",
]
