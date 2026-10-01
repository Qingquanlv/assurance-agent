from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError


_IDENTITY_BEARING_TYPES = frozenset(
    {
        "session.idle",
        "session.error",
        "session.status",
        "session.updated",
        "session.deleted",
        "message.updated",
        "message.part.updated",
    }
)


class OpenCodeSseProperties(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    sessionID: str | None = None


class OpenCodeSseEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    type: str = Field(min_length=1)
    properties: OpenCodeSseProperties | None = None


@dataclass(frozen=True, slots=True)
class SseFrame:
    event: str | None
    data: str
    identifier: str | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class SseReduction:
    cursor: str | None
    malformed: bool
    heartbeat_count: int


def parse_sse_frames(payload: bytes) -> tuple[SseFrame, ...]:
    text = payload.decode("utf-8")
    frames: list[SseFrame] = []
    event: str | None = None
    identifier: str | None = None
    comment: str | None = None
    data_lines: list[str] = []

    def _flush() -> None:
        nonlocal event, identifier, comment, data_lines
        if event is None and identifier is None and comment is None and not data_lines:
            return
        frames.append(
            SseFrame(
                event=event,
                data="\n".join(data_lines),
                identifier=identifier,
                comment=comment,
            )
        )
        event = None
        identifier = None
        comment = None
        data_lines = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            _flush()
            continue
        if line.startswith(":"):
            comment = line[1:].lstrip()
            continue
        if line.startswith("event:"):
            event = line[6:].lstrip()
            continue
        if line.startswith("id:"):
            identifier = line[3:].lstrip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
    _flush()
    return tuple(frames)


def reduce_sse_frames(
    frames: Sequence[SseFrame],
    *,
    session_id: str,
    heartbeat: Callable[[], None],
) -> SseReduction:
    cursor: str | None = None
    heartbeat_count = 0
    for frame in frames:
        if frame.identifier:
            cursor = frame.identifier
        if frame.comment:
            heartbeat()
            heartbeat_count += 1
        event_name = frame.event
        if not frame.data:
            if event_name == "server.heartbeat":
                heartbeat()
                heartbeat_count += 1
            continue
        try:
            parsed = json.loads(frame.data)
        except json.JSONDecodeError:
            if _is_identity_bearing_name(event_name):
                return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
            continue
        if not isinstance(parsed, dict):
            if _is_identity_bearing_name(event_name):
                return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
            continue
        payload = dict(parsed)
        if "type" not in payload and event_name:
            payload["type"] = event_name
        envelope_type = payload.get("type")
        if envelope_type == "server.heartbeat":
            heartbeat()
            heartbeat_count += 1
            continue
        named = envelope_type if isinstance(envelope_type, str) else event_name
        if not _is_identity_bearing_name(named):
            continue
        try:
            envelope = OpenCodeSseEnvelope.model_validate(payload)
        except ValidationError:
            return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
        properties = envelope.properties
        if properties is None or properties.sessionID is None:
            return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
        if properties.sessionID != session_id:
            continue
        heartbeat()
        heartbeat_count += 1
    return SseReduction(cursor=cursor, malformed=False, heartbeat_count=heartbeat_count)


def _is_identity_bearing_name(name: str | None) -> bool:
    return name in _IDENTITY_BEARING_TYPES
