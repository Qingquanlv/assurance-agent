"""Leaf sets and content digests."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def leafs(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
