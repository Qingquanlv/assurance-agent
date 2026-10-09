from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, replace
from typing import Any, Protocol

from graph_engine.attempts.checkpoint import (
    ActiveSystemInterrupt,
    AttemptCheckpoint,
    AttemptPhase,
    AttemptResult,
)
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.runner_lease import StaleFencingToken


ATTEMPT_CHECKPOINT_SCHEMA_VERSION = "1"
FenceAssertion = Callable[[str, int], Awaitable[None]]


class AttemptCheckpointIntegrityError(ValueError):
    """A checkpoint, replacement, or durable encoding violates its contract."""


def encode_attempt_checkpoint(checkpoint: AttemptCheckpoint) -> bytes:
    return canonical_json_bytes(checkpoint.canonical_projection())


def _closed(document: Any, model: type) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {field.name for field in fields(model)}:
        raise AttemptCheckpointIntegrityError("unknown or omitted checkpoint field")
    return dict(document)


def decode_attempt_checkpoint(
    payload: bytes,
    *,
    schema_version: str = ATTEMPT_CHECKPOINT_SCHEMA_VERSION,
    record_digest: str | None = None,
) -> AttemptCheckpoint:
    return _decode_checkpoint(
        payload, schema_version=schema_version, record_digest=record_digest, persisted=True
    )


def _decode_checkpoint(
    payload: bytes, *, schema_version: str, record_digest: str | None, persisted: bool
) -> AttemptCheckpoint:
    if schema_version != ATTEMPT_CHECKPOINT_SCHEMA_VERSION:
        raise AttemptCheckpointIntegrityError("unknown attempt checkpoint schema version")
    try:
        document = json.loads(payload)
        if canonical_json_bytes(document) != payload:
            raise AttemptCheckpointIntegrityError("checkpoint bytes are not canonical")
        if record_digest is not None and canonical_digest(document) != record_digest:
            raise AttemptCheckpointIntegrityError("attempt checkpoint digest drifted")
        values = _closed(document, AttemptCheckpoint)
        key = values["attempt_key"]
        if not isinstance(key, dict) or set(key) != {"digest"} or not isinstance(key["digest"], str):
            raise AttemptCheckpointIntegrityError("checkpoint key drifted")
        values["attempt_key"] = AttemptKey(digest=key["digest"])
        values["phase"] = AttemptPhase(values["phase"])
        for name in ("pending_result", "terminal"):
            if values[name] is not None:
                values[name] = AttemptResult(**_closed(values[name], AttemptResult))
        if values["active_interrupt"] is not None:
            values["active_interrupt"] = ActiveSystemInterrupt(
                **_closed(values["active_interrupt"], ActiveSystemInterrupt)
            )
        if not isinstance(values["active_interrupts"], list) or not isinstance(
            values["retired_generations"], list
        ):
            raise AttemptCheckpointIntegrityError("checkpoint interrupt collections drifted")
        values["active_interrupts"] = tuple(
            ActiveSystemInterrupt(**_closed(item, ActiveSystemInterrupt))
            for item in values["active_interrupts"]
        )
        values["retired_generations"] = tuple(values["retired_generations"])
        checkpoint = AttemptCheckpoint(**values)
        if (
            persisted
            and checkpoint.revision > 0
            and checkpoint.terminal is not None
            and checkpoint.terminal_fencing_token is None
        ):
            raise AttemptCheckpointIntegrityError("persisted terminal is missing ordering metadata")
        return checkpoint
    except (TypeError, ValueError, KeyError, UnicodeError) as error:
        if isinstance(error, AttemptCheckpointIntegrityError):
            raise
        raise AttemptCheckpointIntegrityError("malformed attempt checkpoint") from error


def prepare_checkpoint_commit(
    checkpoint: AttemptCheckpoint,
    previous: AttemptCheckpoint | None,
    *,
    expected_revision: int,
    fencing_token: int,
) -> AttemptCheckpoint:
    """Validate a full-record CAS and preserve every previously durable fact."""
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("expected revision must be a non-negative integer")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 1:
        raise ValueError("fencing token must be a positive integer")
    if not isinstance(checkpoint, AttemptCheckpoint):
        raise TypeError("checkpoint must be an AttemptCheckpoint")
    # Revalidate nested mutable JSON before admitting a replacement.
    checkpoint = _decode_checkpoint(
        encode_attempt_checkpoint(checkpoint),
        schema_version=ATTEMPT_CHECKPOINT_SCHEMA_VERSION,
        record_digest=None,
        persisted=False,
    )
    if previous is not None and fencing_token < previous.fencing_token:
        raise StaleFencingToken("fencing token is stale")
    if expected_revision != (0 if previous is None else previous.revision):
        raise AttemptCheckpointIntegrityError("compare-and-swap conflict")
    if checkpoint.revision != expected_revision:
        raise AttemptCheckpointIntegrityError("candidate revision differs from expected revision")
    if checkpoint.fencing_token != fencing_token:
        raise AttemptCheckpointIntegrityError("fencing token differs from checkpoint")
    if previous is not None:
        identity = (
            "attempt_key",
            "contract_digest",
            "input_digest",
            "graph_revision",
            "invocation_id",
            "public_entrypoint",
            "semantic_node_id",
        )
        for name in identity:
            if getattr(previous, name) != getattr(checkpoint, name):
                raise AttemptCheckpointIntegrityError("immutable attempt identity drifted")
        facts = (
            "authorization_id",
            "activity_id",
            "activity_outcome_digest",
            "activity_dispatch_fingerprint_digest",
            "activity_reference_digest",
            "source_identity_digest",
            "source_receipt_digest",
            "prepared_digest",
            "promotion_receipt_id",
            "promotion_receipt_digest",
            "promotion_staged_digest",
            "pending_result",
            "terminal",
            "terminal_fencing_token",
            "terminal_revision",
        )
        for name in facts:
            old = getattr(previous, name)
            if old is not None and old != getattr(checkpoint, name):
                raise AttemptCheckpointIntegrityError(f"durable {name} cannot be erased or replaced")
        if previous.released and not checkpoint.released:
            raise AttemptCheckpointIntegrityError("release proof cannot be erased")
        states = {None: 0, "prepared": 1, "dispatch_started": 2, "bound": 3, "terminal_observed": 4}
        if states[checkpoint.activity_state] < states[previous.activity_state]:
            raise AttemptCheckpointIntegrityError("activity lifecycle regressed")
        phases = list(AttemptPhase)
        if phases.index(checkpoint.phase) < phases.index(previous.phase):
            raise AttemptCheckpointIntegrityError("handler phase regressed")
        interrupts = {item.generation: item for item in checkpoint.active_interrupts}
        for old_interrupt in previous.active_interrupts:
            current = interrupts.get(old_interrupt.generation)
            if current is None or (current.ordinal, current.envelope_digest) != (
                old_interrupt.ordinal,
                old_interrupt.envelope_digest,
            ):
                raise AttemptCheckpointIntegrityError(
                    "issued interrupt identity cannot be erased or replaced"
                )
            for name in ("issuance_checkpoint_id", "completion_checkpoint_id"):
                old = getattr(old_interrupt, name)
                if old is not None and old != getattr(current, name):
                    raise AttemptCheckpointIntegrityError(
                        "acknowledged interrupt cannot be erased or replaced"
                    )
        if not set(previous.retired_generations) <= set(checkpoint.retired_generations):
            raise AttemptCheckpointIntegrityError("retired interrupt cannot be erased")
    if checkpoint.terminal is not None and (previous is None or previous.terminal is None):
        if checkpoint.terminal_fencing_token is not None or checkpoint.terminal_revision is not None:
            raise AttemptCheckpointIntegrityError("terminal ordering metadata is assigned by the store")
        return replace(
            checkpoint,
            revision=expected_revision + 1,
            terminal_fencing_token=fencing_token,
            terminal_revision=expected_revision + 1,
        )
    return replace(checkpoint, revision=expected_revision + 1)


class AttemptCheckpointStore(Protocol):
    async def load(self, attempt_key: AttemptKey) -> AttemptCheckpoint | None: ...
    async def commit(
        self, checkpoint: AttemptCheckpoint, *, expected_revision: int, fencing_token: int
    ) -> AttemptCheckpoint: ...
    async def read_checkpoints(self) -> tuple[AttemptCheckpoint, ...]: ...
    async def ensure_durable(self, attempt_key: AttemptKey) -> None: ...
    async def latest_generation(
        self, scope: Mapping[str, JSONValue]
    ) -> tuple[int, AttemptKey, bool, JSONValue] | None: ...
    async def register_generation(
        self,
        scope: Mapping[str, JSONValue],
        make_key: Callable[[int], AttemptKey],
        *,
        max_attempts: int,
        validated_input: JSONValue = None,
    ) -> tuple[int, AttemptKey] | None: ...


class MemoryAttemptCheckpointStore:
    def __init__(self, assert_current: FenceAssertion | None = None) -> None:
        self._assert_current = assert_current
        self._checkpoints: dict[str, bytes] = {}
        self._lock = asyncio.Lock()
        self._generations: dict[str, list[tuple[AttemptKey, bytes]]] = {}
        self._abandoned: set[str] = set()

    async def load(self, attempt_key: AttemptKey) -> AttemptCheckpoint | None:
        payload = self._checkpoints.get(attempt_key.digest)
        return None if payload is None else decode_attempt_checkpoint(payload)

    async def commit(
        self, checkpoint: AttemptCheckpoint, *, expected_revision: int, fencing_token: int
    ) -> AttemptCheckpoint:
        async with self._lock:
            if self._assert_current is not None:
                await self._assert_current(checkpoint.invocation_id, fencing_token)
            saved = prepare_checkpoint_commit(
                checkpoint,
                await self.load(checkpoint.attempt_key),
                expected_revision=expected_revision,
                fencing_token=fencing_token,
            )
            payload = encode_attempt_checkpoint(saved)
            self._checkpoints[saved.attempt_key.digest] = payload
            return decode_attempt_checkpoint(payload)

    async def read_checkpoints(self) -> tuple[AttemptCheckpoint, ...]:
        return tuple(decode_attempt_checkpoint(self._checkpoints[key]) for key in sorted(self._checkpoints))

    async def ensure_durable(self, attempt_key: AttemptKey) -> None:
        await self.load(attempt_key)

    async def abandon_generations(self, attempt_key_digests: set[str]) -> None:
        self._abandoned.update(attempt_key_digests)

    async def latest_generation(
        self, scope: Mapping[str, JSONValue]
    ) -> tuple[int, AttemptKey, bool, JSONValue] | None:
        entries = self._generations.get(canonical_digest(dict(scope)), [])
        if not entries:
            return None
        key, payload = entries[-1]
        return len(entries), key, key.digest in self._abandoned, json.loads(payload)

    async def register_generation(
        self,
        scope: Mapping[str, JSONValue],
        make_key: Callable[[int], AttemptKey],
        *,
        max_attempts: int,
        validated_input: JSONValue = None,
    ) -> tuple[int, AttemptKey] | None:
        async with self._lock:
            digest = canonical_digest(dict(scope))
            entries = self._generations.get(digest, [])
            ordinal = len(entries) + 1
            if ordinal > max_attempts:
                return None
            key = make_key(ordinal)
            if not isinstance(key, AttemptKey) or any(
                key == existing for values in self._generations.values() for existing, _ in values
            ):
                raise AttemptCheckpointIntegrityError("generation key must be unique and typed")
            payload = canonical_json_bytes(validated_input)
            entries.append((key, payload))
            self._generations[digest] = entries
            return ordinal, key


__all__ = [
    "ATTEMPT_CHECKPOINT_SCHEMA_VERSION",
    "AttemptCheckpointIntegrityError",
    "AttemptCheckpointStore",
    "MemoryAttemptCheckpointStore",
    "decode_attempt_checkpoint",
    "encode_attempt_checkpoint",
]
