from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from typing import Protocol

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CheckpointIntegrityError(GraphEngineError):
    """Raised when a checkpoint journal record drifts from its identity."""


def _sha256(value: str, kind: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{kind} digest must be a lowercase SHA-256 hex value")
    return value


def _nonempty(value: str, kind: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} must be nonempty")
    return value


def _fencing_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _bytes_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckpointAnchorState:
    invocation_id: str
    thread_id: str
    graph_revision: str
    product_lock_digest: str
    root_input_digest: str
    fencing_token: int

    def __post_init__(self) -> None:
        _nonempty(self.invocation_id, "invocation id")
        _nonempty(self.thread_id, "thread id")
        if self.thread_id != self.invocation_id:
            raise ValueError("thread id must equal invocation id")
        _sha256(self.graph_revision, "graph revision")
        _sha256(self.product_lock_digest, "product lock")
        _sha256(self.root_input_digest, "root input")
        object.__setattr__(self, "fencing_token", _fencing_token(self.fencing_token))


@dataclass(frozen=True, slots=True)
class InvocationStarted:
    invocation_id: str
    thread_id: str
    graph_revision: str
    product_lock_digest: str
    root_input_digest: str
    fencing_token: int

    def __post_init__(self) -> None:
        _nonempty(self.invocation_id, "invocation id")
        _nonempty(self.thread_id, "thread id")
        if self.thread_id != self.invocation_id:
            raise ValueError("thread id must equal invocation id")
        _sha256(self.graph_revision, "graph revision")
        _sha256(self.product_lock_digest, "product lock")
        _sha256(self.root_input_digest, "root input")
        object.__setattr__(self, "fencing_token", _fencing_token(self.fencing_token))

    def anchor_state(self) -> CheckpointAnchorState:
        return CheckpointAnchorState(
            invocation_id=self.invocation_id,
            thread_id=self.thread_id,
            graph_revision=self.graph_revision,
            product_lock_digest=self.product_lock_digest,
            root_input_digest=self.root_input_digest,
            fencing_token=self.fencing_token,
        )


@dataclass(frozen=True, slots=True)
class CheckpointAnchor:
    invocation_id: str
    thread_id: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    checkpoint_bytes: bytes
    pending_write_bytes: tuple[bytes, ...]
    task_identity: str
    graph_revision: str
    product_lock_digest: str
    root_input_digest: str
    fencing_token: int
    anchor_digest: str

    def __post_init__(self) -> None:
        _nonempty(self.invocation_id, "invocation id")
        _nonempty(self.thread_id, "thread id")
        if self.thread_id != self.invocation_id:
            raise ValueError("thread id must equal invocation id")
        _nonempty(self.checkpoint_id, "checkpoint id")
        if self.parent_checkpoint_id is not None:
            _nonempty(self.parent_checkpoint_id, "parent checkpoint id")
        if not isinstance(self.checkpoint_bytes, bytes):
            raise TypeError("checkpoint bytes must be bytes")
        if not isinstance(self.pending_write_bytes, tuple) or any(
            not isinstance(item, bytes) for item in self.pending_write_bytes
        ):
            raise TypeError("pending write bytes must be a tuple of bytes")
        if not isinstance(self.task_identity, str):
            raise TypeError("task identity must be a string")
        _sha256(self.graph_revision, "graph revision")
        _sha256(self.product_lock_digest, "product lock")
        _sha256(self.root_input_digest, "root input")
        object.__setattr__(self, "fencing_token", _fencing_token(self.fencing_token))
        _sha256(self.anchor_digest, "checkpoint anchor")

    @classmethod
    def build(
        cls,
        *,
        invocation_id: str,
        thread_id: str,
        checkpoint_id: str,
        parent_checkpoint_id: str | None,
        checkpoint_bytes: bytes,
        pending_write_bytes: tuple[bytes, ...],
        task_identity: str,
        graph_revision: str,
        product_lock_digest: str,
        root_input_digest: str,
        fencing_token: int,
    ) -> CheckpointAnchor:
        draft = cls(
            invocation_id=invocation_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            parent_checkpoint_id=parent_checkpoint_id,
            checkpoint_bytes=checkpoint_bytes,
            pending_write_bytes=pending_write_bytes,
            task_identity=task_identity,
            graph_revision=graph_revision,
            product_lock_digest=product_lock_digest,
            root_input_digest=root_input_digest,
            fencing_token=fencing_token,
            anchor_digest="0" * 64,
        )
        return replace(draft, anchor_digest=draft.canonical_digest())

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "invocation_id": self.invocation_id,
            "thread_id": self.thread_id,
            "checkpoint_id": self.checkpoint_id,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "checkpoint_bytes_digest": _bytes_digest(self.checkpoint_bytes),
            "pending_write_bytes_digests": [_bytes_digest(item) for item in self.pending_write_bytes],
            "task_identity": self.task_identity,
            "graph_revision": self.graph_revision,
            "product_lock_digest": self.product_lock_digest,
            "root_input_digest": self.root_input_digest,
            "fencing_token": self.fencing_token,
        }

    def canonical_digest(self) -> str:
        return canonical_digest(self.canonical_projection())

    def anchor_state(self) -> CheckpointAnchorState:
        return CheckpointAnchorState(
            invocation_id=self.invocation_id,
            thread_id=self.thread_id,
            graph_revision=self.graph_revision,
            product_lock_digest=self.product_lock_digest,
            root_input_digest=self.root_input_digest,
            fencing_token=self.fencing_token,
        )


class CheckpointAnchorJournalPort(Protocol):
    async def start_invocation(self, record: InvocationStarted, *, fencing_token: int) -> None: ...
    async def append_checkpoint_anchor(self, anchor: CheckpointAnchor, *, fencing_token: int) -> None: ...
    async def read_checkpoint_anchor(self, thread_id: str, checkpoint_id: str) -> CheckpointAnchor | None: ...
    async def assert_current_fence(self, invocation_id: str, fencing_token: int) -> None: ...


def strict_checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )


__all__ = [
    "CheckpointAnchor",
    "CheckpointAnchorJournalPort",
    "CheckpointAnchorState",
    "CheckpointIntegrityError",
    "InvocationStarted",
    "strict_checkpoint_serializer",
]
