from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from graph_engine.attempts.events import AttemptEvent, AttemptSnapshot, fold_attempt_events
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.persistence.runner_lease import StaleFencingToken


class AttemptJournalIntegrityError(GraphEngineError):
    """Raised when an Attempt journal record drifts from its identity."""


def _fencing_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _revision(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("revision must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class AttemptJournalRecord:
    revision: int
    attempt_key_digest: str
    fencing_token: int
    events: tuple[AttemptEvent, ...]
    record_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _revision(self.revision))
        if not self.attempt_key_digest:
            raise ValueError("attempt key digest must be nonempty")
        object.__setattr__(self, "fencing_token", _fencing_token(self.fencing_token))
        if not isinstance(self.events, tuple) or not self.events:
            raise ValueError("journal batch must be a nonempty event tuple")
        if len(self.record_digest) != 64:
            raise ValueError("record digest must be a lowercase SHA-256 hex value")

    @classmethod
    def build(
        cls,
        *,
        revision: int,
        attempt_key: AttemptKey,
        fencing_token: int,
        events: Sequence[AttemptEvent],
    ) -> AttemptJournalRecord:
        draft = cls(
            revision=revision,
            attempt_key_digest=attempt_key.digest,
            fencing_token=fencing_token,
            events=tuple(events),
            record_digest="0" * 64,
        )
        return replace(draft, record_digest=draft.canonical_digest())

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "attempt_key_digest": self.attempt_key_digest,
            "events": [event.canonical_projection() for event in self.events],
            "fencing_token": self.fencing_token,
            "revision": self.revision,
        }

    def canonical_digest(self) -> str:
        return canonical_digest(self.canonical_projection())


class AttemptJournalPort(Protocol):
    async def load(self, attempt_key: AttemptKey) -> AttemptSnapshot | None: ...

    async def append(
        self,
        attempt_key: AttemptKey,
        events: Sequence[AttemptEvent],
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptSnapshot: ...

    async def ensure_durable(self, attempt_key: AttemptKey) -> None: ...


class MemoryAttemptJournal:
    def __init__(self) -> None:
        self._logs: dict[str, list[AttemptJournalRecord]] = {}
        self._durable: dict[str, int] = {}

    async def load(self, attempt_key: AttemptKey) -> AttemptSnapshot | None:
        records = self._logs.get(attempt_key.digest)
        if not records:
            return None
        return self._snapshot(attempt_key, records)

    async def append(
        self,
        attempt_key: AttemptKey,
        events: Sequence[AttemptEvent],
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptSnapshot:
        record = AttemptJournalRecord.build(
            revision=expected_revision,
            attempt_key=attempt_key,
            fencing_token=fencing_token,
            events=events,
        )
        return await self.append_record(
            record, expected_revision=expected_revision, fencing_token=fencing_token
        )

    async def append_record(
        self,
        record: AttemptJournalRecord,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptSnapshot:
        if not isinstance(record, AttemptJournalRecord):
            raise TypeError("record must be an AttemptJournalRecord")
        if record.record_digest != record.canonical_digest():
            raise AttemptJournalIntegrityError("attempt journal digest drifted")
        token = _fencing_token(fencing_token)
        if record.fencing_token != token:
            raise AttemptJournalIntegrityError("fencing token drifted from the record")
        revision = _revision(expected_revision)
        if record.revision != revision:
            raise AttemptJournalIntegrityError("revision gap")
        key_digest = record.attempt_key_digest
        log = self._logs.setdefault(key_digest, [])
        if log and token < log[-1].fencing_token:
            raise StaleFencingToken("fencing token is stale")
        if 0 <= revision < len(log):
            if log[revision] == record:
                return self._snapshot(AttemptKey(digest=key_digest), log)
            raise AttemptJournalIntegrityError("compare-and-swap conflict")
        if revision != len(log):
            raise AttemptJournalIntegrityError("revision gap")
        log.append(record)
        return self._snapshot(AttemptKey(digest=key_digest), log)

    async def ensure_durable(self, attempt_key: AttemptKey) -> None:
        loaded = await self.load(attempt_key)
        if loaded is None:
            return
        self._durable[attempt_key.digest] = loaded.revision

    def durable_revision(self, attempt_key: AttemptKey) -> int:
        return self._durable.get(attempt_key.digest, 0)

    def _snapshot(self, attempt_key: AttemptKey, records: Sequence[AttemptJournalRecord]) -> AttemptSnapshot:
        events = tuple(event for record in records for event in record.events)
        return fold_attempt_events(
            attempt_key,
            events,
            revision=len(records),
            fencing_token=records[-1].fencing_token,
        )


__all__ = [
    "AttemptJournalIntegrityError",
    "AttemptJournalPort",
    "AttemptJournalRecord",
    "MemoryAttemptJournal",
]
