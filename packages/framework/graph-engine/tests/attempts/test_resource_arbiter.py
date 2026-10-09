from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from graph_engine.attempts.models.context import AttemptExecutionContext
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.models.resolutions import PendingTaskResult
from graph_engine.attempts.resources.resource_arbiter import ResourceArbiter, ResourceAuthorization
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.resource_authorization import (
    MemoryResourceAuthorizationStore,
    ResourceAuthorizationError,
    ResourceAuthorizationRecord,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import ResourceClaimTemplate, ResourceClaims


def reads(prefix: str) -> ResourceClaims:
    return ResourceClaims(reads=(prefix,))


def writes(prefix: str) -> ResourceClaims:
    return ResourceClaims(writes=(prefix,))


def exclusive(prefix: str) -> ResourceClaims:
    return ResourceClaims(exclusive=(prefix,))


def _attempt_key(label: str) -> AttemptKey:
    return AttemptKey(digest=canonical_digest({"attempt": label}))


@pytest.fixture
def store() -> MemoryResourceAuthorizationStore:
    return MemoryResourceAuthorizationStore()


@pytest.fixture
def arbiter(store: MemoryResourceAuthorizationStore) -> ResourceArbiter:
    return ResourceArbiter(store)


@pytest.fixture
def attempt_key() -> AttemptKey:
    return _attempt_key("holder")


@pytest.fixture
def claims() -> ResourceClaims:
    return writes("qa/a")


@pytest.mark.parametrize(
    ("left", "right", "conflicts"),
    [
        (reads("qa/a"), reads("qa/a"), False),
        (reads("qa/a"), writes("qa/a"), True),
        (writes("qa/a"), writes("qa/a/b"), True),
        (exclusive("qa"), reads("qa/a"), True),
        (exclusive("qa/a"), exclusive("qa/b"), False),
        (writes("qa/a"), writes("qa/b"), False),
    ],
)
def test_resource_conflict_matrix(left, right, conflicts, arbiter) -> None:
    assert arbiter.claims_conflict(left, right) is conflicts


async def test_replay_adopts_same_authorization_but_stale_fence_cannot_use_it(
    arbiter: ResourceArbiter,
    attempt_key: AttemptKey,
    claims: ResourceClaims,
) -> None:
    first = await arbiter.acquire(attempt_key, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    replay = await arbiter.adopt(attempt_key, fencing_token=5)
    assert replay.authorization_id == first.authorization_id
    with pytest.raises(StaleFencingToken):
        await arbiter.release(attempt_key, fencing_token=4)


def test_conflict_matrix_is_symmetric(arbiter: ResourceArbiter) -> None:
    assert arbiter.claims_conflict(reads("qa/a"), writes("qa/a")) is True
    assert arbiter.claims_conflict(writes("qa/a"), reads("qa/a")) is True
    assert arbiter.claims_conflict(exclusive("qa/a"), exclusive("qa/b")) is False
    assert arbiter.claims_conflict(exclusive("qa/b"), exclusive("qa/a")) is False


async def test_conflict_returns_pending_with_durable_wakeup(
    arbiter: ResourceArbiter,
    attempt_key: AttemptKey,
    claims: ResourceClaims,
) -> None:
    granted = await arbiter.acquire(attempt_key, claims, fencing_token=4)
    assert isinstance(granted, ResourceAuthorization)
    blocked = await arbiter.acquire(_attempt_key("waiter"), claims, fencing_token=4)
    assert isinstance(blocked, PendingTaskResult)
    assert blocked.wakeup.reference_id
    assert granted.authorization_id in blocked.wakeup.reference_id


async def test_nonconflicting_attempts_proceed_concurrently(arbiter: ResourceArbiter) -> None:
    left, right = await asyncio.gather(
        arbiter.acquire(_attempt_key("left"), writes("qa/a"), fencing_token=4),
        arbiter.acquire(_attempt_key("right"), writes("qa/b"), fencing_token=4),
    )
    assert isinstance(left, ResourceAuthorization)
    assert isinstance(right, ResourceAuthorization)
    assert left.authorization_id != right.authorization_id


async def test_newer_fence_adopts_only_the_same_attempt_key(
    arbiter: ResourceArbiter,
    attempt_key: AttemptKey,
    claims: ResourceClaims,
) -> None:
    first = await arbiter.acquire(attempt_key, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    with pytest.raises(ResourceAuthorizationError):
        await arbiter.adopt(_attempt_key("other"), fencing_token=5)
    still_blocked = await arbiter.acquire(_attempt_key("other"), claims, fencing_token=5)
    assert isinstance(still_blocked, PendingTaskResult)
    adopted = await arbiter.adopt(attempt_key, fencing_token=5)
    assert adopted.authorization_id == first.authorization_id
    still_waiting = await arbiter.acquire(_attempt_key("other"), claims, fencing_token=5)
    assert isinstance(still_waiting, PendingTaskResult)
    assert still_blocked.wakeup.reference_id == still_waiting.wakeup.reference_id


async def test_stale_fence_cannot_use_authorization_after_adopt(
    arbiter: ResourceArbiter,
    attempt_key: AttemptKey,
    claims: ResourceClaims,
) -> None:
    first = await arbiter.acquire(attempt_key, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    adopted = await arbiter.adopt(attempt_key, fencing_token=5)
    context = AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
        attempt_key=attempt_key,
        fencing_token=5,
        authorization_id=adopted.authorization_id,
    )
    assert context.authorization_id == first.authorization_id
    usable = await arbiter.assert_usable(attempt_key, fencing_token=5)
    assert usable.authorization_id == first.authorization_id
    with pytest.raises(StaleFencingToken):
        await arbiter.assert_usable(attempt_key, fencing_token=4)


async def test_templates_resolve_only_from_validated_task_input(arbiter: ResourceArbiter) -> None:
    class AreaInput(BaseModel):
        area: str

    template = ResourceClaimTemplate(parameters={"area": "/area"}, writes=("qa/{area}",))
    granted = await arbiter.acquire(
        _attempt_key("templated"),
        template,
        fencing_token=4,
        validated_input=AreaInput(area="a"),
    )
    assert isinstance(granted, ResourceAuthorization)
    assert granted.claims == writes("qa/a")
    with pytest.raises(ValueError, match="validated task input"):
        await arbiter.acquire(_attempt_key("missing-input"), template, fencing_token=4)


def test_parameter_free_template_uses_immutable_validated_default() -> None:
    template = ResourceClaimTemplate(reads=("qa",))
    serialized = template.model_dump(mode="json")

    assert serialized["parameters"] == {}
    with pytest.raises(TypeError):
        template.parameters["area"] = "/area"  # type: ignore[index]
    assert template.model_dump(mode="json") == serialized


@pytest.mark.parametrize(
    ("parameters", "reads", "message"),
    [
        ({"area": "/area"}, ("qa",), "unused resource template parameter"),
        ({}, ("qa/{area}",), "unknown resource template parameter"),
    ],
)
def test_resource_template_rejects_unknown_or_unused_explicit_parameters(
    parameters: dict[str, str], reads: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ResourceClaimTemplate(parameters=parameters, reads=reads)


async def test_acquire_retries_cas_conflict_on_fence_upgrade(
    store: MemoryResourceAuthorizationStore,
    attempt_key: AttemptKey,
    claims: ResourceClaims,
) -> None:
    first = await ResourceArbiter(store).acquire(attempt_key, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    raced = False

    class RacingStore:
        async def read_records(self) -> tuple[ResourceAuthorizationRecord, ...]:
            return await store.read_records()

        async def assert_current_fence(self, authorization_id: str, fencing_token: int) -> None:
            await store.assert_current_fence(authorization_id, fencing_token)

        async def append(
            self,
            record: ResourceAuthorizationRecord,
            *,
            expected_revision: int,
            fencing_token: int,
        ) -> None:
            nonlocal raced
            if not raced:
                raced = True
                racer_key = _attempt_key("racer")
                racer_claims = writes("qa/other")
                await store.append(
                    ResourceAuthorizationRecord.build(
                        revision=expected_revision,
                        action="acquire",
                        authorization_id=canonical_digest(
                            {
                                "attempt_key": racer_key.digest,
                                "claims": racer_claims.model_dump(mode="json"),
                            }
                        ),
                        attempt_key_digest=racer_key.digest,
                        fencing_token=4,
                        claims=racer_claims,
                    ),
                    expected_revision=expected_revision,
                    fencing_token=4,
                )
            await store.append(record, expected_revision=expected_revision, fencing_token=fencing_token)

    upgraded = await ResourceArbiter(RacingStore()).acquire(attempt_key, claims, fencing_token=5)
    assert isinstance(upgraded, ResourceAuthorization)
    assert upgraded.authorization_id == first.authorization_id
    assert upgraded.fencing_token == 5
    assert raced is True


async def test_restart_handoff_releases_once_then_unblocks_waiter() -> None:
    store = MemoryResourceAuthorizationStore()
    arbiter = ResourceArbiter(store)
    holder = _attempt_key("holder")
    waiter = _attempt_key("waiter")
    claims = writes("qa/a")

    first = await arbiter.acquire(holder, claims, fencing_token=4)
    assert isinstance(first, ResourceAuthorization)
    blocked = await arbiter.acquire(waiter, claims, fencing_token=4)
    assert isinstance(blocked, PendingTaskResult)
    wakeup = blocked.wakeup

    rebuilt_store = MemoryResourceAuthorizationStore.from_records(await store.read_records())
    rebuilt = ResourceArbiter(rebuilt_store)
    adopted = await rebuilt.adopt(holder, fencing_token=5)
    assert adopted.authorization_id == first.authorization_id
    with pytest.raises(StaleFencingToken):
        await rebuilt.assert_usable(holder, fencing_token=4)
    with pytest.raises(StaleFencingToken):
        await rebuilt.release(holder, fencing_token=4)

    await rebuilt.release(holder, fencing_token=5)
    await rebuilt.release(holder, fencing_token=5)
    release_records = [record for record in await rebuilt_store.read_records() if record.action == "release"]
    assert len(release_records) == 1

    progressed = await rebuilt.acquire(waiter, claims, fencing_token=5)
    assert isinstance(progressed, ResourceAuthorization)
    assert progressed.authorization_id != first.authorization_id
    replayed_block = await ResourceArbiter(
        MemoryResourceAuthorizationStore.from_records(await store.read_records())
    ).acquire(waiter, claims, fencing_token=4)
    assert isinstance(replayed_block, PendingTaskResult)
    assert replayed_block.wakeup == wakeup
