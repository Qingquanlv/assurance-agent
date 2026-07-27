"""Canonical JSON bytes and content digests shared by artifact producers."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def canonical_json_bytes(value: object) -> bytes:
    """Encode a model or JSON-compatible value with one stable wire format."""
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def sha256_bytes(value: bytes) -> str:
    """Return the repository's prefixed SHA-256 representation."""
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = ["canonical_json_bytes", "sha256_bytes"]
