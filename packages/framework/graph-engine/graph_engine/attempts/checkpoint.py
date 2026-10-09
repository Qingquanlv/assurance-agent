from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Literal, cast

from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes


class AttemptPhase(str, Enum):
    """Durable handler entry; saving a phase does not invoke its handler."""

    AUTHORIZE = "authorize"
    EXECUTE = "execute"
    RECONCILE = "reconcile"
    COMMIT = "commit"
    TERMINATE = "terminate"
    RELEASE = "release"
    DONE = "done"


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty")


@dataclass(frozen=True, slots=True)
class AttemptResult:
    resolution_kind: Literal["committed", "rejected", "permanent", "retryable"] = "committed"
    output: JSONValue = None
    receipt_id: str = ""
    receipt_digest: str = ""
    reason: str = ""
    failure_kind: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if self.resolution_kind not in {"committed", "rejected", "permanent", "retryable"}:
            raise ValueError("unsupported resolution kind")
        for name in ("receipt_id", "receipt_digest", "reason", "failure_kind", "message"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if bool(self.receipt_id) != bool(self.receipt_digest):
            raise ValueError("terminal receipt id and digest must be present together")
        if self.receipt_digest:
            _digest(self.receipt_digest, "terminal receipt")
        if self.resolution_kind == "committed" and not self.receipt_id:
            raise ValueError("committed result requires a promotion receipt")
        canonical_json_bytes(self.output)


@dataclass(frozen=True, slots=True)
class ActiveSystemInterrupt:
    generation: int
    ordinal: int
    envelope_digest: str
    issuance_checkpoint_id: str | None = None
    completion_checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        _integer(self.generation, "interrupt generation", 1)
        _integer(self.ordinal, "interrupt ordinal")
        _digest(self.envelope_digest, "interrupt envelope")
        for name in ("issuance_checkpoint_id", "completion_checkpoint_id"):
            if (value := getattr(self, name)) is not None:
                _text(value, name)
        if self.completion_checkpoint_id is not None and self.issuance_checkpoint_id is None:
            raise ValueError("interrupt completion requires anchored issuance")

    @property
    def issuance_anchored(self) -> bool:
        return self.issuance_checkpoint_id is not None

    @property
    def retired(self) -> bool:
        return self.completion_checkpoint_id is not None


@dataclass(frozen=True, slots=True)
class AttemptCheckpoint:
    attempt_key: AttemptKey
    revision: int
    fencing_token: int
    phase: AttemptPhase
    contract_digest: str
    input_digest: str
    graph_revision: str
    invocation_id: str
    public_entrypoint: str
    semantic_node_id: str
    authorization_id: str | None = None
    activity_id: str | None = None
    activity_state: Literal["prepared", "dispatch_started", "bound", "terminal_observed"] | None = None
    activity_outcome: JSONValue = None
    activity_outcome_digest: str | None = None
    activity_dispatch_fingerprint: JSONValue = None
    activity_dispatch_fingerprint_digest: str | None = None
    activity_reference: JSONValue = None
    activity_reference_digest: str | None = None
    source_identity_digest: str | None = None
    source_receipt_digest: str | None = None
    prepared_digest: str | None = None
    promotion_receipt_id: str | None = None
    promotion_receipt_digest: str | None = None
    promotion_staged_digest: str | None = None
    pending_result: AttemptResult | None = None
    terminal: AttemptResult | None = None
    terminal_fencing_token: int | None = None
    terminal_revision: int | None = None
    released: bool = False
    active_interrupt: ActiveSystemInterrupt | None = None
    active_interrupts: tuple[ActiveSystemInterrupt, ...] = ()
    retired_generations: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_key, AttemptKey) or not isinstance(self.phase, AttemptPhase):
            raise ValueError("checkpoint requires typed attempt key and handler phase")
        _integer(self.revision, "revision")
        _integer(self.fencing_token, "fencing token", 1)
        for name in ("contract_digest", "input_digest", "graph_revision"):
            _digest(getattr(self, name), name)
        for name in ("invocation_id", "public_entrypoint", "semantic_node_id"):
            _text(getattr(self, name), name)
        for name in (
            "authorization_id",
            "activity_outcome_digest",
            "activity_dispatch_fingerprint_digest",
            "activity_reference_digest",
            "source_identity_digest",
            "source_receipt_digest",
            "prepared_digest",
            "promotion_receipt_digest",
            "promotion_staged_digest",
        ):
            if (value := getattr(self, name)) is not None:
                _digest(value, name)
        for name in ("activity_id", "promotion_receipt_id"):
            if (value := getattr(self, name)) is not None:
                _text(value, name)
        states = ("prepared", "dispatch_started", "bound", "terminal_observed")
        if self.activity_state is not None and self.activity_state not in states:
            raise ValueError("unknown activity state")
        if (self.activity_id is None) != (self.activity_state is None):
            raise ValueError("activity identity and state must be present together")
        if self.activity_id is not None and self.authorization_id is None:
            raise ValueError("activity requires authorization")
        for payload_name, digest_name in (
            ("activity_outcome", "activity_outcome_digest"),
            ("activity_dispatch_fingerprint", "activity_dispatch_fingerprint_digest"),
            ("activity_reference", "activity_reference_digest"),
        ):
            payload, digest = getattr(self, payload_name), getattr(self, digest_name)
            canonical_json_bytes(payload)
            if digest is None and payload is not None:
                raise ValueError(f"{payload_name} requires its digest")
            if digest is not None and (self.activity_id is None or canonical_digest(payload) != digest):
                raise ValueError(f"{payload_name} digest drifted")
        if (
            self.activity_state in {"dispatch_started", "bound"}
            and self.activity_dispatch_fingerprint_digest is None
        ):
            raise ValueError("dispatch requires fingerprint proof")
        if self.activity_state == "bound" and self.activity_reference_digest is None:
            raise ValueError("bound activity requires reference proof")
        if self.activity_reference_digest is not None and self.activity_state not in {
            "bound",
            "terminal_observed",
        }:
            raise ValueError("activity reference requires bound or observed terminal state")
        if self.activity_outcome_digest is not None and self.activity_state != "terminal_observed":
            raise ValueError("outcome requires observed terminal activity")
        if self.activity_state == "terminal_observed" and self.activity_outcome_digest is None:
            raise ValueError("observed terminal activity requires outcome proof")
        if (self.source_identity_digest is None) != (self.source_receipt_digest is None):
            raise ValueError("source receipt identity and digest must be present together")
        if self.source_identity_digest is not None and self.activity_state != "terminal_observed":
            raise ValueError("source receipt requires observed terminal activity")
        promotion = (self.promotion_receipt_id, self.promotion_receipt_digest, self.promotion_staged_digest)
        if any(item is not None for item in promotion) and not all(item is not None for item in promotion):
            raise ValueError("promotion proof is incomplete")
        if self.prepared_digest is not None and self.activity_state != "terminal_observed":
            raise ValueError("preparation requires observed output")
        if self.promotion_receipt_id is not None and self.prepared_digest is None:
            raise ValueError("promotion requires preparation proof")
        for name in ("pending_result", "terminal"):
            if (result := getattr(self, name)) is not None and not isinstance(result, AttemptResult):
                raise ValueError("checkpoint result must be typed")
            if result is not None and result.resolution_kind == "committed":
                if (result.receipt_id, result.receipt_digest) != (
                    self.promotion_receipt_id,
                    self.promotion_receipt_digest,
                ):
                    raise ValueError("committed result differs from promotion proof")
            if (
                result is not None
                and self.promotion_receipt_id is not None
                and result.resolution_kind != "committed"
            ):
                raise ValueError("promoted workspace cannot become an uncommitted result")
        if (
            self.terminal is not None
            and self.pending_result is not None
            and self.terminal != self.pending_result
        ):
            raise ValueError("terminal result differs from pending result")
        for name in ("terminal_fencing_token", "terminal_revision"):
            if (value := getattr(self, name)) is not None:
                _integer(value, name, 1)
                if self.terminal is None:
                    raise ValueError("terminal ordering metadata requires terminal proof")
        if (self.terminal_fencing_token is None) != (self.terminal_revision is None):
            raise ValueError("terminal ordering metadata must be present together")
        if self.terminal_fencing_token is not None and self.terminal_fencing_token > self.fencing_token:
            raise ValueError("terminal fence exceeds current fence")
        if self.terminal_revision is not None and self.terminal_revision > self.revision:
            raise ValueError("terminal revision exceeds current revision")
        if not isinstance(self.released, bool) or (self.released and self.terminal is None):
            raise ValueError("release requires terminal proof")
        if self.phase is AttemptPhase.AUTHORIZE and any(
            value is not None
            for value in (self.authorization_id, self.activity_id, self.pending_result, self.terminal)
        ):
            raise ValueError("authorize entry has advanced lifecycle facts")
        if self.phase is AttemptPhase.EXECUTE and (
            self.authorization_id is None or self.activity_state not in {None, "prepared"}
        ):
            raise ValueError("execute entry requires authorization before dispatch")
        if self.phase is AttemptPhase.RECONCILE and self.activity_state not in {
            "prepared",
            "dispatch_started",
            "bound",
        }:
            raise ValueError("reconcile entry requires an in-flight activity")
        if self.phase is AttemptPhase.COMMIT and self.activity_state != "terminal_observed":
            raise ValueError("commit entry requires observed output")
        if self.phase is AttemptPhase.TERMINATE and self.pending_result is None:
            raise ValueError("terminate entry requires a pending result")
        if self.pending_result is not None and self.phase not in {
            AttemptPhase.TERMINATE,
            AttemptPhase.RELEASE,
            AttemptPhase.DONE,
        }:
            raise ValueError("pending result requires terminate, release or done entry")
        if self.terminal is not None and self.phase not in {AttemptPhase.RELEASE, AttemptPhase.DONE}:
            raise ValueError("terminal proof requires release or done entry")
        if self.phase in {AttemptPhase.RELEASE, AttemptPhase.DONE} and self.terminal is None:
            raise ValueError("release and done entries require terminal proof")
        if self.phase is AttemptPhase.DONE and not self.released:
            raise ValueError("done entry requires release proof")
        if self.released and self.phase not in {AttemptPhase.RELEASE, AttemptPhase.DONE}:
            raise ValueError("release proof requires release or done entry")
        if not isinstance(self.active_interrupts, tuple) or not isinstance(self.retired_generations, tuple):
            raise ValueError("interrupt records must be tuples")
        identities: set[int] = set()
        for item in self.active_interrupts:
            if not isinstance(item, ActiveSystemInterrupt) or item.generation in identities:
                raise ValueError("duplicate or untyped interrupt generation")
            identities.add(item.generation)
        if self.active_interrupt is not None and self.active_interrupt not in self.active_interrupts:
            raise ValueError("active interrupt is missing from complete interrupt records")
        for generation in self.retired_generations:
            _integer(generation, "retired generation", 1)
        if len(set(self.retired_generations)) != len(self.retired_generations):
            raise ValueError("duplicate retired generation")
        retired = {item.generation for item in self.active_interrupts if item.retired}
        if set(self.retired_generations) != retired:
            raise ValueError("retired generations differ from completion proofs")

    def canonical_projection(self) -> dict[str, JSONValue]:
        document = asdict(self)
        document["attempt_key"] = {"digest": self.attempt_key.digest}
        document["phase"] = self.phase.value
        document["active_interrupts"] = list(document["active_interrupts"])
        document["retired_generations"] = list(document["retired_generations"])
        return cast(dict[str, JSONValue], document)


__all__ = ["ActiveSystemInterrupt", "AttemptCheckpoint", "AttemptPhase", "AttemptResult"]
