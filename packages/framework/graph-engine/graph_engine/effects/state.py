from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import thaw_json
from graph_engine.persistence.runner_lease import StaleFencingToken


class EffectStateIntegrityError(GraphEngineError):
    """Raised when effect-state uniqueness, reuse, or digest checks fail."""


def require_fencing_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _json_value(value: object) -> JSONValue:
    return thaw_json(value)


def _canonical_bytes(value: object) -> bytes:
    return canonical_json_bytes(_json_value(value))


def effect_intent_digest(kind: str, payload: object) -> str:
    return canonical_digest({"kind": kind, "payload": _json_value(payload)})


@dataclass(frozen=True, slots=True)
class EffectStateObservation:
    status: Literal["absent", "committed"]
    receipt: JSONValue | None = None
    payload: JSONValue | None = None
    business_key: str | None = None
    intent_digest: str | None = None


class EffectStatePort(Protocol):
    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ) -> EffectStateObservation: ...

    async def observe(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        fencing_token: int,
    ) -> EffectStateObservation: ...


@dataclass(frozen=True, slots=True)
class EffectStateRecord:
    effect_kind: str
    settlement_key: str
    business_key: str
    intent_digest: str
    payload: JSONValue
    receipt: JSONValue
    fencing_token: int


def effect_observation(record: EffectStateRecord) -> EffectStateObservation:
    return EffectStateObservation(
        status="committed",
        receipt=record.receipt,
        payload=record.payload,
        business_key=record.business_key,
        intent_digest=record.intent_digest,
    )


def assert_effect_reuse(
    record: EffectStateRecord, *, business_key: str, intent_digest: str, receipt: object | None
) -> None:
    if record.business_key != business_key:
        raise EffectStateIntegrityError("effect business key conflict")
    if record.intent_digest != intent_digest:
        raise EffectStateIntegrityError("intent digest drifted")
    if receipt is not None and _canonical_bytes(record.receipt) != _canonical_bytes(receipt):
        raise EffectStateIntegrityError("effect receipt reuse requires exact bytes")


class MemoryEffectState:
    """In-memory EffectStatePort for Kernel and Capability tests only."""

    def __init__(self) -> None:
        self._by_settlement: dict[tuple[str, str], EffectStateRecord] = {}
        self._by_business: dict[tuple[str, str], tuple[str, str]] = {}

    async def commit(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
        fencing_token: int,
    ) -> EffectStateObservation:
        token = require_fencing_token(fencing_token)
        settlement = (effect_kind, settlement_key)
        business = (effect_kind, business_key)
        existing = self._by_settlement.get(settlement)
        owner = self._by_business.get(business)
        if existing is not None:
            if token < existing.fencing_token:
                raise StaleFencingToken("fencing token is stale")
            assert_effect_reuse(
                existing, business_key=business_key, intent_digest=intent_digest, receipt=receipt
            )
            return effect_observation(existing)
        if owner is not None:
            raise EffectStateIntegrityError("effect business key conflict")
        record = EffectStateRecord(
            effect_kind=effect_kind,
            settlement_key=settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            payload=_json_value(payload),
            receipt=_json_value(receipt),
            fencing_token=token,
        )
        self._by_settlement[settlement] = record
        self._by_business[business] = settlement
        return effect_observation(record)

    async def observe(
        self,
        *,
        effect_kind: str,
        settlement_key: str,
        business_key: str,
        intent_digest: str,
        fencing_token: int,
    ) -> EffectStateObservation:
        token = require_fencing_token(fencing_token)
        existing = self._by_settlement.get((effect_kind, settlement_key))
        owner = self._by_business.get((effect_kind, business_key))
        if existing is None:
            if owner is not None:
                raise EffectStateIntegrityError("effect business key conflict")
            return EffectStateObservation(status="absent")
        if token < existing.fencing_token:
            raise StaleFencingToken("fencing token is stale")
        assert_effect_reuse(existing, business_key=business_key, intent_digest=intent_digest, receipt=None)
        return effect_observation(existing)


class EffectCallContext:
    """Capability-visible seam bound to one kind, settlement key, and live fence."""

    def __init__(
        self,
        state: EffectStatePort,
        *,
        effect_kind: str,
        settlement_key: str,
        fencing_token: int,
    ) -> None:
        self._state = state
        self._effect_kind = effect_kind
        self._settlement_key = settlement_key
        self._fencing_token = require_fencing_token(fencing_token)

    @property
    def settlement_key(self) -> str:
        return self._settlement_key

    async def commit(
        self,
        *,
        business_key: str,
        intent_digest: str,
        payload: JSONValue,
        receipt: JSONValue,
    ) -> EffectStateObservation:
        return await self._state.commit(
            effect_kind=self._effect_kind,
            settlement_key=self._settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            payload=payload,
            receipt=receipt,
            fencing_token=self._fencing_token,
        )

    async def observe(self, *, business_key: str, intent_digest: str) -> EffectStateObservation:
        return await self._state.observe(
            effect_kind=self._effect_kind,
            settlement_key=self._settlement_key,
            business_key=business_key,
            intent_digest=intent_digest,
            fencing_token=self._fencing_token,
        )


def bind_effect_call(
    *,
    state: EffectStatePort,
    effect_kind: str,
    settlement_key: str,
    fencing_token: int,
) -> EffectCallContext:
    return EffectCallContext(
        state,
        effect_kind=effect_kind,
        settlement_key=settlement_key,
        fencing_token=fencing_token,
    )


__all__ = [
    "EffectCallContext",
    "EffectStateIntegrityError",
    "EffectStateObservation",
    "EffectStatePort",
    "EffectStateRecord",
    "MemoryEffectState",
    "assert_effect_reuse",
    "bind_effect_call",
    "effect_intent_digest",
    "effect_observation",
    "require_fencing_token",
]
