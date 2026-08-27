"""Private injected store seam for healing durable effects."""

from __future__ import annotations

from typing import Literal, Protocol

from graph_engine.plugin_api import TaskFailure


class StoreRecord:
    def __init__(
        self,
        *,
        status: Literal["pending", "applied", "permanent"],
        receipt: dict[str, object] | None = None,
        failure: TaskFailure | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.status = status
        self.receipt = receipt
        self.failure = failure
        self.payload = payload


class HealingStore(Protocol):
    async def get(self, key: str) -> StoreRecord | None: ...

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None: ...


class InMemoryHealingStore:
    def __init__(self) -> None:
        self.records: dict[str, StoreRecord] = {}
        self.external_mutation_count = 0

    async def get(self, key: str) -> StoreRecord | None:
        return self.records.get(key)

    async def commit(self, key: str, receipt: dict[str, object], payload: dict[str, object]) -> None:
        self.external_mutation_count += 1
        self.records[key] = StoreRecord(status="applied", receipt=receipt, payload=payload)
