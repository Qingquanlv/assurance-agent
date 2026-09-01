from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest


def _sha256(value: str, kind: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{kind} digest must be a lowercase SHA-256 hex value")
    return value


@dataclass(frozen=True, slots=True)
class AttemptOpened:
    kind: ClassVar[str] = "attempt_opened"
    contract_digest: str = ""
    input_digest: str = ""
    graph_revision: str = ""
    invocation_id: str = ""
    public_entrypoint: str = ""
    semantic_node_id: str = ""

    def __post_init__(self) -> None:
        _sha256(self.contract_digest, "contract")
        _sha256(self.input_digest, "input")
        _sha256(self.graph_revision, "graph revision")
        if not self.invocation_id:
            raise ValueError("invocation id must be nonempty")
        if not self.public_entrypoint:
            raise ValueError("public entrypoint must be nonempty")
        if not self.semantic_node_id:
            raise ValueError("semantic node id must be nonempty")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "kind": self.kind,
            "contract_digest": self.contract_digest,
            "graph_revision": self.graph_revision,
            "input_digest": self.input_digest,
            "invocation_id": self.invocation_id,
            "public_entrypoint": self.public_entrypoint,
            "semantic_node_id": self.semantic_node_id,
        }


@dataclass(frozen=True, slots=True)
class ResourcesAuthorized:
    kind: ClassVar[str] = "resources_authorized"
    authorization_id: str = ""

    def __post_init__(self) -> None:
        _sha256(self.authorization_id, "authorization")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {"authorization_id": self.authorization_id, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class ActivityPrepared:
    kind: ClassVar[str] = "activity_prepared"
    activity_id: str = ""

    def __post_init__(self) -> None:
        if not self.activity_id:
            raise ValueError("activity id must be nonempty")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {"activity_id": self.activity_id, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class ActivityDispatchStarted:
    kind: ClassVar[str] = "activity_dispatch_started"
    activity_id: str = ""
    dispatch_fingerprint: JSONValue = None
    dispatch_fingerprint_digest: str = ""

    def __post_init__(self) -> None:
        if not self.activity_id:
            raise ValueError("activity id must be nonempty")
        _sha256(self.dispatch_fingerprint_digest, "dispatch fingerprint")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "activity_id": self.activity_id,
            "dispatch_fingerprint": self.dispatch_fingerprint,
            "dispatch_fingerprint_digest": self.dispatch_fingerprint_digest,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class ActivityBound:
    kind: ClassVar[str] = "activity_bound"
    activity_id: str = ""
    reference: JSONValue = None
    reference_digest: str = ""

    def __post_init__(self) -> None:
        if not self.activity_id:
            raise ValueError("activity id must be nonempty")
        _sha256(self.reference_digest, "activity reference")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "activity_id": self.activity_id,
            "kind": self.kind,
            "reference": self.reference,
            "reference_digest": self.reference_digest,
        }


@dataclass(frozen=True, slots=True)
class ActivityTerminalObserved:
    kind: ClassVar[str] = "activity_terminal_observed"
    activity_id: str = ""
    outcome: JSONValue = None
    outcome_digest: str = ""

    def __post_init__(self) -> None:
        if not self.activity_id:
            raise ValueError("activity id must be nonempty")
        _sha256(self.outcome_digest, "activity outcome")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "activity_id": self.activity_id,
            "kind": self.kind,
            "outcome": self.outcome,
            "outcome_digest": self.outcome_digest,
        }


@dataclass(frozen=True, slots=True)
class CommitPrepared:
    kind: ClassVar[str] = "commit_prepared"
    prepared_digest: str = ""

    def __post_init__(self) -> None:
        _sha256(self.prepared_digest, "prepared")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {"kind": self.kind, "prepared_digest": self.prepared_digest}


@dataclass(frozen=True, slots=True)
class WorkspacePromoted:
    kind: ClassVar[str] = "workspace_promoted"
    receipt_id: str = ""
    receipt_digest: str = ""
    staged_digest: str = ""

    def __post_init__(self) -> None:
        if not self.receipt_id:
            raise ValueError("receipt id must be nonempty")
        _sha256(self.receipt_digest, "promotion receipt")
        _sha256(self.staged_digest, "staged")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "kind": self.kind,
            "receipt_digest": self.receipt_digest,
            "receipt_id": self.receipt_id,
            "staged_digest": self.staged_digest,
        }


@dataclass(frozen=True, slots=True)
class EffectIntentRecorded:
    kind: ClassVar[str] = "effect_intent_recorded"
    effect_ordinal: int = 1
    effect_kind: str = ""
    intent_digest: str = ""

    def __post_init__(self) -> None:
        if self.effect_ordinal < 1:
            raise ValueError("effect ordinal must be positive")
        if not self.effect_kind:
            raise ValueError("effect kind must be nonempty")
        _sha256(self.intent_digest, "effect intent")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "effect_kind": self.effect_kind,
            "effect_ordinal": self.effect_ordinal,
            "intent_digest": self.intent_digest,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class EffectApplied:
    kind: ClassVar[str] = "effect_applied"
    effect_ordinal: int = 1
    apply_digest: str = ""

    def __post_init__(self) -> None:
        if self.effect_ordinal < 1:
            raise ValueError("effect ordinal must be positive")
        _sha256(self.apply_digest, "effect apply")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "apply_digest": self.apply_digest,
            "effect_ordinal": self.effect_ordinal,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class EffectReceiptRecorded:
    kind: ClassVar[str] = "effect_receipt_recorded"
    effect_ordinal: int = 1
    receipt_digest: str = ""

    def __post_init__(self) -> None:
        if self.effect_ordinal < 1:
            raise ValueError("effect ordinal must be positive")
        _sha256(self.receipt_digest, "effect receipt")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "effect_ordinal": self.effect_ordinal,
            "kind": self.kind,
            "receipt_digest": self.receipt_digest,
        }


@dataclass(frozen=True, slots=True)
class SystemInterruptIssued:
    kind: ClassVar[str] = "system_interrupt_issued"
    generation: int = 1
    ordinal: int = 0
    envelope_digest: str = ""

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        _sha256(self.envelope_digest, "envelope")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "envelope_digest": self.envelope_digest,
            "generation": self.generation,
            "kind": self.kind,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class SystemInterruptIssuanceAnchored:
    kind: ClassVar[str] = "system_interrupt_issuance_anchored"
    generation: int = 1
    ordinal: int = 0
    envelope_digest: str = ""
    checkpoint_id: str = ""

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        _sha256(self.envelope_digest, "envelope")
        if not self.checkpoint_id:
            raise ValueError("checkpoint id must be nonempty")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "envelope_digest": self.envelope_digest,
            "generation": self.generation,
            "kind": self.kind,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class SystemInterruptCompletionCheckpointed:
    kind: ClassVar[str] = "system_interrupt_completion_checkpointed"
    generation: int = 1
    ordinal: int = 0
    envelope_digest: str = ""
    checkpoint_id: str = ""

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        _sha256(self.envelope_digest, "envelope")
        if not self.checkpoint_id:
            raise ValueError("checkpoint id must be nonempty")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "envelope_digest": self.envelope_digest,
            "generation": self.generation,
            "kind": self.kind,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class AttemptTerminated:
    kind: ClassVar[str] = "attempt_terminated"
    resolution_kind: str = "committed"
    output: JSONValue = None
    receipt_id: str = ""
    receipt_digest: str = ""

    def __post_init__(self) -> None:
        if not self.resolution_kind:
            raise ValueError("resolution kind must be nonempty")
        if self.receipt_id:
            if not self.receipt_digest:
                raise ValueError("receipt digest is required with a receipt id")
            _sha256(self.receipt_digest, "terminal receipt")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "kind": self.kind,
            "output": self.output,
            "receipt_digest": self.receipt_digest,
            "receipt_id": self.receipt_id,
            "resolution_kind": self.resolution_kind,
        }


@dataclass(frozen=True, slots=True)
class ResourcesReleased:
    kind: ClassVar[str] = "resources_released"
    authorization_id: str = ""

    def __post_init__(self) -> None:
        _sha256(self.authorization_id, "authorization")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {"authorization_id": self.authorization_id, "kind": self.kind}


AttemptEvent: TypeAlias = (
    AttemptOpened
    | ResourcesAuthorized
    | ActivityPrepared
    | ActivityDispatchStarted
    | ActivityBound
    | ActivityTerminalObserved
    | CommitPrepared
    | WorkspacePromoted
    | EffectIntentRecorded
    | EffectApplied
    | EffectReceiptRecorded
    | SystemInterruptIssued
    | SystemInterruptIssuanceAnchored
    | SystemInterruptCompletionCheckpointed
    | AttemptTerminated
    | ResourcesReleased
)


@dataclass(frozen=True, slots=True)
class ActiveSystemInterrupt:
    generation: int
    ordinal: int
    envelope_digest: str
    issuance_anchored: bool = False
    retired: bool = False


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    attempt_key: AttemptKey
    revision: int
    fencing_token: int
    contract_digest: str | None = None
    input_digest: str | None = None
    graph_revision: str | None = None
    invocation_id: str | None = None
    public_entrypoint: str | None = None
    semantic_node_id: str | None = None
    authorization_id: str | None = None
    activity_id: str | None = None
    activity_state: str | None = None
    activity_outcome: JSONValue = None
    prepared_digest: str | None = None
    promotion_receipt_id: str | None = None
    promotion_receipt_digest: str | None = None
    terminal: AttemptTerminated | None = None
    released: bool = False
    active_interrupt: ActiveSystemInterrupt | None = None
    retired_generations: tuple[int, ...] = ()


_ACTIVITY_STATES = {
    "activity_prepared": "prepared",
    "activity_dispatch_started": "dispatch_started",
    "activity_bound": "bound",
    "activity_terminal_observed": "terminal_observed",
}


def fold_attempt_events(
    attempt_key: AttemptKey,
    events: Sequence[AttemptEvent],
    *,
    revision: int = 0,
    fencing_token: int = 1,
) -> AttemptSnapshot:
    snapshot = AttemptSnapshot(attempt_key=attempt_key, revision=revision, fencing_token=fencing_token)
    retired: list[int] = []
    for event in events:
        if isinstance(event, AttemptOpened):
            snapshot = AttemptSnapshot(
                attempt_key=attempt_key,
                revision=snapshot.revision,
                fencing_token=snapshot.fencing_token,
                contract_digest=event.contract_digest,
                input_digest=event.input_digest,
                graph_revision=event.graph_revision,
                invocation_id=event.invocation_id,
                public_entrypoint=event.public_entrypoint,
                semantic_node_id=event.semantic_node_id,
                authorization_id=snapshot.authorization_id,
                activity_id=snapshot.activity_id,
                activity_state=snapshot.activity_state,
                activity_outcome=snapshot.activity_outcome,
                prepared_digest=snapshot.prepared_digest,
                promotion_receipt_id=snapshot.promotion_receipt_id,
                promotion_receipt_digest=snapshot.promotion_receipt_digest,
                terminal=snapshot.terminal,
                released=snapshot.released,
                active_interrupt=snapshot.active_interrupt,
                retired_generations=snapshot.retired_generations,
            )
            continue
        if isinstance(event, ResourcesAuthorized):
            snapshot = _replace(snapshot, authorization_id=event.authorization_id)
            continue
        if isinstance(
            event, (ActivityPrepared, ActivityDispatchStarted, ActivityBound, ActivityTerminalObserved)
        ):
            outcome = (
                event.outcome if isinstance(event, ActivityTerminalObserved) else snapshot.activity_outcome
            )
            snapshot = _replace(
                snapshot,
                activity_id=event.activity_id,
                activity_state=_ACTIVITY_STATES[event.kind],
                activity_outcome=outcome,
            )
            continue
        if isinstance(event, CommitPrepared):
            snapshot = _replace(snapshot, prepared_digest=event.prepared_digest)
            continue
        if isinstance(event, WorkspacePromoted):
            snapshot = _replace(
                snapshot,
                promotion_receipt_id=event.receipt_id,
                promotion_receipt_digest=event.receipt_digest,
            )
            continue
        if isinstance(event, SystemInterruptIssued):
            snapshot = _replace(
                snapshot,
                active_interrupt=ActiveSystemInterrupt(
                    generation=event.generation,
                    ordinal=event.ordinal,
                    envelope_digest=event.envelope_digest,
                ),
            )
            continue
        if isinstance(event, SystemInterruptIssuanceAnchored):
            current = snapshot.active_interrupt
            if (
                current is not None
                and current.generation == event.generation
                and current.ordinal == event.ordinal
                and current.envelope_digest == event.envelope_digest
            ):
                snapshot = _replace(
                    snapshot,
                    active_interrupt=ActiveSystemInterrupt(
                        generation=current.generation,
                        ordinal=current.ordinal,
                        envelope_digest=current.envelope_digest,
                        issuance_anchored=True,
                    ),
                )
            continue
        if isinstance(event, SystemInterruptCompletionCheckpointed):
            current = snapshot.active_interrupt
            if (
                current is not None
                and current.generation == event.generation
                and current.ordinal == event.ordinal
                and current.envelope_digest == event.envelope_digest
            ):
                retired.append(current.generation)
                snapshot = _replace(
                    snapshot,
                    active_interrupt=None,
                    retired_generations=tuple(retired),
                )
            continue
        if isinstance(event, AttemptTerminated):
            snapshot = _replace(snapshot, terminal=event)
            continue
        if isinstance(event, ResourcesReleased):
            snapshot = _replace(snapshot, released=True)
            continue
    return snapshot


def _replace(snapshot: AttemptSnapshot, **changes: object) -> AttemptSnapshot:
    values = {
        "attempt_key": snapshot.attempt_key,
        "revision": snapshot.revision,
        "fencing_token": snapshot.fencing_token,
        "contract_digest": snapshot.contract_digest,
        "input_digest": snapshot.input_digest,
        "graph_revision": snapshot.graph_revision,
        "invocation_id": snapshot.invocation_id,
        "public_entrypoint": snapshot.public_entrypoint,
        "semantic_node_id": snapshot.semantic_node_id,
        "authorization_id": snapshot.authorization_id,
        "activity_id": snapshot.activity_id,
        "activity_state": snapshot.activity_state,
        "activity_outcome": snapshot.activity_outcome,
        "prepared_digest": snapshot.prepared_digest,
        "promotion_receipt_id": snapshot.promotion_receipt_id,
        "promotion_receipt_digest": snapshot.promotion_receipt_digest,
        "terminal": snapshot.terminal,
        "released": snapshot.released,
        "active_interrupt": snapshot.active_interrupt,
        "retired_generations": snapshot.retired_generations,
    }
    values.update(changes)
    return AttemptSnapshot(**values)  # type: ignore[arg-type]


def event_digest(event: AttemptEvent) -> str:
    return canonical_digest(event.canonical_projection())


__all__ = [
    "ActiveSystemInterrupt",
    "ActivityBound",
    "ActivityDispatchStarted",
    "ActivityPrepared",
    "ActivityTerminalObserved",
    "AttemptEvent",
    "AttemptOpened",
    "AttemptSnapshot",
    "AttemptTerminated",
    "CommitPrepared",
    "EffectApplied",
    "EffectIntentRecorded",
    "EffectReceiptRecorded",
    "ResourcesAuthorized",
    "ResourcesReleased",
    "SystemInterruptCompletionCheckpointed",
    "SystemInterruptIssuanceAnchored",
    "SystemInterruptIssued",
    "WorkspacePromoted",
    "event_digest",
    "fold_attempt_events",
]
