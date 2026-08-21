from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

from agent_runtime_contracts.schema import bound_redacted_diagnostics, thaw_json


_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)cookie\s*[=:]\s*[^;\s]+"),
    re.compile(r"sk-[A-Za-z0-9-]+"),
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"),
    re.compile(r"(?i)https?://[^/\s:@]+:[^/\s:@]+@"),
    re.compile(r"(?i)[A-Z0-9_]*(SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*\s*=\s*\S+"),
)
_DEFAULT_LIMIT = 240


def encoded_canary_forms(canary: bytes) -> tuple[bytes, ...]:
    text = canary.decode("utf-8")
    forms = (
        canary,
        text.encode("utf-8"),
        base64.b64encode(canary),
        quote(text, safe="").encode("utf-8"),
        quote(text).encode("utf-8"),
        canary.hex().encode("ascii"),
    )
    unique: list[bytes] = []
    for form in forms:
        if form and form not in unique:
            unique.append(form)
    return tuple(unique)


def _canary_texts(canaries: Sequence[str | bytes]) -> tuple[str, ...]:
    texts: list[str] = []
    for canary in canaries:
        raw = canary.encode("utf-8") if isinstance(canary, str) else canary
        for form in encoded_canary_forms(raw):
            try:
                decoded = form.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if decoded and decoded not in texts:
                texts.append(decoded)
    return tuple(texts)


def redact_text(
    text: str,
    *,
    canaries: Sequence[str | bytes] = (),
    limit: int | None = None,
) -> str:
    redacted = text
    for canary in _canary_texts(canaries):
        redacted = redacted.replace(canary, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    if limit is not None and len(redacted) > limit:
        redacted = redacted[:limit]
    return redacted


def redact_json(value: object, *, canaries: Sequence[str | bytes] = ()) -> object:
    thawed = thaw_json(value)
    if isinstance(thawed, str):
        return redact_text(thawed, canaries=canaries)
    if isinstance(thawed, Mapping):
        return {
            redact_text(key, canaries=canaries) if isinstance(key, str) else key: redact_json(
                item, canaries=canaries
            )
            for key, item in thawed.items()
        }
    if isinstance(thawed, list):
        return [redact_json(item, canaries=canaries) for item in thawed]
    return thawed


def _payload_contains_canary(value: object, *, canaries: Sequence[str | bytes]) -> bool:
    thawed = thaw_json(value)
    if isinstance(thawed, str):
        return redact_text(thawed, canaries=canaries) != thawed
    if isinstance(thawed, Mapping):
        for key, item in thawed.items():
            if isinstance(key, str) and redact_text(key, canaries=canaries) != key:
                return True
            if _payload_contains_canary(item, canaries=canaries):
                return True
        return False
    if isinstance(thawed, list):
        return any(_payload_contains_canary(item, canaries=canaries) for item in thawed)
    return False


def reject_canaries_in_payload(value: object, *, canaries: Sequence[str | bytes] = ()) -> None:
    if _payload_contains_canary(value, canaries=canaries):
        raise ValueError("structured result contains a credential")


def bound_redacted_messages(
    messages: Sequence[str],
    *,
    canaries: Sequence[str | bytes] = (),
) -> tuple[str, ...]:
    redacted = tuple(redact_text(message, canaries=canaries) for message in messages)
    return bound_redacted_diagnostics(redacted)


def failure_message(message: str, *, canaries: Sequence[str | bytes] = ()) -> str:
    text = redact_text(message, canaries=canaries)
    if not text:
        text = "provider error"
    return redact_text(text, canaries=canaries, limit=_DEFAULT_LIMIT)


def scan_for_canaries(
    *,
    texts: Sequence[str],
    roots: Sequence[Path],
    canaries: Sequence[bytes],
) -> None:
    forms: list[bytes] = []
    for canary in canaries:
        forms.extend(encoded_canary_forms(canary))
    for text in texts:
        blob = text.encode("utf-8")
        for form in forms:
            if form and form in blob:
                raise ValueError("canary credential leaked")
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            for form in forms:
                if form and form in data:
                    raise ValueError("canary credential leaked")
