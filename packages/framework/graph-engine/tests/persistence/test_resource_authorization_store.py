from __future__ import annotations

import inspect

import pytest

from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resource_arbiter import ResourceArbiter, ResourceAuthorization
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.resource_authorization import (
    MemoryResourceAuthorizationStore,
    ResourceAuthorizationAction,
    ResourceAuthorizationIntegrityError,
    ResourceAuthorizationRecord,
    ResourceAuthorizationStorePort,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import ResourceClaims


def _attempt_key(label: str) -> AttemptKey:
    return AttemptKey(digest=canonical_digest({"attempt": label}))


def _claims() -> ResourceClaims:
    return ResourceClaims(writes=("qa/a",))


def _authorization_id(*, label: str = "holder") -> str:
    return canonical_digest(
        {
            "attempt_key": _attempt_key(label).digest,
            "claims": _claims().model_dump(mode="json"),
        }
    )


def _record(
    *,
    revision: int,
    action: ResourceAuthorizationAction,
    fencing_token: int,
    label: str = "holder",
) -> ResourceAuthorizationRecord:
    return ResourceAuthorizationRecord.build(
        revision=revision,
        action=action,
        authorization_id=_authorization_id(label=label),
        attempt_key_digest=_attempt_key(label).digest,
        fencing_token=fencing_token,
        claims=_claims(),
    )


def test_store_port_exposes_fenced_cas_append_without_workflow_api() -> None:
    methods = {
        name
        for name, value in inspect.getmembers(ResourceAuthorizationStorePort)
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"append", "read_records", "assert_current_fence"}


def test_authorization_record_digest_is_canonical_projection() -> None:
    record = _record(revision=0, action="acquire", fencing_token=4)
    assert record.record_digest == record.canonical_digest()
    assert record.record_digest == canonical_digest(
        {
            "action": "acquire",
            "attempt_key_digest": record.attempt_key_digest,
            "authorization_id": record.authorization_id,
            "claims": _claims().model_dump(mode="json"),
            "fencing_token": 4,
            "revision": 0,
        }
    )
    drifted = _record(revision=0, action="acquire", fencing_token=5)
    assert drifted.record_digest != record.record_digest


async def test_identical_cas_append_is_idempotent() -> None:
    store = MemoryResourceAuthorizationStore()
    record = _record(revision=0, action="acquire", fencing_token=4)
    await store.append(record, expected_revision=0, fencing_token=4)
    await store.append(record, expected_revision=0, fencing_token=4)
    assert await store.read_records() == (record,)


async def test_divergent_record_at_same_revision_fails_closed() -> None:
    store = MemoryResourceAuthorizationStore()
    first = _record(revision=0, action="acquire", fencing_token=4)
    await store.append(first, expected_revision=0, fencing_token=4)
    with pytest.raises(ResourceAuthorizationIntegrityError):
        await store.append(
            _record(revision=0, action="acquire", fencing_token=4, label="other"),
            expected_revision=0,
            fencing_token=4,
        )
    assert await store.read_records() == (first,)


async def test_stale_fence_cannot_append_or_assert() -> None:
    store = MemoryResourceAuthorizationStore()
    acquire = _record(revision=0, action="acquire", fencing_token=4)
    adopt = _record(revision=1, action="adopt", fencing_token=5)
    await store.append(acquire, expected_revision=0, fencing_token=4)
    await store.append(adopt, expected_revision=1, fencing_token=5)
    await store.assert_current_fence(acquire.authorization_id, 5)
    with pytest.raises(StaleFencingToken):
        await store.assert_current_fence(acquire.authorization_id, 4)
    with pytest.raises(StaleFencingToken):
        await store.append(
            _record(revision=2, action="release", fencing_token=4),
            expected_revision=2,
            fencing_token=4,
        )


async def test_acquire_adopt_release_persist_as_canonical_cas_records() -> None:
    store = MemoryResourceAuthorizationStore()
    arbiter = ResourceArbiter(store)
    key = _attempt_key("holder")
    claims = _claims()
    first = await arbiter.acquire(key, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    adopted = await arbiter.adopt(key, fencing_token=5)
    await arbiter.release(key, fencing_token=5)
    records = await store.read_records()
    assert [record.action for record in records] == ["acquire", "adopt", "release"]
    assert all(record.record_digest == record.canonical_digest() for record in records)
    assert {record.authorization_id for record in records} == {first.authorization_id}
    assert adopted.authorization_id == first.authorization_id
    assert records[0].fencing_token == 4
    assert records[1].fencing_token == 5
    assert records[2].fencing_token == 5


async def test_reconstructed_store_replays_durable_handoff() -> None:
    store = MemoryResourceAuthorizationStore()
    arbiter = ResourceArbiter(store)
    holder = _attempt_key("holder")
    waiter = _attempt_key("waiter")
    claims = _claims()
    first = await arbiter.acquire(holder, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    blocked = await arbiter.acquire(waiter, claims, fencing_token=4)
    assert not isinstance(blocked, ResourceAuthorization)

    rebuilt = MemoryResourceAuthorizationStore.from_records(await store.read_records())
    successor = ResourceArbiter(rebuilt)
    adopted = await successor.adopt(holder, fencing_token=5)
    assert adopted.authorization_id == first.authorization_id
    with pytest.raises(StaleFencingToken):
        await successor.release(holder, fencing_token=4)
    await successor.release(holder, fencing_token=5)
    progressed = await successor.acquire(waiter, claims, fencing_token=5)
    assert isinstance(progressed, ResourceAuthorization)

    replayed = MemoryResourceAuthorizationStore.from_records(await rebuilt.read_records())
    assert [record.action for record in await replayed.read_records()] == [
        "acquire",
        "adopt",
        "release",
        "acquire",
    ]
