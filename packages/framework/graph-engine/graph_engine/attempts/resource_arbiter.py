from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.resource_authorization import (
    ResourceAuthorizationAction,
    ResourceAuthorizationError,
    ResourceAuthorizationIntegrityError,
    ResourceAuthorizationRecord,
    ResourceAuthorizationStorePort,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import FrozenModel, ResourceClaims, ResourceClaimTemplate


class ResourceAuthorization(FrozenModel):
    authorization_id: str = Field(min_length=1)
    attempt_key: AttemptKey
    fencing_token: int = Field(ge=1)
    claims: ResourceClaims


class ResourceArbiterPort(Protocol):
    def claims_conflict(self, left: ResourceClaims, right: ResourceClaims) -> bool: ...

    async def acquire(
        self,
        attempt_key: AttemptKey,
        claims: ResourceClaims | ResourceClaimTemplate,
        *,
        fencing_token: int,
        validated_input: BaseModel | None = None,
    ) -> ResourceAuthorization | PendingTaskResult: ...

    async def adopt(self, attempt_key: AttemptKey, *, fencing_token: int) -> ResourceAuthorization: ...

    async def release(self, attempt_key: AttemptKey, *, fencing_token: int) -> None: ...

    async def assert_usable(
        self,
        attempt_key: AttemptKey,
        *,
        fencing_token: int,
    ) -> ResourceAuthorization: ...

    async def is_active(self, attempt_key: AttemptKey) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ActiveGrant:
    authorization_id: str
    attempt_key_digest: str
    fencing_token: int
    claims: ResourceClaims


class ResourceArbiter:
    def __init__(self, store: ResourceAuthorizationStorePort) -> None:
        self._store = store

    def claims_conflict(self, left: ResourceClaims, right: ResourceClaims) -> bool:
        return _claims_conflict(left, right)

    async def acquire(
        self,
        attempt_key: AttemptKey,
        claims: ResourceClaims | ResourceClaimTemplate,
        *,
        fencing_token: int,
        validated_input: BaseModel | None = None,
    ) -> ResourceAuthorization | PendingTaskResult:
        resolved = _resolve_claims(claims, validated_input)
        while True:
            records = await self._store.read_records()
            grants = _active_grants(records)
            existing = _grant_for_attempt(grants, attempt_key)
            if existing is not None:
                if existing.claims != resolved:
                    raise ResourceAuthorizationError("attempt already holds different claims")
                if fencing_token < existing.fencing_token:
                    raise StaleFencingToken("fencing token is stale")
                if fencing_token == existing.fencing_token:
                    return _authorization(attempt_key, existing)
                try:
                    return await self._cas_append(
                        attempt_key,
                        existing.claims,
                        action="adopt",
                        fencing_token=fencing_token,
                        authorization_id=existing.authorization_id,
                        expected_revision=len(records),
                    )
                except ResourceAuthorizationIntegrityError:
                    continue
            conflicting = [grant for grant in grants if self.claims_conflict(resolved, grant.claims)]
            if conflicting:
                holder = min(conflicting, key=lambda grant: grant.authorization_id)
                return PendingTaskResult(
                    wakeup=SystemReference(reference_id=f"resource:{holder.authorization_id}")
                )
            try:
                return await self._cas_append(
                    attempt_key,
                    resolved,
                    action="acquire",
                    fencing_token=fencing_token,
                    authorization_id=_authorization_id(attempt_key, resolved),
                    expected_revision=len(records),
                )
            except ResourceAuthorizationIntegrityError:
                continue

    async def adopt(self, attempt_key: AttemptKey, *, fencing_token: int) -> ResourceAuthorization:
        while True:
            records = await self._store.read_records()
            existing = _grant_for_attempt(_active_grants(records), attempt_key)
            if existing is None:
                raise ResourceAuthorizationError("no authorization to adopt for attempt key")
            if fencing_token < existing.fencing_token:
                raise StaleFencingToken("fencing token is stale")
            if fencing_token == existing.fencing_token:
                return _authorization(attempt_key, existing)
            try:
                return await self._cas_append(
                    attempt_key,
                    existing.claims,
                    action="adopt",
                    fencing_token=fencing_token,
                    authorization_id=existing.authorization_id,
                    expected_revision=len(records),
                )
            except ResourceAuthorizationIntegrityError:
                continue

    async def release(self, attempt_key: AttemptKey, *, fencing_token: int) -> None:
        while True:
            records = await self._store.read_records()
            existing = _grant_for_attempt(_active_grants(records), attempt_key)
            if existing is None:
                return
            if fencing_token != existing.fencing_token:
                raise StaleFencingToken("fencing token is stale")
            try:
                await self._cas_append(
                    attempt_key,
                    existing.claims,
                    action="release",
                    fencing_token=fencing_token,
                    authorization_id=existing.authorization_id,
                    expected_revision=len(records),
                )
            except ResourceAuthorizationIntegrityError:
                continue
            return

    async def assert_usable(
        self,
        attempt_key: AttemptKey,
        *,
        fencing_token: int,
    ) -> ResourceAuthorization:
        existing = _grant_for_attempt(_active_grants(await self._store.read_records()), attempt_key)
        if existing is None:
            raise ResourceAuthorizationError("no active authorization")
        if fencing_token != existing.fencing_token:
            raise StaleFencingToken("fencing token is stale")
        await self._store.assert_current_fence(existing.authorization_id, fencing_token)
        return _authorization(attempt_key, existing)

    async def is_active(self, attempt_key: AttemptKey) -> bool:
        existing = _grant_for_attempt(_active_grants(await self._store.read_records()), attempt_key)
        return existing is not None

    async def _cas_append(
        self,
        attempt_key: AttemptKey,
        claims: ResourceClaims,
        *,
        action: ResourceAuthorizationAction,
        fencing_token: int,
        authorization_id: str,
        expected_revision: int,
    ) -> ResourceAuthorization:
        record = ResourceAuthorizationRecord.build(
            revision=expected_revision,
            action=action,
            authorization_id=authorization_id,
            attempt_key_digest=attempt_key.digest,
            fencing_token=fencing_token,
            claims=claims,
        )
        await self._store.append(record, expected_revision=expected_revision, fencing_token=fencing_token)
        return ResourceAuthorization(
            authorization_id=authorization_id,
            attempt_key=attempt_key,
            fencing_token=fencing_token,
            claims=claims,
        )


def _resolve_claims(
    claims: ResourceClaims | ResourceClaimTemplate,
    validated_input: BaseModel | None,
) -> ResourceClaims:
    if isinstance(claims, ResourceClaimTemplate):
        if validated_input is None:
            raise ValueError("resource templates resolve only from validated task input")
        if not isinstance(validated_input, BaseModel):
            raise TypeError("validated task input must be a Pydantic model")
        payload = validated_input.model_dump(mode="json")
        return claims.resolve(payload)
    if not isinstance(claims, ResourceClaims):
        raise TypeError("claims must be ResourceClaims or ResourceClaimTemplate")
    return claims


def _authorization_id(attempt_key: AttemptKey, claims: ResourceClaims) -> str:
    return canonical_digest(
        {
            "attempt_key": attempt_key.digest,
            "claims": claims.model_dump(mode="json"),
        }
    )


def _authorization(attempt_key: AttemptKey, grant: _ActiveGrant) -> ResourceAuthorization:
    return ResourceAuthorization(
        authorization_id=grant.authorization_id,
        attempt_key=attempt_key,
        fencing_token=grant.fencing_token,
        claims=grant.claims,
    )


def _active_grants(records: tuple[ResourceAuthorizationRecord, ...]) -> tuple[_ActiveGrant, ...]:
    grants: dict[str, _ActiveGrant] = {}
    for record in records:
        if record.action in {"acquire", "adopt"}:
            grants[record.authorization_id] = _ActiveGrant(
                authorization_id=record.authorization_id,
                attempt_key_digest=record.attempt_key_digest,
                fencing_token=record.fencing_token,
                claims=record.claims,
            )
        elif record.action == "release":
            grants.pop(record.authorization_id, None)
    return tuple(grants.values())


def _grant_for_attempt(grants: tuple[_ActiveGrant, ...], attempt_key: AttemptKey) -> _ActiveGrant | None:
    for grant in grants:
        if grant.attempt_key_digest == attempt_key.digest:
            return grant
    return None


def _overlaps(first: str, second: str) -> bool:
    return first == second or first.startswith(f"{second}/") or second.startswith(f"{first}/")


def _entries(claims: ResourceClaims) -> tuple[tuple[str, str], ...]:
    return (
        tuple((prefix, "reads") for prefix in claims.reads)
        + tuple((prefix, "writes") for prefix in claims.writes)
        + tuple((prefix, "exclusive") for prefix in claims.exclusive)
    )


def _modes_conflict(left: str, right: str) -> bool:
    return not (left == "reads" and right == "reads")


def _claims_conflict(left: ResourceClaims, right: ResourceClaims) -> bool:
    return any(
        _overlaps(left_prefix, right_prefix) and _modes_conflict(left_mode, right_mode)
        for left_prefix, left_mode in _entries(left)
        for right_prefix, right_mode in _entries(right)
    )


__all__ = [
    "ResourceArbiter",
    "ResourceArbiterPort",
    "ResourceAuthorization",
]
