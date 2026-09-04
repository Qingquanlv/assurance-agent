from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import re
from typing import Literal, Protocol

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import ResourceClaims

RESOURCE_AUTHORIZATION_SCHEMA_VERSION = "1"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ResourceAuthorizationAction = Literal["acquire", "adopt", "release"]


class ResourceAuthorizationError(GraphEngineError):
    """Raised when a resource authorization cannot be granted or adopted."""


class ResourceAuthorizationIntegrityError(GraphEngineError):
    """Raised when a resource-authorization record drifts from its identity."""


def _sha256(value: str, kind: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{kind} digest must be a lowercase SHA-256 hex value")
    return value


def _fencing_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _revision(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("revision must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ResourceAuthorizationRecord:
    revision: int
    action: ResourceAuthorizationAction
    authorization_id: str
    attempt_key_digest: str
    fencing_token: int
    claims: ResourceClaims
    record_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _revision(self.revision))
        if self.action not in {"acquire", "adopt", "release"}:
            raise ValueError("action must be acquire, adopt, or release")
        _sha256(self.authorization_id, "authorization")
        _sha256(self.attempt_key_digest, "attempt key")
        object.__setattr__(self, "fencing_token", _fencing_token(self.fencing_token))
        if not isinstance(self.claims, ResourceClaims):
            raise TypeError("claims must be ResourceClaims")
        _sha256(self.record_digest, "resource authorization")

    @classmethod
    def build(
        cls,
        *,
        revision: int,
        action: ResourceAuthorizationAction,
        authorization_id: str,
        attempt_key_digest: str,
        fencing_token: int,
        claims: ResourceClaims,
    ) -> ResourceAuthorizationRecord:
        draft = cls(
            revision=revision,
            action=action,
            authorization_id=authorization_id,
            attempt_key_digest=attempt_key_digest,
            fencing_token=fencing_token,
            claims=claims,
            record_digest="0" * 64,
        )
        return replace(draft, record_digest=draft.canonical_digest())

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "action": self.action,
            "attempt_key_digest": self.attempt_key_digest,
            "authorization_id": self.authorization_id,
            "claims": self.claims.model_dump(mode="json"),
            "fencing_token": self.fencing_token,
            "revision": self.revision,
        }

    def canonical_digest(self) -> str:
        return canonical_digest(self.canonical_projection())


def decode_resource_authorization_record(
    payload: object,
    *,
    schema_version: str,
    record_digest: str,
) -> ResourceAuthorizationRecord:
    if schema_version != RESOURCE_AUTHORIZATION_SCHEMA_VERSION:
        raise ResourceAuthorizationIntegrityError("unknown resource authorization schema version")
    if not isinstance(payload, Mapping):
        raise ResourceAuthorizationIntegrityError("authorization record is not an object")
    expected = {
        "action",
        "attempt_key_digest",
        "authorization_id",
        "claims",
        "fencing_token",
        "revision",
    }
    extra = set(payload) - expected
    missing = expected - set(payload)
    if extra:
        raise ResourceAuthorizationIntegrityError("unknown authorization record field")
    if missing:
        raise ResourceAuthorizationIntegrityError("omitted required authorization record field")
    try:
        claims = ResourceClaims.model_validate(payload["claims"])
        record = ResourceAuthorizationRecord(
            revision=payload["revision"],  # type: ignore[arg-type]
            action=payload["action"],  # type: ignore[arg-type]
            authorization_id=str(payload["authorization_id"]),
            attempt_key_digest=str(payload["attempt_key_digest"]),
            fencing_token=payload["fencing_token"],  # type: ignore[arg-type]
            claims=claims,
            record_digest=record_digest,
        )
    except (TypeError, ValueError) as error:
        raise ResourceAuthorizationIntegrityError("authorization record fields drifted") from error
    if record.record_digest != record.canonical_digest():
        raise ResourceAuthorizationIntegrityError("resource authorization digest drifted")
    return record


def active_authorization_grant(
    records: Sequence[ResourceAuthorizationRecord],
    authorization_id: str,
) -> ResourceAuthorizationRecord | None:
    current: ResourceAuthorizationRecord | None = None
    for record in records:
        if record.authorization_id != authorization_id:
            continue
        if record.action in {"acquire", "adopt"}:
            current = record
        elif record.action == "release":
            current = None
    return current


def assert_authorization_transition(
    records: Sequence[ResourceAuthorizationRecord],
    record: ResourceAuthorizationRecord,
) -> None:
    current = active_authorization_grant(records, record.authorization_id)
    if record.action == "acquire":
        if current is not None:
            raise ResourceAuthorizationIntegrityError("authorization already active")
        return
    if record.action == "adopt":
        if current is None:
            raise ResourceAuthorizationIntegrityError("no authorization to adopt")
        if record.attempt_key_digest != current.attempt_key_digest:
            raise ResourceAuthorizationIntegrityError("adopt must keep the same attempt key")
        if record.fencing_token <= current.fencing_token:
            raise StaleFencingToken("fencing token is stale")
        return
    if current is None:
        raise ResourceAuthorizationIntegrityError("no authorization to release")
    if record.fencing_token != current.fencing_token:
        raise StaleFencingToken("fencing token is stale")


class ResourceAuthorizationStorePort(Protocol):
    async def append(
        self,
        record: ResourceAuthorizationRecord,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> None: ...

    async def read_records(self) -> tuple[ResourceAuthorizationRecord, ...]: ...

    async def assert_current_fence(self, authorization_id: str, fencing_token: int) -> None: ...


class MemoryResourceAuthorizationStore:
    def __init__(self) -> None:
        self._records: list[ResourceAuthorizationRecord] = []

    @classmethod
    def from_records(
        cls,
        records: Sequence[ResourceAuthorizationRecord],
    ) -> MemoryResourceAuthorizationStore:
        store = cls()
        store._records = list(records)
        return store

    async def append(
        self,
        record: ResourceAuthorizationRecord,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> None:
        if not isinstance(record, ResourceAuthorizationRecord):
            raise TypeError("record must be a ResourceAuthorizationRecord")
        if record.record_digest != record.canonical_digest():
            raise ResourceAuthorizationIntegrityError("resource authorization digest drifted")
        token = _fencing_token(fencing_token)
        if record.fencing_token != token:
            raise ResourceAuthorizationIntegrityError("fencing token drifted from the record")
        revision = _revision(expected_revision)
        if 0 <= revision < len(self._records):
            if self._records[revision] == record:
                return
            raise ResourceAuthorizationIntegrityError("compare-and-swap conflict")
        if revision != len(self._records) or record.revision != revision:
            raise ResourceAuthorizationIntegrityError("revision gap")
        assert_authorization_transition(self._records, record)
        self._records.append(record)

    async def read_records(self) -> tuple[ResourceAuthorizationRecord, ...]:
        return tuple(self._records)

    async def assert_current_fence(self, authorization_id: str, fencing_token: int) -> None:
        token = _fencing_token(fencing_token)
        current = active_authorization_grant(self._records, authorization_id)
        if current is None or current.fencing_token != token:
            raise StaleFencingToken("fencing token is stale")


__all__ = [
    "RESOURCE_AUTHORIZATION_SCHEMA_VERSION",
    "MemoryResourceAuthorizationStore",
    "ResourceAuthorizationAction",
    "ResourceAuthorizationError",
    "ResourceAuthorizationIntegrityError",
    "ResourceAuthorizationRecord",
    "ResourceAuthorizationStorePort",
    "active_authorization_grant",
    "assert_authorization_transition",
    "decode_resource_authorization_record",
]
