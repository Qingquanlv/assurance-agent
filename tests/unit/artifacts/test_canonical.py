"""Byte-level contract for the shared artifact canonical JSON helpers."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes


class _Fixture(BaseModel):
    z: int
    a: str


def test_canonical_json_bytes_match_existing_wire_format() -> None:
    expected = b'{"a":"value","z":2}\n'
    assert canonical_json_bytes({"z": 2, "a": "value"}) == expected
    assert canonical_json_bytes(_Fixture(z=2, a="value")) == expected


def test_sha256_bytes_use_prefixed_lowercase_digest() -> None:
    payload = b'{"a":"value","z":2}\n'
    assert sha256_bytes(payload) == "sha256:" + hashlib.sha256(payload).hexdigest()
